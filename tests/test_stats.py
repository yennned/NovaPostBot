"""Тесты статистики клиента (`app/services/stats.py`)."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.db.models.enums import ShipmentStatus, UserRole, UserStatus
from app.db.repositories import ShipmentItemDraft, ShipmentRepository, UserRepository
from app.services import stats as stats_module
from app.services.stats import _bounds, get_client_stats
from app.sheets.inventory import StockRow
from sqlalchemy.ext.asyncio import AsyncSession

TZ = ZoneInfo("Europe/Kyiv")
_KYIV_SETTINGS = SimpleNamespace(timezone="Europe/Kyiv")


def _freeze_today(monkeypatch, moment: datetime) -> None:
    """Заморозить `datetime.now(tz)` внутри stats на `moment` (для веток `_bounds`)."""

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment if tz is None else moment.astimezone(tz)

    monkeypatch.setattr(stats_module, "datetime", _Frozen)


def test_bounds_today_end_is_next_midnight_not_now(monkeypatch):
    # Регрессия: раньше end=now → строка со штампом БД чуть «в будущем» выпадала.
    moment = datetime(2026, 7, 4, 14, 30, tzinfo=TZ)
    _freeze_today(monkeypatch, moment)
    start, end = _bounds("today", day=None, settings=_KYIV_SETTINGS)
    assert start == datetime(2026, 7, 4, 0, 0, tzinfo=TZ)
    assert end == datetime(2026, 7, 5, 0, 0, tzinfo=TZ)
    assert end > moment  # окно тянется за «сейчас» — свежая строка не выпадет


def test_bounds_week_starts_monday_spans_seven_days(monkeypatch):
    _freeze_today(monkeypatch, datetime(2026, 7, 2, 9, 0, tzinfo=TZ))  # четверг
    start, end = _bounds("week", day=None, settings=_KYIV_SETTINGS)
    assert start.weekday() == 0  # понедельник
    assert start == datetime(2026, 6, 29, 0, 0, tzinfo=TZ)
    assert end - start == timedelta(days=7)


def test_bounds_month_rollover_december_to_january(monkeypatch):
    _freeze_today(monkeypatch, datetime(2026, 12, 15, 10, 0, tzinfo=TZ))
    start, end = _bounds("month", day=None, settings=_KYIV_SETTINGS)
    assert start == datetime(2026, 12, 1, 0, 0, tzinfo=TZ)
    assert end == datetime(2027, 1, 1, 0, 0, tzinfo=TZ)


def test_bounds_range_inclusive_and_orders_dates():
    from datetime import date

    start, end = _bounds(
        "today",
        day=None,
        settings=_KYIV_SETTINGS,
        date_from=date(2026, 7, 1),
        date_to=date(2026, 7, 3),
    )
    assert start == datetime(2026, 7, 1, 0, 0, tzinfo=TZ)
    assert end == datetime(2026, 7, 4, 0, 0, tzinfo=TZ)  # конец — включительно (to+1 день)


def test_bounds_range_swaps_reversed_dates():
    from datetime import date

    start, end = _bounds(
        "today",
        day=None,
        settings=_KYIV_SETTINGS,
        date_from=date(2026, 7, 3),
        date_to=date(2026, 7, 1),  # перепутан порядок
    )
    assert start == datetime(2026, 7, 1, 0, 0, tzinfo=TZ)
    assert end == datetime(2026, 7, 4, 0, 0, tzinfo=TZ)


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


def test_period_label_shows_inclusive_last_day():
    """«Період» показывает последний включённый день, не эксклюзивную границу."""
    from app.bot.texts.client_cabinet import _period_label
    from app.services.stats import ClientStatsSnapshot

    def _snap(start: datetime, end: datetime) -> ClientStatsSnapshot:
        return ClientStatsSnapshot(
            period="range",
            start=start,
            end=end,
            shipped_qty=0,
            returns_qty=0,
            losses_qty=0,
            net_sales_qty=0,
            total_available=0,
            top_skus=[],
        )

    # Диапазон 01–03.07: end эксклюзивна (полночь 04.07) → метка 01.07 — 03.07.
    label = _period_label(_snap(datetime(2026, 7, 1, tzinfo=TZ), datetime(2026, 7, 4, tzinfo=TZ)))
    assert label == "01.07.2026 — 03.07.2026"

    # Один день: start==последний день → одна дата без диапазона.
    single = _period_label(_snap(datetime(2026, 7, 1, tzinfo=TZ), datetime(2026, 7, 2, tzinfo=TZ)))
    assert single == "01.07.2026"
