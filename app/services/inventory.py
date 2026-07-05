"""Сервис чтения клиентских остатков (Фаза 3)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.user import User
from app.db.repositories import ShipmentRepository
from app.services import shipments
from app.sheets import StockRow, StockSheetNotFound, StockSource, build_stock_source

logger = structlog.get_logger(__name__)


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


def stock_sheet_key(client: User) -> str:
    """Ключ листа склада.

    Предпочитаем персистентное поле `users.stock_sheet_key`, чтобы смена ПІБ не
    ломала связь с Sheets между чтением и следующей синхронизацией. Fallback —
    старое поведение для обратной совместимости и данных до миграции.
    """

    return client.stock_sheet_key or client.full_name or str(client.telegram_id)


def stock_view_book_url(client: User) -> str | None:
    """Ссылка на персональную read-only Google-таблицу склада клиента.

    `None`, пока книга-зеркало не заведена провижином (`users.stock_view_book_id`).
    """
    if not client.stock_view_book_id:
        return None
    return f"https://docs.google.com/spreadsheets/d/{client.stock_view_book_id}"


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
    reader: StockSource | None = None,
) -> list[InventoryItem]:
    shipments._require_active_client(client)
    key = stock_sheet_key(client)
    try:
        rows = await asyncio.to_thread((reader or build_stock_source()).read_stock, key)
    except StockSheetNotFound:
        # Лист склада ещё не заведён/переименован — это пустой остаток, а не сбой:
        # клиент видит «склад порожній», а не падение хендлера створення ТТН.
        # Manager-сводка (`stock_totals`) проглатывает это отдельно → None.
        logger.warning("inventory.sheet_missing", client_id=str(client.id), key=key)
        rows = []
    reserved = await ShipmentRepository(session).reserved_by_sku(client.id)
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
    query: str | None = None,
    category: str | None = None,
    limit: int = 8,
    offset: int = 0,
    reader: StockSource | None = None,
) -> InventoryPage:
    items = await get_inventory_snapshot(session, client=client, reader=reader)
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


async def list_inventory_all(
    session: AsyncSession,
    *,
    client: User,
    query: str | None = None,
    category: str | None = None,
    reader: StockSource | None = None,
) -> list[InventoryItem]:
    """Полный отфильтрованный список без UI-пагинации (jobs/reporting/internal use)."""
    page = await list_inventory(
        session,
        client=client,
        query=query,
        category=category,
        limit=10**9,
        offset=0,
        reader=reader,
    )
    return page.items


@dataclass(frozen=True, slots=True)
class StockTotals:
    """Краткая сводка по листу склада клиента: позиции и единицы."""

    positions: int
    units: int


async def stock_totals(client: User, *, reader: StockSource | None = None) -> StockTotals | None:
    """Свод по листу склада клиента (позиции/единицы). `None` — лист недоступен.

    Чтение Sheets синхронно (gspread) → уводим в поток, чтобы не блокировать луп.
    Ошибку одного клиента (нет листа, блип НП/Sheets) глотаем — сводка по
    остальным не должна падать целиком.
    """
    source = reader or build_stock_source()
    try:
        rows = await asyncio.to_thread(source.read_stock, stock_sheet_key(client))
    except Exception:
        # Устойчивость сводки важнее: лист одного клиента может отсутствовать или
        # Sheets/НП блипнуть — это не должно валить сводку по остальным.
        logger.warning("inventory.stock_totals_failed", client_id=str(client.id), exc_info=True)
        return None
    return StockTotals(positions=len(rows), units=sum(row.quantity for row in rows))


async def stock_summary(
    clients: list[User], *, reader: StockSource | None = None
) -> list[tuple[User, StockTotals | None]]:
    """Свод склада по клиентам — лист на клиента (для экрана менеджера «📦 Склад»).

    Читаем последовательно (не `asyncio.gather`): один `SheetsClient`/gspread-сессия
    не рассчитана на параллельные потоки, а активных клиентов немного. Если число
    клиентов сильно вырастет — заводить отдельный источник на поток + ограничитель
    конкуренции, а не делить одну сессию.
    """
    source = reader or build_stock_source()
    return [(client, await stock_totals(client, reader=source)) for client in clients]
