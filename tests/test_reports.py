"""Тесты сервиса отчётов/аналитики (`app/services/reports.py`) — на Postgres."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from app.bot import permissions as perm
from app.db.models.enums import ShipmentStatus, SupportThreadStatus, UserRole, UserStatus
from app.db.repositories import (
    ShipmentItemDraft,
    ShipmentRepository,
    SupportRepository,
    UserRepository,
)
from app.services import reports
from app.services.exceptions import PermissionDenied
from app.services.shipment import compute_shipment_fee
from sqlalchemy.ext.asyncio import AsyncSession

TZ = ZoneInfo("Europe/Kyiv")


async def _owner(session: AsyncSession, telegram_id: int = 1):
    return await UserRepository(session).create(
        telegram_id=telegram_id, role=UserRole.owner, status=UserStatus.active
    )


async def _client(session: AsyncSession, telegram_id: int = 100, name: str = "Клієнт"):
    return await UserRepository(session).create(
        telegram_id=telegram_id, full_name=name, role=UserRole.client, status=UserStatus.active
    )


async def _shipment(session, *, client_id, status, qty, **fields):
    repo = ShipmentRepository(session)
    shipment = await repo.create(
        client_id=client_id,
        recipient_name="Отримувач",
        items=[ShipmentItemDraft(sku="A1", name="Товар", quantity=qty)],
        status=status,
    )
    for key, value in fields.items():
        setattr(shipment, key, value)
    await session.flush()
    return shipment


def test_compute_shipment_fee_formula():
    assert compute_shipment_fee(1) == Decimal(20)
    assert compute_shipment_fee(3) == Decimal(22)
    assert compute_shipment_fee(0) == Decimal(0)


async def test_period_report_aggregates_by_client(db_session: AsyncSession):
    owner = await _owner(db_session)
    client = await _client(db_session)
    await _shipment(db_session, client_id=client.id, status=ShipmentStatus.dispatched, qty=3)
    await _shipment(db_session, client_id=client.id, status=ShipmentStatus.returned, qty=2)
    await _shipment(db_session, client_id=client.id, status=ShipmentStatus.lost, qty=1)

    report = await reports.period_report(db_session, actor=owner, period="today")

    assert (report.shipped, report.returns, report.losses) == (3, 2, 1)
    assert report.net == 0
    assert len(report.clients) == 1
    assert report.clients[0].net == 0


async def test_period_report_counts_dispatched_and_returned_same_shipment(db_session: AsyncSession):
    owner = await _owner(db_session, telegram_id=2)
    client = await _client(db_session, telegram_id=101)
    now = datetime.now(TZ)
    await _shipment(
        db_session,
        client_id=client.id,
        status=ShipmentStatus.returned,
        qty=4,
        dispatched_at=now,
        status_changed_at=now,
    )

    report = await reports.period_report(db_session, actor=owner, period="today")

    assert (report.shipped, report.returns, report.losses) == (4, 4, 0)
    assert report.net == 0
    assert report.clients[0].shipped == 4
    assert report.clients[0].returns == 4


async def test_period_report_custom_day_bounds(db_session: AsyncSession):
    owner = await _owner(db_session, telegram_id=3)
    client = await _client(db_session, telegram_id=102)
    target = datetime(2026, 6, 20, 12, 0, tzinfo=TZ)
    await _shipment(
        db_session,
        client_id=client.id,
        status=ShipmentStatus.dispatched,
        qty=5,
        dispatched_at=target,
        status_changed_at=target,
    )

    same_day = await reports.period_report(db_session, actor=owner, day=target.date())
    assert same_day.shipped == 5
    assert same_day.period == "day"  # текст покажет конкретную дату, не пресет

    other_day = await reports.period_report(db_session, actor=owner, day=date(2026, 6, 19))
    assert other_day.shipped == 0


async def test_period_report_requires_view_permission(db_session: AsyncSession):
    revoked = await UserRepository(db_session).create(
        telegram_id=10,
        role=UserRole.manager,
        status=UserStatus.active,
        permissions={perm.CAN_VIEW_REPORTS: False},
    )
    with pytest.raises(PermissionDenied):
        await reports.period_report(db_session, actor=revoked, period="today")


async def test_financial_report_sums_fee_and_lists_late(db_session: AsyncSession):
    owner = await _owner(db_session)
    client = await _client(db_session)
    now = datetime.now(TZ)
    await _shipment(
        db_session,
        client_id=client.id,
        status=ShipmentStatus.dispatched,
        qty=3,
        dispatched_at=now,
        fee_amount=Decimal("22"),
        fee_free=False,
        sla_met=True,
    )
    await _shipment(
        db_session,
        client_id=client.id,
        status=ShipmentStatus.dispatched,
        qty=1,
        ttn_number="20450000000001",
        dispatched_at=now,
        fee_amount=Decimal("0"),
        fee_free=True,
        sla_met=False,
    )

    fin = await reports.financial_report(db_session, actor=owner, period="today")

    assert fin.fee_total == Decimal("22")
    assert fin.free_count == 1
    assert fin.dispatched_count == 2
    assert [late.ttn_number for late in fin.late] == ["20450000000001"]


async def test_financial_report_owner_only(db_session: AsyncSession):
    manager = await UserRepository(db_session).create(
        telegram_id=11, role=UserRole.manager, status=UserStatus.active
    )
    with pytest.raises(PermissionDenied):
        await reports.financial_report(db_session, actor=manager, period="today")


async def test_manager_support_stats_counts(db_session: AsyncSession):
    owner = await _owner(db_session)
    client = await _client(db_session)
    manager = await UserRepository(db_session).create(
        telegram_id=12, full_name="Олег", role=UserRole.manager, status=UserStatus.active
    )
    support = SupportRepository(db_session)
    await support.create_thread(
        client_id=client.id, assigned_manager_id=manager.id, status=SupportThreadStatus.open
    )
    closed = await support.create_thread(
        client_id=client.id, assigned_manager_id=manager.id, status=SupportThreadStatus.open
    )
    await support.close_thread(closed)

    stats = await reports.manager_support_stats(db_session, actor=owner, period="today")

    by_id = {s.manager_id: s for s in stats}
    assert by_id[manager.id].open_count == 1
    assert by_id[manager.id].closed_count == 1
