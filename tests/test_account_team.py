"""Інваріанти multi-user клієнтського акаунта."""

from __future__ import annotations

import pytest
from app.bot.permissions import CAN_MANAGE_CLIENTS, require_account_member
from app.bot.types import ClientAccountContext
from app.db.models.enums import (
    ClientAccountStatus,
    MembershipRole,
    MembershipStatus,
    UserRole,
    UserStatus,
)
from app.db.repositories import ClientAccountRepository, SenderProfileRepository, UserRepository
from app.services import account_team, clients, sender_profile
from app.services.exceptions import (
    AccountMembershipConflict,
    LastAccountOwnerError,
    PermissionDenied,
)
from sqlalchemy.ext.asyncio import AsyncSession


async def _owner(session: AsyncSession):
    return await UserRepository(session).create(
        telegram_id=7001,
        phone="380507000001",
        full_name="Головний клієнт",
        role=UserRole.client,
        status=UserStatus.active,
    )


async def _context(session: AsyncSession, user):
    membership = await ClientAccountRepository(session).get_membership(user_id=user.id)
    assert membership is not None and membership.account is not None
    return ClientAccountContext(user=user, account=membership.account, membership=membership)


async def test_blocking_client_cuts_off_their_employees(db_session: AsyncSession):
    # Регрессия: `clients._transition` гасит `account.status`, но членства
    # работников остаются active, а `get_context_for_user` смотрел только на
    # членство. Работник заблокированного клиента сохранял полный доступ
    # к складу/ФОП/ТТН акаунта.
    owner = await _owner(db_session)
    owner_context = await _context(db_session, owner)
    invited = await account_team.invite_employee(
        db_session, context=owner_context, phone="0507000009"
    )
    employee = await UserRepository(db_session).get_by_id(invited.user_id)
    await account_team.activate_employee_contact(
        db_session, user=employee, telegram_id=7009, full_name="Працівник"
    )
    accounts = ClientAccountRepository(db_session)
    assert await accounts.get_context_for_user(employee.id) is not None  # до блокировки

    manager = await UserRepository(db_session).create(
        telegram_id=7010,
        phone="380507000010",
        full_name="Менеджер",
        role=UserRole.manager,
        status=UserStatus.active,
        permissions={CAN_MANAGE_CLIENTS: True},
    )
    await clients.block_client(db_session, actor=manager, client_id=owner.id)

    # Членство работника всё ещё active — доступ должен резать статус аккаунта.
    membership = await accounts.get_membership(user_id=employee.id)
    assert membership.status is MembershipStatus.active
    assert employee.status is UserStatus.active
    assert await accounts.get_context_for_user(employee.id) is None

    # Разблокировка возвращает доступ симметрично.
    await clients.unblock_client(db_session, actor=manager, client_id=owner.id)
    assert await accounts.get_context_for_user(employee.id) is not None


async def test_invite_is_idempotent_and_contact_activates_employee(db_session: AsyncSession):
    owner = await _owner(db_session)
    context = await _context(db_session, owner)

    first = await account_team.invite_employee(
        db_session, context=context, phone="+380 50 700 00 02"
    )
    second = await account_team.invite_employee(db_session, context=context, phone="0507000002")
    assert first.user_id == second.user_id
    assert second.status is MembershipStatus.invited

    employee = await UserRepository(db_session).get_by_id(first.user_id)
    assert employee is not None and employee.telegram_id is None
    assert await account_team.activate_employee_contact(
        db_session, user=employee, telegram_id=7002, full_name="Працівник"
    )
    refreshed = await ClientAccountRepository(db_session).get_membership(user_id=employee.id)
    assert refreshed is not None
    assert refreshed.status is MembershipStatus.active
    assert employee.status is UserStatus.active
    assert employee.telegram_id == 7002

    member = await account_team.get_member(db_session, context=context, user_id=employee.id)
    assert member.user_id == employee.id
    assert member.full_name == "Працівник"


