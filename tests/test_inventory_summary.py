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


class _SelectiveSource:
    """Лист «Боб» недоступен, у остальных — одна позиция на 4 единицы."""

    def read_stock(self, client_key: str) -> list[StockRow]:
        if client_key == "Боб":
            raise RuntimeError("лист не знайдено")
        return [StockRow(sku="A", name="a", category=None, quantity=4, price=None)]


async def test_stock_summary_pairs_clients_with_totals() -> None:
    clients = [_client("Аліса", 1), _client("Боб", 2)]
    summary = await inventory.stock_summary(clients, reader=_SelectiveSource())
    assert [c.full_name for c, _ in summary] == ["Аліса", "Боб"]
    assert summary[0][1] == StockTotals(positions=1, units=4)
    assert summary[1][1] is None  # недоступный лист → None, сводка не падает
