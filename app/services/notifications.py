"""Пуш-уведомления и маршрутизация получателей Phase 5."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from decimal import Decimal
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models.enums import ShipmentStatus, UserRole, UserStatus
from app.db.models.shipment import Shipment
from app.db.models.user import User
from app.db.repositories import NotificationSettingRepository, UserRepository
from app.services.client_settings import (
    DEFAULT_NOTIFICATION_SETTINGS,
    NOTIFY_LOW_STOCK,
    NOTIFY_SHIPMENT_STATUS,
)
from app.services.inventory import InventoryItem


class Notifier(Protocol):
    """Транспорт отправки. Бот-слой реализует поверх aiogram `Bot.send_message`."""

    async def send_message(self, telegram_id: int, text: str) -> None: ...


def _client_label(client: User) -> str:
    name = client.full_name or "без імені"
    phone = client.phone or "—"
    return f"{name} ({phone})"


def _money(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return f"{value:.2f}"


def duty_shift_ended_text() -> str:
    """Пуш менеджеру при авто-снятии дежурства (закрытие отделения)."""
    return (
        "🔘 Зміну завершено — відділення зачинилося, ви більше не на звʼязку. "
        "Завтра відкрийте зміну кнопкою «🟢 Я на звʼязку»."
    )


def new_client_text(client: User) -> str:
    return (
        "🆕 <b>Нова заявка на реєстрацію</b>\n"
        f"Клієнт: {_client_label(client)}\n"
        "Підтвердьте або заблокуйте у розділі «Клієнти»."
    )


def client_approved_text() -> str:
    return (
        "✅ <b>Вашу заявку підтверджено!</b>\n"
        "Тепер вам доступний особистий кабінет. Натисніть /start, щоб почати."
    )


def new_shipment_text(client: User, ttn_number: str | None) -> str:
    ttn = ttn_number or "—"
    return (
        "📦 <b>Нова ТТН від клієнта</b>\n"
        f"Клієнт: {_client_label(client)}\n"
        f"№ ТТН: <code>{ttn}</code>\n"
        "Дивіться у розділі «Відправлення» → «Створені»."
    )


def shipment_status_text(shipment: Shipment) -> str:
    ttn = shipment.ttn_number or "—"
    labels = {
        ShipmentStatus.created: "створено",
        ShipmentStatus.confirmed: "підтверджено менеджером",
        ShipmentStatus.dispatched: "відправлено",
        ShipmentStatus.in_transit: "у дорозі",
        ShipmentStatus.arrived: "прибуло у відділення",
        ShipmentStatus.delivered: "вручено",
        ShipmentStatus.returning: "посилка повертається",
        ShipmentStatus.returned: "повернення прийнято на склад",
        ShipmentStatus.lost: "посилку втрачено",
        ShipmentStatus.damaged: "посилку пошкоджено",
        ShipmentStatus.cancelled: "скасовано",
    }
    lines = [
        "📬 <b>Оновлення статусу відправлення</b>",
        f"№ ТТН: <code>{ttn}</code>",
        f"Статус: <b>{labels.get(shipment.status, shipment.status.value)}</b>",
    ]
    if shipment.status is ShipmentStatus.dispatched and shipment.sla_met is not None:
        lines.append("SLA: " + ("вчасно" if shipment.sla_met else "прострочено"))
    return "\n".join(lines)


def shipment_cancelled_text(client: User, shipment: Shipment) -> str:
    return (
        "❌ <b>Клієнт скасував ТТН</b>\n"
        f"Клієнт: {_client_label(client)}\n"
        f"№ ТТН: <code>{shipment.ttn_number or '—'}</code>"
    )


def low_stock_text(client: User, items: list[InventoryItem]) -> str:
    lines = [
        "📦 <b>Низький залишок</b>",
        f"Клієнт: {_client_label(client)}",
    ]
    for item in items[:10]:
        lines.append(
            f"• <b>{item.sku}</b> — {item.name}: доступно {item.available}, резерв {item.reserved}"
        )
    return "\n".join(lines)


def client_low_stock_text(items: list[InventoryItem]) -> str:
    lines = ["📦 <b>Увага: низький залишок</b>"]
    for item in items[:10]:
        line = (
            f"• <b>{item.sku}</b> — {item.name}: "
            f"доступно {item.available} · ціна {_money(item.price)}"
        )
        lines.append(line)
    return "\n".join(lines)


def nonstandard_shipment_text(shipment: Shipment, *, note: str | None = None) -> str:
    lines = [
        "⚠️ <b>Нестандартна ситуація по ТТН</b>",
        f"№ ТТН: <code>{shipment.ttn_number or '—'}</code>",
        f"Статус: <b>{shipment.status.value}</b>",
    ]
    if note:
        lines.append(note)
    lines.append("За потреби звʼяжіться з менеджером.")
    return "\n".join(lines)


async def _staff_recipient_ids(
    session: AsyncSession,
    *,
    settings: Settings | None = None,
) -> set[int]:
    users = UserRepository(session)
    current_settings = settings or get_settings()
    recipient_ids: set[int] = set(current_settings.owner_telegram_ids)
    for owner in await users.list_by_role(UserRole.owner):
        if owner.status is UserStatus.active:
            recipient_ids.add(owner.telegram_id)
    for manager in await users.list_by_role(UserRole.manager):
        if manager.status is UserStatus.active and manager.on_duty:
            recipient_ids.add(manager.telegram_id)
    return recipient_ids


async def _manager_recipient_ids(session: AsyncSession) -> set[int]:
    users = UserRepository(session)
    recipient_ids: set[int] = set()
    for manager in await users.list_by_role(UserRole.manager):
        if manager.status is UserStatus.active and manager.on_duty:
            recipient_ids.add(manager.telegram_id)
    return recipient_ids


async def _notification_enabled(
    session: AsyncSession,
    *,
    user: User,
    key: str,
) -> bool:
    default = bool(DEFAULT_NOTIFICATION_SETTINGS.get(key, True))
    enabled = bool((user.permissions or {}).get(key, default))
    row = await NotificationSettingRepository(session).get_by_user_and_key(user.id, key)
    if row is not None:
        enabled = row.enabled
    return enabled


async def _send_many(notifier: Notifier, recipient_ids: Iterable[int], text: str) -> None:
    unique_ids = list(dict.fromkeys(recipient_ids))
    await asyncio.gather(*(notifier.send_message(tid, text) for tid in unique_ids))


async def notify_new_client_registered(
    session: AsyncSession, notifier: Notifier, *, client: User
) -> None:
    await _send_many(notifier, await _staff_recipient_ids(session), new_client_text(client))


async def notify_shipment_created(
    session: AsyncSession, notifier: Notifier, *, client: User, ttn_number: str | None
) -> None:
    await _send_many(
        notifier, await _staff_recipient_ids(session), new_shipment_text(client, ttn_number)
    )


async def notify_client_approved(notifier: Notifier, *, client: User) -> None:
    await notifier.send_message(client.telegram_id, client_approved_text())


async def notify_shipment_status_changed(
    session: AsyncSession,
    notifier: Notifier,
    *,
    client: User,
    shipment: Shipment,
) -> None:
    if await _notification_enabled(session, user=client, key=NOTIFY_SHIPMENT_STATUS):
        await notifier.send_message(client.telegram_id, shipment_status_text(shipment))


async def notify_low_stock(
    session: AsyncSession,
    notifier: Notifier,
    *,
    client: User,
    items: list[InventoryItem],
) -> None:
    if not items:
        return
    if await _notification_enabled(session, user=client, key=NOTIFY_LOW_STOCK):
        await notifier.send_message(client.telegram_id, client_low_stock_text(items))
    await _send_many(notifier, await _staff_recipient_ids(session), low_stock_text(client, items))


async def notify_shipment_cancelled_by_client(
    session: AsyncSession,
    notifier: Notifier,
    *,
    client: User,
    shipment: Shipment,
) -> None:
    await _send_many(
        notifier, await _staff_recipient_ids(session), shipment_cancelled_text(client, shipment)
    )


async def notify_nonstandard_shipment(
    session: AsyncSession,
    notifier: Notifier,
    *,
    client: User,
    shipment: Shipment,
    note: str | None = None,
) -> None:
    text = nonstandard_shipment_text(shipment, note=note)
    recipients = [client.telegram_id, *(await _manager_recipient_ids(session))]
    await _send_many(notifier, recipients, text)
