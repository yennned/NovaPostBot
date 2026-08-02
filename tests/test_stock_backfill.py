"""Backfill остатка «Склад» → `stock_balances`.

Проверяется то, из-за чего скрипт вообще написан отдельно, а не сделан «руками
один раз»: он обязан быть повторяемым и обязан сохранять инвариант журнала. Плюс
отдельно — что повторный прогон не удваивает остаток: ошибиться здесь означает
разрешить продать чужое, потому что гейт от oversell смотрит именно на это число.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from app.db.models.enums import StockMovementType, UserRole, UserStatus
from app.db.repositories import StockBalanceRepository, UserRepository
from app.sheets.source import StockRow
from scripts.stock_backfill import apply_plans, build_plans
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import account_of


class _SheetSource:
    def __init__(self, by_key: dict[str, list[StockRow]]) -> None:
        self._by_key = by_key
        self.reads = 0

    def read_stock(self, client_key: str) -> list[StockRow]:
        self.reads += 1
        return list(self._by_key.get(client_key, []))


async def _account(session: AsyncSession, telegram_id: int, *, sheet_key: str):
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


async def _quantity(session: AsyncSession, account_id: uuid.UUID, sku: str) -> int:
    balance = await StockBalanceRepository(session).get(account_id=account_id, sku=sku)
    return 0 if balance is None else balance.quantity


async def test_backfill_copies_sheet_and_keeps_ledger_consistent(db_session: AsyncSession):
    """Остаток переносится, а журнал движений сходится с ним по построению."""
    account = await _account(db_session, 1400, sheet_key="Магазин")
    source = _SheetSource(
        {
            "Магазин": [
                StockRow(sku="A", name="Кава", category="Напої", quantity=12, price=Decimal("99")),
                StockRow(sku="B", name="Чай", category="Напої", quantity=0, price=None),
            ]
        }
    )

    plans = await build_plans(db_session, source)
    await apply_plans(db_session, plans)

    assert await _quantity(db_session, account.id, "A") == 12
    assert await _quantity(db_session, account.id, "B") == 0
    repo = StockBalanceRepository(db_session)
    assert await repo.ledger_matches_balance(account.id) == []

    balance = await repo.get(account_id=account.id, sku="A")
    assert balance is not None
    assert (balance.name, balance.category, balance.price) == ("Кава", "Напої", Decimal("99"))
    # Зеркало должно знать, какое число оно записало: без этой базы «человек
    # поправил ячейку» неотличимо от «PG изменился».
    assert balance.mirrored_quantity == 12


async def test_second_run_is_idempotent(db_session: AsyncSession):
    """Повтор не удваивает остаток.

    Ошибка тут — не «неаккуратные данные», а разрешение продать чужое: гейт от
    oversell смотрит ровно на это число.
    """
    account = await _account(db_session, 1401, sheet_key="Магазин")
    source = _SheetSource(
        {"Магазин": [StockRow(sku="A", name="Кава", category=None, quantity=7, price=None)]}
    )

    await apply_plans(db_session, await build_plans(db_session, source))
    await apply_plans(db_session, await build_plans(db_session, source))

    assert await _quantity(db_session, account.id, "A") == 7
    assert await StockBalanceRepository(db_session).ledger_matches_balance(account.id) == []


async def test_backfill_converges_to_the_sheet(db_session: AsyncSession):
    """Расхождение выправляется честной дельтой в обе стороны.

    Именно движением, а не присвоением: иначе инвариант «сумма физических дельт ==
    остаток» разошёлся бы, и джоба сверки начала бы кричать на собственный backfill.
    """
    account = await _account(db_session, 1402, sheet_key="Магазин")
    repo = StockBalanceRepository(db_session)
    source = _SheetSource(
        {"Магазин": [StockRow(sku="A", name="Кава", category=None, quantity=10, price=None)]}
    )
    await apply_plans(db_session, await build_plans(db_session, source))

    source._by_key["Магазин"] = [
        StockRow(sku="A", name="Кава", category=None, quantity=4, price=None)
    ]
    await apply_plans(db_session, await build_plans(db_session, source))

    assert await _quantity(db_session, account.id, "A") == 4
    assert await repo.ledger_matches_balance(account.id) == []


async def test_missing_sheet_is_skipped_not_zeroed(db_session: AsyncSession):
    """Нет листа — аккаунт пропускается, а не обнуляется.

    Обнуление было бы худшим исходом: «склад порожній» выглядит как настоящий
    ответ, и клиент по нему принимает решения.
    """
    from app.sheets.source import StockSheetNotFound

    account = await _account(db_session, 1403, sheet_key="Немає")

    class _NoSheet:
        def read_stock(self, client_key: str):
            raise StockSheetNotFound(client_key)

    plans = await build_plans(db_session, _NoSheet())
    assert [p.missing_sheet for p in plans] == [True]

    # Заранее положим остаток — он должен пережить прогон.
    await StockBalanceRepository(db_session).apply_movement(
        account_id=account.id,
        sku="A",
        delta=5,
        movement_type=StockMovementType.manual,
    )
    await apply_plans(db_session, plans)
    assert await _quantity(db_session, account.id, "A") == 5


async def test_sheet_is_read_once_per_account(db_session: AsyncSession):
    """План и применение работают по ОДНОМУ снимку листа.

    Второе чтение стоило бы квоты и, что хуже, могло бы отличаться от первого —
    тогда напечатанный человеку план не соответствовал бы тому, что записано.
    """
    await _account(db_session, 1404, sheet_key="Магазин")
    source = _SheetSource(
        {"Магазин": [StockRow(sku="A", name="Кава", category=None, quantity=3, price=None)]}
    )

    plans = await build_plans(db_session, source)
    reads_after_plan = source.reads
    await apply_plans(db_session, plans)

    assert reads_after_plan == 1
    assert source.reads == 1, "применение не должно перечитывать лист"
