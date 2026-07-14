"""Best-effort синхронизация клиентских Google Sheets."""

from __future__ import annotations

import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor
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
from app.sheets.source import StockSheetNotFound

logger = structlog.get_logger(__name__)

# Порядок колонок = как в основной книге «Склад» (STOCK_HEADERS провижна): D=Кількість,
# E=Ціна, F=Резерв, G=Доступно. Это позволяет переиспользовать форматирование/формулы/
# pivot «Склада» для книги-зеркала без изменений (они зашиты под этот порядок). Бот
# книгу-зеркало не читает — порядок важен только для оформления и `_view_data_row`.
_VIEW_HEADERS = ["Артикул", "Назва", "Категорія", "Кількість", "Ціна", "Резерв", "Доступно"]
_VIEW_TAB = "Товари"

# Один авторизованный SheetsClient на процесс: пересоздание клиента на каждый синк —
# лишний OAuth-handshake service-account. gspread-сессия не рассчитана на параллельные
# потоки (см. inventory.stock_summary), поэтому синк идёт через выделенный executor из
# ОДНОГО воркера — это сериализует доступ к общему клиенту без глобального лока и, в
# отличие от `asyncio.to_thread`, не занимает воркеров общего пула (иначе медленные
# записи в Sheets головой блокировали бы чтения/записи склада).
_sheets_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sheets-sync")
_shared_sheets_client: SheetsClient | None = None


async def run_on_sheets_executor(fn, /, *args):
    """Выполнить блокирующий вызов Sheets на выделенном single-worker executor.

    Сериализует ВСЕ обращения к Sheets (клиентский синк + записи склада
    `apply_deltas`): один воркер исключает гонку read-modify-write по одному листу
    и конкуренцию за общий gspread-клиент. В отличие от `asyncio.to_thread` (общий
    пул) не позволяет медленной записи в Sheets занять воркеров склада.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_sheets_executor, functools.partial(fn, *args))


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
    account: ClientAccount | None = None,
    previous_sheet_key: str | None = None,
    reader: StockSource | None = None,
    settings: Settings | None = None,
) -> None:
    cfg = settings or get_settings()
    if account is None:
        target_key = desired_stock_sheet_key(
            full_name=client.full_name, telegram_id=client.telegram_id
        )
        source_key = client.stock_sheet_key or target_key
        view_book_id = client.stock_view_book_id
    else:
        # `.strip()`, а не голый `or`: имя из пробелов прошло бы мимо фолбэка и
        # стало бы именем вкладки (ср. `desired_stock_sheet_key`).
        target_key = (account.name or "").strip() or str(account.id)
        source_key = account.stock_sheet_key or target_key
        view_book_id = account.stock_view_book_id
        # `previous_sheet_key` — понятие user-scope (прежнее имя вкладки клиента).
        # Для аккаунта оно не просто бессмысленно, а опасно: `_sync_client_sheets_sync`
        # переименовывает вкладку `previous_sheet_key` → `target_key`, поэтому правка
        # ПІБ работником увела бы вкладку с его именем в имя общего акаунта. Источник
        # правды для аккаунта — `account.stock_sheet_key`, он уже в `source_key`.
        previous_sheet_key = None

    if not _sheets_enabled(cfg):
        scope = account or client
        if scope.stock_sheet_key != target_key:
            scope.stock_sheet_key = target_key
            await session.flush()
        return

    snapshot = await get_inventory_snapshot(
        session,
        client=client,
        account_id=account.id if account else None,
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
    rename_ok, book_id = await asyncio.get_running_loop().run_in_executor(
        _sheets_executor,
        functools.partial(
            _sync_client_sheets_sync,
            cfg,
            source_key,
            previous_sheet_key or (source_key if source_key != target_key else None),
            target_key,
            view_book_id,
            rows,
        ),
    )
    # Продвигаем ключ только при подтверждённом переименовании вкладок: иначе PG
    # указывал бы на лист с новым именем, которого в «Складі» нет → пустой остаток.
    if rename_ok:
        if account is None:
            client.stock_sheet_key = target_key
        else:
            account.stock_sheet_key = target_key
    if book_id and view_book_id != book_id:
        if account is None:
            client.stock_view_book_id = book_id
        else:
            account.stock_view_book_id = book_id
    await session.flush()


async def best_effort_sync(
    session: AsyncSession,
    *,
    client: User,
    account: ClientAccount | None = None,
    log_key: str,
    previous_sheet_key: str | None = None,
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
            previous_sheet_key=previous_sheet_key,
            reader=reader,
            settings=settings,
        )
    except SQLAlchemyError:
        raise
    except Exception:
        logger.warning(log_key, exc_info=True, **log_context)


def _sync_client_sheets_sync(
    settings: Settings,
    source_key: str,
    previous_sheet_key: str | None,
    target_key: str,
    stock_view_book_id: str | None,
    rows: list[ViewRow],
) -> tuple[bool, str | None]:
    # Один воркер `_sheets_executor` → вызовы сериализованы, общий клиент безопасен.
    global _shared_sheets_client
    if _shared_sheets_client is None:
        _shared_sheets_client = SheetsClient(settings)
    client = _shared_sheets_client
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
