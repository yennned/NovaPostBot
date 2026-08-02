"""FSM переживает редеплой бота.

Прежде хранилище было `MemoryStorage` (решение владельца от 19.06.2026), и цена
этого — три вещи сразу: каждый редеплой терял незавершённые формы ТТН, а форма это
четырнадцать экранов; вторая реплика бота была невозможна по построению; и
анти-дабл-тап `_SUBMITTING` защищал ровно одну реплику.

Тест проверяет именно то свойство, ради которого переезд и делался: состояние,
записанное одним диспетчером, видит **другой** — то есть переживает перезапуск
процесса.
"""

from __future__ import annotations

import pytest
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.fsm.storage.redis import RedisEventIsolation, RedisStorage
from app.bot.dispatcher import build_fsm_storage
from app.config import get_settings


@pytest.fixture
async def redis_client():
    from redis.asyncio import from_url

    settings = get_settings()
    client = from_url(settings.redis_url)
    try:
        await client.ping()
    except Exception:
        pytest.skip("Redis недоступен — тест про персистентность FSM пропущен")
    yield client
    await client.aclose()


def test_without_redis_falls_back_to_memory():
    """Фолбэк сохранён намеренно: на нём держатся тесты и харнесс `scripts/e2e`,
    и он же оставляет бота работоспособным, если Redis не поднялся."""
    storage, isolation = build_fsm_storage(None)

    assert isinstance(storage, MemoryStorage)
    assert isinstance(isolation, SimpleEventIsolation)


def test_with_redis_uses_redis_storage_and_isolation(redis_client):
    """Изоляция обязана быть redis-овой, а не `Simple`.

    `SimpleEventIsolation` живёт в памяти процесса: при второй реплике она снова
    стала бы per-process, то есть никакой, — а именно ради второй реплики переезд
    и затевался.
    """
    storage, isolation = build_fsm_storage(redis_client)

    assert isinstance(storage, RedisStorage)
    assert isinstance(isolation, RedisEventIsolation)


async def test_state_survives_a_restart(redis_client):
    """Незавершённая форма переживает перезапуск процесса.

    Хранилища берутся **через `build_fsm_storage`**, а не конструируются руками:
    первая версия теста звала `RedisStorage(...)` напрямую и потому переживала
    мутацию «всегда `MemoryStorage`» — проверяла Redis, а не наш выбор хранилища.
    Мутация обязана красить именно этот тест, иначе он ничего не сторожит.
    """
    key = StorageKey(bot_id=1, chat_id=424242, user_id=424242)

    before, _ = build_fsm_storage(redis_client)
    await before.set_data(key, {"cart": {"SKU-1": 3}, "step": "recipient"})

    # «Редеплой»: процесс перезапустился, хранилище собирается заново.
    after, _ = build_fsm_storage(redis_client)
    try:
        assert await after.get_data(key) == {"cart": {"SKU-1": 3}, "step": "recipient"}
    finally:
        await after.set_data(key, {})
