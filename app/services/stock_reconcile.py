"""Сверка остатка: Postgres против листа «Склад» и Postgres против самого себя.

Две проверки, и вторая важнее первой.

**Внутренний инвариант PG** — `SUM(дельт по физическим типам) == quantity`. Он
ловит баг в нашем собственном коде: движение записали, а баланс не сдвинули, или
наоборот. Никакое сравнение с Google этого не даёт, потому что расхождение с
листом объясняется чем угодно — отставанием зеркала, правкой человека, приёмкой в
процессе.

**Сравнение с листом** — вспомогательное и намеренно осторожное:

- сверяется **только `Кількість`**. Описательными колонками владеет лист
  (`app/services/stock_mirror.py`), и их различие — не расхождение, а норма;
- джоба **никогда не «усыновляет» число из листа**. SKU, которого нет в PG, —
  это приёмка мимо «Приймання» или опечатка в артикуле; импортируй его, и любая
  опечатка откроет дыру под oversell. Сообщаем, но не импортируем;
- расхождение количеств эскалируется, **только пережив два цикла подряд**.
  Зеркало пишет в лист не мгновенно, и одиночное несовпадение — это штатное
  отставание, а не дрейф. Без этого фильтра владелец получал бы поток ложных
  тревог и перестал бы их читать — то есть сверка стала бы хуже, чем её
  отсутствие.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.client_account import ClientAccount
from app.db.repositories import StockBalanceRepository
from app.services.inventory_backend import stock_sheet_key
from app.sheets.mirror import MirrorSheetError, StockSheetMirror
from app.sheets.runtime import run_sheets_read
from app.sheets.source import StockSheetNotFound

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class QuantityDrift:
    sku: str
    pg: int
    sheet: int


@dataclass(frozen=True, slots=True)
class AccountReconcileResult:
    account_id: object
    key: str
    #: Расхождения количеств, пережившие два цикла подряд, — только они и тревога.
    confirmed: tuple[QuantityDrift, ...] = field(default=())
    #: Замечены впервые: ждём следующего цикла, тревогу не поднимаем.
    pending: tuple[QuantityDrift, ...] = field(default=())
    #: Есть в листе, нет в PG. Не импортируем — сообщаем.
    sheet_only: tuple[str, ...] = field(default=())
    #: Есть в PG, нет в листе: оператор их не видит.
    pg_only: tuple[str, ...] = field(default=())
    #: Внутренний инвариант PG разошёлся: `(sku, ожидание по журналу, факт)`.
    ledger_drift: tuple[tuple[str, int, int], ...] = field(default=())
    error: str | None = None


#: Что видели в прошлом цикле: `{(account_id, sku): (pg, sheet)}`. Память процесса,
#: а не таблица: перезапуск воркера всего лишь откладывает эскалацию на один цикл,
#: а отдельная таблица ради этого — лишняя сущность в горячем пути обслуживания.
_seen: dict[tuple[str, str], tuple[int, int]] = {}


def reset_seen() -> None:
    """Забыть замеченные расхождения (тесты, ручной перезапуск сверки)."""
    _seen.clear()


async def reconcile_account(
    session: AsyncSession,
    account: ClientAccount,
    *,
    mirror: StockSheetMirror,
) -> AccountReconcileResult:
    key = stock_sheet_key(account)
    repo = StockBalanceRepository(session)
    ledger_drift = tuple(await repo.ledger_matches_balance(account.id))
    if ledger_drift:
        # Это баг в нашем коде, а не рассинхрон с Google. Отдельный уровень лога,
        # потому что и реакция другая: чинить надо нас, а не таблицу.
        logger.error(
            "stock_reconcile.ledger_drift", account_id=str(account.id), items=len(ledger_drift)
        )

    try:
        snapshot = await run_sheets_read(mirror.read_snapshot, key)
    except StockSheetNotFound:
        return AccountReconcileResult(account.id, key, ledger_drift=ledger_drift)
    except MirrorSheetError as exc:
        return AccountReconcileResult(account.id, key, ledger_drift=ledger_drift, error=str(exc))

    balances = {b.sku: b.quantity for b in await repo.list_for_account(account.id)}
    sheet = {row.sku: row.quantity for row in snapshot.rows}

    confirmed: list[QuantityDrift] = []
    pending: list[QuantityDrift] = []
    for sku, sheet_quantity in sheet.items():
        if sku not in balances:
            continue
        pg_quantity = balances[sku]
        state = (str(account.id), sku)
        if pg_quantity == sheet_quantity:
            _seen.pop(state, None)
            continue
        drift = QuantityDrift(sku=sku, pg=pg_quantity, sheet=sheet_quantity)
        # Сравниваем и ЗНАЧЕНИЯ тоже, а не только факт расхождения: если между
        # циклами числа изменились, это живой процесс (отгрузка, приёмка), а не
        # застывший дрейф, и тревожить рано.
        if _seen.get(state) == (pg_quantity, sheet_quantity):
            confirmed.append(drift)
        else:
            pending.append(drift)
            _seen[state] = (pg_quantity, sheet_quantity)

    return AccountReconcileResult(
        account_id=account.id,
        key=key,
        confirmed=tuple(confirmed),
        pending=tuple(pending),
        sheet_only=tuple(sorted(set(sheet) - set(balances))),
        pg_only=tuple(sorted(set(balances) - set(sheet))),
        ledger_drift=ledger_drift,
    )


async def reconcile_all_accounts(
    session: AsyncSession,
    *,
    mirror: StockSheetMirror | None = None,
) -> list[AccountReconcileResult]:
    sheet = mirror or StockSheetMirror()
    accounts = (await session.scalars(select(ClientAccount).order_by(ClientAccount.name))).all()
    results = [await reconcile_account(session, account, mirror=sheet) for account in accounts]
    logger.info(
        "stock_reconcile.pass",
        accounts=len(results),
        confirmed=sum(len(r.confirmed) for r in results),
        pending=sum(len(r.pending) for r in results),
        sheet_only=sum(len(r.sheet_only) for r in results),
        ledger_drift=sum(len(r.ledger_drift) for r in results),
    )
    return results


def report_text(result: AccountReconcileResult) -> str | None:
    """Сообщение владельцу или `None`, если сообщать не о чем.

    Сознательно молчим о `pending`: одиночное несовпадение — это отставание
    зеркала, и рассказывать о нём значит приучить владельца игнорировать сверку.
    """
    lines: list[str] = []
    for sku, expected, actual in result.ledger_drift:
        lines.append(f"• ⚠️ {sku}: журнал рухів каже {expected}, залишок {actual} — це баг у боті")
    for drift in result.confirmed:
        lines.append(f"• {drift.sku}: у боті {drift.pg}, у листі {drift.sheet}")
    if result.sheet_only:
        listed = ", ".join(result.sheet_only[:10])
        lines.append(f"• у листі є артикули, яких немає в боті: {listed} (не імпортуємо)")
    if result.error:
        lines.append(f"• лист не читається: {result.error}")
    if not lines:
        return None
    return f"🔍 <b>Звірка складу</b> · {result.key}\n" + "\n".join(lines)
