"""Экран склада не читает склад целиком.

Два места, и оба стоили одного тапа кнопки:

- **сводка менеджера «📦 Склад»** ходила за остатком по аккаунту в цикле: на 20
  аккаунтах это 20 последовательных чтений и две трети минутной квоты Google;
- **список товаров** читал весь остаток и резал страницу в Python: у аккаунта с
  1636 позициями каждый тап пагинации стоил полной выгрузки ради восьми строк.

Проверяем не «числа сошлись» — они сходились и раньше, — а сколько строк экран
втянул и сколько запросов сделал. Иначе регрессия «снова читаем всё» проходит
мимо тестов целиком.
"""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal

import pytest
from app.config import get_settings
from app.db.models.enums import UserRole, UserStatus
from app.db.models.stock_balance import StockBalance
from app.db.repositories import StockBalanceRepository, UserRepository
from app.services import inventory
from app.sheets.source import StockRow
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import account_of

_POSITIONS = 40


@pytest.fixture
def pg_source(monkeypatch):
    monkeypatch.setenv("INVENTORY_SOURCE", "pg")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@contextmanager
def count_loaded_balances(session: AsyncSession):
    """Сколько строк остатка стало ORM-объектами за время блока.

    По identity map считать нельзя: она держит слабые ссылки, и строки, из
    которых экран собрал свои DTO, успевают исчезнуть до проверки.
    """
    loaded: list[StockBalance] = []

    def _track(_session, instance) -> None:
        if isinstance(instance, StockBalance):
            loaded.append(instance)

    sync_session = session.sync_session
    event.listen(sync_session, "loaded_as_persistent", _track)
    try:
        yield loaded
    finally:
        event.remove(sync_session, "loaded_as_persistent", _track)


async def _account_with_stock(session: AsyncSession, telegram_id: int, positions: int):
    user = await UserRepository(session).create(
        telegram_id=telegram_id,
        full_name=f"Клієнт {telegram_id}",
        role=UserRole.client,
        status=UserStatus.active,
    )
    account = await account_of(session, user)
    repo = StockBalanceRepository(session)
    for i in range(positions):
        balance = await repo.upsert_meta(
            account_id=account.id,
            sku=f"SKU-{i:03d}",
            name=f"Товар {i:03d}",
            category="Кава" if i % 2 else "Чай",
            price=Decimal("100"),
        )
        balance.quantity = 10
    await session.flush()
    session.expunge_all()
    return user, account


async def test_list_inventory_loads_only_the_page(db_session: AsyncSession, pg_source):
    """Страница из восьми строк обязана стоить восьми строк, а не всего склада."""
    client, account = await _account_with_stock(db_session, 5000, _POSITIONS)

    with count_loaded_balances(db_session) as loaded:
        page = await inventory.list_inventory(
            db_session, client=client, account=account, account_id=account.id, limit=8, offset=0
        )

    assert page.total == _POSITIONS, "общее число позиций считается по всему остатку"
    assert len(page.items) == 8
    assert len(loaded) == 8, (
        f"экран втянул {len(loaded)} строк ради восьми на странице — "
        "пагинация снова режется в Python"
    )


async def test_search_and_category_filter_run_in_sql(db_session: AsyncSession, pg_source):
    """Фильтр тоже уходит в источник: иначе он и есть та самая полная выгрузка."""
    client, account = await _account_with_stock(db_session, 5100, _POSITIONS)

    with count_loaded_balances(db_session) as loaded:
        page = await inventory.list_inventory(
            db_session,
            client=client,
            account=account,
            account_id=account.id,
            category="кава",
            limit=5,
            offset=0,
        )

    assert page.total == _POSITIONS // 2, "категория отбирает половину позиций"
    assert len(page.items) == 5
    assert {item.category for item in page.items} == {"Кава"}
    assert len(loaded) == 5, f"фильтр посчитан в Python: загружено {len(loaded)} строк"
    assert page.categories == ["Кава", "Чай"], (
        "кнопки категорий считаются по всему остатку, иначе выбор одной убирает остальные"
    )


async def test_categories_survive_a_search(db_session: AsyncSession, pg_source):
    """Поиск сужает список позиций, но не список кнопок категорий."""
    client, account = await _account_with_stock(db_session, 5200, _POSITIONS)

    page = await inventory.list_inventory(
        db_session, client=client, account=account, account_id=account.id, query="SKU-001", limit=8
    )

    assert [item.sku for item in page.items] == ["SKU-001"]
    assert page.categories == ["Кава", "Чай"]


