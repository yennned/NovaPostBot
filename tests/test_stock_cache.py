"""Кэш чтений листа «Склад» между апдейтами (`app/sheets/cache.py`).

Проверяем ровно то, ради чего он заведён, и ровно ту границу, которую он не имеет
права переходить:

* чтения между апдейтами переиспользуются — иначе один сценарий ТТН съедает ~8
  чтений из 60/мин квоты на ВЕСЬ бот (замерено E2E-прогоном на проде);
* запись дельт и явная инвалидация сбрасывают снапшот;
* **гейт от oversell кэш не читает** — иначе он выдавал бы разрешение продать
  то, чего на складе уже нет.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.sheets import PerUpdateStockSource, TtlStockSource, invalidate_stock_cache
from app.sheets.source import StockDelta, StockRow


class _CountingSource:
    """Источник, считающий обращения и умеющий менять отдаваемый остаток."""

    def __init__(self, quantity: int = 10) -> None:
        self.reads = 0
        self.applied: list[list[StockDelta]] = []
        self.quantity = quantity

    def read_stock(self, client_key: str) -> list[StockRow]:
        self.reads += 1
        return [
            StockRow(
                sku="SKU-1",
                name="Кава",
                category="Кава",
                quantity=self.quantity,
                price=Decimal("100"),
            )
        ]

    def apply_deltas(self, client_key: str, deltas: list[StockDelta]) -> None:
        self.applied.append(deltas)


def test_second_read_within_ttl_does_not_hit_source():
    source = _CountingSource()
    cache = TtlStockSource(source, ttl_seconds=60)

    cache.read_stock("клієнт")
    cache.read_stock("клієнт")
    cache.read_stock("клієнт")

    assert source.reads == 1
    assert cache.hits == 2


def test_cache_is_per_client_key():
    """Один клиент не должен получать лист другого."""
    source = _CountingSource()
    cache = TtlStockSource(source, ttl_seconds=60)

    cache.read_stock("клієнт-A")
    cache.read_stock("клієнт-B")

    assert source.reads == 2


def test_expired_entry_is_reread():
    source = _CountingSource()
    cache = TtlStockSource(source, ttl_seconds=0.01)

    cache.read_stock("клієнт")
    import time

    time.sleep(0.02)
    cache.read_stock("клієнт")

    assert source.reads == 2


def test_zero_ttl_never_caches():
    source = _CountingSource()
    cache = TtlStockSource(source, ttl_seconds=0)

    cache.read_stock("клієнт")
    cache.read_stock("клієнт")

    assert source.reads == 2


def test_apply_deltas_drops_snapshot():
    """После записи прежний снапшот неверен — следующее чтение идёт в источник."""
    source = _CountingSource()
    cache = TtlStockSource(source, ttl_seconds=60)

    cache.read_stock("клієнт")
    cache.apply_deltas("клієнт", [StockDelta(sku="SKU-1", quantity_delta=-2)])
    cache.read_stock("клієнт")

    assert source.reads == 2
    assert source.applied == [[StockDelta(sku="SKU-1", quantity_delta=-2)]]


def test_invalidate_reaches_through_whole_chain():
    """`PerUpdateStockSource` над `TtlStockSource` — сбрасываться должны ОБА.

    Мемо апдейта тоже держит снапшот: сбросив только внешний слой, гейт
    от oversell всё равно свернул бы на устаревший лист.
    """
    source = _CountingSource()
    chain = PerUpdateStockSource(TtlStockSource(source, ttl_seconds=60))

    chain.read_stock("клієнт")
    invalidate_stock_cache("клієнт", chain)
    chain.read_stock("клієнт")

    assert source.reads == 2


def test_invalidate_is_scoped_to_key():
    source = _CountingSource()
    chain = PerUpdateStockSource(TtlStockSource(source, ttl_seconds=60))

    chain.read_stock("клієнт-A")
    chain.read_stock("клієнт-B")
    invalidate_stock_cache("клієнт-A", chain)
    chain.read_stock("клієнт-A")
    chain.read_stock("клієнт-B")

    assert source.reads == 3  # A прочитан дважды, B — один раз


@pytest.mark.parametrize("ttl", [0, 60])
def test_cache_never_changes_returned_rows(ttl):
    source = _CountingSource(quantity=7)
    cache = TtlStockSource(source, ttl_seconds=ttl)

    rows = cache.read_stock("клієнт")

    assert [(r.sku, r.quantity) for r in rows] == [("SKU-1", 7)]
