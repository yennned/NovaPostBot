"""Контракты источника складских остатков."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


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
    """Источник остатков склада с read/write-операциями доменного слоя."""

    def read_stock(self, client_key: str) -> list[StockRow]: ...

    def apply_deltas(self, client_key: str, deltas: list[StockDelta]) -> None: ...
