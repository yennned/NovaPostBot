"""Фикстуры тестов БД — на реальном Postgres (service container в CI / docker-compose локально).

Схема создаётся один раз на сессию через `Base.metadata.create_all`. Каждому тесту
выдаётся `db_session` во внешней транзакции, которая откатывается по завершении —
так тесты изолированы и не мусорят в БД.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import app.db.models  # noqa: F401 — регистрирует таблицы в Base.metadata
import pytest
import pytest_asyncio
from app.config import get_settings
from app.db.base import Base, make_engine
from app.utils import crypto
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

_TEST_FERNET_KEY = "F4px_xx3G1x9XlQf4q56ubgtVdRNB4RBET5nyqcGF_s="


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """`get_settings` кеширован — сбрасываем кеш вокруг каждого теста, чтобы
    фикстуры с `monkeypatch.setenv` видели свежие значения окружения."""
    original_fernet_key = os.environ.get("FERNET_KEY")
    if not original_fernet_key:
        os.environ["FERNET_KEY"] = _TEST_FERNET_KEY
    get_settings.cache_clear()
    crypto._fernet.cache_clear()
    yield
    if original_fernet_key is None:
        os.environ.pop("FERNET_KEY", None)
    else:
        os.environ["FERNET_KEY"] = original_fernet_key
    get_settings.cache_clear()
    crypto._fernet.cache_clear()


def _assert_safe_test_database(url: str) -> None:
    if not url.strip():
        raise RuntimeError("DATABASE_URL is empty. Configure a dedicated test database first.")
    database = (make_url(url).database or "").lower()
    if "test" in database:
        return
    if os.getenv("PYTEST_ALLOW_DB_RESET") == "1":
        return
    raise RuntimeError(
        "Refusing to reset a non-test database. "
        "Use a *_test database or set PYTEST_ALLOW_DB_RESET=1 explicitly."
    )


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    _assert_safe_test_database(get_settings().database_url)
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
    # join_transaction_mode="create_savepoint": session.commit() внутри теста
    # освобождает savepoint, а не внешнюю транзакцию — изоляция сохраняется,
    # хотя хендлеры коммитят (commit-before-notify).
    session = AsyncSession(
        bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    try:
        yield session
    finally:
        await session.close()
        await trans.rollback()
        await conn.close()
