"""Тесты сервиса ФОП-профилей (`app/services/sender_profile.py`) — на Postgres."""

from __future__ import annotations

import uuid

import pytest
from app.db.models.enums import OrgType, UserRole, UserStatus
from app.db.repositories import UserRepository
from app.services import sender_profile as sp
from app.services.exceptions import PermissionDenied, SenderProfileNotFound
from sqlalchemy.ext.asyncio import AsyncSession


async def _user(session: AsyncSession, telegram_id: int, role=UserRole.client):
    return await UserRepository(session).create(
        telegram_id=telegram_id, role=role, status=UserStatus.active
    )


async def test_create_first_profile_is_default(db_session: AsyncSession):
    client = await _user(db_session, 100)
    view = await sp.create_profile(
        db_session, actor=client, client_id=client.id, name="ФОП Іванов", np_api_key="np-key-1"
    )
    assert view.is_default is True
    assert view.has_api_key is True
    assert view.org_type is OrgType.fop
    # ключ наружу не отдаётся
    assert not hasattr(view, "np_api_key")


async def test_second_profile_and_set_default(db_session: AsyncSession):
    client = await _user(db_session, 101)
    first = await sp.create_profile(
        db_session, actor=client, client_id=client.id, name="ФОП-1", np_api_key="k1"
    )
    second = await sp.create_profile(
        db_session, actor=client, client_id=client.id, name="ФОП-2", np_api_key="k2"
    )
    assert first.is_default is True
    assert second.is_default is False

    promoted = await sp.set_default(db_session, actor=client, profile_id=second.id)
    assert promoted.is_default is True
    views = {v.id: v for v in await sp.list_profiles(db_session, actor=client, client_id=client.id)}
    assert views[second.id].is_default is True
    assert views[first.id].is_default is False


async def test_manager_can_manage_client_profiles(db_session: AsyncSession):
    client = await _user(db_session, 102)
    manager = await _user(db_session, 9, role=UserRole.manager)
    view = await sp.create_profile(
        db_session, actor=manager, client_id=client.id, name="ФОП", np_api_key="k"
    )
    assert view.client_id == client.id


async def test_foreign_client_denied(db_session: AsyncSession):
    client = await _user(db_session, 103)
    other = await _user(db_session, 104)
    with pytest.raises(PermissionDenied):
        await sp.create_profile(
            db_session, actor=other, client_id=client.id, name="ФОП", np_api_key="k"
        )


async def test_update_masks_api_key_in_audit(db_session: AsyncSession):
    from app.db.models.audit import AuditLog
    from sqlalchemy import select

    client = await _user(db_session, 105)
    created = await sp.create_profile(
        db_session, actor=client, client_id=client.id, name="Старе", np_api_key="k"
    )
    await sp.update_profile(
        db_session, actor=client, profile_id=created.id, name="Нове", np_api_key="new-key"
    )
    entry = await db_session.scalar(
        select(AuditLog).where(AuditLog.action == "sender_profile_updated")
    )
    assert entry.after["name"] == "Нове"
    assert entry.after["np_api_key"] == "***"  # секрет в аудит не пишем


async def test_profile_not_found(db_session: AsyncSession):
    client = await _user(db_session, 106)
    with pytest.raises(SenderProfileNotFound):
        await sp.get_profile(db_session, actor=client, profile_id=uuid.uuid4())
