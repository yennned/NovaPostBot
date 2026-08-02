"""Разовая уборка журнала: снятие брони под ТТН, закрытыми до PR #158.

Проверяется то, из-за чего скрипт мог бы «отработать» и ничего не починить:
выборка идёт по всем аккаунтам, а не по первому, и запись не спотыкается о
ленивую загрузку `shipment.items`.
"""

from __future__ import annotations

from decimal import Decimal

from app.db.models.enums import ShipmentStatus, StockMovementType, UserRole, UserStatus
from app.db.models.stock_movement import StockMovement
from app.db.repositories import (
    ShipmentItemDraft,
    ShipmentRepository,
    StockMovementRepository,
    UserRepository,
)
from scripts.repair_stock_reserves import find_broken, repair
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import account_of


async def _closed_ttn_with_reserve(session: AsyncSession, *, telegram_id: int, ttn: str):
    user = await UserRepository(session).create(
        telegram_id=telegram_id,
        full_name=f"Клієнт {telegram_id}",
        role=UserRole.client,
        status=UserStatus.active,
    )
    account = await account_of(session, user)
    shipments = ShipmentRepository(session)
    created = await shipments.create(
        client_id=user.id,
        recipient_name="Іван",
        ttn_number=ttn,
        status=ShipmentStatus.cancelled,
        items=[ShipmentItemDraft(sku="SKU-1", name="Кава", quantity=4, unit_price=Decimal("100"))],
    )
    shipment = await shipments.get_by_id(created.id)
    await StockMovementRepository(session).record_for_items(
        client_id=user.id,
        account_id=account.id,
        shipment_id=shipment.id,
        actor_user_id=user.id,
        items=shipment.items,
        movement_type=StockMovementType.ttn_reserve,
        sign=-1,
        comment="Резерв",
    )
    await session.flush()
    return created.id


async def test_repair_closes_reserves_across_all_accounts(db_session: AsyncSession):
    """Чинятся все аккаунты, а журнал после уборки сходится в ноль.

    Мутация: оборвать цикл по аккаунтам после первого (`break` в `find_broken`) —
    вторая ТТН останется в выборке, и финальный assert покраснеет.
    """
    first = await _closed_ttn_with_reserve(db_session, telegram_id=1900, ttn="59001900")
    second = await _closed_ttn_with_reserve(db_session, telegram_id=1901, ttn="59001901")

    broken = await find_broken(db_session)
    assert sorted(number for _, _, number, _ in broken) == ["59001900", "59001901"]

    await repair(db_session, [shipment_id for _, shipment_id, _, _ in broken])
    await db_session.flush()

    assert await find_broken(db_session) == []
    for shipment_id in (first, second):
        rows = list(
            await db_session.scalars(
                select(StockMovement).where(StockMovement.shipment_id == shipment_id)
            )
        )
        assert sorted(row.movement_type.value for row in rows) == ["ttn_cancel", "ttn_reserve"]
        assert sum(row.quantity_delta for row in rows) == 0


async def test_repair_does_not_move_stock(db_session: AsyncSession):
    """Уборка чинит журнал и только его: количество на складе трогать нечем.

    Бронь никогда не двигала `quantity` — она выводится из статуса ТТН. Если бы
    компенсирующее движение считалось физическим, уборка задрала бы остаток на
    всю накопленную бронь разом.
    """
    from app.db.repositories import StockBalanceRepository

    shipment_id = await _closed_ttn_with_reserve(db_session, telegram_id=1902, ttn="59001902")
    broken = await find_broken(db_session)
    account_id = broken[0][0].id
    balances = StockBalanceRepository(db_session)
    await balances.apply_movement(
        account_id=account_id, sku="SKU-1", delta=10, movement_type=StockMovementType.intake
    )

    await repair(db_session, [shipment_id])
    await db_session.flush()

    balance = await balances.get(account_id=account_id, sku="SKU-1")
    assert balance is not None and balance.quantity == 10
    assert await balances.ledger_matches_balance(account_id) == []
