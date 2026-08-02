"""Порт источника остатка: развилка Sheets/Postgres и её граничные правила.

Ценность этих тестов не в «вызвалось нужное», а в трёх местах, где ошибка была бы
тихой: чтение с `INVENTORY_SOURCE=pg` не должно идти в Google вовсе; явный
`reader=` обязан побеждать конфигурацию (иначе per-update мемо и подмена источника
в тестах отключились бы молча); сбой Postgres не должен превращаться в «залишки
тимчасово недоступні» — это правдоподобная неправда на экране менеджера.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.config import get_settings
from app.db.models.enums import StockMovementType, UserRole, UserStatus
from app.db.repositories import StockBalanceRepository, UserRepository
from app.services import inventory
from app.services.inventory_backend import (
    PgInventoryBackend,
    SheetsInventoryBackend,
    build_inventory_backend,
    resolve_inventory_backend,
)
from app.sheets.source import StockRow
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import account_of


class _ExplodingSource:
    """Любое обращение к Google — провал теста, а не тихо другой результат."""

    def read_stock(self, client_key: str) -> list[StockRow]:
        raise AssertionError(f"чтение Google не должно происходить (key={client_key})")


class _StubSource:
    def __init__(self, rows: list[StockRow]) -> None:
        self._rows = rows
        self.keys: list[str] = []

    def read_stock(self, client_key: str) -> list[StockRow]:
        self.keys.append(client_key)
        return self._rows


async def _owner_account(session: AsyncSession, telegram_id: int):
    user = await UserRepository(session).create(
        telegram_id=telegram_id,
        full_name=f"Клієнт {telegram_id}",
        role=UserRole.client,
        status=UserStatus.active,
    )
    account = await account_of(session, user)
    return user, account


@pytest.fixture
def pg_source(monkeypatch):
    monkeypatch.setenv("INVENTORY_SOURCE", "pg")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_build_backend_follows_config(monkeypatch):
    for value, expected in (("sheets", "sheets"), ("crm", "sheets"), ("pg", "pg")):
        monkeypatch.setenv("INVENTORY_SOURCE", value)
        get_settings.cache_clear()
        assert build_inventory_backend().name == expected
    get_settings.cache_clear()


def test_explicit_reader_beats_config(pg_source):
    """Явный `reader=` — это «читать вот этим источником Sheets», и он обязан
    выигрывать даже при `INVENTORY_SOURCE=pg`.

    На нём держатся per-update мемо (`PerUpdateStockSource`) и подмена источника в
    тестах. Если бы конфигурация его перебивала, оба механизма отключились бы
    молча — код звал бы `reader`, а данные приходили бы из PG.
    """
    assert isinstance(resolve_inventory_backend(reader=_StubSource([])), SheetsInventoryBackend)
    assert isinstance(resolve_inventory_backend(), PgInventoryBackend)


async def test_pg_backend_reads_balances_and_never_touches_google(
    db_session: AsyncSession, pg_source
):
    """Ради этого всё и делается: чтение остатка перестаёт стоить квоты Google."""
    user, account = await _owner_account(db_session, 1200)
    repo = StockBalanceRepository(db_session)
    await repo.apply_movement(
        account_id=account.id, sku="SKU-1", delta=9, movement_type=StockMovementType.intake
    )
    await repo.upsert_meta(
        account_id=account.id, sku="SKU-1", name="Кава", category="Напої", price=Decimal("120.00")
    )

    # Источник Sheets подсунут намеренно взрывающийся: если бэкенд всё же полезет
    # в Google, тест упадёт с понятным сообщением, а не пройдёт на пустом складе.
    from app.sheets import reset_stock_source, use_stock_source

    token = use_stock_source(_ExplodingSource())
    try:
        items = await inventory.get_inventory_snapshot(
            db_session, client=user, account_id=account.id, account=account
        )
    finally:
        reset_stock_source(token)

    assert [(item.sku, item.stock, item.name, item.price) for item in items] == [
        ("SKU-1", 9, "Кава", Decimal("120.00"))
    ]


async def test_pg_backend_empty_account_is_empty_stock(db_session: AsyncSession, pg_source):
    """Аккаунт без строк остатка — пустой склад, а не ошибка."""
    user, account = await _owner_account(db_session, 1201)
    items = await inventory.get_inventory_snapshot(
        db_session, client=user, account_id=account.id, account=account
    )
    assert items == []


async def test_pg_backend_failure_is_not_reported_as_unavailable_sheet(
    db_session: AsyncSession, pg_source, monkeypatch
):
    """Сбой Postgres обязан пробиться наверх, а не стать «лист недоступний».

    Sheets-бэкенд глотает свои сбои осознанно: у Google отсутствие листа и 429 —
    штатная жизнь, и сводка по остальным аккаунтам из-за них падать не должна. Для
    Postgres та же трактовка была бы ложью — менеджер увидел бы «недоступно» и
    решил, что дело в Google, тогда как на самом деле упала БД.
    """
    _, account = await _owner_account(db_session, 1202)

    async def _boom(self, session, account):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(PgInventoryBackend, "read_rows", _boom)
    with pytest.raises(RuntimeError, match="connection reset"):
        await inventory.stock_totals(db_session, account)


async def test_sheets_backend_failure_still_degrades_to_none(db_session: AsyncSession):
    """Обратная сторона того же правила: поведение Sheets менять нельзя."""
    _, account = await _owner_account(db_session, 1203)

    class _Boom:
        def read_stock(self, client_key: str) -> list[StockRow]:
            raise RuntimeError("лист не знайдено")

    assert await inventory.stock_totals(db_session, account, reader=_Boom()) is None


async def test_sheets_backend_addresses_sheet_by_stock_key(db_session: AsyncSession):
    """Адресация Sheets — по ключу листа, и она не должна поехать при переносе."""
    _, account = await _owner_account(db_session, 1204)
    account.stock_sheet_key = "Старий Ключ"
    account.name = "Нове Імʼя"
    source = _StubSource([StockRow(sku="A", name="a", category=None, quantity=4, price=None)])

    totals = await inventory.stock_totals(db_session, account, reader=source)

    assert source.keys == ["Старий Ключ"]
    assert totals is not None and totals.units == 4
