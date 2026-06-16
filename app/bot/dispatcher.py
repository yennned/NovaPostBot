"""Сборка aiogram Dispatcher."""

from __future__ import annotations

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.handlers import dev_router, start_router
from app.bot.middlewares import EffectiveContextMiddleware, ServicesMiddleware
from app.bot.services import InMemoryDevState
from app.config import Settings
from app.db.base import get_sessionmaker


def build_dispatcher(settings: Settings) -> Dispatcher:
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    services_middleware = ServicesMiddleware(
        get_sessionmaker(),
        dev_ids=frozenset(settings.dev_telegram_ids),
        dev_state=InMemoryDevState(),
    )
    context_middleware = EffectiveContextMiddleware()

    dp.update.outer_middleware(services_middleware)
    dp.update.outer_middleware(context_middleware)

    dp.include_router(start_router)
    dp.include_router(dev_router)
    return dp
