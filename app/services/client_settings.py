"""Настройки клиента и self-service профиль (Фаза 3)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.enums import UserRole, UserStatus
from app.db.models.user import User
from app.db.repositories import (
    AuditRepository,
    SenderProfileRepository,
    UserRepository,
)
from app.services.exceptions import (
    InvalidNotificationSetting,
    PermissionDenied,
    PhoneAlreadyTaken,
)

NOTIFY_APPROVED = "notify_registration_approved"
NOTIFY_SHIPMENT_STATUS = "notify_shipment_status"
NOTIFY_LOW_STOCK = "notify_low_stock"

DEFAULT_NOTIFICATION_SETTINGS = {
    NOTIFY_APPROVED: True,
    NOTIFY_SHIPMENT_STATUS: True,
    NOTIFY_LOW_STOCK: True,
}


@dataclass(frozen=True, slots=True)
class NotificationSettingView:
    key: str
    label: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class ClientSettingsView:
    full_name: str | None
    phone: str | None
    notifications: list[NotificationSettingView]
    sender_profiles_count: int
    default_sender_name: str | None


def _require_active_client(client: User) -> None:
    if client.role is not UserRole.client:
        raise PermissionDenied("налаштування доступні тільки клієнту")
    if client.status is not UserStatus.active:
        raise PermissionDenied("налаштування доступні після підтвердження")


def _settings_payload(user: User) -> dict:
    payload = dict(user.permissions or {})
    for key, value in DEFAULT_NOTIFICATION_SETTINGS.items():
        payload.setdefault(key, value)
    return payload


def _settings_view(
    user: User,
    *,
    sender_profiles_count: int,
    default_sender_name: str | None,
) -> ClientSettingsView:
    payload = _settings_payload(user)
    labels = {
        NOTIFY_APPROVED: "Підтвердження реєстрації",
        NOTIFY_SHIPMENT_STATUS: "Статуси відправлень",
        NOTIFY_LOW_STOCK: "Залишки та low-stock",
    }
    notifications = [
        NotificationSettingView(key=key, label=labels[key], enabled=bool(payload[key]))
        for key in DEFAULT_NOTIFICATION_SETTINGS
    ]
    return ClientSettingsView(
        full_name=user.full_name,
        phone=user.phone,
        notifications=notifications,
        sender_profiles_count=sender_profiles_count,
        default_sender_name=default_sender_name,
    )


async def get_client_settings(session: AsyncSession, *, client: User) -> ClientSettingsView:
    _require_active_client(client)
    profiles = await SenderProfileRepository(session).list_for_client(client.id)
    default = next((profile for profile in profiles if profile.is_default), None)
    return _settings_view(
        client,
        sender_profiles_count=len(profiles),
        default_sender_name=default.name if default else None,
    )


async def toggle_notification(
    session: AsyncSession, *, client: User, key: str
) -> ClientSettingsView:
    _require_active_client(client)
    if key not in DEFAULT_NOTIFICATION_SETTINGS:
        raise InvalidNotificationSetting(key)
    payload = _settings_payload(client)
    payload[key] = not bool(payload[key])
    await UserRepository(session).set_permissions(client, payload)
    await AuditRepository(session).log(
        "client_notification_toggled",
        user_id=client.id,
        affected_entity=f"user:{client.id}",
        after={key: payload[key]},
    )
    return await get_client_settings(session, client=client)


async def update_self_profile(
    session: AsyncSession,
    *,
    client: User,
    full_name: str | None = None,
    phone: str | None = None,
) -> ClientSettingsView:
    _require_active_client(client)
    repo = UserRepository(session)
    before = {"full_name": client.full_name, "phone": client.phone}
    changed = False
    if full_name is not None and full_name != client.full_name:
        client.full_name = full_name
        changed = True
    if phone is not None and phone != client.phone:
        clash = await repo.get_by_phone(phone)
        if clash is not None and clash.id != client.id:
            raise PhoneAlreadyTaken(phone)
        client.phone = phone
        changed = True
    if changed:
        await session.flush()
        await AuditRepository(session).log(
            "client_self_profile_updated",
            user_id=client.id,
            affected_entity=f"user:{client.id}",
            before=before,
            after={"full_name": client.full_name, "phone": client.phone},
        )
    return await get_client_settings(session, client=client)
