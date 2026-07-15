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
    """Состояние членства при вступлении: подтверждён контактом — сразу `active`.

    Телефон оседает в `users` только через `request_contact`, поэтому живой
    `telegram_id` означает, что Telegram этот номер уже подтвердил, и второй
    /start ничего не добавит. Главное — его там и не будет:
    `StartService.register_contact` для известного `telegram_id` возвращается
    раньше `activate_employee_contact`, то есть `invited` с такого пользователя
    уже никто бы не снял, и человек застрял бы без аккаунта.
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
    """Забрать номер неактивного пользователя в команду `context.account`.

    `/start` заводит собственный `ClientAccount` каждому, кто прислал контакт
    (`UserRepository.create(create_account=True)`), а членство уникально по
    `user_id`. Без переноса любой номер, хоть раз видевший бота, навсегда
    оставался бы незапрашиваемым — «удаление» клиента его тоже не освобождает
    (`clients._transition` только гасит статусы).
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
            # Аккаунт остаётся без владельца — гасим, чтобы он не считался живым.
            # Не удаляем: `audit_logs.account_id` — FK `ondelete=SET NULL`, и
            # удаление стёрло бы скоуп истории этого клиента.
            current.account.status = ClientAccountStatus.archived
        membership = current
        # Присваиваем именно relationship, а не `account_id`: при живом
        # `membership.account` (его подтягивает `get_membership`) flush иначе
        # откатил бы FK назад на старый аккаунт.
        membership.account = context.account
        membership.role = MembershipRole.employee
        membership.invited_by_user_id = context.user.id
        membership.joined_at = None  # свежий штамп проставит `set_membership_status`
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
    # `for_update`: дальше по прочитанному решаем, переносить ли членство. Без
    # блокировки два владельца, зовущие один номер одновременно, оба прошли бы
    # проверки и перезаписали `membership.account` — оба увидели бы «додано», а
    # человек оказался бы в команде того, кто закоммитил последним. Строка
    # `users` — общий предок обеих веток (перенос и заведение членства).
    existing = await users.get_by_phone(normalized, for_update=True)
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

    # Роль платформы проверяем ДО членства: у демоутнутого менеджера членство
    # есть (`staff.delete_manager` заводит ему аккаунт), и без этого порядка он
    # получал бы «пов'язаний з іншим акаунтом» вместо правды.
    if existing.role in {UserRole.manager, UserRole.owner}:
        raise AccountMembershipConflict("номер належить внутрішньому працівнику платформи")

    current = await accounts.get_membership(user_id=existing.id)
    if current is not None and current.account_id == context.account.id:
        if current.role is not MembershipRole.employee:
            raise AccountMembershipConflict("це головний клієнт цього акаунта")
        if current.status is MembershipStatus.invited:
            return _view(current)
        raise AccountMembershipConflict("цей працівник уже є в команді")

    # Чужой аккаунт либо вовсе без членства. Забрать номер можно только у
    # неактивного: активный клиент со своим делом молча стал бы чужим
    # работником и пропал бы из списка «Клієнти» (`list_by_status` держит
    # только `account_owner`).
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
