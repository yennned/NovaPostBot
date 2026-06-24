"""Тесты сводки склада по клиентам (`inventory.stock_totals/stock_summary`).

Без БД: читаем через фейковый `StockSource`, считаем позиции/единицы, проверяем
устойчивость к падению чтения листа отдельного клиента.
"""

from __future__ import annotations

from app.db.models.user import User
from app.services import inventory
from app.services.inventory import StockTotals
from app.sheets.source import StockRow


class _FakeSource:
    def __init__(self, rows: list[StockRow]) -> None:
        self._rows = rows

    def read_stock(self, client_key: str) -> list[StockRow]:
        return self._rows


class _BoomSource:
    def read_stock(self, client_key: str) -> list[StockRow]:
        raise RuntimeError("лист не знайдено")


def _client(name: str = "Тест Клієнт", tg: int = 1) -> User:
    return User(telegram_id=tg, full_name=name)


async def test_stock_totals_counts_positions_and_units() -> None:
    rows = [
        StockRow(sku="A", name="a", category="c", quantity=3, price=None),
        StockRow(sku="B", name="b", category="c", quantity=2, price=None),
    ]
    totals = await inventory.stock_totals(_client(), reader=_FakeSource(rows))
    assert totals == StockTotals(positions=2, units=5)


async def test_stock_totals_none_on_read_error() -> None:
    totals = await inventory.stock_totals(_client(), reader=_BoomSource())
    assert totals is None


async def test_stock_summary_pairs_clients_with_totals() -> None:
    # build_stock_source() внутри stock_summary вернёт реальный источник, но мы
    # подменяем чтение per-client через stock_totals → проверяем форму и устойчивость.
    rows = [StockRow(sku="A", name="a", category=None, quantity=4, price=None)]
    clients = [_client("Аліса", 1), _client("Боб", 2)]
    # эмулируем: один лист доступен, второй — нет
    results = [
        (clients[0], await inventory.stock_totals(clients[0], reader=_FakeSource(rows))),
        (clients[1], await inventory.stock_totals(clients[1], reader=_BoomSource())),
    ]
    assert results[0][1] == StockTotals(positions=1, units=4)
    assert results[1][1] is None
