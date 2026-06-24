"""Тесты статистики клиента (`app/services/stats.py`)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.db.models.enums import ShipmentStatus, UserRole, UserStatus
from app.db.repositories import ShipmentItemDraft, ShipmentRepository, UserRepository
from app.services.stats import get_client_stats
from app.sheets.inventory import StockRow
from sqlalchemy.ext.asyncio import AsyncSession

TZ = ZoneInfo("Europe/Kyiv")


class _Reader:
    def read_stock(self, client_key: str):
        return [
            StockRow(
                sku="SKU-1",
                name="Кава",
                category="Напої",
                quantity=10,
                price=Decimal("100"),
            )
        ]


async def _client(session: AsyncSession, telegram_id: int = 600):
    return await UserRepository(session).create(
        telegram_id=telegram_id,
        full_name="Клієнт",
        role=UserRole.client,
        status=UserStatus.active,
    )


async def test_client_stats_count_dispatched_and_returned_same_shipment(
    db_session: AsyncSession,
):
    client = await _client(db_session)
    now = datetime.now(TZ)
    shipment = await ShipmentRepository(db_session).create(
        client_id=client.id,
        recipient_name="Отримувач",
        items=[ShipmentItemDraft(sku="SKU-1", name="Кава", quantity=3, unit_price=Decimal("100"))],
        status=ShipmentStatus.returned,
        status_changed_at=now,
    )
    shipment.dispatched_at = now
    await db_session.flush()

    stats = await get_client_stats(db_session, client=client, period="today", reader=_Reader())

    assert stats.shipped_qty == 3
    assert stats.returns_qty == 3
    assert stats.losses_qty == 0
    assert stats.net_sales_qty == 0
    assert stats.total_available == 10
    assert stats.top_skus[0].sku == "SKU-1"
