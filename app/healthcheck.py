"""`python -m app.healthcheck bot|worker` — healthcheck контейнера.

Отдельный модуль, а не однострочник в YAML: команду можно прогнать руками при
разборе инцидента и покрыть тестом, а кавычки внутри `docker-compose.yml` ничего
не экранируют молча.

Код возврата 0 — пульс свежий; 1 — протух или Redis недоступен. Второе тоже
«нездоров»: без Redis у бота нет ни FSM, ни кэша справочников НП, то есть он не
может обслуживать людей, даже если процесс жив.

**Compose сам по нездоровью не перезапускает** (это делает только Swarm), поэтому
проверка здесь — не самолечение, а видимость: `docker ps` показывает состояние, а
деплой может дождаться `healthy` вместо «контейнер стартовал».
"""

from __future__ import annotations

import asyncio
import sys

from redis.asyncio import from_url as redis_from_url

from app.config import get_settings
from app.utils.heartbeat import heartbeat_key

_NAMES = ("bot", "worker")


async def _is_alive(name: str) -> bool:
    settings = get_settings()
    redis = redis_from_url(settings.redis_url)
    try:
        return bool(await redis.exists(heartbeat_key(name)))
    except Exception:
        return False
    finally:
        await redis.aclose()


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1 or args[0] not in _NAMES:
        print(f"usage: python -m app.healthcheck {{{'|'.join(_NAMES)}}}", file=sys.stderr)
        return 2
    return 0 if asyncio.run(_is_alive(args[0])) else 1


if __name__ == "__main__":  # pragma: no cover — точка входа контейнера
    raise SystemExit(main())
