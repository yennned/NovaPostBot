"""Сервис чтения клиентских остатков (Фаза 3)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.enums import UserRole, UserStatus
from app.db.models.user import User
from app.db.repositories import ShipmentRepository
from app.services.exceptions import PermissionDenied
from app.sheets import StockRow, StockSource, build_stock_source

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


def _require_active_client(client: User) -> None:
    if client.role is not UserRole.client:
        raise PermissionDenied("кабінет доступний тільки клієнту")
    if client.status is not UserStatus.active:
        raise PermissionDenied("кабінет клієнта доступний після підтвердження")


def stock_sheet_key(client: User) -> str:
    """Ключ листа склада.

    Пока используем имя клиента как основной идентификатор листа; если ПІБ ещё
    нет, fallback — Telegram ID. Этого достаточно до появления отдельного mapping
    слоя CRM/WMS в поздних фазах.
    """

    return client.full_name or str(client.telegram_id)


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
    _require_active_client(client)
    rows = (reader or build_stock_source()).read_stock(stock_sheet_key(client))
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
