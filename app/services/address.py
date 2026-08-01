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
from app.novaposhta import methods
from app.novaposhta.cache import NPReferenceCache
from app.novaposhta.client import NovaPoshtaClient
from app.novaposhta.schemas import City, Warehouse
from app.services.sender_scope import resolve_scoped_profile


async def _profile_key(
    session: AsyncSession,
    client: User,
    sender_profile_id: uuid.UUID | None,
    account_id: uuid.UUID | None = None,
) -> str:
    """Ключ НП профиля в скоупе актора (явного/дефолтного) для вызова справочников."""
    profile = await resolve_scoped_profile(
        session, client=client, sender_profile_id=sender_profile_id, account_id=account_id
    )
    return profile.np_api_key  # EncryptedString расшифровывает при чтении


async def _key_and_limits(
    session: AsyncSession,
    client: User,
    sender_profile_id: uuid.UUID | None,
    account_id: uuid.UUID | None = None,
) -> tuple[str, dict[str, int | float]]:
    """Ключ ФОП + «интерактивные» лимиты НП для одного lookup'а.

    Зовётся ТОЛЬКО из loader'а (т.е. на промахе кэша), поэтому резолв ключа
    (запрос в БД + расшифровка Fernet) не бьёт по попаданиям в кэш. Лимиты —
    жёсткий таймаут/меньше ретраев (быстрый фейл вместо зависания).
    """
    settings = get_settings()
    api_key = await _profile_key(session, client, sender_profile_id, account_id)
    limits: dict[str, int | float] = {
        "attempts": settings.np_lookup_max_retries,
        "timeout_seconds": settings.np_lookup_timeout_seconds,
    }
    return api_key, limits


async def search_cities(
    session: AsyncSession,
    *,
    client: User,
    query: str,
    np_client: NovaPoshtaClient,
    cache: NPReferenceCache,
    sender_profile_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
) -> list[City]:
    """Найти города по подстроке (через кэш справочников НП; ключ ФОП — лениво)."""

    async def loader() -> list[City]:
        api_key, limits = await _key_and_limits(session, client, sender_profile_id, account_id)
        return await methods.get_cities(np_client, api_key=api_key, query=query, **limits)

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
    account_id: uuid.UUID | None = None,
) -> list[Warehouse]:
    """Найти відділення в городе (опц. поиск; ключ ФОП резолвим лениво в `loader`)."""

    async def loader() -> list[Warehouse]:
        api_key, limits = await _key_and_limits(session, client, sender_profile_id, account_id)
        return await methods.get_warehouses(
            np_client, api_key=api_key, city_ref=city_ref, query=query, **limits
        )

    return await cache.warehouses(city_ref, loader=loader, query=query)