async def test_employee_cannot_join_second_account_or_manage_team(db_session: AsyncSession):
    owner = await _owner(db_session)
    owner_context = await _context(db_session, owner)
    invited = await account_team.invite_employee(
        db_session, context=owner_context, phone="0507000003"
    )
    employee = await UserRepository(db_session).get_by_id(invited.user_id)
    assert employee is not None
    assert (
        await account_team.invite_employee(db_session, context=owner_context, phone="0507000003")
    ).user_id == employee.id
    await account_team.activate_employee_contact(db_session, user=employee, telegram_id=7003)

    manager = await UserRepository(db_session).create(
        telegram_id=7009, phone="380507000009", role=UserRole.manager, status=UserStatus.active
    )
    with pytest.raises(AccountMembershipConflict):
        await account_team.invite_employee(
            db_session, context=owner_context, phone=manager.phone or ""
        )

    employee_context = await _context(db_session, employee)
    with pytest.raises(PermissionDenied):
        await account_team.list_team(db_session, context=employee_context)


async def test_block_restore_is_immediate_and_self_block_is_rejected(db_session: AsyncSession):
    owner = await _owner(db_session)
    context = await _context(db_session, owner)
    invited = await account_team.invite_employee(db_session, context=context, phone="0507000004")
    employee = await UserRepository(db_session).get_by_id(invited.user_id)
    assert employee is not None
    await account_team.activate_employee_contact(db_session, user=employee, telegram_id=7004)

    blocked = await account_team.block_employee(db_session, context=context, user_id=employee.id)
    assert blocked.status is MembershipStatus.blocked
    with pytest.raises(PermissionDenied):
        require_account_member(await _context(db_session, employee))

    restored = await account_team.restore_employee(db_session, context=context, user_id=employee.id)
    assert restored.status is MembershipStatus.active
    require_account_member(await _context(db_session, employee))

    with pytest.raises(LastAccountOwnerError):
        await account_team.block_employee(db_session, context=context, user_id=owner.id)


async def _self_registered(session: AsyncSession, *, telegram_id: int | None, phone: str):
    """Кто угодно после /start: свой `ClientAccount` + членство `account_owner`."""
    user = await UserRepository(session).create(
        telegram_id=telegram_id,
        phone=phone,
        full_name="Випадковий",
        role=UserRole.client,
        status=UserStatus.pending,
    )
    membership = await ClientAccountRepository(session).get_membership(user_id=user.id)
    assert membership is not None and membership.role is MembershipRole.account_owner
    return user, membership.account


async def test_invite_reclaims_self_registered_phone(db_session: AsyncSession):
    # Регрессия: `/start` заводит свой аккаунт каждому (`create_account=True`), а
    # членство уникально по `user_id` — любой засветившийся в боте номер навсегда
    # оставался незапрашиваемым в чужую команду.
    owner = await _owner(db_session)
    context = await _context(db_session, owner)
    stranger, own_account = await _self_registered(
        db_session, telegram_id=7101, phone="380507000101"
    )

    member = await account_team.invite_employee(db_session, context=context, phone="0507000101")

    assert member.user_id == stranger.id
    assert member.role is MembershipRole.employee
    assert member.account_id == context.account.id
    # Контакт Telegram уже подтверждён — второго /start не ждём. Иначе членство
    # застряло бы `invited` навсегда: `register_contact` для известного
    # `telegram_id` возвращается раньше `activate_employee_contact`.
    assert member.status is MembershipStatus.active
    assert stranger.status is UserStatus.active
    assert own_account.status is ClientAccountStatus.archived

    memberships = await ClientAccountRepository(db_session).list_members(context.account.id)
    assert {row.user_id for row in memberships[0]} == {owner.id, stranger.id}
    require_account_member(await _context(db_session, stranger))


async def test_invite_reclaims_deleted_client(db_session: AsyncSession):
    # Сценарий из репорта: номер зарегался сам, владелец клиента «удалил»
    # (soft-delete → blocked), номер остался занят и в команду не заводился.
    owner = await _owner(db_session)
    context = await _context(db_session, owner)
    stranger, own_account = await _self_registered(
        db_session, telegram_id=7102, phone="380507000102"
    )
    manager = await UserRepository(db_session).create(
        telegram_id=7110,
        phone="380507000110",
        role=UserRole.manager,
        status=UserStatus.active,
        permissions={CAN_MANAGE_CLIENTS: True},
    )
    await clients.approve_client(db_session, actor=manager, client_id=stranger.id)
    await clients.block_client(db_session, actor=manager, client_id=stranger.id)
    assert stranger.status is UserStatus.blocked

    member = await account_team.invite_employee(db_session, context=context, phone="0507000102")

    assert member.account_id == context.account.id
    assert member.role is MembershipRole.employee
    assert member.status is MembershipStatus.active
    assert own_account.status is ClientAccountStatus.archived
    require_account_member(await _context(db_session, stranger))


