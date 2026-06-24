"""Складские adapters и фабрика источника остатков."""

from app.config import Settings, get_settings
from app.sheets.client import SheetsClient
from app.sheets.inventory import (
    CrmStockSource,
    GoogleSheetsStockSource,
    InventorySheetMutator,
    InventorySheetReader,
)
from app.sheets.source import StockDelta, StockRow, StockSheetNotFound, StockSource


def build_stock_source(settings: Settings | None = None) -> StockSource:
    """Собрать источник остатков согласно `INVENTORY_SOURCE`."""
    cfg = settings or get_settings()
    if cfg.inventory_source == "crm":
        return CrmStockSource()
    return GoogleSheetsStockSource(client=SheetsClient(settings=cfg))


__all__ = [
    "CrmStockSource",
    "GoogleSheetsStockSource",
    "InventorySheetMutator",
    "InventorySheetReader",
    "SheetsClient",
    "StockDelta",
    "StockRow",
    "StockSheetNotFound",
    "StockSource",
    "build_stock_source",
]
