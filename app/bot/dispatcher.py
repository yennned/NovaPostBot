"""Сборка aiogram Dispatcher."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.handlers import (
    account_team_router,
    analytics_router,
    client_cabinet_router,
    clients_router,
    dev_router,
    duty_router,
    errors_router,
    manager_shipments_router,
    menu_escape_router,
    reports_router,
    staff_router,
    start_router,
    support_router,
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

    # Первым: тап кнопки нижней панели снимает FSM-стейт брошенного сценария и
    # уходит дальше (SkipHandler). Хендлеры со свободным текстом дополнительно
    # исключают MENU_TEXTS — см. app/bot/handlers/menu_escape.py.
    dp.include_router(menu_escape_router)
    dp.include_router(start_router)
    dp.include_router(dev_router)
    dp.include_router(clients_router)
    dp.include_router(duty_router)
    dp.include_router(manager_shipments_router)
    dp.include_router(support_router)
    dp.include_router(staff_router)
    dp.include_router(reports_router)
    dp.include_router(analytics_router)
    dp.include_router(account_team_router)
    dp.include_router(client_cabinet_router)
    dp.include_router(ttn_router)
    dp.include_router(errors_router)  # backstop: ключ ФОП (FERNET_KEY) + «message is not modified»
    return dp
