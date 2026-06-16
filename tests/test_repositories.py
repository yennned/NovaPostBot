"""Тесты репозиториев на реальном Postgres (фикстура `db_session`)."""

from __future__ import annotations

import uuid

from app.db.models.enums import OrgType, UserRole, UserStatus
from app.db.repositories import (
    AuditRepository,
    SenderProfileRepository,
    UserRepository,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def test_user_create_and_lookup(db_session: AsyncSession):
    repo = UserRepository(db_session)
    user = await repo.create(telegram_id=111, full_name="Іван")

    assert isinstance(user.id, uuid.UUID)
    assert user.role is UserRole.client  # дефолт
    assert user.status is UserStatus.pending  # дефолт
    assert user.permissions == {}

    assert await repo.get_by_telegram_id(111) is user
    assert await repo.get_by_id(user.id) is user
    assert await repo.get_by_telegram_id(999) is None


async def test_user_status_role_permissions_duty(db_session: AsyncSession):
    repo = UserRepository(db_session)
    user = await repo.create(telegram_id=222)

    await repo.update_status(user, UserStatus.active)
    await repo.update_role(user, UserRole.manager)
    await repo.set_permissions(user, {"can_edit_clients": True})
    await repo.set_duty(user, on_duty=True)

    assert user.status is UserStatus.active
    assert user.role is UserRole.manager
    assert user.permissions == {"can_edit_clients": True}
    assert user.on_duty is True

    managers = await repo.list_by_role(UserRole.manager)
    assert user in managers


async def test_sender_profile_encrypts_api_key_in_db(db_session: AsyncSession):
    users = UserRepository(db_session)
    profiles = SenderProfileRepository(db_session)
    client = await users.create(telegram_id=333)

    raw_key = "secret-np-key-xyz"
    profile = await profiles.create(
        client_id=client.id, name="ФОП Іванов", np_api_key=raw_key, org_type=OrgType.fop
    )

    # Через ORM читается открытый ключ.
    assert profile.np_api_key == raw_key
    # В самой БД хранится шифртекст (читаем сырое значение мимо TypeDecorator).
    stored = await db_session.scalar(
        text("SELECT np_api_key FROM sender_profiles WHERE id = :id"),
        {"id": profile.id},
    )
    assert stored != raw_key
    assert raw_key not in stored


async def test_sender_profile_set_default_is_exclusive(db_session: AsyncSession):
    users = UserRepository(db_session)
    profiles = SenderProfileRepository(db_session)
    client = await users.create(telegram_id=444)

    first = await profiles.create(
        client_id=client.id, name="ФОП-1", np_api_key="k1", is_default=True
    )
    second = await profiles.create(
        client_id=client.id, name="ФОП-2", np_api_key="k2", is_default=True
    )

    await db_session.refresh(first)
    assert first.is_default is False  # флаг снят с предыдущего
    assert second.is_default is True

    default = await profiles.get_default_for_client(client.id)
    assert default is second
    assert len(await profiles.list_for_client(client.id)) == 2


async def test_audit_log_append(db_session: AsyncSession):
    users = UserRepository(db_session)
    audit = AuditRepository(db_session)
    actor = await users.create(telegram_id=555)

    entry = await audit.log(
        "user_activated",
        user_id=actor.id,
        affected_entity=f"user:{actor.id}",
        before={"status": "pending"},
        after={"status": "active"},
    )

    assert isinstance(entry.id, uuid.UUID)
    assert entry.action == "user_activated"
    assert entry.before == {"status": "pending"}
    assert entry.after == {"status": "active"}
    assert entry.created_at is not None
