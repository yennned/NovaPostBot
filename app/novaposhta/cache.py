"""Redis-кэш справочников НП (`Address.*`).

Cache-aside: на miss зовём `loader` (обращение к API НП), кладём в Redis с TTL;
на hit отдаём из кэша. Первое использование Redis в проекте — клиент живёт
здесь (как gspread в `app/sheets/`), сервисы видят только `NPReferenceCache`.

Справочники городов/відділень от ключа ФОП не зависят (любой валидный ключ даёт
тот же список), поэтому ключи кэша — общие, без `api_key`.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import TYPE_CHECKING

from app.config import Settings, get_settings
from app.novaposhta.schemas import City, Warehouse

if TYPE_CHECKING:
    from redis.asyncio import Redis


def _norm(value: str) -> str:
    """Нормализовать часть ключа кэша (регистр/пробелы не плодят дубли)."""
    return value.strip().lower()


class NPReferenceCache:
    """Cache-aside поверх `redis.asyncio.Redis` для городов и відділень."""

    def __init__(self, redis: Redis, *, settings: Settings | None = None) -> None:
        self._redis = redis
        settings = settings or get_settings()
        self._cities_ttl = settings.np_cities_ttl_seconds
        self._warehouses_ttl = settings.np_warehouses_ttl_seconds

    async def cities(
        self, query: str, *, loader: Callable[[], Awaitable[list[City]]]
    ) -> list[City]:
        """Города по подстроке: из кэша или через `loader` (с записью в кэш)."""
        key = f"np:cities:{_norm(query)}"
        cached = await self._redis.get(key)
        if cached is not None:
            return [City(**row) for row in json.loads(cached)]
        cities = await loader()
        await self._store(key, cities, self._cities_ttl)
        return cities

    async def warehouses(
        self,
        city_ref: str,
        *,
        loader: Callable[[], Awaitable[list[Warehouse]]],
        query: str | None = None,
    ) -> list[Warehouse]:
        """Відділення города (опц. поиск): из кэша или через `loader`."""
        key = f"np:wh:{city_ref}:{_norm(query or '')}"
        cached = await self._redis.get(key)
        if cached is not None:
            return [Warehouse(**row) for row in json.loads(cached)]
        warehouses = await loader()
        await self._store(key, warehouses, self._warehouses_ttl)
        return warehouses

    async def _store(self, key: str, items: list, ttl: int) -> None:
        """Записать в кэш, но не залипать на ошибках/мисконфиге.

        - Пустой результат **не** кэшируем: НП мог отдать `[]` на блипе/слишком
          узком запросе (это не исключение), иначе «ничего не знайдено»
          залипло бы на весь TTL.
        - `ttl ≤ 0` (выключенный кэш через конфиг) — пропускаем `set`, иначе
          Redis бросил бы `invalid expire time` уже **после** успешного loader.
        """
        if items and ttl > 0:
            await self._redis.set(key, _dump(items), ex=ttl)


def _dump(items: list) -> str:
    """Сериализовать список dataclass'ов в JSON для Redis."""
    return json.dumps([asdict(item) for item in items], ensure_ascii=False)
