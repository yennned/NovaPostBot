"""Фикстуры тестов БД — на реальном Postgres (service container в CI / docker-compose локально).

Схема создаётся один раз на сессию через `Base.metadata.create_all`. Каждому тесту
выдаётся `db_session` во внешней транзакции, которая откатывается по завершении —
так тесты изолированы и не мусорят в БД.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import app.db.models  # noqa: F401 — регистрирует таблицы в Base.metadata
import pytest_asyncio
from app.db.base import Base, make_engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = make_engine()
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await eng.dispose()


@pytest_asyncio.fixture(loop_scope="session")
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    conn = await engine.connect()
    trans = await conn.begin()
    session = AsyncSession(bind=conn, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        await trans.rollback()
        await conn.close()
