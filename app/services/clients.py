"""Сервис управления клиентами (Фаза 2) — доменная логика без aiogram.

Паттерн как в `app/services/bootstrap.py`: функции принимают `AsyncSession`,
внутри строят репозитории, пишут аудит. Транзакцией управляет вызывающий
(middleware бота / тест). Бот-слой зовёт эти функции и рендерит результат;
ошибки — подтипы `ClientServiceError` (см. `app/services/exceptions.py`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import permissions
from app.config import Settings
from app.db.models.enums import ClientAccountStatus, MembershipStatus, UserRole, UserStatus
from app.db.models.user import User
from app.db.repositories import (
    AuditRepository,
    ClientAccountRepository,
    SenderProfileRepository,
    UserRepository,
)
from app.services.client_sheet_sync import best_effort_sync
from app.services.exceptions import (
    AlreadyInStatus,
    ClientNotFound,
    PhoneAlreadyTaken,
    TransitionForbidden,
)

# Per-flag права (ключи в `users.permissions`). Канонический источник —
# `app/bot/permissions.py`; здесь — алиас для обратной совместимости вызовов
# `clients.CAN_MANAGE_CLIENTS`. Правка профиля клиента per-flag'ом больше не
# гейтится — это действие только владельца (`permissions.require_owner`).
CAN_MANAGE_CLIENTS = permissions.CAN_MANAGE_CLIENTS  # подтверждение/блокировка


@dataclass(frozen=True, slots=True)
class ClientListItem:
    id: uuid.UUID
    telegram_id: int
    full_name: str | None
    phone: str | None
    status: UserStatus
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ClientPage:
    items: list[ClientListItem]
    total: int
    status_counts: dict[UserStatus, int]
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class ClientCard:
    id: uuid.UUID
    telegram_id: int
    full_name: str | None
    phone: str | None
    role: UserRole
    status: UserStatus
    created_at: datetime
    sender_profiles_count: int
    default_sender_name: str | None


async def _get_client(users: UserRepository, client_id: uuid.UUID) -> User:
    user = await users.get_by_id(client_id)
    if user is None or user.role is not UserRole.client:
        raise ClientNotFound(str(client_id))
    return user


async def _card(session: AsyncSession, user: User) -> ClientCard:
    profiles = SenderProfileRepository(session)
    items = await profiles.list_for_client(user.id)
    # Дефолт уже среди items — отдельный запрос не нужен.
    default = next((p for p in items if p.is_default), None)
    return ClientCard(
        id=user.id,
        telegram_id=user.telegram_id,
        full_name=user.full_name,
        phone=user.phone,
        role=user.role,
        status=user.status,
        created_at=user.created_at,
        sender_profiles_count=len(items),
        default_sender_name=default.name if default else None,
    )


def _check_transition(user: User, to: UserStatus, allowed_from: set[UserStatus]) -> None:
    if user.status is to:
        raise AlreadyInStatus(to)
    if user.status not in allowed_from:
        raise TransitionForbidden(user.status, to)


async def _transition(
    session: AsyncSession,
    *,
    actor: User,
    client_id: uuid.UUID,
    to: UserStatus,
    allowed_from: set[UserStatus],
    action: str,
    settings: Settings | None,
    notes: str | None = None,
) -> ClientCard:
    users = UserRepository(session)
    user = await _get_client(users, client_id)
    permissions.require_can_manage(actor, user, CAN_MANAGE_CLIENTS, settings)
    _check_transition(user, to, allowed_from)

    before = {"status": user.status}
    await users.update_status(user, to)
    membership = await ClientAccountRepository(session).get_membership(user_id=user.id)
    if membership is not None:
        account_status = {
            UserStatus.blocked: ClientAccountStatus.blocked,
            UserStatus.archived: ClientAccountStatus.archived,
        }.get(to, ClientAccountStatus.active)
        membership.account.status = account_status
        membership_status = (
            MembershipStatus.blocked
            if to in {UserStatus.blocked, UserStatus.archived}
            else MembershipStatus.active
        )
        await ClientAccountRepository(session).set_membership_status(membership, membership_status)
    await AuditRepository(session).log(
        action,
        user_id=actor.id,
        affected_entity=f"user:{user.id}",
        before=before,
        after={"status": to},
        notes=notes,
    )
    return await _card(session, user)


# --- Чтение ---------------------------------------------------------------


async def list_clients(
    session: AsyncSession,
    *,
    actor: User,
    status: UserStatus | None = None,
    query: str | None = None,
    limit: int = 20,
    offset: int = 0,
    settings: Settings | None = None,
) -> ClientPage:
    permissions.require_staff(actor, settings)
    users = UserRepository(session)
    rows, total = await users.list_by_status(status=status, query=query, limit=limit, offset=offset)
    counts = await users.count_by_status()
    items = [
        ClientListItem(
            id=u.id,
            telegram_id=u.telegram_id,
            full_name=u.full_name,
            phone=u.phone,
            status=u.status,
            created_at=u.created_at,
        )
        for u in rows
    ]
    return ClientPage(items=items, total=total, status_counts=counts, limit=limit, offset=offset)


async def get_client_card(
    session: AsyncSession,
    *,
    actor: User,
    client_id: uuid.UUID,
    settings: Settings | None = None,
) -> ClientCard:
    permissions.require_staff(actor, settings)
    user = await _get_client(UserRepository(session), client_id)
    return await _card(session, user)


# --- Мутации --------------------------------------------------------------


async def approve_client(
    session: AsyncSession, *, actor: User, client_id: uuid.UUID, settings: Settings | None = None
) -> ClientCard:
    return await _transition(
        session,
        actor=actor,
        client_id=client_id,
        to=UserStatus.active,
        allowed_from={UserStatus.pending},
        action="client_approved",
        settings=settings,
    )


async def block_client(
    session: AsyncSession,
    *,
    actor: User,
    client_id: uuid.UUID,
    reason: str | None = None,
    settings: Settings | None = None,
) -> ClientCard:
    return await _transition(
        session,
        actor=actor,
        client_id=client_id,
        to=UserStatus.blocked,
        allowed_from={UserStatus.pending, UserStatus.active},
        action="client_blocked",
        settings=settings,
        notes=reason,
    )


async def unblock_client(
    session: AsyncSession, *, actor: User, client_id: uuid.UUID, settings: Settings | None = None
) -> ClientCard:
    return await _transition(
        session,
        actor=actor,
        client_id=client_id,
        to=UserStatus.active,
        allowed_from={UserStatus.blocked},
        action="client_unblocked",
        settings=settings,
    )


async def archive_client(
    session: AsyncSession, *, actor: User, client_id: uuid.UUID, settings: Settings | None = None
) -> ClientCard:
    return await _transition(
        session,
        actor=actor,
        client_id=client_id,
        to=UserStatus.archived,
        allowed_from={UserStatus.pending, UserStatus.active, UserStatus.blocked},
        action="client_archived",
        settings=settings,
    )


async def restore_client(
    session: AsyncSession, *, actor: User, client_id: uuid.UUID, settings: Settings | None = None
) -> ClientCard:
    # Архивный клиент мог быть заархивирован из blocked — возвращаем в pending
    # (повторное подтверждение), а не сразу в active, чтобы не «снять» блок молча.
    return await _transition(
        session,
        actor=actor,
        client_id=client_id,
        to=UserStatus.pending,
        allowed_from={UserStatus.archived},
        action="client_restored",
        settings=settings,
    )


async def update_client_profile(
    session: AsyncSession,
    *,
    actor: User,
    client_id: uuid.UUID,
    full_name: str | None = None,
    phone: str | None = None,
    settings: Settings | None = None,
) -> ClientCard:
    users = UserRepository(session)
    user = await _get_client(users, client_id)
    permissions.require_owner(actor, settings)

    before = {"full_name": user.full_name, "phone": user.phone}
    changed = False
    previous_sheet_key = user.stock_sheet_key
    if full_name is not None and full_name != user.full_name:
        user.full_name = full_name
        changed = True
    if phone is not None and phone != user.phone:
        # Телефон — UNIQUE: проверяем коллизию заранее, чтобы вернуть доменную
        # ошибку, а не «сырой» IntegrityError на flush (бот ловит ClientServiceError).
        clash = await users.get_by_phone(phone)
        if clash is not None and clash.id != user.id:
            raise PhoneAlreadyTaken(phone)
        user.phone = phone
        changed = True
    if changed:
        await session.flush()
        await AuditRepository(session).log(
            "client_profile_updated",
            user_id=actor.id,
            affected_entity=f"user:{user.id}",
            before=before,
            after={"full_name": user.full_name, "phone": user.phone},
        )
        if full_name is not None:
            await best_effort_sync(
                session,
                client=user,
                log_key="client_profile_sheet_sync_failed",
                previous_sheet_key=previous_sheet_key,
                user_id=str(user.id),
            )
    return await _card(session, user)
