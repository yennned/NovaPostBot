"""Google Sheets-реализация источника склада и совместимые алиасы Phase 3/5."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.sheets.client import SheetsClient
from app.sheets.source import StockDelta, StockRow

_SKU_KEYS = ("артикул", "sku")
_NAME_KEYS = ("назва", "name")
_CATEGORY_KEYS = ("категорія", "category")
_QUANTITY_KEYS = ("кількість", "quantity", "qty")
_PRICE_KEYS = ("ціна", "price")


def _lookup(row: dict[str, object], *aliases: str) -> str | None:
    lowered = {str(key).strip().lower(): value for key, value in row.items()}
    for alias in aliases:
        value = lowered.get(alias)
        if value not in (None, ""):
            return str(value).strip()
    return None


def _parse_quantity(raw: str | None) -> int:
    if not raw:
        return 0
    normalized = raw.replace(" ", "").replace(",", ".")
    return int(float(normalized))


def _parse_price(raw: str | None) -> Decimal | None:
    if not raw:
        return None
    return Decimal(raw.replace(" ", "").replace(",", "."))


class GoogleSheetsStockSource:
    """Рабочий источник склада поверх Google Sheets."""

    def __init__(self, client: SheetsClient | None = None) -> None:
        self.client = client or SheetsClient()

    def read_stock(self, client_key: str) -> list[StockRow]:
        rows = self.client.read_rows(client_key)
        result: list[StockRow] = []
        for row in rows:
            sku = _lookup(row, *_SKU_KEYS)
            name = _lookup(row, *_NAME_KEYS)
            if not sku or not name:
                continue
            result.append(
                StockRow(
                    sku=sku,
                    name=name,
                    category=_lookup(row, *_CATEGORY_KEYS),
                    quantity=_parse_quantity(_lookup(row, *_QUANTITY_KEYS)),
                    price=_parse_price(_lookup(row, *_PRICE_KEYS)),
                )
            )
        return result

    def apply_deltas(self, client_key: str, deltas: list[StockDelta]) -> None:
        if not deltas:
            return

        worksheet = self.client.get_stock_worksheet(client_key)
        rows = list(worksheet.get_all_records(default_blank=""))
        headers = [str(header).strip().lower() for header in worksheet.row_values(1)]
        quantity_col = _column_index(headers, _QUANTITY_KEYS)

        indexed: dict[str, tuple[int, dict[str, Any]]] = {}
        for offset, row in enumerate(rows, start=2):
            sku = _lookup(row, *_SKU_KEYS)
            if sku:
                indexed[sku] = (offset, row)

        for delta in deltas:
            current = indexed.get(delta.sku)
            if current is None:
                if delta.quantity_delta < 0:
                    raise ValueError(f"sku {delta.sku} not found in stock sheet")
                worksheet.append_row(
                    [
                        delta.sku,
                        delta.name or delta.sku,
                        delta.category or "",
                        delta.quantity_delta,
                        str(delta.price) if delta.price is not None else "",
                    ]
                )
                continue

            row_index, row = current
            before = _parse_quantity(_lookup(row, *_QUANTITY_KEYS))
            after = before + delta.quantity_delta
            if after < 0:
                raise ValueError(f"sku {delta.sku} would become negative")
            worksheet.update_cell(row_index, quantity_col, after)
            row["кількість"] = str(after)


class CrmStockSource:
    """Заглушка под будущий REST-источник CRM/WMS.

    Phase 7 добавляет seam и переключатель конфигурации, но не реальную
    интеграцию. Явная ошибка безопаснее «тихого» fallback в Sheets.
    """

    def read_stock(self, client_key: str) -> list[StockRow]:
        raise RuntimeError(
            "INVENTORY_SOURCE=crm ще не реалізовано: Phase 7 додає тільки контракт інтеграції"
        )

    def apply_deltas(self, client_key: str, deltas: list[StockDelta]) -> None:
        raise RuntimeError(
            "INVENTORY_SOURCE=crm ще не реалізовано: Phase 7 додає тільки контракт інтеграції"
        )


class InventorySheetReader(GoogleSheetsStockSource):
    """Совместимый alias для read-side старых импортов."""


class InventorySheetMutator(GoogleSheetsStockSource):
    """Совместимый alias для write-side старых импортов."""


def _column_index(headers: list[str], aliases: tuple[str, ...]) -> int:
    for alias in aliases:
        if alias in headers:
            return headers.index(alias) + 1
    raise ValueError(f"column not found: {aliases[0]}")
