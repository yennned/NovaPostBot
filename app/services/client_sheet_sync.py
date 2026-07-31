"""Best-effort синхронизация клиентских Google Sheets."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import structlog
from gspread.exceptions import WorksheetNotFound
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models.client_account import ClientAccount
from app.db.models.user import User
from app.services.inventory import get_inventory_snapshot
from app.sheets import GoogleSheetsStockSource, StockSource
from app.sheets.client import SheetsClient
from app.sheets.runtime import run_on_sheets_executor, shared_sheets_client
from app.sheets.source import StockSheetNotFound

logger = structlog.get_logger(__name__)

# Порядок колонок = как в основной книге «Склад» (STOCK_HEADERS провижна): D=Кількість,
# E=Ціна, F=Резерв, G=Доступно. Это позволяет переиспользовать форматирование/формулы/
# pivot «Склада» для книги-зеркала без изменений (они зашиты под этот порядок). Бот
# книгу-зеркало не читает — порядок важен только для оформления и `_view_data_row`.
_VIEW_HEADERS = ["Артикул", "Назва", "Категорія", "Кількість", "Ціна", "Резерв", "Доступно"]
_VIEW_TAB = "Товари"

# Общий клиент и single-worker executor переехали в `app/sheets/runtime.py` — ими
# пользуется и чтение склада (`services/inventory`), а держать их здесь означало бы
# цикл импортов. `run_on_sheets_executor` реэкспортируем: на него ссылаются
# `services/tracking` и `services/returns`.


@dataclass(frozen=True, slots=True)
class ViewRow:
    sku: str
    name: str
    category: str | None
    price: Decimal | None
    stock: int
    reserved: int
    available: int


def _sheets_enabled(settings: Settings) -> bool:
    return bool(settings.google_sa_json.strip())


async def sync_client_sheets(
    session: AsyncSession,
    *,
    client: User,
    account: ClientAccount,
    reader: StockSource | None = None,
    settings: Settings | None = None,
) -> None:
    """Синхронизировать вкладки склада аккаунта и его книгу-зеркало.

    Всё здесь привязано к аккаунту: вкладка принадлежит аккаунту, а не человеку.
    `client` нужен только как читатель остатков (`get_inventory_snapshot`).
    """
    cfg = settings or get_settings()
    # `.strip()`, а не голый `or`: имя из пробелов прошло бы мимо фолбэка и
    # стало бы именем вкладки.
    target_key = (account.name or "").strip() or str(account.id)
    source_key = account.stock_sheet_key or target_key
    view_book_id = account.stock_view_book_id

    if not _sheets_enabled(cfg):
        if account.stock_sheet_key != target_key:
            account.stock_sheet_key = target_key
            await session.flush()
        return

    snapshot = await get_inventory_snapshot(
        session,
        client=client,
        account_id=account.id,
        account=account,
        reader=reader,
    )
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
    rename_ok, book_id = await run_on_sheets_executor(
        _sync_client_sheets_sync,
        cfg,
        # Явные `settings` → свой клиент, как в `build_stock_source`: расшаренный
        # создаётся один раз под `get_settings()`, и подмена конфигурации на нём
        # молча не сработала бы.
        settings is not None,
        source_key,
        source_key if source_key != target_key else None,
        target_key,
        view_book_id,
        rows,
    )
    # Продвигаем ключ только при подтверждённом переименовании вкладок: иначе PG
    # указывал бы на лист с новым именем, которого в «Складі» нет → пустой остаток.
    if rename_ok:
        account.stock_sheet_key = target_key
    if book_id and view_book_id != book_id:
        account.stock_view_book_id = book_id
    await session.flush()


async def best_effort_sync(
    session: AsyncSession,
    *,
    client: User,
    account: ClientAccount,
    log_key: str,
    reader: StockSource | None = None,
    settings: Settings | None = None,
    **log_context: str,
) -> None:
    """Best-effort обёртка над `sync_client_sheets` — единый гейт для всех write-путей.

    Сбой Sheets/НП (нет листа, 5xx, права) глотаем и логируем `log_key` — синк не
    должен валить основную операцию. Но `SQLAlchemyError` пробрасываем: sync делает
    SELECT/flush на той же сессии, и её проглатывание оставит сессию в
    rollback-required — следующий commit потеряет уже сфлашенные изменения.
    """
    try:
        await sync_client_sheets(
            session,
            client=client,
            account=account,
            reader=reader,
            settings=settings,
        )
    except SQLAlchemyError:
        raise
    except Exception:
        logger.warning(log_key, exc_info=True, **log_context)


def _sync_client_sheets_sync(
    settings: Settings,
    own_client: bool,
    source_key: str,
    previous_sheet_key: str | None,
    target_key: str,
    stock_view_book_id: str | None,
    rows: list[ViewRow],
) -> tuple[bool, str | None]:
    # Один воркер executor'а → вызовы сериализованы, общий клиент безопасен.
    client = SheetsClient(settings) if own_client else shared_sheets_client()
    gc = client._authorize()  # кэшируется на инстансе → OAuth-handshake только раз
    rename_ok = _rename_main_worksheets(gc, settings, previous_sheet_key or source_key, target_key)
    # Зеркалим резерв (из снапшота PG) в колонку «Резерв» актуального листа «Склад».
    _write_stock_reserved(client, target_key if rename_ok else source_key, rows)
    book_id = _sync_view_book(gc, stock_view_book_id=stock_view_book_id, rows=rows)
    return rename_ok, book_id


def _write_stock_reserved(client: SheetsClient, sheet_key: str, rows: list[ViewRow]) -> None:
    """Best-effort: записать Резерв (из PG-снапшота) в лист «Склад». Доступно — формула.

    Не должно ронять синк: нет листа/колонки/ошибка API → просто лог. Источник правды
    резерва остаётся Postgres.
    """
    reserved = {row.sku: row.reserved for row in rows}
    try:
        GoogleSheetsStockSource(client).write_reserved(sheet_key, reserved)
    except StockSheetNotFound:
        pass  # лист клиента в «Складі» ещё не заведён — нормально
    except Exception:
        logger.warning("stock_reserved_sync_failed", sheet_key=sheet_key, exc_info=True)


def _rename_main_worksheets(gc, settings: Settings, source_key: str, target_key: str) -> bool:
    """Переименовать вкладки клиента в «Складі»/«Приёмке». Вернуть успех.

    Успех (True) — переименовывать нечего или переименование подтверждено. Если
    исходной вкладки нет (вероятно, уже переименована или книга без неё) — это не
    провал, пропускаем. Реальная ошибка (`update_title` упал: коллизия имени, 5xx,
    права) → False, чтобы вызывающий не продвигал `stock_sheet_key`.
    """
    if not source_key or source_key == target_key:
        return True
    ok = True
    for book_id in (settings.sheets_stock_book_id, settings.sheets_intake_book_id):
        if not book_id:
            continue
        try:
            book = gc.open_by_key(book_id)
            titles = {ws.title for ws in book.worksheets()}
            if source_key not in titles:
                continue
            book.worksheet(source_key).update_title(target_key)
        except Exception:
            logger.warning(
                "client_sheet_rename_failed",
                book_id=book_id,
                source_key=source_key,
                target_key=target_key,
                exc_info=True,
            )
            ok = False
    return ok


def _view_data_row(row: ViewRow) -> list[str | int | float]:
    """Строка данных «Товари» (A–F, порядок «Склада»): Артикул, Назва, Категорія,
    Кількість, Ціна, Резерв. «Доступно» (G) — ARRAYFORMULA, её пишет провижн."""
    return [
        row.sku,
        row.name,
        row.category or "",
        row.stock,
        float(row.price) if row.price is not None else "",
        row.reserved,
    ]


def _sync_view_book(gc, *, stock_view_book_id: str | None, rows: list[ViewRow]) -> str | None:
    # View-book отложен: рантайм-сервис-аккаунт имеет только drive.readonly, а
    # gc.create() требует Drive write → 403. Книгу создаёт provisioning (полный drive +
    # share + оформление/pivot); пока id не задан — синк строк пропускаем.
    if not stock_view_book_id:
        return None
    book = gc.open_by_key(stock_view_book_id)
    try:
        ws = book.worksheet(_VIEW_TAB)
    except WorksheetNotFound:
        # Нет вкладки «Товари» → книга не была провижена (провижн всегда создаёт вкладку
        # + оформление + формулу «Доступно»). Не «дооформляем» наполовину — иначе получим
        # книгу без формулы/стилей и замаскируем пробел провижна. Логируем и пропускаем.
        logger.warning("view_book_not_provisioned", stock_view_book_id=stock_view_book_id)
        return None
    # Пишем ТОЛЬКО данные (A2:F): заголовки/оформление/бэндинг/CF/формула «Доступно»(G)
    # и лист «📊 Зведення» ставит провижн один раз; `values:clear` их не трогает.
    # Цену — числом (RAW), иначе comma-локаль книги исказит "12.34".
    ws.batch_clear(["A2:F1000"])  # снять «хвост» ранее удалённых позиций
    if rows:
        ws.update(values=[_view_data_row(row) for row in rows], range_name=f"A2:F{1 + len(rows)}")
    return book.id
