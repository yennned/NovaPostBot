"""Зеркало Postgres → лист «Склад» и приём ручных правок из листа.

Один проход на аккаунт стоит **одно чтение и одну запись** Google — вместо ~9
запросов на каждую отправленную ТТН, как было, когда лист был источником правды.

Проход делает три вещи в строго этом порядке:

1. **Забирает описательные поля** (`Назва`, `Категорія`, `Ціна`) из листа в PG.
   Ими владеет лист: человек правит их прямо в «Складі», и это остаётся его
   способом коррекции. Зеркало их не пишет никогда и потому физически не может
   откатить такую правку.
2. **Распознаёт ручную правку `Кількість`** сравнением ячейки с
   `mirrored_quantity` — тем числом, которое зеркало записало в прошлый раз. Не с
   `quantity`: расхождение с ним — это штатное отставание листа от PG, а не
   правка. Правка применяется движением `manual` с честной дельтой, попадает в
   `stock_movements` и уходит пушем владельцу — то есть становится **строже**
   сегодняшнего положения, когда фиксация правки зависит от того, вспомнил ли
   человек её записать.
3. **Пишет обратно** только `Кількість` и `Резерв`, и только изменившиеся ячейки.

**Предохранитель.** Правка, уводящая остаток в минус или превышающая
`STOCK_MANUAL_DELTA_LIMIT`, НЕ применяется: ближайшая запись зеркала возвращает в
ячейку значение из PG, и владельцу уходит сообщение. Иначе опечатка в одну цифру
становится реальным изменением остатка — а гейт от oversell смотрит именно на
него. Отказ самозалечивается: после возврата значения ячейка снова совпадает с
`mirrored_quantity`, и повторных сообщений не будет.

**Новые строки не усыновляются.** SKU, которого нет в PG, не импортируется: это
не коррекция, а приёмка мимо «Приймання», и её место — в книге приёмки. Импорт
такой строки открыл бы дыру под oversell на любой опечатке в артикуле.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models.client_account import ClientAccount
from app.db.models.enums import StockMovementType
from app.db.repositories import ShipmentRepository, StockBalanceRepository
from app.services.inventory_backend import stock_sheet_key
from app.sheets.mirror import MirrorSheetError, SheetSnapshot, StockSheetMirror
from app.sheets.runtime import run_on_sheets_executor, run_sheets_read
from app.sheets.source import StockSheetNotFound

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ManualEdit:
    sku: str
    was: int
    now: int
    applied: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class AccountMirrorResult:
    account_id: object
    key: str
    cells_written: int = 0
    edits: tuple[ManualEdit, ...] = field(default=())
    #: SKU, которые есть в листе, но не заведены в PG. Не импортируем — сообщаем.
    unknown_skus: tuple[str, ...] = field(default=())
    #: SKU, которые есть в PG, но не видны в листе: оператор их не увидит вовсе.
    invisible_skus: tuple[str, ...] = field(default=())
    error: str | None = None


async def mirror_account(
    session: AsyncSession,
    account: ClientAccount,
    *,
    mirror: StockSheetMirror,
    settings: Settings | None = None,
) -> AccountMirrorResult:
    cfg = settings or get_settings()
    key = stock_sheet_key(account)
    try:
        snapshot: SheetSnapshot = await run_sheets_read(mirror.read_snapshot, key)
    except StockSheetNotFound:
        # Листа нет — аккаунт ещё не заведён в книге. Не ошибка зеркала.
        return AccountMirrorResult(account.id, key)
    except MirrorSheetError as exc:
        # Сломанная структура листа: молча пропустить значило бы перестать
        # зеркалить аккаунт и никому об этом не сказать.
        logger.error("stock_mirror.bad_sheet", account_id=str(account.id), key=key, error=str(exc))
        return AccountMirrorResult(account.id, key, error=str(exc))

    balances_repo = StockBalanceRepository(session)
    balances = {b.sku: b for b in await balances_repo.list_for_account(account.id)}
    reserved = await ShipmentRepository(session).reserved_by_account(account.id)

    updates: list[tuple[int, int, int]] = []
    edits: list[ManualEdit] = []
    unknown: list[str] = []
    seen: set[str] = set()

    for row in snapshot.rows:
        balance = balances.get(row.sku)
        if balance is None:
            unknown.append(row.sku)
            continue
        seen.add(row.sku)

        await balances_repo.upsert_meta(
            account_id=account.id,
            sku=row.sku,
            name=row.name,
            category=row.category,
            price=row.price,
        )

        verdict = _edit_verdict(row.quantity, balance.mirrored_quantity, balance.quantity, cfg)
        if verdict is not None:
            applied, reason = verdict
            edit = ManualEdit(
                sku=row.sku,
                was=balance.mirrored_quantity or 0,
                now=row.quantity,
                applied=applied,
                reason=reason,
            )
            edits.append(edit)
            if applied:
                await balances_repo.apply_movement(
                    account_id=account.id,
                    sku=row.sku,
                    delta=row.quantity - balance.quantity,
                    movement_type=StockMovementType.manual,
                    comment=f"ручна правка в листі «{key}»: {edit.was} → {edit.now}",
                )

        if balance.quantity != row.quantity:
            updates.append((row.row, snapshot.quantity_col, balance.quantity))
        if snapshot.reserve_col is not None:
            want = int(reserved.get(row.sku, 0))
            if row.reserve != want:
                updates.append((row.row, snapshot.reserve_col, want))
        # База для следующего цикла — то, что после этой записи будет в ячейке.
        balance.mirrored_quantity = balance.quantity

    if updates:
        # Запись ретраебельна (полная перезапись значений, не дельта), но идёт через
        # `run_on_sheets_executor`, а не `run_sheets_read`: ретраи чтения настроены
        # под другой профиль ошибок, и мешать их здесь незачем.
        await run_on_sheets_executor(mirror.write_columns, key, updates)

    invisible = tuple(sorted(set(balances) - seen))
    if invisible:
        logger.warning("stock_mirror.invisible_skus", key=key, count=len(invisible))
    return AccountMirrorResult(
        account_id=account.id,
        key=key,
        cells_written=len(updates),
        edits=tuple(edits),
        unknown_skus=tuple(sorted(set(unknown))),
        invisible_skus=invisible,
    )


def _edit_verdict(
    sheet_quantity: int, mirrored: int | None, pg_quantity: int, settings: Settings
) -> tuple[bool, str] | None:
    """Правил ли человек ячейку — и можно ли применить эту правку.

    Сравниваем с `mirrored_quantity`, а НЕ с `quantity`: расхождение с `quantity` —
    это штатное отставание листа от PG между циклами, и трактовать его как правку
    значило бы применять к остатку собственное же прошлое значение.

    `mirrored is None` — строка ещё ни разу не зеркалилась (backfill не проходил).
    Базы для сравнения нет, а угадывать здесь нельзя: без базы «человек поправил»
    неотличимо от «PG ушёл вперёд».
    """
    if mirrored is None or sheet_quantity == mirrored:
        return None
    if sheet_quantity < 0:
        return False, "відʼємний залишок"
    limit = settings.stock_manual_delta_limit
    delta = sheet_quantity - pg_quantity
    if limit and abs(delta) > limit:
        return False, f"зміна на {delta:+d} більша за ліміт {limit}"
    return True, ""


async def mirror_all_accounts(
    session: AsyncSession,
    *,
    mirror: StockSheetMirror | None = None,
    settings: Settings | None = None,
) -> list[AccountMirrorResult]:
    cfg = settings or get_settings()
    sheet = mirror or StockSheetMirror()
    accounts = (await session.scalars(select(ClientAccount).order_by(ClientAccount.name))).all()
    results = []
    for account in accounts:
        results.append(await mirror_account(session, account, mirror=sheet, settings=cfg))
    logger.info(
        "stock_mirror.pass",
        accounts=len(results),
        cells=sum(r.cells_written for r in results),
        edits=sum(len(r.edits) for r in results),
        errors=sum(1 for r in results if r.error),
    )
    return results
