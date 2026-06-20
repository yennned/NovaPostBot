"""Тесты настроек клиента и self-service профиля."""

from __future__ import annotations

import pytest
from app.db.models.enums import UserRole, UserStatus
from app.db.repositories import NotificationSettingRepository, UserRepository
from app.services import client_settings, sender_profile
from app.services.exceptions import PhoneAlreadyTaken
from sqlalchemy.ext.asyncio import AsyncSession


async def _active_client(session: AsyncSession, telegram_id: int, phone: str | None = None):
    return await UserRepository(session).create(
        telegram_id=telegram_id,
        phone=phone,
        full_name=f"Client {telegram_id}",
        role=UserRole.client,
        status=UserStatus.active,
    )


async def test_get_client_settings_counts_profiles_and_toggles_notifications(
    db_session: AsyncSession,
):
    client = await _active_client(db_session, 810, "+380810")
    await sender_profile.create_profile(
        db_session,
        actor=client,
        client_id=client.id,
        name="ФОП-1",
        np_api_key="np-key-1",
        sender_phone="+380501112233",
    )
    await sender_profile.create_profile(
        db_session,
        actor=client,
        client_id=client.id,
        name="ФОП-2",
        np_api_key="np-key-2",
        sender_phone="+380501112233",
    )

    view = await client_settings.get_client_settings(db_session, client=client)

    assert view.sender_profiles_count == 2
    assert view.default_sender_name == "ФОП-1"
    assert any(item.enabled for item in view.notifications)

    toggled = await client_settings.toggle_notification(
        db_session,
        client=client,
        key=client_settings.NOTIFY_LOW_STOCK,
    )

    low_stock = next(
        item for item in toggled.notifications if item.key == client_settings.NOTIFY_LOW_STOCK
    )
    assert low_stock.enabled is False
    row = await NotificationSettingRepository(db_session).get_by_user_and_key(
        client.id,
        client_settings.NOTIFY_LOW_STOCK,
    )
    assert row is not None and row.enabled is False


async def test_get_client_settings_uses_legacy_permissions_as_fallback(db_session: AsyncSession):
    client = await UserRepository(db_session).create(
        telegram_id=813,
        phone="+380813",
        full_name="Client 813",
        role=UserRole.client,
        status=UserStatus.active,
        permissions={client_settings.NOTIFY_SHIPMENT_STATUS: False},
    )

    view = await client_settings.get_client_settings(db_session, client=client)

    shipment_toggle = next(
        item for item in view.notifications if item.key == client_settings.NOTIFY_SHIPMENT_STATUS
    )
    assert shipment_toggle.enabled is False


async def test_update_self_profile_changes_fields_and_checks_unique(db_session: AsyncSession):
    client = await _active_client(db_session, 811, "+380811")
    await _active_client(db_session, 812, "+380812")

    updated = await client_settings.update_self_profile(
        db_session,
        client=client,
        full_name="Нове Ім'я",
        phone="+3808111",
    )

    assert updated.full_name == "Нове Ім'я"
    assert updated.phone == "+3808111"

    with pytest.raises(PhoneAlreadyTaken):
        await client_settings.update_self_profile(
            db_session,
            client=client,
            phone="+380812",
        )
