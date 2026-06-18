"""Тесты сервиса управления клиентами (`app/services/clients.py`) — на Postgres."""

from __future__ import annotations

import uuid

import pytest
from app.db.models.audit import AuditLog
from app.db.models.enums import UserRole, UserStatus
from app.db.repositories import UserRepository
from app.services import clients
from app.services.exceptions import (
    AlreadyInStatus,
    ClientNotFound,
    PermissionDenied,
    PhoneAlreadyTaken,
    TransitionForbidden,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


async def _manager(session: AsyncSession, telegram_id: int = 10, permissions: dict | None = None):
    return await UserRepository(session).create(
        telegram_id=telegram_id,
        role=UserRole.manager,
        status=UserStatus.active,
        permissions=permissions,
    )


async def _client(session: AsyncSession, telegram_id: int = 100, status=UserStatus.pending):
    return await UserRepository(session).create(
        telegram_id=telegram_id,
        phone=f"+38000{telegram_id}",
        full_name=f"Client {telegram_id}",
        role=UserRole.client,
        status=status,
    )


async def _audit_actions(session: AsyncSession) -> list[str]:
    rows = await session.scalars(select(AuditLog.action).order_by(AuditLog.created_at))
    return list(rows)


async def test_approve_pending_client(db_session: AsyncSession):
    actor = await _manager(db_session)
    client = await _client(db_session)

    card = await clients.approve_client(db_session, actor=actor, client_id=client.id)

    assert card.status is UserStatus.active
    assert client.status is UserStatus.active
    assert "client_approved" in await _audit_actions(db_session)


async def test_approve_non_pending_raises(db_session: AsyncSession):
    actor = await _manager(db_session)
    active = await _client(db_session, telegram_id=101, status=UserStatus.active)
    blocked = await _client(db_session, telegram_id=102, status=UserStatus.blocked)

    with pytest.raises(AlreadyInStatus):
        await clients.approve_client(db_session, actor=actor, client_id=active.id)
    with pytest.raises(TransitionForbidden):
        await clients.approve_client(db_session, actor=actor, client_id=blocked.id)


async def test_block_then_unblock(db_session: AsyncSession):
    actor = await _manager(db_session)
    client = await _client(db_session, status=UserStatus.active)

    blocked = await clients.block_client(
        db_session, actor=actor, client_id=client.id, reason="спам"
    )
    assert blocked.status is UserStatus.blocked

    restored = await clients.unblock_client(db_session, actor=actor, client_id=client.id)
    assert restored.status is UserStatus.active


async def test_archive_then_restore(db_session: AsyncSession):
    actor = await _manager(db_session)
    client = await _client(db_session, status=UserStatus.active)

    archived = await clients.archive_client(db_session, actor=actor, client_id=client.id)
    assert archived.status is UserStatus.archived

    # restore возвращает в pending (повторное подтверждение), не сразу в active —
    # чтобы заблокированный-и-заархивированный не «разблокировался» молча.
    restored = await clients.restore_client(db_session, actor=actor, client_id=client.id)
    assert restored.status is UserStatus.pending


async def test_unblock_non_blocked_forbidden(db_session: AsyncSession):
    actor = await _manager(db_session)
    client = await _client(db_session, status=UserStatus.pending)
    with pytest.raises(TransitionForbidden):
        await clients.unblock_client(db_session, actor=actor, client_id=client.id)


async def test_blocked_manager_cannot_manage(db_session: AsyncSession):
    # Менеджер, которого заблокировали, не должен управлять клиентами (по «залипшим»
    # reply-кнопкам), хотя роль осталась manager.
    actor = await _manager(db_session)
    actor.status = UserStatus.blocked
    await db_session.flush()
    client = await _client(db_session)
    with pytest.raises(PermissionDenied):
        await clients.approve_client(db_session, actor=actor, client_id=client.id)
    with pytest.raises(PermissionDenied):
        await clients.list_clients(db_session, actor=actor)


async def test_update_profile_phone_collision(db_session: AsyncSession):
    actor = await _manager(db_session)
    a = await _client(db_session, telegram_id=400)
    b = await _client(db_session, telegram_id=401)
    with pytest.raises(PhoneAlreadyTaken):
        await clients.update_client_profile(db_session, actor=actor, client_id=a.id, phone=b.phone)


async def test_permission_denied_for_client_actor(db_session: AsyncSession):
    actor = await _client(db_session, telegram_id=200, status=UserStatus.active)
    target = await _client(db_session, telegram_id=201)
    with pytest.raises(PermissionDenied):
        await clients.approve_client(db_session, actor=actor, client_id=target.id)


async def test_permission_denied_when_flag_revoked(db_session: AsyncSession):
    actor = await _manager(db_session, permissions={clients.CAN_MANAGE_CLIENTS: False})
    client = await _client(db_session)
    with pytest.raises(PermissionDenied):
        await clients.approve_client(db_session, actor=actor, client_id=client.id)


async def test_client_not_found(db_session: AsyncSession):
    actor = await _manager(db_session)
    with pytest.raises(ClientNotFound):
        await clients.approve_client(db_session, actor=actor, client_id=uuid.uuid4())


async def test_list_clients_pagination_search_counts(db_session: AsyncSession):
    actor = await _manager(db_session)
    await _client(db_session, telegram_id=300, status=UserStatus.pending)
    await _client(db_session, telegram_id=301, status=UserStatus.active)
    await _client(db_session, telegram_id=302, status=UserStatus.active)

    page = await clients.list_clients(db_session, actor=actor, status=UserStatus.active, limit=1)
    assert page.total == 2
    assert len(page.items) == 1
    assert page.status_counts[UserStatus.active] == 2
    assert page.status_counts[UserStatus.pending] == 1

    found = await clients.list_clients(db_session, actor=actor, query="301")
    assert {i.telegram_id for i in found.items} == {301}


async def test_get_client_card(db_session: AsyncSession):
    actor = await _manager(db_session)
    client = await _client(db_session)
    card = await clients.get_client_card(db_session, actor=actor, client_id=client.id)
    assert card.telegram_id == client.telegram_id
    assert card.sender_profiles_count == 0
    assert card.default_sender_name is None


async def test_update_profile_requires_edit_flag(db_session: AsyncSession):
    actor = await _manager(db_session, permissions={clients.CAN_EDIT_CLIENTS: False})
    client = await _client(db_session)
    with pytest.raises(PermissionDenied):
        await clients.update_client_profile(
            db_session, actor=actor, client_id=client.id, full_name="New"
        )


async def test_update_profile_writes_audit(db_session: AsyncSession):
    actor = await _manager(db_session)
    client = await _client(db_session)
    await clients.update_client_profile(
        db_session, actor=actor, client_id=client.id, full_name="Оновлене Імʼя"
    )
    assert client.full_name == "Оновлене Імʼя"
    count = await db_session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.action == "client_profile_updated")
    )
    assert count == 1
