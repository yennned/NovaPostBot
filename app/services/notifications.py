"""Пуш-уведомления (Фаза 2+) — доменный слой без aiogram.

Решает, КОМУ и ЧТО отправить, и формирует uk-текст; собственно отправку делает
инъектированный `Notifier` (бот-слой реализует его поверх aiogram `Bot`).
Так логику переиспользует и воркер Фазы 5. Тексты живут здесь (backend-owned).
"""

from __future__ import annotations

from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.enums import UserRole, UserStatus
from app.db.models.user import User
from app.db.repositories import UserRepository


class Notifier(Protocol):
    """Транспорт отправки. Бот-слой реализует поверх aiogram `Bot.send_message`."""

    async def send_message(self, telegram_id: int, text: str) -> None: ...


def _client_label(client: User) -> str:
    name = client.full_name or "без імені"
    phone = client.phone or "—"
    return f"{name} ({phone})"


def new_client_text(client: User) -> str:
    """Текст владельцу/менеджеру о новой заявке (uk)."""
    return (
        "🆕 <b>Нова заявка на реєстрацію</b>\n"
        f"Клієнт: {_client_label(client)}\n"
        "Підтвердьте або заблокуйте у розділі «Клієнти»."
    )


def client_approved_text() -> str:
    """Текст клиенту о подтверждении (uk)."""
    return (
        "✅ <b>Вашу заявку підтверджено!</b>\n"
        "Тепер вам доступний особистий кабінет. Натисніть /start, щоб почати."
    )


async def notify_new_client_registered(
    session: AsyncSession, notifier: Notifier, *, client: User
) -> None:
    """Оповестить персонал о новом `pending`-клиенте.

    Получатели: все владельцы + дежурные менеджеры (дедуп по telegram_id).
    Ошибки доставки отдельным получателям не должны валить регистрацию —
    конкретный `Notifier` отвечает за «тихую» обработку сбоев отправки.
    """
    users = UserRepository(session)
    recipients: dict[int, User] = {}
    for owner in await users.list_by_role(UserRole.owner):
        if owner.status is UserStatus.active:
            recipients[owner.telegram_id] = owner
    for manager in await users.list_by_role(UserRole.manager):
        if manager.status is UserStatus.active and manager.on_duty:
            recipients[manager.telegram_id] = manager

    text = new_client_text(client)
    for telegram_id in recipients:
        await notifier.send_message(telegram_id, text)


async def notify_client_approved(notifier: Notifier, *, client: User) -> None:
    """Оповестить клиента о подтверждении заявки."""
    await notifier.send_message(client.telegram_id, client_approved_text())
