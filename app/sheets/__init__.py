"""Складские adapters и фабрика источника остатков."""

from contextvars import ContextVar, Token

from app.config import Settings, get_settings
from app.sheets.cache import TtlStockSource
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
    shared_stock_cache,
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

    Без явных `settings` переиспользуем процесс-глобальный `SheetsClient` и общий
    кэш чтений (`TtlStockSource`): раньше здесь на каждый вызов создавался новый
    клиент, а значит новый OAuth-handshake перед каждым чтением склада. С явными
    `settings` — отдельный клиент и БЕЗ общего кэша: расшаренные завязаны на
    `get_settings()`, и подмена конфигурации молча не сработала бы.
    """
    cfg = settings or get_settings()
    if cfg.inventory_source == "crm":
        return CrmStockSource()
    if settings is not None:
        return GoogleSheetsStockSource(client=SheetsClient(settings=cfg))

    ttl = cfg.stock_cache_ttl_seconds
    if ttl <= 0:
        return GoogleSheetsStockSource(client=shared_sheets_client())
    return shared_stock_cache(
        lambda: TtlStockSource(
            GoogleSheetsStockSource(client=shared_sheets_client()), ttl_seconds=ttl
        )
    )


def invalidate_stock_cache(client_key: str, source: StockSource | None = None) -> None:
    """Заставить следующее чтение склада пойти в Google, минуя все кэши.

    Нужна ровно одному месту — гейту от oversell в `create_shipment`: решение
    «хватает ли остатка» обязано приниматься по свежему листу, иначе кэш экранов
    превратился бы в разрешение продать чужое. Идёт по всей цепочке обёрток
    (`PerUpdateStockSource` → `TtlStockSource`), потому что мемо апдейта тоже
    держит снапшот.
    """
    current = source or _current_source.get()
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        invalidate = getattr(current, "invalidate", None)
        if callable(invalidate):
            invalidate(client_key)
        current = getattr(current, "_source", None)


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
    "TtlStockSource",
    "build_stock_source",
    "current_stock_source",
    "invalidate_stock_cache",
    "reset_sheets_runtime",
    "reset_stock_source",
    "run_on_sheets_executor",
    "run_sheets_read",
    "shared_sheets_client",
    "shared_stock_cache",
    "use_stock_source",
]
