"""Read-side абстракция книги «Склад» (Фаза 3)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

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
