"""RBAC-ядро: иерархия ролей, per-flag права менеджера, dev god-mode.

Чистая логика без зависимостей от aiogram/БД — чтобы переиспользовать в будущем
WebApp и легко тестировать. Источник правды по ролям — `users.role`; dev-allowlist
(`DEV_TELEGRAM_IDS`) проверяется **первым** и обходит обычные правила
([docs/03-roles-permissions.md](../../docs/03-roles-permissions.md)).
"""

from __future__ import annotations

from app.config import Settings, get_settings
from app.db.models.enums import UserRole
from app.db.models.user import User

# Числовой ранг роли (client < manager < owner) — для строгого сравнения «сверху вниз».
_ROLE_RANK: dict[UserRole, int] = {
    UserRole.client: 0,
    UserRole.manager: 1,
    UserRole.owner: 2,
}


def _settings(settings: Settings | None) -> Settings:
    return settings or get_settings()


def is_dev(telegram_id: int, settings: Settings | None = None) -> bool:
    """Входит ли Telegram-ID в dev-allowlist (`DEV_TELEGRAM_IDS`)."""
    return telegram_id in _settings(settings).dev_telegram_ids


def is_configured_owner(telegram_id: int, settings: Settings | None = None) -> bool:
    """Объявлен ли Telegram-ID владельцем в конфиге (`OWNER_TELEGRAM_IDS`)."""
    return telegram_id in _settings(settings).owner_telegram_ids


def role_rank(role: UserRole) -> int:
    return _ROLE_RANK[role]


def role_at_least(role: UserRole, minimum: UserRole) -> bool:
    """Роль `role` не ниже `minimum` по иерархии."""
    return role_rank(role) >= role_rank(minimum)


def can_manage(actor: User, target: User, settings: Settings | None = None) -> bool:
    """Может ли `actor` управлять `target` (подтверждать/блокировать/менять роль).

    Правило: dev — может всеми; иначе роль actor **строго выше** роли target
    (менеджеры друг другом не управляют, владелец — менеджерами и клиентами).
    Управлять собой нельзя.
    """
    if is_dev(actor.telegram_id, settings):
        return True
    if actor.id == target.id:
        return False
    return role_rank(actor.role) > role_rank(target.role)


def has_permission(user: User, flag: str, settings: Settings | None = None) -> bool:
    """Есть ли у пользователя гранулярное право `flag`.

    dev и owner — всё разрешено. Менеджер: флаг включён по умолчанию, владелец/dev
    могут отозвать (`permissions[flag] = false`). Клиент — менеджерские флаги не
    применимы → запрещено.
    """
    if is_dev(user.telegram_id, settings):
        return True
    if user.role is UserRole.owner:
        return True
    if user.role is UserRole.manager:
        return bool(user.permissions.get(flag, True))
    return False
