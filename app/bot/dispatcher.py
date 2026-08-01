"""Сборка aiogram Dispatcher."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Dispatcher, Router
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.handlers import (
    account_team_router,
    analytics_router,
    client_cabinet_router,
    clients_router,
    dev_router,
    duty_router,
    errors_router,
    fallback_router,
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


#: Порядок подключения роутеров — часть контракта, а не оформление.
#:
#: * `menu_escape` **первым**: тап кнопки нижней панели снимает FSM-стейт брошенного
#:   сценария и уходит дальше (`SkipHandler`). Хендлеры со свободным текстом
#:   дополнительно исключают `MENU_TEXTS` — `raw_state` для фильтров вычисляется до
#:   того, как этот роутер очистит состояние (см. `handlers/menu_escape.py`).
#: * `fallback` **предпоследним**: отвечает на callback, который не подобрал ни один
#:   хендлер выше. Без него такой тап уходит в тишину, а Telegram крутит спиннер на
#:   кнопке — самая частая жалоба «кнопка зависла».
#: * `errors` **строго последним**: внутри обработчик без фильтра на любое
#:   неожиданное исключение; роутер, добавленный после, остался бы без него.
#:
#: Вынесено отдельной константой, чтобы порядок можно было проверить тестом, не
#: собирая `Dispatcher`: роутеры — модульные синглтоны, и второй `build_dispatcher`
#: в том же процессе падает с «Router is already attached».
ROUTER_ORDER: tuple[Router, ...] = (
    menu_escape_router,
    start_router,
    dev_router,
    clients_router,
    duty_router,
    manager_shipments_router,
    support_router,
    staff_router,
    reports_router,
    analytics_router,
    account_team_router,
    client_cabinet_router,
    ttn_router,
    fallback_router,
    errors_router,
)


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

    for router in ROUTER_ORDER:
        dp.include_router(router)
    return dp