async def test_reclaimed_employee_uses_account_np_key(db_session: AsyncSession):
    # Второй симптом репорта: «у работника нет доступа к API». Он был следствием
    # первого — человек сидел в своём пустом аккаунте. После переноса ФОП и ключ
    # НП аккаунта ему видны (гейт `owner_only` режет только правку ФОП).
    owner = await _owner(db_session)
    context = await _context(db_session, owner)
    profile = await SenderProfileRepository(db_session).create(
        client_id=owner.id,
        name="ФОП Вероніка",
        np_api_key="np-key",
        sender_phone="380507000001",
        is_default=True,
        np_sender_ref="cp",
        np_contact_ref="contact",
    )
    stranger, _ = await _self_registered(db_session, telegram_id=7103, phone="380507000103")
    await account_team.invite_employee(db_session, context=context, phone="0507000103")

    visible = await sender_profile.list_profiles(
        db_session, actor=stranger, client_id=stranger.id, account_id=context.account.id
    )
    assert [item.id for item in visible] == [profile.id]

    # Именно этот путь резолвит ключ в `shipment._resolve_sender`.
    default = await SenderProfileRepository(db_session).get_default_for_client(
        stranger.id, account_id=context.account.id
    )
    assert default is not None and default.id == profile.id
    assert default.np_api_key == "np-key"


async def test_invite_without_contact_stays_invited(db_session: AsyncSession):
    # Номер в базе есть (напр. заведён заранее), но контакт боту не слал —
    # активировать его нельзя, ждём `request_contact`.
    owner = await _owner(db_session)
    context = await _context(db_session, owner)
    stranger, _ = await _self_registered(db_session, telegram_id=None, phone="380507000104")

    member = await account_team.invite_employee(db_session, context=context, phone="0507000104")

    assert member.status is MembershipStatus.invited
    assert stranger.status is UserStatus.pending
    assert await account_team.activate_employee_contact(
        db_session, user=stranger, telegram_id=7104, full_name="Працівник"
    )
    assert stranger.status is UserStatus.active


async def test_invite_refuses_active_client_and_self(db_session: AsyncSession):
    owner = await _owner(db_session)
    context = await _context(db_session, owner)
    active_client = await UserRepository(db_session).create(
        telegram_id=7105,
        phone="380507000105",
        role=UserRole.client,
        status=UserStatus.active,
    )

    # Активный клиент со своим делом молча стал бы чужим работником и пропал бы
    # из списка «Клієнти» (`list_by_status` держит только `account_owner`).
    with pytest.raises(AccountMembershipConflict, match="активному клієнту"):
        await account_team.invite_employee(db_session, context=context, phone="0507000105")
    assert active_client.status is UserStatus.active

    # Себя же в работники — внятный текст вместо «пов'язаний з іншим акаунтом».
    with pytest.raises(AccountMembershipConflict, match="головний клієнт"):
        await account_team.invite_employee(db_session, context=context, phone=owner.phone or "")


async def test_invite_refuses_active_employee_of_another_team(db_session: AsyncSession):
    owner = await _owner(db_session)
    owner_context = await _context(db_session, owner)
    rival = await UserRepository(db_session).create(
        telegram_id=7106,
        phone="380507000106",
        full_name="Інший клієнт",
        role=UserRole.client,
        status=UserStatus.active,
    )
    rival_context = await _context(db_session, rival)
    invited = await account_team.invite_employee(
        db_session, context=rival_context, phone="0507000107"
    )
    employee = await UserRepository(db_session).get_by_id(invited.user_id)
    assert employee is not None
    await account_team.activate_employee_contact(db_session, user=employee, telegram_id=7107)

    with pytest.raises(AccountMembershipConflict, match="активному клієнту"):
        await account_team.invite_employee(db_session, context=owner_context, phone="0507000107")

    # А заблокированного в своей команде — забрать можно.
    await account_team.block_employee(db_session, context=rival_context, user_id=employee.id)
    member = await account_team.invite_employee(
        db_session, context=owner_context, phone="0507000107"
    )
    assert member.account_id == owner_context.account.id
    assert member.status is MembershipStatus.active
    # Чужой аккаунт живой — работник ушёл, но гасить его нельзя.
    assert rival_context.account.status is ClientAccountStatus.active
