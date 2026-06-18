"""Тесты bootstrap владельцев (`app/services/bootstrap.py`) — на реальном Postgres."""

from __future__ import annotations

import pytest
from app.config import get_settings
from app.db.models.audit import AuditLog
from app.db.models.enums import UserRole, UserStatus
from app.db.repositories import UserRepository
from app.services.bootstrap import ensure_owners
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
def owner_settings(monkeypatch):
    monkeypatch.setenv("OWNER_TELEGRAM_IDS", "700700")
    return get_settings()


async def _audit_count(session: AsyncSession) -> int:
    return await session.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.action == "owner_bootstrapped")
    )


async def test_ensure_owners_creates_missing(db_session: AsyncSession, owner_settings):
    owners = await ensure_owners(db_session, owner_settings)

    assert len(owners) == 1
    owner = owners[0]
    assert owner.telegram_id == 700700
    assert owner.role is UserRole.owner
    assert owner.status is UserStatus.active
    assert await _audit_count(db_session) == 1

    # bootstrap — системное действие: актора нет (user_id IS NULL).
    entry = await db_session.scalar(select(AuditLog).where(AuditLog.action == "owner_bootstrapped"))
    assert entry.user_id is None
    assert entry.affected_entity == f"user:{owner.id}"


async def test_ensure_owners_promotes_existing(db_session: AsyncSession, owner_settings):
    users = UserRepository(db_session)
    existing = await users.create(telegram_id=700700)  # client / pending по умолчанию
    assert existing.role is UserRole.client

    await ensure_owners(db_session, owner_settings)

    assert existing.role is UserRole.owner
    assert existing.status is UserStatus.active
    assert await _audit_count(db_session) == 1


async def test_ensure_owners_is_idempotent(db_session: AsyncSession, owner_settings):
    await ensure_owners(db_session, owner_settings)
    await ensure_owners(db_session, owner_settings)  # повторный запуск — без изменений

    # Второй прогон не плодит аудит-записей (никаких изменений не было).
    assert await _audit_count(db_session) == 1
