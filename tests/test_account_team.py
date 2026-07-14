"""Інваріанти multi-user клієнтського акаунта."""

from __future__ import annotations

import pytest
from app.bot.permissions import CAN_MANAGE_CLIENTS, require_account_member
from app.bot.types import ClientAccountContext
from app.db.models.enums import MembershipStatus, UserRole, UserStatus
from app.db.repositories import ClientAccountRepository, UserRepository
from app.services import account_team, clients
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
