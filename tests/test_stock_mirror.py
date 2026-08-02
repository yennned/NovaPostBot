"""Зеркало PG → лист «Склад» и приём ручных правок количества.

Фейкается только объект worksheet — разбор шапки, поиск колонок и адресация строк
настоящие, из `app/sheets/mirror.py`.

Проверяются четыре места, где ошибка молча ломает либо остаток, либо доверие к
листу: описательные колонки зеркало не смеет трогать; ручная правка отличается от
штатного отставания листа только по `mirrored_quantity`; опечатка в разряд не
должна становиться реальным остатком; новая строка в листе — не приёмка.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from app.config import get_settings
from app.db.models.enums import StockMovementType, UserRole, UserStatus
from app.db.repositories import StockBalanceRepository, UserRepository
from app.services import stock_mirror
from app.sheets.mirror import StockSheetMirror
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import account_of

_HEADER = ["Артикул", "Назва", "Категорія", "Кількість", "Ціна", "Резерв", "Доступно"]


class _FakeWorksheet:
    def __init__(self, rows: list[list[Any]]) -> None:
        self.values = [list(_HEADER), *rows]
        self.writes: list[dict[str, Any]] = []

    def get_values(self) -> list[list[Any]]:
        return [list(row) for row in self.values]

    def batch_update(self, updates: list[dict[str, Any]]) -> None:
        self.writes.append({"count": len(updates)})
        for update in updates:
            col_letters = "".join(ch for ch in update["range"] if ch.isalpha())
            row = int("".join(ch for ch in update["range"] if ch.isdigit()))
            col = 0
            for ch in col_letters:
                col = col * 26 + (ord(ch.upper()) - 64)
            while len(self.values[row - 1]) < col:
                self.values[row - 1].append("")
            self.values[row - 1][col - 1] = update["values"][0][0]


class _FakeClient:
    def __init__(self, worksheet: _FakeWorksheet) -> None:
        self.worksheet = worksheet

    def get_stock_worksheet(self, client_key: str) -> _FakeWorksheet:
        return self.worksheet


def _mirror(rows: list[list[Any]]) -> tuple[StockSheetMirror, _FakeWorksheet]:
    worksheet = _FakeWorksheet(rows)
    return StockSheetMirror(client=_FakeClient(worksheet)), worksheet


async def _account(session: AsyncSession, telegram_id: int, *, sheet_key: str = "Магазин"):
    user = await UserRepository(session).create(
        telegram_id=telegram_id,
        full_name=f"Клієнт {telegram_id}",
        role=UserRole.client,
        status=UserStatus.active,
    )
    account = await account_of(session, user)
    account.stock_sheet_key = sheet_key
    await session.flush()
    return account


async def _seed(session: AsyncSession, account_id: uuid.UUID, sku: str, quantity: int, mirrored):
    repo = StockBalanceRepository(session)
    if quantity:
        await repo.apply_movement(
            account_id=account_id,
            sku=sku,
            delta=quantity,
            movement_type=StockMovementType.intake,
        )
    balance = await repo.upsert_meta(account_id=account_id, sku=sku, name=sku)
    balance.mirrored_quantity = mirrored
    await session.flush()
    return balance


def _settings(monkeypatch, **overrides):
    for key, value in overrides.items():
        monkeypatch.setenv(key, str(value))
    get_settings.cache_clear()
    return get_settings()


async def test_mirror_writes_quantity_and_reserve_only(db_session: AsyncSession, monkeypatch):
    """Описательные колонки зеркало не трогает — иначе оно откатывало бы правки.

    Это и есть плата за то, что лист остаётся исправляемым руками: имя, категорию
    и цену человек правит прямо в «Складі», и переписывающее лист целиком зеркало
    молча возвращало бы их назад.
    """
    settings = _settings(monkeypatch)
    account = await _account(db_session, 1500)
    await _seed(db_session, account.id, "A", 10, mirrored=10)
    mirror, worksheet = _mirror([["A", "Кава мелена", "Напої", 10, "99.50", 0, 10]])

    result = await stock_mirror.mirror_account(
        db_session, account, mirror=mirror, settings=settings
    )

    assert result.cells_written == 0, "ничего не изменилось — писать нечего"
    assert worksheet.values[1] == ["A", "Кава мелена", "Напої", 10, "99.50", 0, 10]
    # И описательные поля забраны в PG: их источник — лист.
    balance = await StockBalanceRepository(db_session).get(account_id=account.id, sku="A")
    assert balance is not None
    assert (balance.name, balance.category, balance.price) == (
        "Кава мелена",
        "Напої",
        Decimal("99.50"),
    )


async def test_pg_quantity_is_pushed_back_to_the_sheet(db_session: AsyncSession, monkeypatch):
    """PG ушёл вперёд (отгрузка) — лист догоняет, и это НЕ считается правкой."""
    settings = _settings(monkeypatch)
    account = await _account(db_session, 1501)
    # В листе ещё 10, зеркало в прошлый раз записало 10, а в PG уже 7.
    await _seed(db_session, account.id, "A", 7, mirrored=10)
    mirror, worksheet = _mirror([["A", "Кава", "", 10, "", 0, 10]])

    result = await stock_mirror.mirror_account(
        db_session, account, mirror=mirror, settings=settings
    )

    assert result.edits == (), "отставание листа — не ручная правка"
    assert worksheet.values[1][3] == 7
    balance = await StockBalanceRepository(db_session).get(account_id=account.id, sku="A")
    assert balance is not None and balance.mirrored_quantity == 7


async def test_manual_edit_is_applied_as_a_movement(db_session: AsyncSession, monkeypatch):
    """Правка ячейки становится движением `manual` — то есть попадает в аудит.

    Сегодня фиксация такой правки зависит от того, вспомнил ли человек её
    записать. Здесь она фиксируется по построению.
    """
    settings = _settings(monkeypatch)
    account = await _account(db_session, 1502)
    await _seed(db_session, account.id, "A", 10, mirrored=10)
    # Человек поправил ячейку: было 10, стало 12.
    mirror, worksheet = _mirror([["A", "Кава", "", 12, "", 0, 12]])

    result = await stock_mirror.mirror_account(
        db_session, account, mirror=mirror, settings=settings
    )

    assert [(e.sku, e.was, e.now, e.applied) for e in result.edits] == [("A", 10, 12, True)]
    repo = StockBalanceRepository(db_session)
    balance = await repo.get(account_id=account.id, sku="A")
    assert balance is not None and balance.quantity == 12
    assert balance.mirrored_quantity == 12
    assert await repo.ledger_matches_balance(account.id) == []
    # Ячейку зеркало не перезаписывало: PG уже совпал с листом.
    assert worksheet.values[1][3] == 12


async def test_oversized_edit_is_rejected_and_the_cell_restored(
    db_session: AsyncSession, monkeypatch
):
    """Опечатка в разряд не должна становиться реальным остатком.

    Гейт от oversell смотрит ровно на это число: примени «1000» вместо «100» — и
    аккаунт продаст девять сотен несуществующих единиц. Значение возвращается в
    ячейку, поэтому отказ самозалечивается и не спамит сообщениями.
    """
    settings = _settings(monkeypatch, STOCK_MANUAL_DELTA_LIMIT=100)
    account = await _account(db_session, 1503)
    await _seed(db_session, account.id, "A", 100, mirrored=100)
    mirror, worksheet = _mirror([["A", "Кава", "", 1000, "", 0, 1000]])

    result = await stock_mirror.mirror_account(
        db_session, account, mirror=mirror, settings=settings
    )

    assert len(result.edits) == 1 and result.edits[0].applied is False
    balance = await StockBalanceRepository(db_session).get(account_id=account.id, sku="A")
    assert balance is not None and balance.quantity == 100
    assert worksheet.values[1][3] == 100, "значение обязано вернуться в ячейку"

    # Следующий цикл видит согласованное состояние — повторных сообщений нет.
    mirror2, _ = _mirror([list(worksheet.values[1])])
    again = await stock_mirror.mirror_account(
        db_session, account, mirror=mirror2, settings=settings
    )
    assert again.edits == ()


async def test_unknown_sku_in_sheet_is_not_adopted(db_session: AsyncSession, monkeypatch):
    """Новая строка в листе — это приёмка мимо «Приймання», а не коррекция.

    Импортируй её — и опечатка в артикуле заводит позицию с любым остатком, то
    есть открывает дыру под oversell.
    """
    settings = _settings(monkeypatch)
    account = await _account(db_session, 1504)
    mirror, _ = _mirror([["НОВИЙ", "Щось", "", 500, "", 0, 500]])

    result = await stock_mirror.mirror_account(
        db_session, account, mirror=mirror, settings=settings
    )

    assert result.unknown_skus == ("НОВИЙ",)
    assert await StockBalanceRepository(db_session).get(account_id=account.id, sku="НОВИЙ") is None


async def test_first_mirror_without_base_does_not_invent_an_edit(
    db_session: AsyncSession, monkeypatch
):
    """Без `mirrored_quantity` базы для сравнения нет — правку выдумывать нельзя.

    Строка, ни разу не зеркалившаяся (backfill не проходил), даёт расхождение
    листа с PG, неотличимое от ручной правки. Трактовать его как правку значило бы
    применить к остатку неизвестно что.
    """
    settings = _settings(monkeypatch)
    account = await _account(db_session, 1505)
    await _seed(db_session, account.id, "A", 5, mirrored=None)
    mirror, worksheet = _mirror([["A", "Кава", "", 42, "", 0, 42]])

    result = await stock_mirror.mirror_account(
        db_session, account, mirror=mirror, settings=settings
    )

    assert result.edits == ()
    balance = await StockBalanceRepository(db_session).get(account_id=account.id, sku="A")
    assert balance is not None and balance.quantity == 5
    assert worksheet.values[1][3] == 5, "лист приводится к PG, а не наоборот"


async def test_reserve_column_mirrors_postgres(db_session: AsyncSession, monkeypatch):
    """«Резерв» — вычисляемая вьюшка Postgres, лист её только показывает."""
    settings = _settings(monkeypatch)
    account = await _account(db_session, 1506)
    await _seed(db_session, account.id, "A", 10, mirrored=10)
    mirror, worksheet = _mirror([["A", "Кава", "", 10, "", 7, 3]])

    await stock_mirror.mirror_account(db_session, account, mirror=mirror, settings=settings)

    # Броней в PG нет — значит и в листе их быть не должно.
    assert worksheet.values[1][5] == 0
