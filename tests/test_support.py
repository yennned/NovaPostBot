"""Тесты сервиса поддержки (`app/services/support.py`) — на Postgres + чистые."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from app.config import Settings
from app.db.models.enums import SupportThreadStatus, UserRole, UserStatus
from app.db.repositories import SupportRepository, UserRepository
from app.services import notifications, support
from app.services.exceptions import PermissionDenied
from sqlalchemy.ext.asyncio import AsyncSession

TZ = ZoneInfo("Europe/Kyiv")
_WEEKDAYS = json.dumps({str(day): ["08:00", "20:00"] for day in range(5)})


def _settings() -> Settings:
    s = Settings(_env_file=None)
    s.work_schedule_raw = _WEEKDAYS
    return s


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 6, day, hour, minute, tzinfo=TZ)


async def _client(session: AsyncSession, telegram_id: int = 100, status=UserStatus.active):
    return await UserRepository(session).create(
        telegram_id=telegram_id,
        phone=f"+38050{telegram_id}",
        full_name=f"Клієнт {telegram_id}",
        role=UserRole.client,
        status=status,
    )


async def _duty_manager(session: AsyncSession, telegram_id: int = 10):
    manager = await UserRepository(session).create(
        telegram_id=telegram_id, role=UserRole.manager, status=UserStatus.active
    )
    await UserRepository(session).set_duty(
        manager, on_duty=True, duty_date=_at(22, 8).date(), duty_since=_at(22, 8)
    )
    return manager


async def test_open_routes_to_duty_manager(db_session: AsyncSession):
    client = await _client(db_session)
    manager = await _duty_manager(db_session)

    result = await support.open_or_get_thread(
        db_session, client=client, settings=_settings(), now=_at(22, 10)
    )

    assert result.created and result.routed
    assert result.notify_owner is False
    assert result.thread.status is SupportThreadStatus.open
    assert result.thread.assigned_manager_id == manager.id


async def test_open_queues_without_duty_notifies_owner(db_session: AsyncSession):
    client = await _client(db_session)

    result = await support.open_or_get_thread(
        db_session, client=client, settings=_settings(), now=_at(22, 10)
    )

    assert result.created and not result.routed
    assert result.notify_owner is True  # рабочее время без дежурного → владельцу
    assert result.thread.status is SupportThreadStatus.waiting


async def test_open_queues_outside_hours_without_owner_ping(db_session: AsyncSession):
    client = await _client(db_session)

    result = await support.open_or_get_thread(
        db_session, client=client, settings=_settings(), now=_at(22, 21)
    )

    assert result.thread.status is SupportThreadStatus.waiting
    assert result.notify_owner is False


async def test_open_returns_existing_active_thread(db_session: AsyncSession):
    client = await _client(db_session)
    first = await support.open_or_get_thread(
        db_session, client=client, settings=_settings(), now=_at(22, 10)
    )
    second = await support.open_or_get_thread(
        db_session, client=client, settings=_settings(), now=_at(22, 10)
    )

    assert second.created is False
    assert second.thread.id == first.thread.id


async def test_open_rejects_non_active_client(db_session: AsyncSession):
    pending = await _client(db_session, telegram_id=101, status=UserStatus.pending)
    with pytest.raises(PermissionDenied):
        await support.open_or_get_thread(
            db_session, client=pending, settings=_settings(), now=_at(22, 10)
        )


async def test_post_messages_and_close(db_session: AsyncSession):
    client = await _client(db_session)
    manager = await _duty_manager(db_session)
    result = await support.open_or_get_thread(
        db_session, client=client, settings=_settings(), now=_at(22, 10)
    )
    thread = result.thread

    await support.post_message(db_session, thread=thread, sender_role="client", text="Доброго дня")
    await support.post_message(db_session, thread=thread, sender_role="manager", text="Вітаю")
    loaded = await SupportRepository(db_session).get_with_messages(thread.id)
    assert [m.text for m in loaded.messages] == ["Доброго дня", "Вітаю"]
    assert loaded.assigned_manager_id == manager.id

    await support.close_thread(db_session, thread=thread)
    assert thread.status is SupportThreadStatus.closed


async def test_claim_if_waiting_assigns_to_manager(db_session: AsyncSession):
    client = await _client(db_session)
    manager = await UserRepository(db_session).create(
        telegram_id=20, role=UserRole.manager, status=UserStatus.active
    )
    waiting = await SupportRepository(db_session).create_thread(
        client_id=client.id, status=SupportThreadStatus.waiting
    )

    await support.claim_if_waiting(db_session, thread=waiting, manager=manager)

    assert waiting.assigned_manager_id == manager.id
    assert waiting.status is SupportThreadStatus.open


def test_relay_text_escapes_html():
    # Пользовательский текст идёт в parse_mode=HTML — должен экранироваться.
    text = notifications.support_message_for_client_text("<b>hi</b> & <script>")
    assert "&lt;b&gt;hi&lt;/b&gt;" in text
    assert "<script>" not in text
