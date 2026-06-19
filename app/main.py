"""Точка входа бота (long polling)."""

from __future__ import annotations

import asyncio

from aiogram import Bot
from redis.asyncio import from_url as redis_from_url

from app.bot import build_dispatcher
from app.config import get_settings
from app.db.base import get_sessionmaker
from app.logging_config import configure_logging, get_logger
from app.novaposhta.cache import NPReferenceCache
from app.novaposhta.client import NovaPoshtaClient
from app.services.bootstrap import ensure_owners


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    log = get_logger("bot")
    log.info("bot.start", timezone=settings.timezone)
    if not settings.bot_token:
        log.warning("bot.token_missing")
        await asyncio.Event().wait()
        return

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        owners = await ensure_owners(session, settings)
        await session.commit()
        log.info("bot.owners_bootstrapped", count=len(owners))

    # Composition root: один NovaPoshtaClient + один redis-клиент + кэш справочников
    # НП на весь процесс (как gspread в app/sheets/). Проброс в хендлеры — через deps.
    np_client = NovaPoshtaClient(settings=settings)
    redis_client = redis_from_url(settings.redis_url)
    np_cache = NPReferenceCache(redis_client, settings=settings)

    dispatcher = build_dispatcher(settings, np_client=np_client, np_cache=np_cache)
    bot = Bot(token=settings.bot_token)
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await np_client.aclose()
        await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
