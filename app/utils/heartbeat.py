"""Пульс живости процессов бота и воркера через Redis.

**Зачем не `pgrep python`.** Проверка «процесс существует» истинна всегда, пока
контейнер жив, поэтому она не может покраснеть — а `restart: unless-stopped` и без
неё поднимает упавший контейнер. Отказ, ради которого healthcheck заводят, другой:
процесс жив, но перестал работать — long-polling залип на сокете, планировщик
воркера умер внутри, event loop заблокирован синхронным вызовом. Снаружи это
неотличимо от нормы, и именно это надо сделать видимым.

Пульс — ключ в Redis с TTL: процесс продлевает его из своего event loop, и если
loop встал, ключ протухает сам. Отдельного состояния хранить не нужно — TTL и есть
проверка свежести.

**Пульс воркера НЕ гейтится рабочими часами.** Иначе каждую ночь воркер честно
объявлял бы себя мёртвым, и «нездоров» перестало бы что-либо значить — ровно так
мониторинги и приучают себя игнорировать.
"""

from __future__ import annotations

import asyncio

import structlog
from redis.asyncio import Redis

logger = structlog.get_logger(__name__)

#: Как часто продлевать ключ.
BEAT_INTERVAL_SECONDS = 30
#: TTL ключа. Втрое больше интервала: одна пропущенная итерация (сборка мусора,
#: блип сети до Redis) не должна объявлять процесс мёртвым — иначе healthcheck
#: краснеет на шуме, а не на отказе.
BEAT_TTL_SECONDS = 90


def heartbeat_key(name: str) -> str:
    return f"hb:{name}"


async def beat(redis: Redis, name: str, *, ttl: int = BEAT_TTL_SECONDS) -> None:
    """Одно продление пульса. Ошибки Redis глотаем: пульс не важнее работы."""
    try:
        await redis.set(heartbeat_key(name), "1", ex=ttl)
    except Exception:
        logger.warning("heartbeat.failed", name=name, exc_info=True)


async def run_heartbeat(
    redis: Redis,
    name: str,
    *,
    interval: int = BEAT_INTERVAL_SECONDS,
    ttl: int = BEAT_TTL_SECONDS,
) -> None:
    """Фоновая задача пульса. Живёт столько же, сколько процесс."""
    while True:
        await beat(redis, name, ttl=ttl)
        await asyncio.sleep(interval)
