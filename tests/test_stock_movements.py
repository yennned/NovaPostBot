"""Запись движений брони под ТТН — одной пачкой, а не по строке."""

from __future__ import annotations

from dataclasses import dataclass

from app.db.models.enums import StockMovementType, UserRole, UserStatus
from app.db.repositories import StockMovementRepository, UserRepository
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import account_of


@dataclass(frozen=True, slots=True)
class _Item:
    sku: str
    quantity: int


async def test_record_for_items_writes_one_batch(db_session: AsyncSession, monkeypatch):
    """Многопозиционная ТТН — один flush, а не flush на позицию.

    Каждый flush это round-trip, и все они ложатся внутрь апдейта, при котором
    коннект и так удерживается через вызовы НП и Sheets. На восьмипозиционной
    корзине это восемь лишних ожиданий сети на ровном месте.
    """
    client = await UserRepository(db_session).create(
        telegram_id=9100, role=UserRole.client, status=UserStatus.active
    )
    account = await account_of(db_session, client)
    repo = StockMovementRepository(db_session)

    flushes = 0
    original = db_session.flush

    async def counting_flush(*args, **kwargs):
        nonlocal flushes
        flushes += 1
        return await original(*args, **kwargs)

    monkeypatch.setattr(db_session, "flush", counting_flush)
    await repo.record_for_items(
        client_id=client.id,
        account_id=account.id,
        shipment_id=None,
        items=[_Item(sku=f"SKU-{i}", quantity=1) for i in range(8)],
        movement_type=StockMovementType.ttn_reserve,
        sign=-1,
        comment="резерв",
    )

    assert flushes == 1, f"ожидался один flush на пачку, было {flushes}"


async def test_empty_basket_writes_nothing(db_session: AsyncSession, monkeypatch):
    """Пустая пачка не должна даже ходить в БД."""
    client = await UserRepository(db_session).create(
        telegram_id=9101, role=UserRole.client, status=UserStatus.active
    )
    account = await account_of(db_session, client)

    flushed = False
    original = db_session.flush

    async def counting_flush(*args, **kwargs):
        nonlocal flushed
        flushed = True
        return await original(*args, **kwargs)

    monkeypatch.setattr(db_session, "flush", counting_flush)
    await StockMovementRepository(db_session).record_for_items(
        client_id=client.id,
        account_id=account.id,
        shipment_id=None,
        items=[],
        movement_type=StockMovementType.ttn_reserve,
        sign=-1,
        comment="резерв",
    )

    assert flushed is False
