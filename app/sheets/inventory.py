"""Абстракции книги «Склад»: чтение и адресные корректировки остатков."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.sheets.client import SheetsClient

_SKU_KEYS = ("артикул", "sku")
_NAME_KEYS = ("назва", "name")
_CATEGORY_KEYS = ("категорія", "category")
_QUANTITY_KEYS = ("кількість", "quantity", "qty")
_PRICE_KEYS = ("ціна", "price")


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


class InventorySheetReader:
    """Тонкий адаптер над `SheetsClient` с нормализацией строк листа."""

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


class InventorySheetMutator:
    """Тонкий writer поверх листа «Склад».

    Изменяет только колонку количества и при необходимости добавляет новую строку
    для возврата/ручной корректировки товара, которого ещё нет в листе.
    """

    def __init__(self, client: SheetsClient | None = None) -> None:
        self.client = client or SheetsClient()

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


def _column_index(headers: list[str], aliases: tuple[str, ...]) -> int:
    for alias in aliases:
        if alias in headers:
            return headers.index(alias) + 1
    raise ValueError(f"column not found: {aliases[0]}")
