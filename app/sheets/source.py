"""Контракты источника складских остатков."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


class StockSheetNotFound(Exception):
    """Лист склада клиента отсутствует в источнике остатков.

    Доменная обёртка над `gspread.WorksheetNotFound` — чтобы сервис-слой не
    импортировал gspread и одинаково реагировал на «нет листа» у любого источника
    (Sheets/CRM). Отсутствие листа — ожидаемое состояние (клиент ещё не заведён или
    лист переименован), а не сбой: верхний слой трактует его как пустой остаток.
    """

    def __init__(self, client_key: str) -> None:
        super().__init__(f"лист склада не найден: {client_key}")
        self.client_key = client_key


@dataclass(frozen=True, slots=True)
class StockRow:
    sku: str
    name: str
    category: str | None
    quantity: int
    price: Decimal | None


@dataclass(frozen=True, slots=True)
class StockDelta:
    sku: str
    quantity_delta: int
    name: str | None = None
    category: str | None = None
    price: Decimal | None = None


class StockSource(Protocol):
    """Источник остатков склада с read/write-операциями доменного слоя.

    Зеркалирование резерва в книгу (`GoogleSheetsStockSource.write_reserved`) — это
    вьюшка поверх Sheets, а не capability источника: оно вызывается напрямую из
    `client_sheet_sync`, не через этот seam, поэтому в протокол не входит.
    """

    def read_stock(self, client_key: str) -> list[StockRow]: ...

    def apply_deltas(self, client_key: str, deltas: list[StockDelta]) -> None: ...
