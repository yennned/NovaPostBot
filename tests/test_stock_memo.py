"""Мемоизация чтения склада в пределах одного апдейта (`PerUpdateStockSource`).

Открытие «📦 Товари» читало лист «Склад» ДВАЖДЫ: `list_inventory` для рендера, следом
`best_effort_sync` — тот же лист ещё раз. То же в `create_shipment`. Квота Sheets
(60 read/min) считается на service-account, то есть на весь бот, поэтому лишнее
чтение — не мелочь.
"""

from __future__ import annotations

from app.db.models.enums import UserRole, UserStatus
from app.db.repositories import ClientAccountRepository, UserRepository
from app.services.inventory import get_inventory_snapshot
from app.sheets import PerUpdateStockSource, reset_stock_source, use_stock_source
from app.sheets.source import StockDelta, StockRow
from sqlalchemy.ext.asyncio import AsyncSession


class _CountingSource:
    def __init__(self) -> None:
        self.reads = 0
        self.applied: list[tuple[str, list[StockDelta]]] = []

    def read_stock(self, client_key: str) -> list[StockRow]:
        self.reads += 1
        return [StockRow(sku="A", name="Кава", category="Напої", quantity=5, price=None)]

    def apply_deltas(self, client_key: str, deltas: list[StockDelta]) -> None:
        self.applied.append((client_key, deltas))


def test_memo_reads_source_once_per_key():
    source = _CountingSource()
    memo = PerUpdateStockSource(source)

    assert memo.read_stock("Магазин") == memo.read_stock("Магазин")
    assert source.reads == 1
    memo.read_stock("Інший")
    assert source.reads == 2  # ключи не смешиваются


def test_memo_invalidated_by_apply_deltas():
    source = _CountingSource()
    memo = PerUpdateStockSource(source)
    memo.read_stock("Магазин")

    memo.apply_deltas("Магазин", [StockDelta(sku="A", quantity_delta=-1)])
    memo.read_stock("Магазин")

    assert source.applied and source.reads == 2  # после записи снапшот перечитан


def test_memo_invalidates_only_written_key():
    source = _CountingSource()
    memo = PerUpdateStockSource(source)
    memo.read_stock("Магазин")
    memo.read_stock("Інший")

    memo.apply_deltas("Магазин", [StockDelta(sku="A", quantity_delta=-1)])
    memo.read_stock("Інший")

    assert source.reads == 2  # «Інший» остался мемоизированным


async def _account_owner(session: AsyncSession, telegram_id: int):
    owner = await UserRepository(session).create(
        telegram_id=telegram_id,
        phone=f"+38099000{telegram_id}",
        full_name="Магазин",
        role=UserRole.client,
        status=UserStatus.active,
        account_name="Магазин",
    )
    membership = await ClientAccountRepository(session).get_membership(user_id=owner.id)
    membership.account.stock_sheet_key = "Магазин"
    await session.flush()
    return owner, membership.account


async def test_two_snapshots_in_one_update_read_sheet_once(db_session: AsyncSession):
    """Главное утверждение PR: рендер + следующий за ним синк = ОДНО чтение листа.

    Раньше каждый из них читал «Склад» самостоятельно. `reader=` намеренно не
    передаём — проверяем именно тот путь, которым ходят хендлеры (источник берётся
    из контекста апдейта, который ставит `ServicesMiddleware`).
    """
    owner, account = await _account_owner(db_session, 8300)
    source = _CountingSource()
    token = use_stock_source(PerUpdateStockSource(source))
    try:
        await get_inventory_snapshot(db_session, client=owner, account=account)
        await get_inventory_snapshot(db_session, client=owner, account=account)
    finally:
        reset_stock_source(token)

    assert source.reads == 1


async def test_without_update_context_each_snapshot_reads_again(db_session: AsyncSession):
    """Вне апдейта мемо нет: воркер и фоновые джобы обязаны читать свежее."""
    owner, account = await _account_owner(db_session, 8301)
    source = _CountingSource()

    await get_inventory_snapshot(db_session, client=owner, account=account, reader=source)
    await get_inventory_snapshot(db_session, client=owner, account=account, reader=source)

    assert source.reads == 2
