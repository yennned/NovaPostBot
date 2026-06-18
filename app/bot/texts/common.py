"""Текстовые шаблоны для Phase 1."""

from __future__ import annotations

from app.db.models.enums import UserRole
from app.db.models.user import User

ROLE_LABELS = {
    UserRole.client: "клієнта",
    UserRole.manager: "менеджера",
    UserRole.owner: "власника",
}


def ask_contact_text() -> str:
    return (
        "Щоб увійти до кабінету, надішліть свій номер телефону кнопкою нижче. "
        "Доступ надається після перевірки менеджером або власником."
    )


def contact_mismatch_text() -> str:
    return "Потрібно надіслати саме свій контакт через кнопку Telegram."


def pending_text(user: User) -> str:
    return (
        f"{user.full_name}, ваш запит уже створено. "
        "Статус: очікує підтвердження менеджером або власником."
    )


def registered_pending_text(user: User) -> str:
    return (
        f"Дякуємо, {user.full_name}. Заявку створено, щойно її підтвердять — "
        "доступ до кабінету відкриється."
    )


def blocked_text() -> str:
    return "Ваш доступ зараз заблоковано. Для уточнення зверніться до менеджера."


def welcome_text(user: User, role: UserRole) -> str:
    return f"Вітаю, {user.full_name}. Відкриваю меню {ROLE_LABELS[role]}."


def dev_help_text() -> str:
    return (
        "Ви в dev god-mode. Для перегляду конкретного сценарію скористайтесь "
        "`/as client`, `/as manager`, `/as owner`, `/as_user <id|телефон>`, "
        "`/as off`, `/kill_switch`, `/kill_switch confirm`, `/kill_switch cancel`."
    )


def dev_mode_banner(role: UserRole | None, impersonated: bool = False) -> str:
    if role is None:
        return "🧪 dev god-mode"
    suffix = " · impersonation" if impersonated else ""
    return f"🧪 as {role.value}{suffix}"
