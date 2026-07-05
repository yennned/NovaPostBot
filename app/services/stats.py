"""Статистика клиента по отправлениям и остаткам (Фаза 3)."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models.enums import ShipmentStatus
from app.db.models.user import User
from app.db.repositories import ShipmentRepository
from app.services import shipments
from app.services.inventory import get_inventory_snapshot
from app.sheets import StockSource
from app.utils.timefmt import now_local

DISPATCHED_STATUSES = {
    ShipmentStatus.dispatched,
    ShipmentStatus.in_transit,
    ShipmentStatus.arrived,
    ShipmentStatus.delivered,
}
# RETURN_STATUSES / LOSS_STATUSES — единый источник в `shipments` (см. там).
RETURN_STATUSES = shipments.RETURN_STATUSES
LOSS_STATUSES = shipments.LOSS_STATUSES


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


def _bounds(
    period: str,
    *,
    day: date | None,
    settings: Settings,
    date_from: date | None = None,
    date_to: date | None = None,
) -> tuple[datetime, datetime]:
    """Границы периода `[start, end)` в зоне отделения (`time.min` каждой даты).

    `end` — начало следующего периода (завтра / след. понедельник / 1-е след.
    месяца), НЕ `now`. Верхняя граница по `now()` семантически урезала бы «сьогодні»
    и была хрупкой: `status_changed_at` штампует Postgres (`server_default now()`), а
    `now` считался на часах приложения — рассинхрон в пару мс уводил свежую строку
    «в будущее» относительно `end`, и она выпадала из отчёта.

    Приоритет: диапазон (`date_from`/`date_to`, включительно по обе даты) → один
    день (`day`) → предустановленный `period`.
    """
    tz = ZoneInfo(settings.timezone)

    def _midnight(value: date) -> datetime:
        return datetime.combine(value, time.min, tzinfo=tz)

    if date_from is not None and date_to is not None:
        start_day, end_day = sorted((date_from, date_to))
        return _midnight(start_day), _midnight(end_day + timedelta(days=1))
    if day is not None:
        return _midnight(day), _midnight(day + timedelta(days=1))

    today = datetime.now(tz).date()
    if period == "week":
        monday = today - timedelta(days=today.weekday())
        return _midnight(monday), _midnight(monday + timedelta(days=7))
    if period == "month":
        first = today.replace(day=1)
        if first.month == 12:
            next_first = first.replace(year=first.year + 1, month=1)
        else:
            next_first = first.replace(month=first.month + 1)
        return _midnight(first), _midnight(next_first)
    return _midnight(today), _midnight(today + timedelta(days=1))


async def get_client_stats(
    session: AsyncSession,
    *,
    client: User,
    period: str = "today",
    day: date | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    reader: StockSource | None = None,
    settings: Settings | None = None,
) -> ClientStatsSnapshot:
    shipments._require_active_client(client)
    cfg = settings or get_settings()
    start, end = _bounds(period, day=day, settings=cfg, date_from=date_from, date_to=date_to)
    repo = ShipmentRepository(session)
    dispatched_shipments = await repo.list_dispatched_between(client.id, start=start, end=end)
    returned_shipments = await repo.list_status_changed_between(
        client.id,
        start=start,
        end=end,
        statuses=RETURN_STATUSES,
    )
    lost_shipments = await repo.list_status_changed_between(
        client.id,
        start=start,
        end=end,
        statuses=LOSS_STATUSES,
    )

    shipped = Counter[str]()
    returned = Counter[str]()
    lost = Counter[str]()
    for shipment in dispatched_shipments:
        for item in shipment.items:
            shipped[item.sku] += item.quantity
    for shipment in returned_shipments:
        for item in shipment.items:
            returned[item.sku] += item.quantity
    for shipment in lost_shipments:
        for item in shipment.items:
            lost[item.sku] += item.quantity

    inventory = await get_inventory_snapshot(session, client=client, reader=reader)
    total_available = sum(item.available for item in inventory)
    shipped_qty = sum(shipped.values())
    returns_qty = sum(returned.values())
    losses_qty = sum(lost.values())
    top_skus = [TopSkuStat(sku=sku, quantity=qty) for sku, qty in shipped.most_common(5)]
    if date_from is not None and date_to is not None:
        resolved_period = "range"
    elif day is not None:
        resolved_period = "day"
    else:
        resolved_period = period
    return ClientStatsSnapshot(
        period=resolved_period,
        start=start,
        # Для показа обрезаем верхнюю границу до «сейчас»: окно запроса `[start, end)`
        # тянется до конца периода (см. `_bounds`), но пользователю «Період» не должен
        # уходить в завтра/след. месяц.
        end=min(end, now_local(cfg)),
        shipped_qty=shipped_qty,
        returns_qty=returns_qty,
        losses_qty=losses_qty,
        net_sales_qty=shipped_qty - returns_qty - losses_qty,
        total_available=total_available,
        top_skus=top_skus,
    )
