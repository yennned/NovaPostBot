"""Тесты сервиса управления персоналом (`app/services/staff.py`) — на Postgres."""

from __future__ import annotations

import pytest
from app.bot import permissions as perm
from app.db.models.audit import AuditLog
from app.db.models.enums import SupportThreadStatus, UserRole, UserStatus
from app.db.repositories import SupportRepository, UserRepository
from app.services import staff
from app.services.exceptions import (
    InvalidPermissionFlag,
    PermissionDenied,
    StaffAlreadyManager,
    StaffPromotionForbidden,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def _owner(session: AsyncSession, telegram_id: int = 1):
    return await UserRepository(session).create(
        telegram_id=telegram_id, role=UserRole.owner, status=UserStatus.active
    )


async def _manager(session: AsyncSession, telegram_id: int = 10):
    return await UserRepository(session).create(
        telegram_id=telegram_id,
        phone=f"+38067{telegram_id}",
        full_name=f"Менеджер {telegram_id}",
        role=UserRole.manager,
        status=UserStatus.active,
    )


async def _client(session: AsyncSession, telegram_id: int = 100, status=UserStatus.active):
    return await UserRepository(session).create(
        telegram_id=telegram_id,
        phone=f"+38050{telegram_id}",
        full_name=f"Клієнт {telegram_id}",
        role=UserRole.client,
        status=status,
    )


async def _audit_actions(session: AsyncSession) -> list[str]:
    rows = await session.scalars(select(AuditLog.action).order_by(AuditLog.created_at))
    return list(rows)


async def test_list_staff_owner_only(db_session: AsyncSession):
    owner = await _owner(db_session)
    await _manager(db_session)
    client = await _client(db_session)

    page = await staff.list_staff(db_session, actor=owner)
    assert page.total == 1

    with pytest.raises(PermissionDenied):
        await staff.list_staff(db_session, actor=client)


async def test_add_manager_by_telegram_creates(db_session: AsyncSession):
    owner = await _owner(db_session)

    result = await staff.add_manager(db_session, actor=owner, telegram_id=555)

    created = await UserRepository(db_session).get_by_telegram_id(555)
    assert created.role is UserRole.manager
    assert created.status is UserStatus.active
    assert result.telegram_id == 555
    assert perm.has_permission(created, perm.CAN_HANDLE_SUPPORT)  # флаги on по умолчанию
    assert "manager_added" in await _audit_actions(db_session)


async def test_add_manager_rejects_active_client(db_session: AsyncSession):
    owner = await _owner(db_session)
    client = await _client(db_session, telegram_id=200)

    with pytest.raises(StaffPromotionForbidden):
        await staff.add_manager(db_session, actor=owner, telegram_id=client.telegram_id)


async def test_add_manager_already_manager(db_session: AsyncSession):
    owner = await _owner(db_session)
    manager = await _manager(db_session, telegram_id=11)

    with pytest.raises(StaffAlreadyManager):
        await staff.add_manager(db_session, actor=owner, telegram_id=manager.telegram_id)


async def test_set_permission_toggles_and_audits(db_session: AsyncSession):
    owner = await _owner(db_session)
    manager = await _manager(db_session)

    card = await staff.set_permission(
        db_session,
        actor=owner,
        manager_id=manager.id,
        flag=perm.CAN_HANDLE_SUPPORT,
        enabled=False,
    )

    state = {p.key: p.enabled for p in card.permissions}
    assert state[perm.CAN_HANDLE_SUPPORT] is False
    assert not perm.has_permission(manager, perm.CAN_HANDLE_SUPPORT)
    assert "permission_changed" in await _audit_actions(db_session)


async def test_set_permission_invalid_flag(db_session: AsyncSession):
    owner = await _owner(db_session)
    manager = await _manager(db_session)
    with pytest.raises(InvalidPermissionFlag):
        await staff.set_permission(
            db_session, actor=owner, manager_id=manager.id, flag="can_fly", enabled=True
        )


async def test_block_clears_duty_and_threads_then_unblock(db_session: AsyncSession):
    owner = await _owner(db_session)
    manager = await _manager(db_session)
    client = await _client(db_session)
    await UserRepository(db_session).set_duty(manager, on_duty=True, duty_since=None)
    thread = await SupportRepository(db_session).create_thread(
        client_id=client.id, assigned_manager_id=manager.id, status=SupportThreadStatus.open
    )

    card = await staff.block_manager(db_session, actor=owner, manager_id=manager.id)
    assert card.status is UserStatus.blocked
    assert manager.on_duty is False
    refreshed = await SupportRepository(db_session).get_with_messages(thread.id)
    assert refreshed.status is SupportThreadStatus.waiting  # тред вернулся в очередь
    assert refreshed.assigned_manager_id is None

    back = await staff.unblock_manager(db_session, actor=owner, manager_id=manager.id)
    assert back.status is UserStatus.active


async def test_demote_manager_clears_role_duty_threads(db_session: AsyncSession):
    owner = await _owner(db_session)
    manager = await _manager(db_session)
    client = await _client(db_session)
    await UserRepository(db_session).set_duty(manager, on_duty=True, duty_since=None)
    thread = await SupportRepository(db_session).create_thread(
        client_id=client.id, assigned_manager_id=manager.id, status=SupportThreadStatus.open
    )

    await staff.demote_manager(db_session, actor=owner, manager_id=manager.id)

    assert manager.role is UserRole.client
    assert manager.on_duty is False
    refreshed = await SupportRepository(db_session).get_with_messages(thread.id)
    assert refreshed.status is SupportThreadStatus.waiting
    assert "manager_demoted" in await _audit_actions(db_session)
