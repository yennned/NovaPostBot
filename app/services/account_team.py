"""Керування командою клієнтського акаунта."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.permissions import require_account_owner
from app.bot.types import ClientAccountContext
from app.db.models.client_account import ClientAccountMembership
from app.db.models.enums import (
    ClientAccountStatus,
    MembershipRole,
    MembershipStatus,
    UserRole,
    UserStatus,
)
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


def _joining_status(user: User) -> MembershipStatus:
    """Стан членства при вступі: підтверджений контактом — одразу `active`.

    Телефон осідає в `users` лише з `request_contact`, тож наявний `telegram_id`
    означає, що Telegram цей номер уже підтвердив, і другий /start нічого не
    додасть. Головне — його там і не буде: `StartService.register_contact` для
    відомого `telegram_id` повертається раніше за `activate_employee_contact`,
    тобто `invited` на такому користувачі не зняв би вже ніхто, і людина
    застрягла б без акаунта.
    """
    return MembershipStatus.active if user.telegram_id is not None else MembershipStatus.invited


async def _take_over(
    session: AsyncSession,
    *,
    context: ClientAccountContext,
    user: User,
    current: ClientAccountMembership | None,
    accounts: ClientAccountRepository,
) -> AccountMemberView:
    """Забрати номер неактивного користувача в команду `context.account`.

    `/start` заводить власний `ClientAccount` кожному, хто надіслав контакт
    (`UserRepository.create(create_account=True)`), а членство унікальне по
    `user_id`. Без переносу будь-який номер, що хоч раз бачив бота, лишався б
    незапрошуваним назавжди — «видалення» клієнта його теж не звільняє
    (`clients._transition` тільки гасить статуси).
    """
    status = _joining_status(user)
    before = (
        {
            "account_id": str(current.account_id),
            "role": current.role.value,
            "status": current.status.value,
        }
        if current is not None
        else None
    )
    if current is None:
        membership = await accounts.create_invited_membership(
            account_id=context.account.id,
            user=user,
            invited_by_user_id=context.user.id,
        )
    else:
        if current.role is MembershipRole.account_owner:
            # Акаунт лишається без власника — гасимо, щоб він не рахувався живим.
            # Не видаляємо: `audit_logs.account_id` — FK `ondelete=SET NULL`, і
            # видалення стерло б скоуп історії цього клієнта.
            current.account.status = ClientAccountStatus.archived
        membership = current
        # Присвоюємо саме relationship, а не `account_id`: при живому
        # `membership.account` (його підтягує `get_membership`) flush інакше
        # відкотив би FK назад на старий акаунт.
        membership.account = context.account
        membership.role = MembershipRole.employee
        membership.invited_by_user_id = context.user.id
        membership.joined_at = None  # свіжий стамп проставить `set_membership_status`
    membership = await accounts.set_membership_status(membership, status)
    if status is MembershipStatus.active:
        user.status = UserStatus.active
        await session.flush()
    await AuditRepository(session).log(
        "account_employee_reclaimed" if current is not None else "account_employee_invited",
        user_id=context.user.id,
        account_id=context.account.id,
        affected_entity=f"user:{user.id}",
        before=before,
        after={"phone": user.phone, "membership": membership.status.value},
    )
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
    accounts = ClientAccountRepository(session)
    existing = await users.get_by_phone(normalized)
    if existing is None:
        user = await users.create(
            phone=normalized,
            role=UserRole.client,
            status=UserStatus.pending,
            create_account=False,
        )
        return await _take_over(
            session, context=context, user=user, current=None, accounts=accounts
        )

    # Роль платформи перевіряємо ДО членства: у демоутнутого менеджера членство є
    # (`staff.delete_manager` заводить йому акаунт), і без цього порядку він
    # отримував би «пов’язаний з іншим акаунтом» замість правди.
    if existing.role in {UserRole.manager, UserRole.owner}:
        raise AccountMembershipConflict("номер належить внутрішньому працівнику платформи")

    current = await accounts.get_membership(user_id=existing.id)
    if current is not None and current.account_id == context.account.id:
        if current.role is not MembershipRole.employee:
            raise AccountMembershipConflict("це головний клієнт цього акаунта")
        if current.status is MembershipStatus.invited:
            return _view(current)
        raise AccountMembershipConflict("цей працівник уже є в команді")

    # Чужий акаунт або взагалі без членства. Забрати номер можна лише в
    # неактивного: активний клієнт зі своєю справою мовчки став би чужим
    # працівником і зник би зі списку «Клієнти» (`list_by_status` тримає лише
    # `account_owner`).
    if existing.status is UserStatus.active:
        raise AccountMembershipConflict("номер належить активному клієнту")
    return await _take_over(
        session, context=context, user=existing, current=current, accounts=accounts
    )


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
