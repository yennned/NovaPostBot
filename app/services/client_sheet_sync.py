"""Best-effort синхронизация клиентских Google Sheets."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models.user import User
from app.db.repositories import SenderProfileRepository
from app.services.inventory import get_inventory_snapshot
from app.sheets import StockSource
from app.sheets.client import SheetsClient

logger = structlog.get_logger(__name__)

_VIEW_HEADERS = ["Артикул", "Назва", "Категорія", "Ціна", "Кількість", "Резерв", "Доступно"]
_VIEW_TAB = "Товари"


@dataclass(frozen=True, slots=True)
class ViewRow:
    sku: str
    name: str
    category: str | None
    price: Decimal | None
    stock: int
    reserved: int
    available: int


def desired_stock_sheet_key(*, full_name: str | None, telegram_id: int) -> str:
    return (full_name or "").strip() or str(telegram_id)


def _sheets_enabled(settings: Settings) -> bool:
    return bool(settings.google_sa_json.strip())


async def sync_client_sheets(
    session: AsyncSession,
    *,
    client: User,
    previous_sheet_key: str | None = None,
    reader: StockSource | None = None,
    settings: Settings | None = None,
) -> None:
    cfg = settings or get_settings()
    target_key = desired_stock_sheet_key(full_name=client.full_name, telegram_id=client.telegram_id)
    source_key = client.stock_sheet_key or target_key

    if not _sheets_enabled(cfg):
        if client.stock_sheet_key != target_key:
            client.stock_sheet_key = target_key
            await session.flush()
        return

    snapshot = await get_inventory_snapshot(session, client=client, reader=reader)
    default_profile = await SenderProfileRepository(session).get_default_for_client(client.id)
    rows = [
        ViewRow(
            sku=item.sku,
            name=item.name,
            category=item.category,
            price=item.price,
            stock=item.stock,
            reserved=item.reserved,
            available=item.available,
        )
        for item in snapshot
    ]
    book_id = await asyncio.to_thread(
        _sync_client_sheets_sync,
        cfg,
        source_key,
        previous_sheet_key or (source_key if source_key != target_key else None),
        target_key,
        client.stock_view_book_id,
        client.full_name or str(client.telegram_id),
        (default_profile.name if default_profile is not None else None),
        rows,
    )
    client.stock_sheet_key = target_key
    if book_id and client.stock_view_book_id != book_id:
        client.stock_view_book_id = book_id
    await session.flush()


def _sync_client_sheets_sync(
    settings: Settings,
    source_key: str,
    previous_sheet_key: str | None,
    target_key: str,
    stock_view_book_id: str | None,
    client_label: str,
    sender_name: str | None,
    rows: list[ViewRow],
) -> str | None:
    client = SheetsClient(settings)
    gc = client._authorize()
    _rename_main_worksheets(gc, settings, previous_sheet_key or source_key, target_key)
    return _sync_view_book(
        gc,
        stock_view_book_id=stock_view_book_id,
        client_label=client_label,
        sender_name=sender_name,
        rows=rows,
    )


def _rename_main_worksheets(gc, settings: Settings, source_key: str, target_key: str) -> None:
    if not source_key or source_key == target_key:
        return
    for book_id in (settings.sheets_stock_book_id, settings.sheets_intake_book_id):
        if not book_id:
            continue
        try:
            book = gc.open_by_key(book_id)
            ws = book.worksheet(source_key)
            ws.update_title(target_key)
        except Exception:
            logger.warning(
                "client_sheet_rename_failed",
                book_id=book_id,
                source_key=source_key,
                target_key=target_key,
                exc_info=True,
            )


def _sync_view_book(
    gc,
    *,
    stock_view_book_id: str | None,
    client_label: str,
    sender_name: str | None,
    rows: list[ViewRow],
) -> str:
    if stock_view_book_id:
        book = gc.open_by_key(stock_view_book_id)
    else:
        book = gc.create(f"{client_label} · Перегляд складу")
    if not book.worksheets():
        ws = book.add_worksheet(title=_VIEW_TAB, rows=1000, cols=10)
    else:
        ws = book.worksheet(book.worksheets()[0].title)
        if ws.title != _VIEW_TAB:
            ws.update_title(_VIEW_TAB)
    values: list[list[str | int]] = [
        [f"Клієнт: {client_label}"],
        [f"ФОП: {sender_name or '—'}"],
        [],
        _VIEW_HEADERS,
    ]
    for row in rows:
        values.append(
            [
                row.sku,
                row.name,
                row.category or "",
                f"{row.price:.2f}" if row.price is not None else "",
                row.stock,
                row.reserved,
                row.available,
            ]
        )
    ws.clear()
    ws.update(values=values, range_name=f"A1:G{len(values)}")
    ws.freeze(rows=4)
    return book.id
