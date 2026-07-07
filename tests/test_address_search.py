"""Тесты address-search сервиса (Фаза 4, PR 7) — Postgres + fakeredis + фейковый NP."""

from __future__ import annotations

import json

import fakeredis.aioredis
import httpx
import pytest
from app.config import Settings
from app.db.models.enums import UserRole, UserStatus
from app.db.repositories import SenderProfileRepository, UserRepository
from app.novaposhta.cache import NPReferenceCache
from app.novaposhta.client import NovaPoshtaClient
from app.services import address
from app.services.exceptions import SenderProfileNotConfigured
from sqlalchemy.ext.asyncio import AsyncSession


def _np_client(routes: dict[tuple[str, str], object], calls: dict | None = None):
    settings = Settings(_env_file=None)
    settings.np_retry_backoff = 0.0

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if calls is not None:
            calls["n"] = calls.get("n", 0) + 1
        data = routes[(body["modelName"], body["calledMethod"])]
        return httpx.Response(
            200, json={"success": True, "data": data, "errors": [], "errorCodes": []}
        )

    return NovaPoshtaClient(settings=settings, transport=httpx.MockTransport(handler))


def _cache() -> NPReferenceCache:
    return NPReferenceCache(fakeredis.aioredis.FakeRedis(), settings=Settings(_env_file=None))


async def _client_with_profile(session: AsyncSession, telegram_id: int = 600):
    client = await UserRepository(session).create(
        telegram_id=telegram_id, role=UserRole.client, status=UserStatus.active
    )
    await SenderProfileRepository(session).create(
        client_id=client.id, name="ФОП", np_api_key="np-key", is_default=True
    )
    return client


async def test_search_cities_returns_and_caches(db_session: AsyncSession):
    client = await _client_with_profile(db_session)
    calls: dict = {}
    np_client = _np_client(
        {("Address", "getCities"): [{"Ref": "c1", "Description": "Київ"}]}, calls
    )
    cache = _cache()

    first = await address.search_cities(
        db_session, client=client, query="Київ", np_client=np_client, cache=cache
    )
    second = await address.search_cities(
        db_session, client=client, query="Київ", np_client=np_client, cache=cache
    )

    assert [c.ref for c in first] == ["c1"]
    assert first == second
    assert calls["n"] == 1  # второй вызов — из кэша


async def test_search_cities_cache_hit_skips_profile_lookup(db_session: AsyncSession):
    """Cache-first: попадание в кэш не резолвит ключ ФОП (нет запроса в БД).

    Прогреваем кэш клиентом с профилем, затем ищем тем же запросом клиентом БЕЗ
    профиля: холодный путь упал бы `SenderProfileNotConfigured`, но на cache hit
    ключ не резолвится, и данные отдаются из кэша.
    """
    warm = await _client_with_profile(db_session, telegram_id=610)
    cache = _cache()
    np_client = _np_client({("Address", "getCities"): [{"Ref": "c1", "Description": "Київ"}]})
    await address.search_cities(
        db_session, client=warm, query="Київ", np_client=np_client, cache=cache
    )

    no_profile = await UserRepository(db_session).create(
        telegram_id=611, role=UserRole.client, status=UserStatus.active
    )
    result = await address.search_cities(
        db_session, client=no_profile, query="Київ", np_client=np_client, cache=cache
    )
    assert [c.ref for c in result] == ["c1"]  # из кэша, без SenderProfileNotConfigured


async def test_search_warehouses_returns(db_session: AsyncSession):
    client = await _client_with_profile(db_session, telegram_id=601)
    np_client = _np_client(
        {
            ("Address", "getWarehouses"): [
                {"Ref": "w1", "Number": "5", "Description": "Відділення №5"}
            ]
        }
    )
    whs = await address.search_warehouses(
        db_session, client=client, city_ref="c1", np_client=np_client, cache=_cache()
    )
    assert whs[0].ref == "w1"
    assert whs[0].number == "5"


async def test_search_without_profile_raises(db_session: AsyncSession):
    client = await UserRepository(db_session).create(
        telegram_id=602, role=UserRole.client, status=UserStatus.active
    )
    with pytest.raises(SenderProfileNotConfigured):
        await address.search_cities(
            db_session,
            client=client,
            query="Київ",
            np_client=_np_client({("Address", "getCities"): []}),
            cache=_cache(),
        )
