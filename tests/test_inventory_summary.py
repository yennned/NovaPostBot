"""Тесты сводки склада по аккаунтам (`inventory.stock_totals/stock_summary`).

Без БД: читаем через фейковый `StockSource`, считаем позиции/единицы, проверяем
устойчивость к падению чтения листа отдельного аккаунта.

Сводка идёт по `ClientAccount`, а не по `User`: лист склада принадлежит аккаунту.
DB-регрессия на работников (они не должны давать отдельных строк) — в
`tests/test_inventory_summary_db.py`.
"""

from __future__ import annotations

import uuid

from app.db.models.client_account import ClientAccount
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


def _account(name: str = "Тест Клієнт") -> ClientAccount:
    return ClientAccount(name=name, stock_sheet_key=name)


async def test_stock_totals_counts_positions_and_units() -> None:
    rows = [
        StockRow(sku="A", name="a", category="c", quantity=3, price=None),
        StockRow(sku="B", name="b", category="c", quantity=2, price=None),
    ]
    totals = await inventory.stock_totals(_account(), reader=_FakeSource(rows))
    assert totals == StockTotals(positions=2, units=5)


async def test_stock_totals_none_on_read_error() -> None:
    totals = await inventory.stock_totals(_account(), reader=_BoomSource())
    assert totals is None


class _SelectiveSource:
    """Лист «Боб» недоступен, у остальных — одна позиция на 4 единицы."""

    def read_stock(self, client_key: str) -> list[StockRow]:
        if client_key == "Боб":
            raise RuntimeError("лист не знайдено")
        return [StockRow(sku="A", name="a", category=None, quantity=4, price=None)]


async def test_stock_summary_pairs_accounts_with_totals() -> None:
    accounts = [_account("Аліса"), _account("Боб")]
    summary = await inventory.stock_summary(accounts, reader=_SelectiveSource())
    assert [a.name for a, _ in summary] == ["Аліса", "Боб"]
    assert summary[0][1] == StockTotals(positions=1, units=4)
    assert summary[1][1] is None  # недоступный лист → None, сводка не падает


def test_stock_sheet_key_whitespace_name_falls_back_to_id() -> None:
    """Читатель и синк должны сходиться на имени из пробелов.

    Синк на таком имени берёт `account.id` (см. `test_client_sheet_sync`), а
    голый `or` пропустил бы «   » как непустую строку — читатель полез бы во
    вкладку, которой нет, и склад молча стал бы пустым.
    """
    account = ClientAccount(name="   ", stock_sheet_key=None)
    account.id = uuid.uuid4()
    assert inventory.stock_sheet_key(account) == str(account.id)

    # Обычное имя фолбэком не портится.
    named = ClientAccount(name="Магазин", stock_sheet_key=None)
    assert inventory.stock_sheet_key(named) == "Магазин"


async def test_stock_totals_reads_account_key_not_owner_name() -> None:
    """Ключ берётся из `account.stock_sheet_key`, а не из имени аккаунта.

    Иначе переименование аккаунта увело бы чтение на несуществующий лист.
    """
    seen: list[str] = []

    class _Spy:
        def read_stock(self, client_key: str) -> list[StockRow]:
            seen.append(client_key)
            return []

    account = ClientAccount(name="Нове Імʼя", stock_sheet_key="Старий Ключ")
    await inventory.stock_totals(account, reader=_Spy())
    assert seen == ["Старий Ключ"]
