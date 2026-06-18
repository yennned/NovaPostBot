"""Украинские тексты раздела «Клієнти» (Фаза 2)."""

from __future__ import annotations

from app.db.models.enums import UserStatus
from app.services.clients import ClientCard, ClientListItem
from app.services.exceptions import (
    AlreadyInStatus,
    ClientNotFound,
    ClientServiceError,
    PermissionDenied,
    TransitionForbidden,
)

STATUS_LABELS: dict[UserStatus, str] = {
    UserStatus.pending: "Очікують",
    UserStatus.active: "Активні",
    UserStatus.blocked: "Заблоковані",
    UserStatus.archived: "Архів",
}

STATUS_BADGE: dict[UserStatus, str] = {
    UserStatus.pending: "🕓",
    UserStatus.active: "✅",
    UserStatus.blocked: "⛔",
    UserStatus.archived: "🗄",
}


def clients_header(total: int) -> str:
    return f"👥 <b>Клієнти</b> — знайдено: {total}"


def client_list_button(item: ClientListItem) -> str:
    name = item.full_name or "без імені"
    return f"{STATUS_BADGE[item.status]} {name} · {item.phone or '—'}"


def empty_list_text() -> str:
    return "Порожньо. Спробуйте інший фільтр або пошук."


def client_card_text(card: ClientCard) -> str:
    return (
        f"{STATUS_BADGE[card.status]} <b>{card.full_name or 'без імені'}</b>\n"
        f"Телефон: {card.phone or '—'}\n"
        f"Telegram ID: <code>{card.telegram_id}</code>\n"
        f"Статус: {STATUS_LABELS[card.status]}\n"
        f"ФОП: {card.sender_profiles_count}"
        + (f" (дефолт: {card.default_sender_name})" if card.default_sender_name else "")
        + f"\nЗареєстровано: {card.created_at:%Y-%m-%d %H:%M}"
    )


def search_prompt_text() -> str:
    return "Введіть ПІБ або телефон для пошуку:"


def edit_prompt_text(field_label: str) -> str:
    return f"Введіть нове значення «{field_label}»:"


def profile_updated_text() -> str:
    return "✅ Дані клієнта оновлено."


def action_done_text(card: ClientCard) -> str:
    return f"✅ Готово. Новий статус: {STATUS_LABELS[card.status]}."


def client_error_text(exc: ClientServiceError) -> str:
    """uk-сообщение для доменной ошибки сервиса клиентов."""
    if isinstance(exc, ClientNotFound):
        return "Клієнта не знайдено."
    if isinstance(exc, AlreadyInStatus):
        return "Клієнт уже в цьому статусі."
    if isinstance(exc, TransitionForbidden):
        return "Цю дію не можна виконати з поточного статусу."
    if isinstance(exc, PermissionDenied):
        return "Недостатньо прав для цієї дії."
    return "Не вдалося виконати дію."
