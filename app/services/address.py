"""Сервис поиска адресов НП (города/відділення) для FSM создания ТТН. Без aiogram.

Тонкая обёртка: резолвит ключ ФОП клиента и ходит в справочники НП через
`NPReferenceCache` (cache-aside). `Address.*` требует валидный ключ, но **не**
требует провалидированного ФОП (Ref отправителя тут не нужен) — поэтому берём
ключ профиля как есть.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models.user import User
from app.db.repositories import SenderProfileRepository
from app.novaposhta import methods
from app.novaposhta.cache import NPReferenceCache
from app.novaposhta.client import NovaPoshtaClient
from app.novaposhta.schemas import City, Warehouse
from app.services.exceptions import SenderProfileNotConfigured


async def _profile_key(
    session: AsyncSession, client: User, sender_profile_id: uuid.UUID | None
) -> str:
    """Ключ НП профиля клиента (явного/дефолтного) для вызова справочников."""
    repo = SenderProfileRepository(session)
    if sender_profile_id is not None:
        profile = await repo.get_by_id(sender_profile_id)
        if profile is None or profile.client_id != client.id:
            raise SenderProfileNotConfigured("ФОП не знайдено")
    else:
        profile = await repo.get_default_for_client(client.id)
        if profile is None:
            raise SenderProfileNotConfigured("ФОП ще не налаштований, зверніться до менеджера")
    return profile.np_api_key  # EncryptedString расшифровывает при чтении


async def search_cities(
    session: AsyncSession,
    *,
    client: User,
    query: str,
    np_client: NovaPoshtaClient,
    cache: NPReferenceCache,
    sender_profile_id: uuid.UUID | None = None,
) -> list[City]:
    """Найти города по подстроке (через кэш справочников НП).

    Ключ ФОП резолвим ЛЕНИВО — только на промахе кэша (внутри `loader`), чтобы
    попадание в кэш не тянуло лишний запрос в БД + расшифровку ключа на каждый
    ввод. Таймаут/ретраи — «интерактивные» (быстрый фейл вместо зависания).
    """
    settings = get_settings()

    async def loader() -> list[City]:
        api_key = await _profile_key(session, client, sender_profile_id)
        return await methods.get_cities(
            np_client,
            api_key=api_key,
            query=query,
            attempts=settings.np_lookup_max_retries,
            timeout_seconds=settings.np_lookup_timeout_seconds,
        )

    return await cache.cities(query, loader=loader)


async def search_warehouses(
    session: AsyncSession,
    *,
    client: User,
    city_ref: str,
    np_client: NovaPoshtaClient,
    cache: NPReferenceCache,
    query: str | None = None,
    sender_profile_id: uuid.UUID | None = None,
) -> list[Warehouse]:
    """Найти відділення в городе (опц. поиск по номеру/строке).

    Ключ ФОП резолвим лениво (внутри `loader`) — попадание в кэш не ходит в БД.
    """
    settings = get_settings()

    async def loader() -> list[Warehouse]:
        api_key = await _profile_key(session, client, sender_profile_id)
        return await methods.get_warehouses(
            np_client,
            api_key=api_key,
            city_ref=city_ref,
            query=query,
            attempts=settings.np_lookup_max_retries,
            timeout_seconds=settings.np_lookup_timeout_seconds,
        )

    return await cache.warehouses(city_ref, loader=loader, query=query)
