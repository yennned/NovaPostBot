"""Тесты bootstrap владельцев (`app/services/bootstrap.py`) — на реальном Postgres."""

from __future__ import annotations

import pytest
from app.config import get_settings
from app.db.models.audit import AuditLog
from app.db.models.enums import ClientAccountStatus, MembershipRole, UserRole, UserStatus
from app.db.repositories import ClientAccountRepository, UserRepository
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


async def test_ensure_owners_flags_but_never_freezes_client_account(
    db_session: AsyncSession, owner_settings
):
    # Клиент в OWNER_TELEGRAM_IDS — ошибка конфига (владелец: клиент и менеджер
    # платформы не пересекаются). Повышаем — иначе владелец молча остался бы
    # клиентом; акаунт НЕ гасим — разморозка идёт через `_get_client`, который
    # 404-ит на не-клиенте, то есть заморозка была бы необратимой. Только датчик.
    users = UserRepository(db_session)
    existing = await users.create(telegram_id=700700, phone="380507000900")
    accounts = ClientAccountRepository(db_session)
    membership = await accounts.get_membership(user_id=existing.id)
    assert membership is not None and membership.role is MembershipRole.account_owner

    await ensure_owners(db_session, owner_settings)

    assert existing.role is UserRole.owner
    assert membership.account.status is ClientAccountStatus.active  # не тронут
    entry = await db_session.scalar(select(AuditLog).where(AuditLog.action == "owner_bootstrapped"))
    assert str(membership.account_id) in entry.notes


async def test_ensure_owners_promotion_without_account_stays_quiet(
    db_session: AsyncSession, owner_settings
):
    # Негативная половина датчика. Без неё условие не пришпилено ничем: заменив его
    # на `if True:`, весь сьют остаётся зелёным — то есть ложная тревога на каждом
    # легитимном повышении прошла бы незамеченной. Датчик ценен только точностью.
    users = UserRepository(db_session)
    # Менеджер → `create_for_owner` не зовётся (гейт `role is client`), членства нет.
    existing = await users.create(
        telegram_id=700700, role=UserRole.manager, status=UserStatus.blocked
    )
    assert await ClientAccountRepository(db_session).get_membership(user_id=existing.id) is None

    await ensure_owners(db_session, owner_settings)

    assert existing.role is UserRole.owner
    entry = await db_session.scalar(select(AuditLog).where(AuditLog.action == "owner_bootstrapped"))
    assert entry.notes == "повышение до владельца из OWNER_TELEGRAM_IDS"  # без пометки


async def test_ensure_owners_is_idempotent(db_session: AsyncSession, owner_settings):
    await ensure_owners(db_session, owner_settings)
    await ensure_owners(db_session, owner_settings)  # повторный запуск — без изменений

    # Второй прогон не плодит аудит-записей (никаких изменений не было).
    assert await _audit_count(db_session) == 1
