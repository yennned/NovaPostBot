"""Процесс-глобальный runtime доступа к Google Sheets: один клиент + сериализация.

gspread-сессия не рассчитана на параллельные потоки, а пересоздание `SheetsClient`
на каждый вызов означает новый OAuth-handshake service-account — то есть лишний
round-trip перед КАЖДЫМ чтением склада. Держим один авторизованный клиент на процесс
и пускаем все блокирующие вызовы через выделенный executor из ОДНОГО воркера.

Один воркер — это не только защита общего клиента: он сериализует read-modify-write
по листу (на это уже опирается `create_shipment`) и заодно работает естественным
ограничителем под квоту Sheets (60 read/min на service-account, то есть на весь бот).
Плата — потолок примерно в одно обращение к Sheets за раз; при реальной конкуренции
это станет узким местом, и тогда нужен пул на N воркеров с thread-local `SheetsClient`,
а не общий клиент на всех.

Живёт в `app/sheets/`, а не в `app/services/`, чтобы им мог пользоваться и
`services/inventory.py`, и `services/client_sheet_sync.py` без цикла импортов.
"""

from __future__ import annotations

import asyncio
import functools
import threading
from concurrent.futures import ThreadPoolExecutor

from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.config import get_settings
from app.sheets.client import SheetsClient
from app.sheets.source import StockSourceUnavailable

_sheets_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sheets")
_shared_sheets_client: SheetsClient | None = None
# Ленивая инициализация зовётся и из лупа, и из потока executor'а — под локом, чтобы
# два первых обращения не создали два клиента (и два OAuth-handshake).
_client_lock = threading.Lock()


def shared_sheets_client() -> SheetsClient:
    """Единственный на процесс авторизованный `SheetsClient` (всегда `get_settings()`).

    Параметра `settings` здесь намеренно НЕТ: клиент создаётся один раз, поэтому
    настройки второго и последующих вызовов молча игнорировались бы — и вызывающий
    думал бы, что подменил конфигурацию. Кому нужна своя конфигурация (воркер,
    тесты), тот заводит собственный `SheetsClient` — см. `build_stock_source`.
    """
    global _shared_sheets_client
    with _client_lock:
        if _shared_sheets_client is None:
            _shared_sheets_client = SheetsClient(get_settings())
        return _shared_sheets_client


async def run_on_sheets_executor(fn, /, *args):
    """Выполнить блокирующий вызов Sheets на выделенном single-worker executor.

    В отличие от `asyncio.to_thread` (общий пул) медленная запись в Sheets не может
    занять воркеров, нужных остальному коду, и наоборот.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_sheets_executor, functools.partial(fn, *args))


def _retryable(exc: BaseException) -> bool:
    """Временная ли недоступность: квота (429), 5xx или неопознанный сбой транспорта."""
    if not isinstance(exc, StockSourceUnavailable):
        return False
    return exc.status is None or exc.status == 429 or exc.status >= 500


async def run_sheets_read(fn, /, *args):
    """Чтение Sheets на выделенном executor'е, с ретраями на временную недоступность.

    Ретраим ТОЛЬКО чтение. `apply_deltas` повторять нельзя: запись могла примениться
    частично, и повтор удвоил бы дельту остатка.

    Бэкофф ждём на async-стороне, а не внутри блокирующей функции: `time.sleep` в
    единственном воркере executor'а заблокировал бы весь Sheets-I/O бота на время
    ожидания.
    """
    settings = get_settings()
    async for attempt in AsyncRetrying(
        retry=retry_if_exception(_retryable),
        stop=stop_after_attempt(max(1, settings.sheets_retry_attempts)),
        wait=wait_exponential(multiplier=settings.sheets_retry_backoff, max=4),
        reraise=True,
    ):
        with attempt:
            return await run_on_sheets_executor(fn, *args)
    raise AssertionError("unreachable")  # pragma: no cover — AsyncRetrying всегда вернёт/бросит


def reset_sheets_runtime() -> None:
    """Сбросить процесс-глобальный клиент (тесты; смена конфигурации)."""
    global _shared_sheets_client
    with _client_lock:
        _shared_sheets_client = None
