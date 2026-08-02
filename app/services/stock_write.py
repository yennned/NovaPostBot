"""Куда уходит физическое движение остатка: в лист Google или в `stock_balances`.

**Зачем отдельный модуль.** Развилку чтения держит `InventoryBackend`
(`app/services/inventory_backend.py`), и он намеренно только про чтение: у гейта от
oversell на Sheets и на Postgres нет общего интерфейса, который не был бы фикцией.
Но физическое списание при отправке и приём возврата — это НЕ гейт: проверять
нечего, есть готовая дельта, которую надо применить. Здесь формы совпадают, и
общая точка честна.

**Почему без неё нельзя выкатывать `INVENTORY_SOURCE=pg`.** Чтение переехало в
Postgres ещё в шаге 3, а списание при отправке продолжало писать в лист. Дальше
включалось зеркало и видело `лист != mirrored_quantity` — то есть трактовало
**штатную отправку как ручную правку человека**: движение `manual`, пуш владельцу
на каждую ТТН. А если корзина больше `STOCK_MANUAL_DELTA_LIMIT`, зеркало правку
отклоняло и возвращало в ячейку старое число — списание не применялось в PG
**никогда**, и остаток оставался завышенным. Тем же ломался инвариант
«сумма физических дельт == quantity»: `ttn_dispatch` числится физическим типом, но
`record_for_items` пишет его заглушками и количество не двигает, а зеркало
добавляет поверх второе движение.

На пути `sheets` поведение прежнее до последнего вызова — это сеть безопасности
отката.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.client_account import ClientAccount
from app.db.models.enums import StockMovementType
from app.db.repositories import StockBalanceRepository, StockMovementRepository
from app.services.inventory_backend import build_inventory_backend, stock_sheet_key
from app.sheets import StockDelta, StockSource, build_stock_source, run_on_sheets_executor

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class StockWriteItem:
    """Позиция физического движения. `quantity` — всегда положительная величина;
    направление задаёт `sign` в `apply_physical_movement`."""

    sku: str
    quantity: int
    name: str | None = None
    category: str | None = None
    unit_price: Decimal | None = None


def items_from_shipment(items: Iterable) -> list[StockWriteItem]:
    """Позиции ТТН → позиции движения. Отдельная функция, чтобы вызывающие не
    зависели от того, как устроен `ShipmentItem`."""
    return [
        StockWriteItem(
            sku=item.sku,
            quantity=item.quantity,
            name=item.name,
            category=item.category,
            unit_price=item.unit_price,
        )
        for item in items
    ]


async def apply_physical_movement(
    session: AsyncSession,
    *,
    account: ClientAccount,
    client_id: uuid.UUID | None,
    shipment_id: uuid.UUID | None,
    items: Sequence[StockWriteItem],
    movement_type: StockMovementType,
    sign: int,
    comment: str,
    actor_user_id: uuid.UUID | None = None,
    mutator: StockSource | None = None,
) -> None:
    """Применить физическое движение остатка тем источником, который сейчас главный.

    `sign` — `-1` на списание, `+1` на возврат/приход. Пустой список — no-op: ходить
    ни в Google, ни в БД незачем.

    Идемпотентность (`movement_exists`) остаётся на вызывающем: он знает, по какому
    признаку движение уже могло быть записано.
    """
    if not items:
        return

    # Ветку выбирает КОНФИГУРАЦИЯ, а `mutator` — это лишь реализация Sheets внутри
    # своей ветки. На чтении правило обратное (явный `reader` побеждает конфиг), и
    # копировать его сюда было бы тихой поломкой: воркер передаёт `mutator`
    # безусловно (`app/worker.py:144`), поэтому при чтении-правиле PG-ветка не
    # включилась бы в проде никогда, а тесты этого не заметили бы — они передают
    # `mutator` ровно так же.
    if build_inventory_backend().name != "pg":
        await _apply_to_sheets(
            account=account,
            items=items,
            sign=sign,
            mutator=mutator,
        )
        await StockMovementRepository(session).record_for_items(
            client_id=client_id,
            account_id=account.id,
            shipment_id=shipment_id,
            items=items,
            movement_type=movement_type,
            sign=sign,
            comment=comment,
            actor_user_id=actor_user_id,
        )
        return

    await _apply_to_postgres(
        session,
        account_id=account.id,
        client_id=client_id,
        shipment_id=shipment_id,
        items=items,
        movement_type=movement_type,
        sign=sign,
        comment=comment,
        actor_user_id=actor_user_id,
    )


async def _apply_to_sheets(
    *,
    account: ClientAccount,
    items: Sequence[StockWriteItem],
    sign: int,
    mutator: StockSource | None,
) -> None:
    """Прежний путь целиком: одна запись в лист на всю пачку дельт.

    Не ретраится сознательно (`app/sheets/runtime.py`): запись могла примениться
    частично, и повтор удвоил бы дельту остатка.
    """
    await run_on_sheets_executor(
        (mutator or build_stock_source()).apply_deltas,
        stock_sheet_key(account),
        [
            StockDelta(
                sku=item.sku,
                quantity_delta=sign * item.quantity,
                name=item.name,
                category=item.category,
                price=item.unit_price,
            )
            for item in items
        ],
    )


async def _apply_to_postgres(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    client_id: uuid.UUID | None,
    shipment_id: uuid.UUID | None,
    items: Sequence[StockWriteItem],
    movement_type: StockMovementType,
    sign: int,
    comment: str,
    actor_user_id: uuid.UUID | None,
) -> None:
    """Остаток и журнал — одной транзакцией, через единственную точку мутации.

    `apply_movement` держит инвариант «сумма физических дельт == quantity» по
    построению: он лочит строку баланса, двигает количество и пишет движение с
    честными `quantity_before`/`quantity_after`. Отдельный `record_for_items` здесь
    не нужен и был бы вторым движением на ту же дельту.

    Порядок по SKU — как в гейте от oversell (`StockBalanceRepository.lock_stmt`):
    две одновременные операции по пересекающимся наборам обязаны брать строки в
    одном порядке, иначе Postgres снимет одну из них дедлоком.
    """
    balances = StockBalanceRepository(session)
    for item in sorted(items, key=lambda row: row.sku):
        await balances.apply_movement(
            account_id=account_id,
            sku=item.sku,
            delta=sign * item.quantity,
            movement_type=movement_type,
            client_id=client_id,
            shipment_id=shipment_id,
            actor_user_id=actor_user_id,
            comment=comment,
        )
    await session.flush()
    logger.info(
        "stock.physical_movement",
        account_id=str(account_id),
        movement_type=movement_type.value,
        positions=len(items),
        units=sign * sum(item.quantity for item in items),
    )
