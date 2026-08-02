"""Пульс живости и healthcheck контейнера.

Тест обязан отличать «здоров» от «нездоров». Проверка, которая не умеет
покраснеть, хуже отсутствующей: она даёт зелёный кружок в `docker ps` при мёртвом
боте — то есть активно вводит в заблуждение ровно тогда, когда на неё смотрят.
"""

from __future__ import annotations

import asyncio

import pytest
from app import healthcheck
from app.utils.heartbeat import beat, heartbeat_key, run_heartbeat


@pytest.fixture
async def redis_client():
    from app.config import get_settings
    from redis.asyncio import from_url

    client = from_url(get_settings().redis_url)
    try:
        await client.ping()
    except Exception:
        pytest.skip("Redis недоступен — тест пульса пропущен")
    yield client
    await client.delete(heartbeat_key("test-proc"))
    await client.aclose()


async def test_beat_expires_on_its_own(redis_client):
    """Ключ уходит по TTL, а не по чьей-то команде.

    Именно поэтому пульс и годится в healthcheck: если event loop встал, продлевать
    некому и запись протухнет сама. Проверка, снимающая ключ явно при остановке,
    молчала бы как раз в том случае, ради которого заводится, — процесс жив, но
    ничего не делает.
    """
    await beat(redis_client, "test-proc", ttl=1)
    assert await redis_client.exists(heartbeat_key("test-proc")) == 1

    await asyncio.sleep(1.3)

    assert await redis_client.exists(heartbeat_key("test-proc")) == 0


async def test_run_heartbeat_renews_before_expiry(redis_client):
    """Живой цикл продлевает ключ, и тот не успевает протухнуть."""
    task = asyncio.create_task(run_heartbeat(redis_client, "test-proc", interval=1, ttl=2))
    try:
        await asyncio.sleep(2.5)
        assert await redis_client.exists(heartbeat_key("test-proc")) == 1
    finally:
        task.cancel()


async def test_healthcheck_red_without_pulse_green_with(redis_client, monkeypatch):
    """Главное утверждение: healthcheck УМЕЕТ покраснеть.

    Мутация: заставить `_is_alive` всегда возвращать `True` — тест на «нет пульса»
    станет зелёным, и проверка превратится в украшение.
    """
    monkeypatch.setattr(healthcheck, "_NAMES", ("test-proc",))
    await redis_client.delete(heartbeat_key("test-proc"))

    # Через поток: `main` — точка входа контейнера, внутри неё свой `asyncio.run`,
    # и звать её из работающего цикла нельзя. Проверяем именно её, а не `_is_alive`:
    # docker выполняет команду целиком, вместе с разбором аргументов и кодом выхода.
    assert await asyncio.to_thread(healthcheck.main, ["test-proc"]) == 1  # пульса нет

    await beat(redis_client, "test-proc", ttl=30)

    assert await asyncio.to_thread(healthcheck.main, ["test-proc"]) == 0  # пульс есть


def test_healthcheck_rejects_unknown_process():
    """Опечатка в имени — это код 2, а не «здоров» и не «мёртв».

    Иначе `python -m app.healthcheck bott` в compose тихо давал бы вечно красный
    контейнер, и разбирались бы с ботом, а не с YAML.
    """
    assert healthcheck.main(["ботик"]) == 2
    assert healthcheck.main([]) == 2
