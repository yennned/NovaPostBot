"""Тесты `SupportRepository` (`support_threads` / `support_messages`) — на Postgres."""

from __future__ import annotations

from app.db.models.enums import SupportThreadStatus, UserRole, UserStatus
from app.db.repositories import SupportRepository, UserRepository
from sqlalchemy.ext.asyncio import AsyncSession


async def _client(session: AsyncSession, telegram_id: int = 100, full_name: str = "Іван Клієнт"):
    return await UserRepository(session).create(
        telegram_id=telegram_id,
        phone=f"+38050{telegram_id}",
        full_name=full_name,
        role=UserRole.client,
        status=UserStatus.active,
    )


async def _manager(session: AsyncSession, telegram_id: int = 10, full_name: str = "Олег Менеджер"):
    return await UserRepository(session).create(
        telegram_id=telegram_id,
        phone=f"+38067{telegram_id}",
        full_name=full_name,
        role=UserRole.manager,
        status=UserStatus.active,
    )


async def test_create_thread_with_messages_ordered(db_session: AsyncSession):
    repo = SupportRepository(db_session)
    client = await _client(db_session)
    manager = await _manager(db_session)

    thread = await repo.create_thread(client_id=client.id, assigned_manager_id=manager.id)
    assert thread.status is SupportThreadStatus.open

    await repo.add_message(thread_id=thread.id, sender_role="client", text="Доброго дня")
    await repo.add_message(thread_id=thread.id, sender_role="manager", text="Вітаю, слухаю")

    loaded = await repo.get_with_messages(thread.id)
    assert loaded is not None
    assert [m.text for m in loaded.messages] == ["Доброго дня", "Вітаю, слухаю"]
    assert loaded.assigned_manager is not None
    assert loaded.assigned_manager.id == manager.id


async def test_active_thread_excludes_closed(db_session: AsyncSession):
    repo = SupportRepository(db_session)
    client = await _client(db_session)

    closed = await repo.create_thread(client_id=client.id)
    await repo.close_thread(closed)
    assert closed.status is SupportThreadStatus.closed
    assert closed.closed_at is not None
    assert await repo.get_active_thread_for_client(client.id) is None

    waiting = await repo.create_thread(client_id=client.id, status=SupportThreadStatus.waiting)
    active = await repo.get_active_thread_for_client(client.id)
    assert active is not None
    assert active.id == waiting.id


async def test_list_for_manager_filters_by_assignment(db_session: AsyncSession):
    repo = SupportRepository(db_session)
    client = await _client(db_session)
    mine = await _manager(db_session, telegram_id=11, full_name="Мій Менеджер")
    other = await _manager(db_session, telegram_id=12, full_name="Інший Менеджер")

    await repo.create_thread(client_id=client.id, assigned_manager_id=mine.id)
    await repo.create_thread(client_id=client.id, assigned_manager_id=other.id)

    threads, total = await repo.list_for_manager(mine.id)
    assert total == 1
    assert all(t.assigned_manager_id == mine.id for t in threads)


async def test_list_all_search_and_status_filter(db_session: AsyncSession):
    repo = SupportRepository(db_session)
    alice = await _client(db_session, telegram_id=200, full_name="Аліса Петренко")
    bob = await _client(db_session, telegram_id=201, full_name="Богдан Сидоренко")
    manager = await _manager(db_session)

    await repo.create_thread(client_id=alice.id, assigned_manager_id=manager.id)
    queued = await repo.create_thread(client_id=bob.id, status=SupportThreadStatus.waiting)

    by_name, total_name = await repo.list_all(query="Аліса")
    assert total_name == 1
    assert by_name[0].client_id == alice.id

    by_status, total_status = await repo.list_all(statuses={SupportThreadStatus.waiting})
    assert total_status == 1
    assert by_status[0].id == queued.id


async def test_assign_manager_updates_thread(db_session: AsyncSession):
    repo = SupportRepository(db_session)
    client = await _client(db_session)
    manager = await _manager(db_session)

    thread = await repo.create_thread(client_id=client.id, status=SupportThreadStatus.waiting)
    await repo.assign_manager(thread, manager.id, status=SupportThreadStatus.open)

    assert thread.assigned_manager_id == manager.id
    assert thread.status is SupportThreadStatus.open
