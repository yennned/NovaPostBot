"""Керування командою клієнтського акаунта."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.permissions import require_account_owner
from app.bot.types import ClientAccountContext
from app.db.models.client_account import ClientAccountMembership
from app.db.models.enums import MembershipRole, MembershipStatus, UserRole, UserStatus
from app.db.models.user import User
from app.db.repositories import AuditRepository, ClientAccountRepository, UserRepository
from app.services.exceptions import (
    AccountMemberNotFound,
    AccountMembershipConflict,
    AlreadyInStatus,
    LastAccountOwnerError,
    PermissionDenied,
)
from app.utils.phone import normalize_phone


@dataclass(frozen=True, slots=True)
class AccountMemberView:
    id: uuid.UUID
    account_id: uuid.UUID
    user_id: uuid.UUID
    phone: str | None
    full_name: str | None
    telegram_id: int | None
    role: MembershipRole
    status: MembershipStatus


def _view(membership: ClientAccountMembership) -> AccountMemberView:
    user = membership.user
    return AccountMemberView(
        id=membership.id,
        account_id=membership.account_id,
        user_id=membership.user_id,
        phone=user.phone,
        full_name=user.full_name,
        telegram_id=user.telegram_id,
        role=membership.role,
        status=membership.status,
    )


async def list_team(
    session: AsyncSession,
    *,
    context: ClientAccountContext,
    query: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[AccountMemberView], int]:
    require_account_owner(context)
    rows, total = await ClientAccountRepository(session).list_members(
        context.account.id, query=query, limit=limit, offset=offset
    )
    return [_view(row) for row in rows], total


async def get_member(
    session: AsyncSession, *, context: ClientAccountContext, user_id: uuid.UUID
) -> AccountMemberView:
    require_account_owner(context)
    membership = await ClientAccountRepository(session).get_membership(
        user_id=user_id, account_id=context.account.id
    )
    if membership is None:
        raise AccountMemberNotFound(str(user_id))
    return _view(membership)


async def invite_employee(
    session: AsyncSession,
    *,
    context: ClientAccountContext,
    phone: str,
) -> AccountMemberView:
    require_account_owner(context)
    normalized = normalize_phone(phone)
    if normalized is None:
        raise AccountMembershipConflict("вкажіть коректний номер телефону")

    users = UserRepository(session)
    existing = await users.get_by_phone(normalized)
    accounts = ClientAccountRepository(session)
    if existing is not None:
        current = await accounts.get_membership(user_id=existing.id)
        if current is not None:
            if current.account_id == context.account.id and current.role is MembershipRole.employee:
                if current.status is MembershipStatus.invited:
                    return _view(current)
                raise AccountMembershipConflict("цей працівник уже є в команді")
            raise AccountMembershipConflict("номер уже пов’язаний з іншим акаунтом")
        if existing.role in {UserRole.manager, UserRole.owner}:
            raise AccountMembershipConflict("номер належить внутрішньому працівнику платформи")
        raise AccountMembershipConflict("номер уже пов’язаний з іншим користувачем")

    user = await users.create(
        phone=normalized,
        role=UserRole.client,
        status=UserStatus.pending,
        create_account=False,
    )
    membership = await accounts.create_invited_membership(
        account_id=context.account.id,
        user=user,
        invited_by_user_id=context.user.id,
    )
    await AuditRepository(session).log(
        "account_employee_invited",
        user_id=context.user.id,
        account_id=context.account.id,
        affected_entity=f"user:{user.id}",
        after={"phone": normalized, "membership": membership.status.value},
    )
    return _view(membership)


async def activate_employee_contact(
    session: AsyncSession,
    *,
    user: User,
    telegram_id: int,
    full_name: str | None = None,
) -> bool:
    """Прив’язати Telegram до запрошеного користувача після request_contact."""
    membership = await ClientAccountRepository(session).get_membership(user_id=user.id)
    if membership is None or membership.role is not MembershipRole.employee:
        return False
    if membership.status is MembershipStatus.blocked:
        return False
    user.telegram_id = telegram_id
    if full_name:
        user.full_name = full_name
    user.status = UserStatus.active
    await ClientAccountRepository(session).set_membership_status(
        membership, MembershipStatus.active
    )
    return True


async def set_employee_status(
    session: AsyncSession,
    *,
    context: ClientAccountContext,
    user_id: uuid.UUID,
    status: MembershipStatus,
) -> AccountMemberView:
    require_account_owner(context)
    accounts = ClientAccountRepository(session)
    membership = await accounts.get_membership(user_id=user_id, account_id=context.account.id)
    if membership is None:
        raise AccountMemberNotFound(str(user_id))
    if membership.user_id == context.user.id:
        raise LastAccountOwnerError("не можна заблокувати самого себе")
    if membership.role is not MembershipRole.employee:
        raise LastAccountOwnerError("в акаунті має залишитися активний власник")
    if membership.status is status:
        raise AlreadyInStatus(status)  # type: ignore[arg-type]
    if status not in {MembershipStatus.active, MembershipStatus.blocked}:
        raise PermissionDenied("недозволений перехід стану працівника")
    membership = await accounts.set_membership_status(membership, status)
    membership.user.status = (
        UserStatus.active if status is MembershipStatus.active else UserStatus.blocked
    )
    await session.flush()
    await AuditRepository(session).log(
        f"account_employee_{status.value}",
        user_id=context.user.id,
        account_id=context.account.id,
        affected_entity=f"user:{user_id}",
        after={"status": status.value},
    )
    return _view(membership)


async def block_employee(
    session: AsyncSession, *, context: ClientAccountContext, user_id: uuid.UUID
) -> AccountMemberView:
    return await set_employee_status(
        session, context=context, user_id=user_id, status=MembershipStatus.blocked
    )


async def restore_employee(
    session: AsyncSession, *, context: ClientAccountContext, user_id: uuid.UUID
) -> AccountMemberView:
    return await set_employee_status(
        session, context=context, user_id=user_id, status=MembershipStatus.active
    )
