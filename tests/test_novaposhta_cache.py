"""Тесты cache-aside кэша справочников НП (PR3) — на fakeredis, без реального Redis."""

from __future__ import annotations

import fakeredis.aioredis
import pytest
from app.config import Settings
from app.novaposhta.cache import NPReferenceCache
from app.novaposhta.exceptions import NovaPoshtaUnavailable
from app.novaposhta.schemas import City, Warehouse
from redis.exceptions import RedisError


def _cache(**over) -> NPReferenceCache:
    settings = Settings(_env_file=None)
    for key, value in over.items():
        setattr(settings, key, value)
    return NPReferenceCache(fakeredis.aioredis.FakeRedis(), settings=settings)


def _counting_loader(items):
    calls = {"n": 0}

    async def loader():
        calls["n"] += 1
        return items

    return loader, calls


async def test_cities_miss_then_hit_calls_loader_once():
    cache = _cache()
    loader, calls = _counting_loader([City(ref="c1", name="Київ", area="Київська")])

    first = await cache.cities("Київ", loader=loader)
    second = await cache.cities("Київ", loader=loader)

    assert first == second == [City(ref="c1", name="Київ", area="Київська")]
    assert calls["n"] == 1  # второй вызов — из кэша, loader не дёргается


async def test_cities_key_normalized_by_case_and_spaces():
    cache = _cache()
    loader, calls = _counting_loader([City(ref="c1", name="Київ")])

    await cache.cities("Київ", loader=loader)
    await cache.cities("  київ  ", loader=loader)  # тот же ключ после нормализации

    assert calls["n"] == 1


async def test_cities_different_query_is_separate_entry():
    cache = _cache()
    loader, calls = _counting_loader([City(ref="c1", name="Київ")])

    await cache.cities("Київ", loader=loader)
    await cache.cities("Львів", loader=loader)

    assert calls["n"] == 2


async def test_warehouses_miss_then_hit_per_city_and_query():
    cache = _cache()
    loader, calls = _counting_loader(
        [Warehouse(ref="w1", number="5", description="Відділення №5", city_ref="c1")]
    )

    first = await cache.warehouses("c1", loader=loader, query="5")
    second = await cache.warehouses("c1", loader=loader, query="5")
    assert first == second
    assert calls["n"] == 1

    # другой город — отдельная запись
    await cache.warehouses("c2", loader=loader, query="5")
    assert calls["n"] == 2


async def test_warehouses_default_query_distinct_from_specific():
    cache = _cache()
    loader, calls = _counting_loader([Warehouse(ref="w1", number="1", description="№1")])

    await cache.warehouses("c1", loader=loader)  # query=None → пустой
    await cache.warehouses("c1", loader=loader, query="1")  # отдельный ключ

    assert calls["n"] == 2


async def test_ttl_is_applied_from_settings():
    cache = _cache(np_cities_ttl_seconds=123)
    redis = cache._redis
    loader, _ = _counting_loader([City(ref="c1", name="Київ")])

    await cache.cities("Київ", loader=loader)
    ttl = await redis.ttl("np:cities:київ")
    assert 0 < ttl <= 123


async def test_empty_result_is_not_cached():
    cache = _cache()
    loader, calls = _counting_loader([])  # НП отдала «нічого не знайдено»

    assert await cache.cities("Невідоме", loader=loader) == []
    assert await cache.cities("Невідоме", loader=loader) == []
    assert calls["n"] == 2  # пусто не кэшируем — не залипаем на TTL
    assert await cache._redis.get("np:cities:невідоме") is None


async def test_non_positive_ttl_disables_caching_without_error():
    cache = _cache(np_cities_ttl_seconds=0)  # «выключить кэш» через конфиг
    loader, calls = _counting_loader([City(ref="c1", name="Київ")])

    # не падаем на set(ex=0); данные отдаём, но не кэшируем
    assert await cache.cities("Київ", loader=loader) == [City(ref="c1", name="Київ")]
    assert await cache.cities("Київ", loader=loader) == [City(ref="c1", name="Київ")]
    assert calls["n"] == 2
    assert await cache._redis.get("np:cities:київ") is None


