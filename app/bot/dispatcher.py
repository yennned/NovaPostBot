"""Сборка aiogram Dispatcher."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.handlers import (
    client_cabinet_router,
    clients_router,
    dev_router,
    duty_router,
    errors_router,
    manager_shipments_router,
    start_router,
    ttn_router,
)
from app.bot.middlewares import EffectiveContextMiddleware, ServicesMiddleware
from app.bot.services import InMemoryDevState
from app.config import Settings
from app.db.base import get_sessionmaker

if TYPE_CHECKING:
    from app.novaposhta.cache import NPReferenceCache
    from app.novaposhta.client import NovaPoshtaClient


def build_dispatcher(
    settings: Settings,
    *,
    np_client: NovaPoshtaClient | None = None,
    np_cache: NPReferenceCache | None = None,
) -> Dispatcher:
    # FSM-хранилище — MemoryStorage (решение владельца): redis-клиент служит только
    # кэшу справочников НП, бот не зависит от Redis для FSM/`/start`.
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    services_middleware = ServicesMiddleware(
        get_sessionmaker(),
        dev_ids=frozenset(settings.dev_telegram_ids),
        dev_state=InMemoryDevState(),
        np_client=np_client,
        np_cache=np_cache,
    )
    context_middleware = EffectiveContextMiddleware()

    dp.update.outer_middleware(services_middleware)
    dp.update.outer_middleware(context_middleware)

    dp.include_router(start_router)
    dp.include_router(dev_router)
    dp.include_router(clients_router)
    dp.include_router(duty_router)
    dp.include_router(manager_shipments_router)
    dp.include_router(client_cabinet_router)
    dp.include_router(ttn_router)
    dp.include_router(errors_router)  # backstop: непрочитанный ключ ФОП (ротация FERNET_KEY)
    return dp
