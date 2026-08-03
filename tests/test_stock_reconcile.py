"""Сверка остатка: PG против листа и PG против собственного журнала.

Проверяются те свойства, из-за которых сверка либо полезна, либо вредна: она не
должна тащить числа из листа в PG, не должна кричать на штатное отставание зеркала
и обязана отдельно замечать расхождение PG с собственным журналом — это уже баг в
нашем коде, а не рассинхрон с Google.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.config import get_settings
from app.db.models.enums import StockMovementType, UserRole, UserStatus
from app.db.repositories import StockBalanceRepository, UserRepository
from app.services import stock_reconcile
from app.sheets.mirror import StockSheetMirror
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import account_of

_HEADER = ["Артикул", "Назва", "Категорія", "Кількість", "Ціна", "Резерв", "Доступно"]


@pytest.fixture(autouse=True)
def _pg_backend(monkeypatch):
    """Сверка количеств применима только при `INVENTORY_SOURCE=pg`.

    На `sheets` количество ведёт лист, а движение пишется без сдвига остатка —
    и внутренний инвариант, и сравнение с листом расходятся по построению.
    Поэтому `reconcile_account` там их не выполняет вовсе, а тесты про количества
    гоняем в том режиме, где они имеют смысл. Сам гейт проверяется отдельно.
    """
    monkeypatch.setenv("INVENTORY_SOURCE", "pg")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


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


async def test_unreleased_reserve_is_reported_even_when_sheet_agrees(db_session: AsyncSession):
    """Бронь под закрытой ТТН видна сверке, хотя ни одна прежняя проверка её не ловит.

    Остаток совпадает и с листом, и с журналом физических дельт: `ttn_reserve`
    количество не двигает. Именно поэтому дефект «ТТН удалили в кабинете НП» жил
    полтора года — сверка молчала, потому что смотреть было нечем.

    Мутация: убрать `unreleased` из `reconcile_account` — оба assert покраснеют.
    """
    from decimal import Decimal

    from app.db.models.enums import ShipmentStatus
    from app.db.repositories import ShipmentItemDraft, ShipmentRepository, StockMovementRepository

    stock_reconcile.reset_seen()
    user = await UserRepository(db_session).create(
        telegram_id=1806,
        full_name="Клієнт 1806",
        role=UserRole.client,
        status=UserStatus.active,
    )
    account = await account_of(db_session, user)
    account.stock_sheet_key = "Магазин"
    await db_session.flush()
    await _stock(db_session, account.id, "A", 5)

    shipments = ShipmentRepository(db_session)
    created = await shipments.create(
        client_id=user.id,
        recipient_name="Іван",
        ttn_number="59001806",
        status=ShipmentStatus.cancelled,
        items=[ShipmentItemDraft(sku="A", name="Кава", quantity=2, unit_price=Decimal("100"))],
    )
    shipment = await shipments.get_by_id(created.id)
    await StockMovementRepository(db_session).record_for_items(
        client_id=user.id,
        account_id=account.id,
        shipment_id=shipment.id,
        actor_user_id=user.id,
        items=shipment.items,
        movement_type=StockMovementType.ttn_reserve,
        sign=-1,
        comment="Резерв",
    )
    await db_session.flush()

    mirror, _ = _mirror([["A", "Кава", "", 5, "", 0, 5]])
    result = await stock_reconcile.reconcile_account(db_session, account, mirror=mirror)

    # Ни одна прежняя проверка расхождения не видит.
    assert result.ledger_drift == () and result.confirmed == () and result.pending == ()
    assert [(number, reserve) for _, number, reserve in result.unreleased] == [("59001806", -2)]
    assert "ТТН 59001806" in (stock_reconcile.report_text(result) or "")


async def test_quantity_checks_are_silent_until_pg_owns_the_stock(
    db_session: AsyncSession, monkeypatch
):
    """На `sheets` сверка количеств молчит — иначе она кричала бы всегда.

    Разложим по шагам ровно то, что произошло бы в проде между backfill и
    переключением: backfill завёл остаток движением `manual`, отправка записала
    физический `ttn_dispatch`, но количество в PG не сдвинула (на `sheets` его
    ведёт лист). Инвариант «сумма физических дельт == остаток» расходится, и лист
    с PG — тоже. Обе тревоги верны по форме и пусты по сути: это не дефект, а
    устройство пути записи.

    Мутация: убрать гейт по бэкенду — оба assert покраснеют.
    """
    from decimal import Decimal

    from app.db.models.enums import ShipmentStatus
    from app.db.repositories import ShipmentItemDraft, ShipmentRepository, StockMovementRepository

    stock_reconcile.reset_seen()
    user = await UserRepository(db_session).create(
        telegram_id=1807,
        full_name="Клієнт 1807",
        role=UserRole.client,
        status=UserStatus.active,
    )
    account = await account_of(db_session, user)
    account.stock_sheet_key = "Магазин"
    await db_session.flush()
    await _stock(db_session, account.id, "A", 10)  # backfill: PG == лист == 10

    # Отправка на пути `sheets`: движение есть, остаток в PG не двигается.
    created = await ShipmentRepository(db_session).create(
        client_id=user.id,
        recipient_name="Іван",
        ttn_number="59001807",
        status=ShipmentStatus.dispatched,
        items=[ShipmentItemDraft(sku="A", name="Кава", quantity=3, unit_price=Decimal("100"))],
    )
    shipment = await ShipmentRepository(db_session).get_by_id(created.id)
    await StockMovementRepository(db_session).record_for_items(
        client_id=user.id,
        account_id=account.id,
        shipment_id=shipment.id,
        actor_user_id=user.id,
        items=shipment.items,
        movement_type=StockMovementType.ttn_dispatch,
        sign=-1,
        comment="Списання",
    )
    await db_session.flush()

    mirror, _ = _mirror([["A", "Кава", "", 7, "", 0, 7]])  # лист уже уменьшился

    monkeypatch.setenv("INVENTORY_SOURCE", "sheets")
    get_settings.cache_clear()
    result = await stock_reconcile.reconcile_account(db_session, account, mirror=mirror)

    assert result.ledger_drift == (), "на `sheets` расхождение журнала — норма пути записи"
    assert result.confirmed == () and result.pending == (), "лист впереди PG — тоже норма"
    assert stock_reconcile.report_text(result) is None
