"""Складские adapters и фабрика источника остатков."""

from contextvars import ContextVar, Token

from app.config import Settings, get_settings
from app.sheets.client import SheetsClient
from app.sheets.inventory import (
    CrmStockSource,
    GoogleSheetsStockSource,
    InventorySheetMutator,
    InventorySheetReader,
)
from app.sheets.memo import PerUpdateStockSource
from app.sheets.runtime import (
    reset_sheets_runtime,
    run_on_sheets_executor,
    run_sheets_read,
    shared_sheets_client,
)
from app.sheets.source import (
    StockDelta,
    StockRow,
    StockSheetNotFound,
    StockSource,
    StockSourceUnavailable,
)

# Источник остатков текущей операции. Ставится `ServicesMiddleware` на каждый апдейт
# (`PerUpdateStockSource`), снимается по выходу. ContextVar, а не параметр через все
# сигнатуры: до `get_inventory_snapshot` его пришлось бы тащить через ~15 хендлеров и
# сервисов, и любая пропущенная точка молча теряла бы мемоизацию. Явный `reader=`
# остаётся главным контрактом сервис-слоя и всегда побеждает — ContextVar лишь даёт
# значение по умолчанию. aiogram обрабатывает каждый апдейт отдельной задачей, поэтому
# значение естественно ограничено одним апдейтом.
_current_source: ContextVar[StockSource | None] = ContextVar("stock_source", default=None)


def use_stock_source(source: StockSource | None) -> Token:
    """Назначить источник остатков для текущей операции (вернуть токен для сброса)."""
    return _current_source.set(source)


def reset_stock_source(token: Token) -> None:
    _current_source.reset(token)


def current_stock_source(settings: Settings | None = None) -> StockSource:
    """Источник для текущей операции: per-update мемо, иначе — общий процессный."""
    return _current_source.get() or build_stock_source(settings)


def build_stock_source(settings: Settings | None = None) -> StockSource:
    """Собрать источник остатков согласно `INVENTORY_SOURCE`.

    Без явных `settings` переиспользуем процесс-глобальный `SheetsClient`: раньше
    здесь на каждый вызов создавался новый, а значит новый OAuth-handshake перед
    каждым чтением склада. С явными `settings` — отдельный клиент: расшаренный
    закэширован под `get_settings()`, и подмена конфигурации молча не сработала бы.
    """
    cfg = settings or get_settings()
    if cfg.inventory_source == "crm":
        return CrmStockSource()
    client = SheetsClient(settings=cfg) if settings is not None else shared_sheets_client()
    return GoogleSheetsStockSource(client=client)


__all__ = [
    "CrmStockSource",
    "GoogleSheetsStockSource",
    "InventorySheetMutator",
    "InventorySheetReader",
    "PerUpdateStockSource",
    "SheetsClient",
    "StockDelta",
    "StockRow",
    "StockSheetNotFound",
    "StockSource",
    "StockSourceUnavailable",
    "build_stock_source",
    "current_stock_source",
    "reset_sheets_runtime",
    "reset_stock_source",
    "run_on_sheets_executor",
    "run_sheets_read",
    "shared_sheets_client",
    "use_stock_source",
]
