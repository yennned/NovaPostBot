"""Сверка остатка: PG против листа и PG против собственного журнала.

Проверяются те свойства, из-за которых сверка либо полезна, либо вредна: она не
должна тащить числа из листа в PG, не должна кричать на штатное отставание зеркала
и обязана отдельно замечать расхождение PG с собственным журналом — это уже баг в
нашем коде, а не рассинхрон с Google.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.db.models.enums import StockMovementType, UserRole, UserStatus
from app.db.repositories import StockBalanceRepository, UserRepository
from app.services import stock_reconcile
from app.sheets.mirror import StockSheetMirror
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import account_of

_HEADER = ["Артикул", "Назва", "Категорія", "Кількість", "Ціна", "Резерв", "Доступно"]


class _FakeWorksheet:
    def __init__(self, rows: list[list[Any]]) -> None:
        self.values = [list(_HEADER), *rows]

    def get_values(self) -> list[list[Any]]:
        return [list(row) for row in self.values]


class _FakeClient:
    def __init__(self, worksheet: _FakeWorksheet) -> None:
        self.worksheet = worksheet

    def get_stock_worksheet(self, client_key: str) -> _FakeWorksheet:
        return self.worksheet


def _mirror(rows: list[list[Any]]) -> tuple[StockSheetMirror, _FakeWorksheet]:
    worksheet = _FakeWorksheet(rows)
    return StockSheetMirror(client=_FakeClient(worksheet)), worksheet


async def _account(session: AsyncSession, telegram_id: int):
    user = await UserRepository(session).create(
        telegram_id=telegram_id,
        full_name=f"Клієнт {telegram_id}",
        role=UserRole.client,
        status=UserStatus.active,
    )
    account = await account_of(session, user)
    account.stock_sheet_key = "Магазин"
    await session.flush()
    return account


async def _stock(session: AsyncSession, account_id: uuid.UUID, sku: str, quantity: int):
    await StockBalanceRepository(session).apply_movement(
        account_id=account_id, sku=sku, delta=quantity, movement_type=StockMovementType.intake
    )


async def test_matching_stock_reports_nothing(db_session: AsyncSession):
    stock_reconcile.reset_seen()
    account = await _account(db_session, 1800)
    await _stock(db_session, account.id, "A", 5)
    mirror, _ = _mirror([["A", "Кава", "", 5, "", 0, 5]])

    result = await stock_reconcile.reconcile_account(db_session, account, mirror=mirror)

    assert result.confirmed == () and result.pending == ()
    assert stock_reconcile.report_text(result) is None


async def test_single_mismatch_waits_for_a_second_cycle(db_session: AsyncSession):
    """Одиночное несовпадение — отставание зеркала, а не дрейф.

    Кричи о нём сразу — владелец получит поток ложных тревог и перестанет читать
    сверку вовсе. Тогда она станет хуже, чем её отсутствие.
    """
    stock_reconcile.reset_seen()
    account = await _account(db_session, 1801)
    await _stock(db_session, account.id, "A", 5)
    mirror, _ = _mirror([["A", "Кава", "", 9, "", 0, 9]])

    first = await stock_reconcile.reconcile_account(db_session, account, mirror=mirror)
    assert [d.sku for d in first.pending] == ["A"]
    assert first.confirmed == ()
    assert stock_reconcile.report_text(first) is None

    second = await stock_reconcile.reconcile_account(db_session, account, mirror=mirror)
    assert [(d.sku, d.pg, d.sheet) for d in second.confirmed] == [("A", 5, 9)]
    assert "у боті 5, у листі 9" in (stock_reconcile.report_text(second) or "")


async def test_changing_numbers_are_not_escalated(db_session: AsyncSession):
    """Числа поехали между циклами — это живой процесс, а не застывший дрейф."""
    stock_reconcile.reset_seen()
    account = await _account(db_session, 1802)
    await _stock(db_session, account.id, "A", 5)
    mirror, worksheet = _mirror([["A", "Кава", "", 9, "", 0, 9]])

    await stock_reconcile.reconcile_account(db_session, account, mirror=mirror)
    worksheet.values[1][3] = 7  # лист догоняет — значит зеркало работает

    second = await stock_reconcile.reconcile_account(db_session, account, mirror=mirror)
    assert second.confirmed == ()
    assert [d.sheet for d in second.pending] == [7]


async def test_resolved_mismatch_forgets_its_state(db_session: AsyncSession):
    """Расхождение сошлось — счётчик обязан обнулиться.

    Иначе следующее, ни с чем не связанное расхождение по тому же SKU
    эскалировалось бы сразу, без положенного второго цикла.
    """
    stock_reconcile.reset_seen()
    account = await _account(db_session, 1803)
    await _stock(db_session, account.id, "A", 5)
    mirror, worksheet = _mirror([["A", "Кава", "", 9, "", 0, 9]])

    await stock_reconcile.reconcile_account(db_session, account, mirror=mirror)
    worksheet.values[1][3] = 5
    await stock_reconcile.reconcile_account(db_session, account, mirror=mirror)
    worksheet.values[1][3] = 9

    again = await stock_reconcile.reconcile_account(db_session, account, mirror=mirror)
    assert again.confirmed == (), "после схождения нужен новый полный цикл"
    assert [d.sku for d in again.pending] == ["A"]


async def test_sheet_only_sku_is_reported_but_never_imported(db_session: AsyncSession):
    """Артикул из листа не попадает в PG ни при каких условиях.

    Импортируй его — и опечатка человека в артикуле заводит позицию с любым
    остатком, то есть открывает дыру под oversell.
    """
    stock_reconcile.reset_seen()
    account = await _account(db_session, 1804)
    mirror, _ = _mirror([["ЧУЖИЙ", "Щось", "", 500, "", 0, 500]])

    result = await stock_reconcile.reconcile_account(db_session, account, mirror=mirror)

    assert result.sheet_only == ("ЧУЖИЙ",)
    assert await StockBalanceRepository(db_session).get(account_id=account.id, sku="ЧУЖИЙ") is None
    assert "не імпортуємо" in (stock_reconcile.report_text(result) or "")


async def test_ledger_drift_is_reported_as_our_bug(db_session: AsyncSession):
    """Расхождение PG с собственным журналом — единственная проверка, которую
    сравнение с Google дать не может в принципе."""
    stock_reconcile.reset_seen()
    account = await _account(db_session, 1805)
    await _stock(db_session, account.id, "A", 5)
    repo = StockBalanceRepository(db_session)
    balance = await repo.get(account_id=account.id, sku="A")
    assert balance is not None
    balance.quantity = 42  # мимо `apply_movement` — ровно то, что джоба обязана ловить
    await db_session.flush()

    mirror, _ = _mirror([["A", "Кава", "", 42, "", 0, 42]])
    result = await stock_reconcile.reconcile_account(db_session, account, mirror=mirror)

    assert result.ledger_drift == (("A", 5, 42),)
    # Лист с PG при этом сходится — то есть по сравнению с Google всё «хорошо».
    assert result.confirmed == () and result.pending == ()
    assert "це баг у боті" in (stock_reconcile.report_text(result) or "")
