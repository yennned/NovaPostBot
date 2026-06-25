"""Тесты дежурства (`app/services/duty.py`) — на Postgres."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from app.config import Settings
from app.db.models.audit import AuditLog
from app.db.models.enums import UserRole, UserStatus
from app.db.repositories import UserRepository
from app.services import duty
from app.services.exceptions import OfficeClosed, PermissionDenied
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

TZ = ZoneInfo("Europe/Kyiv")
# 2026-06-22 — понедельник (weekday 0), 2026-06-28 — воскресенье (weekday 6).
_WEEKDAYS = json.dumps({str(day): ["08:00", "20:00"] for day in range(5)})  # пн–пт 08–20


def _settings() -> Settings:
    s = Settings(_env_file=None)
    s.work_schedule_raw = _WEEKDAYS
    return s


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 6, day, hour, minute, tzinfo=TZ)


async def _manager(session: AsyncSession, telegram_id: int = 10):
    return await UserRepository(session).create(
        telegram_id=telegram_id,
        phone=f"+38067{telegram_id}",
        full_name=f"Менеджер {telegram_id}",
        role=UserRole.manager,
        status=UserStatus.active,
    )


async def _audit_actions(session: AsyncSession) -> list[str]:
    rows = await session.scalars(select(AuditLog.action).order_by(AuditLog.created_at))
    return list(rows)


async def test_go_on_duty_opens_shift(db_session: AsyncSession):
    manager = await _manager(db_session)
    now = _at(22, 10)

    result = await duty.go_on_duty(db_session, user=manager, settings=_settings(), now=now)

    assert result.window_end == _at(22, 20)
    assert manager.on_duty is True
    assert manager.duty_date == now.date()
    assert manager.duty_since == now
    assert "duty_started" in await _audit_actions(db_session)


async def test_go_on_duty_office_closed_after_hours(db_session: AsyncSession):
    manager = await _manager(db_session)

    with pytest.raises(OfficeClosed) as exc:
        await duty.go_on_duty(db_session, user=manager, settings=_settings(), now=_at(22, 21))

    assert exc.value.next_open == _at(23, 8)  # следующее открытие — вторник 08:00
    assert manager.on_duty is False


async def test_go_on_duty_office_closed_on_day_off(db_session: AsyncSession):
    manager = await _manager(db_session)

    with pytest.raises(OfficeClosed) as exc:
        await duty.go_on_duty(db_session, user=manager, settings=_settings(), now=_at(28, 12))

    assert exc.value.next_open == _at(29, 8)  # понедельник 08:00


async def test_go_on_duty_requires_staff_role(db_session: AsyncSession):
    client = await UserRepository(db_session).create(
        telegram_id=500, role=UserRole.client, status=UserStatus.active
    )
    with pytest.raises(PermissionDenied):
        await duty.go_on_duty(db_session, user=client, settings=_settings(), now=_at(22, 10))


async def test_go_on_duty_denies_owner(db_session: AsyncSession):
    owner = await UserRepository(db_session).create(
        telegram_id=501, role=UserRole.owner, status=UserStatus.active
    )

    with pytest.raises(PermissionDenied):
        await duty.go_on_duty(db_session, user=owner, settings=_settings(), now=_at(22, 10))


async def test_current_duty_managers_orders_latest_first(db_session: AsyncSession):
    repo = UserRepository(db_session)
    early = await _manager(db_session, telegram_id=11)
    late = await _manager(db_session, telegram_id=12)
    stale = await _manager(db_session, telegram_id=13)
    await repo.set_duty(early, on_duty=True, duty_date=_at(22, 8).date(), duty_since=_at(22, 8))
    await repo.set_duty(late, on_duty=True, duty_date=_at(22, 9).date(), duty_since=_at(22, 9))
    # «Зависшая» смена прошлого дня — не должна попасть в текущих дежурных.
    await repo.set_duty(stale, on_duty=True, duty_date=_at(21, 9).date(), duty_since=_at(21, 9))

    on_duty = await duty.current_duty_managers(db_session, settings=_settings(), now=_at(22, 10))

    assert [u.id for u in on_duty] == [late.id, early.id]


async def test_clear_expired_duty_clears_after_close(db_session: AsyncSession):
    repo = UserRepository(db_session)
    manager = await _manager(db_session)
    await repo.set_duty(manager, on_duty=True, duty_date=_at(22, 8).date(), duty_since=_at(22, 8))

    cleared = await duty.clear_expired_duty(db_session, settings=_settings(), now=_at(22, 21))

    assert [u.id for u in cleared] == [manager.id]
    assert manager.on_duty is False
    assert manager.duty_since is None
    assert "duty_ended" in await _audit_actions(db_session)


async def test_clear_expired_duty_keeps_active_shift(db_session: AsyncSession):
    repo = UserRepository(db_session)
    manager = await _manager(db_session)
    await repo.set_duty(manager, on_duty=True, duty_date=_at(22, 8).date(), duty_since=_at(22, 8))

    cleared = await duty.clear_expired_duty(db_session, settings=_settings(), now=_at(22, 10))

    assert cleared == []
    assert manager.on_duty is True


async def test_clear_expired_duty_clears_stale_previous_day(db_session: AsyncSession):
    repo = UserRepository(db_session)
    manager = await _manager(db_session)
    # Смена вчерашняя (duty_date=пятница), сейчас рабочее окно следующего дня.
    await repo.set_duty(manager, on_duty=True, duty_date=_at(19, 9).date(), duty_since=_at(19, 9))

    cleared = await duty.clear_expired_duty(db_session, settings=_settings(), now=_at(22, 10))

    assert [u.id for u in cleared] == [manager.id]
    assert manager.on_duty is False