async def test_stock_summary_is_one_query_for_all_accounts(db_session: AsyncSession, pg_source):
    """Сводка по N аккаунтам — один поход в остаток, а не N.

    Ради этого вся развилка и поднималась: на Sheets экран стоил чтения на книгу,
    и цикл жил в сервисе, приколачивая поведение Google ко всем бэкендам сразу.
    """
    accounts = []
    for i in range(4):
        _, account = await _account_with_stock(db_session, 5300 + i * 10, 3)
        accounts.append(account)

    statements: list[str] = []
    sync_conn = (await db_session.connection()).sync_connection

    def _track(conn, cursor, statement, *args) -> None:
        if "stock_balances" in statement:
            statements.append(statement)

    event.listen(sync_conn, "before_cursor_execute", _track)
    try:
        summary = await inventory.stock_summary(db_session, accounts)
    finally:
        event.remove(sync_conn, "before_cursor_execute", _track)

    assert [totals.positions for _, totals in summary] == [3, 3, 3, 3]
    assert [totals.units for _, totals in summary] == [30, 30, 30, 30]
    assert len(statements) == 1, (
        f"сводка сделала {len(statements)} запросов на {len(accounts)} аккаунта(ов) — "
        "цикл по аккаунтам вернулся"
    )


async def test_account_without_stock_reads_as_empty_not_broken(db_session: AsyncSession, pg_source):
    """Пустой склад — это нули, а не «недоступно».

    `GROUP BY` такой аккаунт не возвращает вовсе, и без подстановки нулей экран
    показал бы сбой там, где просто ничего не заведено.
    """
    user = await UserRepository(db_session).create(
        telegram_id=5400, full_name="Порожній", role=UserRole.client, status=UserStatus.active
    )
    account = await account_of(db_session, user)

    summary = await inventory.stock_summary(db_session, [account])

    assert summary[0][1] == inventory.StockTotals(positions=0, units=0)


async def test_both_backends_order_the_page_the_same(db_session: AsyncSession):
    """Порядок страницы не должен зависеть от источника.

    Сортировка живёт в двух местах — `ORDER BY` в SQL и ключ на Python для Sheets.
    Если они разъедутся, переключение `INVENTORY_SOURCE` молча перетасует
    пагинацию под пользователем: на второй странице окажется не то, что было.
    """
    rows = [
        StockRow(sku="B-2", name="бета", category="Чай", quantity=1, price=None),
        StockRow(sku="A-1", name="Альфа", category=None, quantity=2, price=None),
        StockRow(sku="C-3", name="ГАМА", category="кава", quantity=3, price=None),
        StockRow(sku="D-4", name="альфа", category=None, quantity=4, price=None),
    ]

    user = await UserRepository(db_session).create(
        telegram_id=5500, full_name="Клієнт", role=UserRole.client, status=UserStatus.active
    )
    account = await account_of(db_session, user)
    repo = StockBalanceRepository(db_session)
    for row in rows:
        await repo.upsert_meta(
            account_id=account.id, sku=row.sku, name=row.name, category=row.category
        )
    await db_session.flush()

    class _Source:
        def read_stock(self, client_key: str) -> list[StockRow]:
            return list(rows)

    from app.services.inventory_backend import PgInventoryBackend, SheetsInventoryBackend

    pg_page = await PgInventoryBackend().read_page(
        db_session, account, query=None, category=None, limit=10, offset=0
    )
    sheets_page = await SheetsInventoryBackend(_Source()).read_page(
        db_session, account, query=None, category=None, limit=10, offset=0
    )

    # Порядок пришпилен явно, а не только сравнением бэкендов между собой: два
    # пустых ответа тоже «равны друг другу», и такой тест прошёл бы вхолостую.
    # Ключ — (категория, название, sku), всё в нижнем регистре; позиции без
    # категории идут первыми одной группой.
    expected = ["A-1", "D-4", "C-3", "B-2"]
    assert [row.sku for row in pg_page.rows] == expected
    assert [row.sku for row in sheets_page.rows] == expected
    assert pg_page.categories == sheets_page.categories == ["Чай", "кава"]
