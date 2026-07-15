"""Сервис чтения клиентских остатков (Фаза 3)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.client_account import ClientAccount
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


def stock_sheet_key(client: User | ClientAccount) -> str:
    """Ключ листа склада.

    Предпочитаем персистентное поле `stock_sheet_key`, чтобы смена ПІБ не ломала
    связь с Sheets между чтением и следующей синхронизацией. Fallback — старое
    поведение для обратной совместимости и данных до миграции.

    User-ветка — легаси: лист склада принадлежит аккаунту, а не человеку, и у
    работника аккаунта своего листа нет. Она доживает только на путях, где
    `account` ещё объявлен опциональным (`account or client`); снести её вместе с
    колонками `users.stock_sheet_key`/`stock_view_book_id` можно после того, как
    инвариант «аккаунт есть всегда» станет типом, а не соглашением.
    """

    if isinstance(client, ClientAccount):
        return client.stock_sheet_key or client.name or str(client.id)
    return client.stock_sheet_key or client.full_name or str(client.telegram_id)


def stock_view_book_url(client: User | ClientAccount) -> str | None:
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
    account_id=None,
    account: ClientAccount | None = None,
    reader: StockSource | None = None,
) -> list[InventoryItem]:
    shipments._require_active_client(client)
    key = stock_sheet_key(account or client)
    try:
        rows = await asyncio.to_thread((reader or build_stock_source()).read_stock, key)
    except StockSheetNotFound:
        # Лист склада ещё не заведён/переименован — это пустой остаток, а не сбой:
        # клиент видит «склад порожній», а не падение хендлера створення ТТН.
        # Manager-сводка (`stock_totals`) проглатывает это отдельно → None.
        logger.warning("inventory.sheet_missing", client_id=str(client.id), key=key)
        rows = []
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
) -> InventoryPage:
    items = await get_inventory_snapshot(
        session, client=client, account_id=account_id, account=account, reader=reader
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


@dataclass(frozen=True, slots=True)
class StockTotals:
    """Краткая сводка по листу склада клиента: позиции и единицы."""

    positions: int
    units: int


async def stock_totals(
    account: ClientAccount, *, reader: StockSource | None = None
) -> StockTotals | None:
    """Свод по листу склада аккаунта (позиции/единицы). `None` — лист недоступен.

    Именно аккаунта, а не пользователя: лист склада принадлежит аккаунту, а не
    конкретному человеку. Работник аккаунта своего листа не имеет — раньше
    сводка звалась по `User` и показывала каждого работника отдельной строкой
    «лист недоступний».

    Чтение Sheets синхронно (gspread) → уводим в поток, чтобы не блокировать луп.
    Ошибку одного аккаунта (нет листа, блип НП/Sheets) глотаем — сводка по
    остальным не должна падать целиком.
    """
    source = reader or build_stock_source()
    try:
        rows = await asyncio.to_thread(source.read_stock, stock_sheet_key(account))
    except Exception:
        # Устойчивость сводки важнее: лист одного аккаунта может отсутствовать или
        # Sheets/НП блипнуть — это не должно валить сводку по остальным.
        logger.warning("inventory.stock_totals_failed", account_id=str(account.id), exc_info=True)
        return None
    return StockTotals(positions=len(rows), units=sum(row.quantity for row in rows))


async def stock_summary(
    accounts: list[ClientAccount], *, reader: StockSource | None = None
) -> list[tuple[ClientAccount, StockTotals | None]]:
    """Свод склада по аккаунтам — лист на аккаунт (для экрана менеджера «📦 Склад»).

    Читаем последовательно (не `asyncio.gather`): один `SheetsClient`/gspread-сессия
    не рассчитана на параллельные потоки, а активных аккаунтов немного. Если число
    аккаунтов сильно вырастет — заводить отдельный источник на поток + ограничитель
    конкуренции, а не делить одну сессию.
    """
    source = reader or build_stock_source()
    return [(account, await stock_totals(account, reader=source)) for account in accounts]
