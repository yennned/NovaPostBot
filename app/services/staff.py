"""Управление персоналом (Фаза 6) — owner-only, доменная логика без aiogram.

Владелец (и dev) управляет менеджерами: список/карточка, найм по телефону/Telegram-ID,
per-flag права (реестр `permissions.PERMISSION_FLAGS`), блокировка и снятие роли.
Все мутации — через `permissions.can_manage` + запись в `audit_logs`. Паттерн —
как в [services/clients.py](clients.py).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import permissions
from app.config import Settings
from app.db.models.enums import MembershipRole, UserRole, UserStatus
from app.db.models.user import User
from app.db.repositories import (
    AuditRepository,
    ClientAccountRepository,
    SupportRepository,
    UserRepository,
)
from app.services.exceptions import (
    AlreadyInStatus,
    InvalidPermissionFlag,
    PermissionDenied,
    StaffAlreadyManager,
    StaffNotFound,
    StaffPromotionForbidden,
    TransitionForbidden,
)

_FLAG_KEYS = {flag.key for flag in permissions.PERMISSION_FLAGS}


@dataclass(frozen=True, slots=True)
class StaffPermissionState:
    key: str
    label: str
    description: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class StaffListItem:
    id: uuid.UUID
    telegram_id: int | None
    full_name: str | None
    phone: str | None
    status: UserStatus
    on_duty: bool


@dataclass(frozen=True, slots=True)
class StaffPage:
    items: list[StaffListItem]
    total: int
    limit: int
    offset: int
    query: str | None


@dataclass(frozen=True, slots=True)
class StaffCard:
    id: uuid.UUID
    telegram_id: int | None
    full_name: str | None
    phone: str | None
    status: UserStatus
    on_duty: bool
    permissions: list[StaffPermissionState]


@dataclass(frozen=True, slots=True)
class AddManagerResult:
    card: StaffCard
    # telegram_id для пуша приветствия новому менеджеру; None — если заведён по
    # телефону и ещё не входил в бота (пуш отправим при первом входе).
    telegram_id: int | None


def _require_owner(actor: User, settings: Settings | None) -> None:
    if permissions.is_dev(actor.telegram_id, settings):
        return
    if actor.status is not UserStatus.active:
        raise PermissionDenied("учётная запись неактивна")
    if actor.role is not UserRole.owner:
        raise PermissionDenied("керування персоналом доступне лише власнику")


def _permission_states(user: User) -> list[StaffPermissionState]:
    perms = user.permissions or {}
    return [
        StaffPermissionState(
            key=flag.key,
            label=flag.label,
            description=flag.description,
            enabled=bool(perms.get(flag.key, True)),
        )
        for flag in permissions.PERMISSION_FLAGS
    ]


def _card(user: User) -> StaffCard:
    return StaffCard(
        id=user.id,
        telegram_id=user.telegram_id,
        full_name=user.full_name,
        phone=user.phone,
        status=user.status,
        on_duty=user.on_duty,
        permissions=_permission_states(user),
    )


async def _get_manager(users: UserRepository, manager_id: uuid.UUID) -> User:
    user = await users.get_by_id(manager_id)
    if user is None or user.role is not UserRole.manager:
        raise StaffNotFound(str(manager_id))
    return user


def _require_can_manage(actor: User, manager: User, settings: Settings | None) -> None:
    if not permissions.can_manage(actor, manager, settings):
        raise PermissionDenied("нет прав управлять этим пользователем")


# --- Чтение ---------------------------------------------------------------


async def list_staff(
    session: AsyncSession,
    *,
    actor: User,
    query: str | None = None,
    limit: int = 20,
    offset: int = 0,
    settings: Settings | None = None,
) -> StaffPage:
    _require_owner(actor, settings)
    rows, total = await UserRepository(session).list_by_status(
        role=UserRole.manager, status=None, query=query, limit=limit, offset=offset
    )
    items = [
        StaffListItem(
            id=u.id,
            telegram_id=u.telegram_id,
            full_name=u.full_name,
            phone=u.phone,
            status=u.status,
            on_duty=u.on_duty,
        )
        for u in rows
    ]
    return StaffPage(items=items, total=total, limit=limit, offset=offset, query=query)


async def get_staff_card(
    session: AsyncSession,
    *,
    actor: User,
    manager_id: uuid.UUID,
    settings: Settings | None = None,
) -> StaffCard:
    _require_owner(actor, settings)
    return _card(await _get_manager(UserRepository(session), manager_id))


# --- Мутации --------------------------------------------------------------


async def add_manager(
    session: AsyncSession,
    *,
    actor: User,
    telegram_id: int | None = None,
    phone: str | None = None,
    settings: Settings | None = None,
) -> AddManagerResult:
    """Назначить менеджера по Telegram-ID или телефону.

    Существующего владельца/уже-менеджера отклоняем; активного клиента нельзя
    «переназначить» в менеджеры; работника клиентского акаунта — тоже нельзя
    (менеджер платформы и люди со стороны клиента не пересекаются). Если по
    телефону пользователь ещё не найден —
    заводим предзаготовленную запись менеджера (`telegram_id` пуст): при первом
    входе по контакту `register_contact` подхватит её по номеру. Все флаги прав
    включены по умолчанию.
    """
    _require_owner(actor, settings)
    provided = [x for x in (telegram_id, phone) if x is not None]
    if len(provided) != 1:
        raise StaffPromotionForbidden("вкажіть Telegram-ID або телефон")
    users = UserRepository(session)
    if telegram_id is not None:
        existing = await users.get_by_telegram_id(telegram_id)
    else:
        existing = await users.get_by_phone(phone)

    if existing is not None:
        if existing.role is UserRole.manager:
            raise StaffAlreadyManager(str(existing.id))
        if existing.role is UserRole.owner:
            raise StaffPromotionForbidden("не можна змінити роль власника")
        if existing.status is UserStatus.active:
            raise StaffPromotionForbidden("активного клієнта не можна призначити менеджером")
        # Зеркало запрета в `account_team.invite_employee` («номер належить
        # внутрішньому працівнику платформи»): клиент/его работники и менеджер
        # платформы — непересекающиеся множества. Приглашённый работник заведён
        # как `role=client, status=pending`, то есть мимо проверок выше проходит.
        # Владельца акаунта (`account_owner`) НЕ отбиваем: членство автосоздаётся
        # каждому клиенту, а найм `pending`-клиента и есть основной флоу найма.
        membership = await ClientAccountRepository(session).get_membership(user_id=existing.id)
        if membership is not None and membership.role is MembershipRole.employee:
            raise StaffPromotionForbidden("номер належить працівнику клієнта")
        await users.update_role(existing, UserRole.manager)
        await users.update_status(existing, UserStatus.active)
        await users.set_permissions(existing, {})
        manager, action = existing, "manager_promoted"
    else:
        # По телефону — предзаготовка (telegram_id пуст, подхват при входе);
        # по Telegram-ID — обычное создание на лету.
        manager = await users.create(
            telegram_id=telegram_id,
            phone=phone,
            role=UserRole.manager,
            status=UserStatus.active,
        )
        action = "manager_added"

    await AuditRepository(session).log(
        action,
        user_id=actor.id,
        affected_entity=f"user:{manager.id}",
        after={"role": UserRole.manager},
    )
    return AddManagerResult(card=_card(manager), telegram_id=manager.telegram_id)


async def set_permission(
    session: AsyncSession,
    *,
    actor: User,
    manager_id: uuid.UUID,
    flag: str,
    enabled: bool,
    settings: Settings | None = None,
) -> StaffCard:
    _require_owner(actor, settings)
    if flag not in _FLAG_KEYS:
        raise InvalidPermissionFlag(flag)
    users = UserRepository(session)
    manager = await _get_manager(users, manager_id)
    _require_can_manage(actor, manager, settings)
    perms = dict(manager.permissions or {})
    before = bool(perms.get(flag, True))
    perms[flag] = enabled
    await users.set_permissions(manager, perms)
    await AuditRepository(session).log(
        "permission_changed",
        user_id=actor.id,
        affected_entity=f"user:{manager.id}",
        before={flag: before},
        after={flag: enabled},
    )
    return _card(manager)


async def _transition_status(
    session: AsyncSession,
    *,
    actor: User,
    manager_id: uuid.UUID,
    to: UserStatus,
    allowed_from: set[UserStatus],
    action: str,
    settings: Settings | None,
) -> StaffCard:
    _require_owner(actor, settings)
    users = UserRepository(session)
    manager = await _get_manager(users, manager_id)
    _require_can_manage(actor, manager, settings)
    if manager.status is to:
        raise AlreadyInStatus(to)
    if manager.status not in allowed_from:
        raise TransitionForbidden(manager.status, to)

    before = {"status": manager.status}
    await users.update_status(manager, to)
    if to is UserStatus.blocked:
        await users.set_duty(manager, on_duty=False, duty_date=manager.duty_date, duty_since=None)
        await SupportRepository(session).unassign_open_for_manager(manager.id)
    await AuditRepository(session).log(
        action,
        user_id=actor.id,
        affected_entity=f"user:{manager.id}",
        before=before,
        after={"status": to},
    )
    return _card(manager)


async def block_manager(
    session: AsyncSession, *, actor: User, manager_id: uuid.UUID, settings: Settings | None = None
) -> StaffCard:
    return await _transition_status(
        session,
        actor=actor,
        manager_id=manager_id,
        to=UserStatus.blocked,
        allowed_from={UserStatus.active},
        action="manager_blocked",
        settings=settings,
    )


async def unblock_manager(
    session: AsyncSession, *, actor: User, manager_id: uuid.UUID, settings: Settings | None = None
) -> StaffCard:
    return await _transition_status(
        session,
        actor=actor,
        manager_id=manager_id,
        to=UserStatus.active,
        allowed_from={UserStatus.blocked},
        action="manager_unblocked",
        settings=settings,
    )


async def delete_manager(
    session: AsyncSession, *, actor: User, manager_id: uuid.UUID, settings: Settings | None = None
) -> None:
    """Удалить менеджера из персонала: снять роль, закрыть доступ, вернуть треды в очередь."""
    _require_owner(actor, settings)
    users = UserRepository(session)
    manager = await _get_manager(users, manager_id)
    _require_can_manage(actor, manager, settings)
    before_status = manager.status
    await users.set_duty(manager, on_duty=False, duty_date=manager.duty_date, duty_since=None)
    await SupportRepository(session).unassign_open_for_manager(manager.id)
    if manager.status is not UserStatus.blocked:
        await users.update_status(manager, UserStatus.blocked)
    await users.update_role(manager, UserRole.client)
    await users.set_permissions(manager, {})
    # Клиент без акаунта — сломанное состояние: `account_id` во всех клиентских
    # таблицах NOT NULL, а `resolve_account_scope` без членства вернёт None → любая
    # запись (ФОП, ТТН, склад) падает NotNullViolation. `users.create` заводит акаунт
    # только для роли `client`, поэтому у менеджера его нет, и смена роли обязана
    # его создать. Достижимо через UI: найм по Telegram-ID → зняття ролі → розблокування.
    accounts = ClientAccountRepository(session)
    if await accounts.get_membership(user_id=manager.id) is None:
        await accounts.create_for_owner(manager)
    await AuditRepository(session).log(
        "manager_deleted",
        user_id=actor.id,
        affected_entity=f"user:{manager.id}",
        before={"role": UserRole.manager, "status": before_status},
        after={"role": UserRole.client, "status": UserStatus.blocked},
    )
