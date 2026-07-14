"""RBAC-ядро: иерархия ролей, per-flag права менеджера, dev god-mode.

Чистая логика без зависимостей от aiogram/БД — чтобы переиспользовать в будущем
WebApp и легко тестировать. Источник правды по ролям — `users.role`; dev-allowlist
(`DEV_TELEGRAM_IDS`) проверяется **первым** и обходит обычные правила
([docs/03-roles-permissions.md](../../docs/03-roles-permissions.md)).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.bot.types import ClientAccountContext, EffectiveContext
from app.config import Settings, get_settings
from app.db.models.enums import (
    ClientAccountStatus,
    MembershipRole,
    MembershipStatus,
    UserRole,
    UserStatus,
)
from app.db.models.user import User
from app.services.exceptions import PermissionDenied

# Числовой ранг роли (client < manager < owner) — для строгого сравнения «сверху вниз».
_ROLE_RANK: dict[UserRole, int] = {
    UserRole.client: 0,
    UserRole.manager: 1,
    UserRole.owner: 2,
}

# Канонические ключи per-flag прав менеджера (хранятся в `users.permissions`).
# Единый источник правды — на них ссылаются сервисы (`services/clients`,
# `services/staff`) и экран «👔 Персонал» (рендер тоглов из `PERMISSION_FLAGS`).
CAN_MANAGE_CLIENTS = "can_manage_clients"
CAN_HANDLE_SUPPORT = "can_handle_support"
CAN_VIEW_REPORTS = "can_view_reports"


@dataclass(frozen=True, slots=True)
class PermissionFlag:
    """Описание гранулярного права для UI управления персоналом."""

    key: str
    label: str  # uk-метка на экране «Персонал»
    description: str  # короткое пояснение, что именно разрешает


# Порядок = порядок отображения тоглов в карточке менеджера.
PERMISSION_FLAGS: tuple[PermissionFlag, ...] = (
    PermissionFlag(
        CAN_MANAGE_CLIENTS,
        "Керування клієнтами",
        "Підтвердження та блокування клієнтів",
    ),
    PermissionFlag(
        CAN_HANDLE_SUPPORT,
        "Підтримка й чергування",
        "Чергування та відповіді клієнтам у підтримці",
    ),
    PermissionFlag(
        CAN_VIEW_REPORTS,
        "Звіти",
        "Перегляд звітів по відправленнях",
    ),
)


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


def require_staff(actor: User, settings: Settings | None = None) -> None:
    """Гейт чтения staff-экранов: активный менеджер+ или dev, иначе `PermissionDenied`."""
    if is_dev(actor.telegram_id, settings):
        return
    if actor.status is not UserStatus.active:
        raise PermissionDenied("обліковий запис неактивний")
    if not role_at_least(actor.role, UserRole.manager):
        raise PermissionDenied("потрібна роль менеджера або вище")


def require_can_manage(
    actor: User, target: User, flag: str, settings: Settings | None = None
) -> None:
    """Гейт мутации клиента: актёр активен + иерархия `can_manage` + per-flag право.

    Статус актёра проверяем здесь, т.к. `/start` гейтит вход, но reply-клавиатуры в
    Telegram сохраняются — заблокированный/архивный менеджер не должен управлять
    клиентами по «залипшим» кнопкам (dev обходит проверку).
    """
    if not is_dev(actor.telegram_id, settings) and actor.status is not UserStatus.active:
        raise PermissionDenied("обліковий запис неактивний")
    if not can_manage(actor, target, settings):
        raise PermissionDenied("немає прав керувати цим користувачем")
    if not has_permission(actor, flag, settings):
        raise PermissionDenied(f"право {flag} відкликано")


def require_owner(actor: User, settings: Settings | None = None) -> None:
    """Гейт действий уровня владельца (напр. правка профиля клиента).

    Редактирование данных клиента — только владелец (per-flag убран, менеджерам
    недоступно). dev обходит проверку; иначе актёр должен быть активным владельцем.
    """
    if is_dev(actor.telegram_id, settings):
        return
    if actor.status is not UserStatus.active:
        raise PermissionDenied("обліковий запис неактивний")
    if actor.role is not UserRole.owner:
        raise PermissionDenied("потрібна роль власника")


def require_account_member(context: EffectiveContext | ClientAccountContext) -> None:
    """Перевірити активність користувача, акаунта та membership."""
    if isinstance(context, EffectiveContext) and context.is_dev:
        return
    account_context = context.account_context if isinstance(context, EffectiveContext) else context
    if account_context is None:
        raise PermissionDenied("не вибрано клієнтський акаунт")
    if account_context.user.status is not UserStatus.active:
        raise PermissionDenied("обліковий запис неактивний")
    if account_context.account.status is not ClientAccountStatus.active:
        raise PermissionDenied("клієнтський акаунт заблоковано")
    if account_context.membership.status is not MembershipStatus.active:
        raise PermissionDenied("членство в акаунті неактивне")


def require_account_owner(context: EffectiveContext | ClientAccountContext) -> None:
    """Доступ до команди, ФОП, реквізитів і ключів НП."""
    require_account_member(context)
    account_context = context.account_context if isinstance(context, EffectiveContext) else context
    if (
        account_context is None
        or account_context.membership.role is not MembershipRole.account_owner
    ):
        raise PermissionDenied("потрібна роль головного клієнта")
