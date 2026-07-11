"""Настройки клиента и self-service профиль (Фаза 3)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.client_account import ClientAccount
from app.db.models.enums import UserRole, UserStatus
from app.db.models.user import User
from app.db.repositories import (
    AuditRepository,
    NotificationSettingRepository,
    SenderProfileRepository,
    UserRepository,
)
from app.services.client_sheet_sync import best_effort_sync
from app.services.exceptions import (
    InvalidNotificationSetting,
    PermissionDenied,
    PhoneAlreadyTaken,
)

NOTIFY_APPROVED = "notify_registration_approved"
NOTIFY_SHIPMENT_STATUS = "notify_shipment_status"
NOTIFY_LOW_STOCK = "notify_low_stock"
NOTIFY_ALL_ACCOUNT_SHIPMENTS = "notify_all_account_shipments"

DEFAULT_NOTIFICATION_SETTINGS = {
    NOTIFY_APPROVED: True,
    NOTIFY_SHIPMENT_STATUS: True,
    NOTIFY_LOW_STOCK: True,
    NOTIFY_ALL_ACCOUNT_SHIPMENTS: False,
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


def _settings_view(
    *,
    full_name: str | None,
    phone: str | None,
    notification_payload: dict[str, bool],
    sender_profiles_count: int,
    default_sender_name: str | None,
) -> ClientSettingsView:
    labels = {
        NOTIFY_APPROVED: "Підтвердження реєстрації",
        NOTIFY_SHIPMENT_STATUS: "Статуси відправлень",
        NOTIFY_LOW_STOCK: "Залишки та low-stock",
        NOTIFY_ALL_ACCOUNT_SHIPMENTS: "Усі ТТН мого акаунта",
    }
    notifications = [
        NotificationSettingView(
            key=key,
            label=labels[key],
            enabled=bool(notification_payload[key]),
        )
        for key in DEFAULT_NOTIFICATION_SETTINGS
    ]
    return ClientSettingsView(
        full_name=full_name,
        phone=phone,
        notifications=notifications,
        sender_profiles_count=sender_profiles_count,
        default_sender_name=default_sender_name,
    )


async def _notification_payload(session: AsyncSession, user: User) -> dict[str, bool]:
    # Backward-compat: если тумблер ещё не переехал в `notification_settings`,
    # читаем legacy-значение из `users.permissions`.
    payload = {
        key: bool((user.permissions or {}).get(key, default))
        for key, default in DEFAULT_NOTIFICATION_SETTINGS.items()
    }
    repo = NotificationSettingRepository(session)
    for row in await repo.list_for_user(user.id):
        if row.key in DEFAULT_NOTIFICATION_SETTINGS:
            payload[row.key] = row.enabled
    return payload


async def get_client_settings(
    session: AsyncSession,
    *,
    client: User,
    account_id=None,
) -> ClientSettingsView:
    _require_active_client(client)
    profiles = await SenderProfileRepository(session).list_for_client(
        client.id, account_id=account_id
    )
    default = next((profile for profile in profiles if profile.is_default), None)
    notification_payload = await _notification_payload(session, client)
    return _settings_view(
        full_name=client.full_name,
        phone=client.phone,
        notification_payload=notification_payload,
        sender_profiles_count=len(profiles),
        default_sender_name=default.name if default else None,
    )


async def toggle_notification(
    session: AsyncSession, *, client: User, key: str, account_id=None
) -> ClientSettingsView:
    _require_active_client(client)
    if key not in DEFAULT_NOTIFICATION_SETTINGS:
        raise InvalidNotificationSetting(key)
    payload = await _notification_payload(session, client)
    enabled = not bool(payload[key])
    await NotificationSettingRepository(session).set_enabled(
        user_id=client.id,
        key=key,
        enabled=enabled,
    )
    await AuditRepository(session).log(
        "client_notification_toggled",
        user_id=client.id,
        affected_entity=f"user:{client.id}",
        after={key: enabled},
    )
    return await get_client_settings(session, client=client, account_id=account_id)


async def update_self_profile(
    session: AsyncSession,
    *,
    client: User,
    full_name: str | None = None,
    phone: str | None = None,
    account_id=None,
    account: ClientAccount | None = None,
) -> ClientSettingsView:
    _require_active_client(client)
    repo = UserRepository(session)
    before = {"full_name": client.full_name, "phone": client.phone}
    changed = False
    previous_sheet_key = client.stock_sheet_key
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
        if full_name is not None:
            await best_effort_sync(
                session,
                client=client,
                account=account,
                log_key="client_self_profile_sheet_sync_failed",
                previous_sheet_key=previous_sheet_key,
                user_id=str(client.id),
            )
    return await get_client_settings(session, client=client, account_id=account_id)
