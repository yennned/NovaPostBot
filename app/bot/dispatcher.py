"""Сборка aiogram Dispatcher."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Dispatcher, Router
from aiogram.fsm.storage.base import BaseEventIsolation, BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.fsm.storage.redis import RedisEventIsolation, RedisStorage
from redis.asyncio import Redis

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


def build_fsm_storage(redis: Redis | None) -> tuple[BaseStorage, BaseEventIsolation]:
    """Хранилище FSM и изоляция апдейтов. Вынесено, чтобы это можно было проверить.

    Собрать второй `Dispatcher` в процессе нельзя — роутеры в `app/bot/handlers`
    модульные синглтоны, — поэтому тест «с Redis и без» через `build_dispatcher`
    невозможен в принципе. Развилка отвечает за то, переживёт ли незавершённая
    форма ТТН редеплой, и оставлять её непроверяемой нельзя.

    Изоляция идёт **парой** с хранилищем, а не отдельно: `SimpleEventIsolation`
    живёт в памяти процесса, и при второй реплике она снова стала бы per-process,
    то есть никакой, — а ради второй реплики переезд и затевался.
    """
    if redis is not None:
        return RedisStorage(redis), RedisEventIsolation(redis)
    return MemoryStorage(), SimpleEventIsolation()


def build_dispatcher(
    settings: Settings,
    *,
    np_client: NovaPoshtaClient | None = None,
    np_cache: NPReferenceCache | None = None,
    redis: Redis | None = None,
) -> Dispatcher:
    """Диспетчер с роутерами и мидлварями. Один на процесс: роутеры — синглтоны.

    **FSM в Redis, если redis-клиент передан.** Прежде здесь стоял `MemoryStorage`
    (решение владельца от 19.06.2026); решение изменено, потому что цена его —
    три вещи сразу: каждый редеплой терял незавершённые формы ТТН (а форма это
    четырнадцать экранов), вторая реплика бота была невозможна по построению, и
    анти-дабл-тап `_SUBMITTING` в `handlers/ttn.py` оставался множеством в памяти
    процесса, то есть защищал ровно одну реплику.

    Фолбэк на `MemoryStorage` при `redis=None` сохранён намеренно: на нём держатся
    тесты и харнесс `scripts/e2e`, которым Redis не нужен, — и он же оставляет
    бота работоспособным, если Redis не поднялся.

    `events_isolation` идёт вместе с хранилищем, а не отдельно: без него два
    быстрых тапа одного пользователя обрабатываются параллельно (`handle_as_tasks`
    у aiogram включён по умолчанию), и FSM видит гонку внутри одного диалога.
    """
    storage, isolation = build_fsm_storage(redis)
    dp = Dispatcher(storage=storage, events_isolation=isolation)
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