async def test_loader_error_propagates_and_nothing_cached():
    cache = _cache()

    async def boom():
        raise RuntimeError("NP down")

    with pytest.raises(RuntimeError):
        await cache.cities("Київ", loader=boom)
    # ничего не закэшировано — следующий вызов снова попытается loader
    assert await cache._redis.get("np:cities:київ") is None


# ----------------------------------------- устойчивость к падению Redis (graceful)


class _BrokenRedis:
    """Redis-двойник, недоступный на любой команде (ConnectionError/мисконфиг)."""

    async def get(self, key):
        raise RedisError("redis down")

    async def set(self, key, value, ex=None):
        raise RedisError("redis down")


class _WriteOnlyBroken:
    """Чтение — miss, запись падает (Redis прилёг между get и set)."""

    async def get(self, key):
        return None

    async def set(self, key, value, ex=None):
        raise RedisError("redis down")


async def test_cities_redis_down_falls_back_to_loader():
    cache = NPReferenceCache(_BrokenRedis(), settings=Settings(_env_file=None))
    loader, calls = _counting_loader([City(ref="c1", name="Київ")])

    # упавший Redis не должен ронять поиск — данные берём из loader (НП)
    assert await cache.cities("Київ", loader=loader) == [City(ref="c1", name="Київ")]
    assert calls["n"] == 1


async def test_warehouses_redis_down_falls_back_to_loader():
    cache = NPReferenceCache(_BrokenRedis(), settings=Settings(_env_file=None))
    loader, calls = _counting_loader([Warehouse(ref="w1", number="1", description="№1")])

    assert await cache.warehouses("c1", loader=loader) == [
        Warehouse(ref="w1", number="1", description="№1")
    ]
    assert calls["n"] == 1


async def test_cache_write_failure_is_swallowed():
    cache = NPReferenceCache(_WriteOnlyBroken(), settings=Settings(_env_file=None))
    loader, calls = _counting_loader([City(ref="c1", name="Київ")])

    # set падает, но пользователь всё равно получает данные от loader (не падаем)
    assert await cache.cities("Київ", loader=loader) == [City(ref="c1", name="Київ")]
    assert calls["n"] == 1


async def _failing_loader():
    raise NovaPoshtaUnavailable("довідник тимчасово недоступний")


async def test_warehouses_stale_fallback_filters_cached_full_list():
    """НП лёг на поиске відділення → отдаём отфильтрованный кэш полного списка."""
    cache = _cache()
    full = [
        Warehouse(ref="w1", number="1", description="Відділення №1: вул. Центральна, 104"),
        Warehouse(ref="w2", number="2", description="Відділення №2: вул. Інша, 5"),
    ]
    loader_full, _ = _counting_loader(full)
    await cache.warehouses("kyiv", loader=loader_full)  # query=None → кладём полный список

    result = await cache.warehouses("kyiv", loader=_failing_loader, query="1")

    assert [w.ref for w in result] == ["w1"]  # отфильтровано по номеру «1»


async def test_warehouses_reraises_when_no_cached_full_list():
    """Полного списка в кэше нет → транзиентную ошибку пробрасываем как есть."""
    cache = _cache()
    with pytest.raises(NovaPoshtaUnavailable):
        await cache.warehouses("kyiv", loader=_failing_loader, query="1")


async def test_warehouses_stale_fallback_returns_empty_on_no_match():
    """Нет совпадений в кэше → пустой результат, а НЕ весь список города."""
    cache = _cache()
    full = [Warehouse(ref="w2", number="2", description="Відділення №2: вул. Інша, 5")]
    loader_full, _ = _counting_loader(full)
    await cache.warehouses("kyiv", loader=loader_full)

    result = await cache.warehouses("kyiv", loader=_failing_loader, query="777")

    assert result == []  # не подсовываем неподходящие відділення
