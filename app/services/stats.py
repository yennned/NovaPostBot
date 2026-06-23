"""Статистика клиента по отправлениям и остаткам (Фаза 3)."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models.enums import ShipmentStatus, UserRole, UserStatus
from app.db.models.user import User
from app.db.repositories import ShipmentRepository
from app.services.exceptions import PermissionDenied
from app.services.inventory import get_inventory_snapshot
from app.sheets import StockSource

DISPATCHED_STATUSES = {
    ShipmentStatus.dispatched,
    ShipmentStatus.in_transit,
    ShipmentStatus.arrived,
    ShipmentStatus.delivered,
}
RETURN_STATUSES = {ShipmentStatus.returning, ShipmentStatus.returned}
LOSS_STATUSES = {ShipmentStatus.lost, ShipmentStatus.damaged}


@dataclass(frozen=True, slots=True)
class TopSkuStat:
    sku: str
    quantity: int


@dataclass(frozen=True, slots=True)
class ClientStatsSnapshot:
    period: str
    start: datetime
    end: datetime
    shipped_qty: int
    returns_qty: int
    losses_qty: int
    net_sales_qty: int
    total_available: int
    top_skus: list[TopSkuStat]


def _require_active_client(client: User) -> None:
    if client.role is not UserRole.client:
        raise PermissionDenied("кабінет доступний тільки клієнту")
    if client.status is not UserStatus.active:
        raise PermissionDenied("кабінет клієнта доступний після підтвердження")


def _bounds(period: str, *, day: date | None, settings: Settings) -> tuple[datetime, datetime]:
    tz = ZoneInfo(settings.timezone)
    now = datetime.now(tz)
    if day is not None:
        start = datetime.combine(day, time.min, tzinfo=tz)
        return start, start + timedelta(days=1)
    if period == "week":
        start = datetime.combine((now - timedelta(days=now.weekday())).date(), time.min, tzinfo=tz)
        return start, now
    if period == "month":
        start = datetime.combine(now.date().replace(day=1), time.min, tzinfo=tz)
        return start, now
    start = datetime.combine(now.date(), time.min, tzinfo=tz)
    return start, now


async def get_client_stats(
    session: AsyncSession,
    *,
    client: User,
    period: str = "today",
    day: date | None = None,
    reader: StockSource | None = None,
    settings: Settings | None = None,
) -> ClientStatsSnapshot:
    _require_active_client(client)
    cfg = settings or get_settings()
    start, end = _bounds(period, day=day, settings=cfg)
    shipments = await ShipmentRepository(session).list_status_changed_between(
        client.id, start=start, end=end
    )

    shipped = Counter[str]()
    returned = Counter[str]()
    lost = Counter[str]()
    for shipment in shipments:
        target = None
        if shipment.status in DISPATCHED_STATUSES:
            target = shipped
        elif shipment.status in RETURN_STATUSES:
            target = returned
        elif shipment.status in LOSS_STATUSES:
            target = lost
        if target is None:
            continue
        for item in shipment.items:
            target[item.sku] += item.quantity

    inventory = await get_inventory_snapshot(session, client=client, reader=reader)
    total_available = sum(item.available for item in inventory)
    shipped_qty = sum(shipped.values())
    returns_qty = sum(returned.values())
    losses_qty = sum(lost.values())
    top_skus = [TopSkuStat(sku=sku, quantity=qty) for sku, qty in shipped.most_common(5)]
    return ClientStatsSnapshot(
        period="day" if day is not None else period,
        start=start,
        end=end,
        shipped_qty=shipped_qty,
        returns_qty=returns_qty,
        losses_qty=losses_qty,
        net_sales_qty=shipped_qty - returns_qty - losses_qty,
        total_available=total_available,
        top_skus=top_skus,
    )
