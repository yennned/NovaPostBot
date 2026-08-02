"""Сервис чтения клиентских остатков (Фаза 3)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.client_account import ClientAccount
from app.db.models.user import User
from app.db.repositories import ShipmentRepository
from app.services import shipments
from app.services.inventory_backend import (
    InventoryBackend,
    resolve_inventory_backend,
    stock_sheet_key,
)
from app.sheets import StockRow, StockSource

logger = structlog.get_logger(__name__)

# Реэкспорт: `stock_sheet_key` переехал к Sheets-бэкенду (это его способ адресации),
# но продолжает импортироваться отсюда — `tracking`, `returns`, `shipment`.
__all__ = [
    "InventoryItem",
    "InventoryPage",
    "StockTotals",
    "find_inventory_item",
    "get_inventory_snapshot",
    "list_inventory",
    "stock_sheet_key",
    "stock_summary",
    "stock_totals",
    "stock_view_book_url",
]


@dataclass(frozen=True, slots=True)
class InventoryItem:
    sku: str
    name: str
    category: str | None
    stock: int
    reserved: int
    available: int
    price: Decimal | None


@dataclass(frozen=True, slots=True)
class InventoryPage:
    items: list[InventoryItem]
    total: int
    limit: int
    offset: int
    categories: list[str]


def stock_view_book_url(account: ClientAccount) -> str | None:
    """Ссылка на персональную read-only Google-таблицу склада аккаунта.

    `None`, пока книга-зеркало не заведена провижином (`stock_view_book_id`).
    """
    if not account.stock_view_book_id:
        return None
    return f"https://docs.google.com/spreadsheets/d/{account.stock_view_book_id}"


def _build_items(rows: list[StockRow], reserved: dict[str, int]) -> list[InventoryItem]:
    items: list[InventoryItem] = []
    for row in rows:
        reserved_qty = reserved.get(row.sku, 0)
        items.append(
            InventoryItem(
                sku=row.sku,
                name=row.name,
                category=row.category,
                stock=row.quantity,
                reserved=reserved_qty,
                available=max(row.quantity - reserved_qty, 0),
                price=row.price,
            )
        )
    return items


async def get_inventory_snapshot(
    session: AsyncSession,
    *,
    client: User,
    account_id=None,
    account: ClientAccount | None = None,
    reader: StockSource | None = None,
    backend: InventoryBackend | None = None,
) -> list[InventoryItem]:
    account = shipments.require_client_account(client, account)
    rows = await resolve_inventory_backend(reader=reader, backend=backend).read_rows(
        session, account
    )
    reserved = (
        await ShipmentRepository(session).reserved_by_sku(client.id)
        if account_id is None
        else await ShipmentRepository(session).reserved_by_account(account_id)
    )
    items = _build_items(rows, reserved)
    items.sort(
        key=lambda item: (
            (item.category or "").lower(),
            item.name.lower(),
            item.sku.lower(),
        )
    )
    return items


async def list_inventory(
    session: AsyncSession,
    *,
    client: User,
    account_id=None,
    account: ClientAccount | None = None,
    query: str | None = None,
    category: str | None = None,
    limit: int = 8,
    offset: int = 0,
    reader: StockSource | None = None,
    backend: InventoryBackend | None = None,
) -> InventoryPage:
    items = await get_inventory_snapshot(
        session,
        client=client,
        account_id=account_id,
        account=account,
        reader=reader,
        backend=backend,
    )
    categories = sorted({item.category for item in items if item.category})
    if query:
        needle = query.strip().lower()
        items = [
            item
            for item in items
            if needle in item.sku.lower()
            or needle in item.name.lower()
            or needle in (item.category or "").lower()
        ]
    if category:
        items = [item for item in items if (item.category or "").lower() == category.lower()]
    total = len(items)
    return InventoryPage(
        items=items[offset : offset + limit],
        total=total,
        limit=limit,
        offset=offset,
        categories=categories,
    )


async def find_inventory_item(
    session: AsyncSession,
    *,
    client: User,
    sku: str,
    account_id=None,
    account: ClientAccount | None = None,
    reader: StockSource | None = None,
    backend: InventoryBackend | None = None,
) -> InventoryItem | None:
    """Позиция склада по ТОЧНОМУ `sku` из свежего снапшота (`None` — позиции нет).

    Для случая «кнопка уже знает sku, нужен только актуальный остаток».
    `list_inventory(query=sku)` для этого не годится: `query` — подстрочный матч по
    sku/name/category, и нужная позиция может не попасть в первую страницу выдачи —
    вызывающий тихо получил бы «товара нет» вместо остатка.
    """
    items = await get_inventory_snapshot(
        session,
        client=client,
        account_id=account_id,
        account=account,
        reader=reader,
        backend=backend,
    )
    return next((item for item in items if item.sku == sku), None)


@dataclass(frozen=True, slots=True)
class StockTotals:
    """Краткая сводка по листу склада клиента: позиции и единицы."""

    positions: int
    units: int


async def stock_totals(
    session: AsyncSession,
    account: ClientAccount,
    *,
    reader: StockSource | None = None,
    backend: InventoryBackend | None = None,
) -> StockTotals | None:
    """Свод по складу аккаунта (позиции/единицы). `None` — источник недоступен.

    Именно аккаунта, а не пользователя: лист склада принадлежит аккаунту, а не
    конкретному человеку. Работник аккаунта своего листа не имеет — раньше
    сводка звалась по `User` и показывала каждого работника отдельной строкой
    «лист недоступний».

    Что именно глотается, решает бэкенд (`transient_read_errors`), а не эта
    функция. У Sheets отсутствие листа и блип квоты — штатная жизнь, и сводка по
    остальным аккаунтам из-за них падать не должна. У Postgres таких ошибок нет:
    сбой БД обязан быть виден, а не притворяться недоступным листом.
    """
    chosen = resolve_inventory_backend(reader=reader, backend=backend)
    try:
        rows = await chosen.read_rows(session, account)
    except chosen.transient_read_errors:
        logger.warning(
            "inventory.stock_totals_failed",
            account_id=str(account.id),
            backend=chosen.name,
            exc_info=True,
        )
        return None
    return StockTotals(positions=len(rows), units=sum(row.quantity for row in rows))


async def stock_summary(
    session: AsyncSession,
    accounts: list[ClientAccount],
    *,
    reader: StockSource | None = None,
    backend: InventoryBackend | None = None,
) -> list[tuple[ClientAccount, StockTotals | None]]:
    """Свод склада по аккаунтам — лист на аккаунт (для экрана менеджера «📦 Склад»).

    Читаем последовательно (не `asyncio.gather`): один `SheetsClient`/gspread-сессия
    не рассчитана на параллельные потоки, а активных аккаунтов немного. Если число
    аккаунтов сильно вырастет — заводить отдельный источник на поток + ограничитель
    конкуренции, а не делить одну сессию.
    """
    chosen = resolve_inventory_backend(reader=reader, backend=backend)
    return [(account, await stock_totals(session, account, backend=chosen)) for account in accounts]
