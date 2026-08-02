"""Ингест приёмки «Історія» → `stock_balances`.

Фейкается только лист Google (объект worksheet), а разбор строк, арифметика
диапазона и отпечаток — настоящие, из `app/sheets/history.py`. Иначе тест
проверял бы свою копию логики: ровно так уже один раз прошла мутация «убрать
ORDER BY» в тестах остатка.

Проверяются те места, где ошибка молча портит остаток: повтор прохода не должен
задваивать приёмку, сдвинувшийся журнал обязан останавливать ингест целиком, а
водораздел — проезжать чужие и битые строки, а не застревать перед ними.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from app.config import get_settings
from app.db.models.enums import StockMovementType, UserRole, UserStatus
from app.db.repositories import (
    StockBalanceRepository,
    StockIntakeCursorRepository,
    UserRepository,
)
from app.services import stock_ingest
from app.sheets.history import HISTORY_TAB, IntakeHistoryReader
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import account_of

_HEADER = ["Час", "Лист (клієнт)", "Артикул", "Кількість +", "Накладна", "Хто"]


class _FakeWorksheet:
    """Минимальный worksheet: диапазонное чтение и колонка A, как у gspread.

    Важные повадки настоящего API, которые здесь воспроизведены: `get` не
    возвращает хвостовые пустые строки диапазона и обрезает диапазон по данным.
    """

    def __init__(self, rows: list[list[Any]]) -> None:
        self.rows = rows
        self.reads = 0

    def get(self, range_name: str) -> list[list[Any]]:
        self.reads += 1
        match = re.fullmatch(r"A(\d+):F(\d+)", range_name)
        assert match, f"неожиданный диапазон: {range_name}"
        first, last = int(match.group(1)), int(match.group(2))
        window = self.rows[first - 1 : last]
        while window and not any(str(cell).strip() for cell in window[-1]):
            window.pop()
        return [list(row) for row in window]

    def col_values(self, index: int) -> list[Any]:
        return [row[index - 1] if len(row) >= index else "" for row in self.rows]


class _FakeSheetsClient:
    def __init__(self, worksheet: _FakeWorksheet) -> None:
        self.worksheet = worksheet

    def get_stock_worksheet(self, client_key: str) -> _FakeWorksheet:
        assert client_key == HISTORY_TAB
        return self.worksheet


def _reader(rows: list[list[Any]]) -> tuple[IntakeHistoryReader, _FakeWorksheet]:
    worksheet = _FakeWorksheet([_HEADER, *rows])
    return IntakeHistoryReader(client=_FakeSheetsClient(worksheet)), worksheet


def _event(tab: str, sku: str, qty: int, who: str = "склад@example.com") -> list[Any]:
    return ["01.08.2026 10:00:00", tab, sku, qty, "", who]


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


def _settings(monkeypatch, **overrides):
    monkeypatch.setenv("SHEETS_STOCK_BOOK_ID", "book-1")
    for key, value in overrides.items():
        monkeypatch.setenv(key, str(value))
    get_settings.cache_clear()
    return get_settings()


async def _quantity(session: AsyncSession, account_id: uuid.UUID, sku: str) -> int:
    balance = await StockBalanceRepository(session).get(account_id=account_id, sku=sku)
    return 0 if balance is None else balance.quantity


async def test_first_pass_only_sets_watermark_at_the_end_of_the_journal(
    db_session: AsyncSession, monkeypatch
):
    """Первый проход НИЧЕГО не применяет — и это главное свойство.

    Вся прошлая приёмка уже сидит в количествах листа «Склад», откуда её заберёт
    backfill. Переиграй ингест журнал с начала — остаток удвоился бы по всем
    позициям сразу, тихо и целиком.
    """
    settings = _settings(monkeypatch)
    account = await _account(db_session, 1300, sheet_key="Магазин")
    reader, _ = _reader([_event("Магазин", "SKU-1", 5), _event("Магазин", "SKU-2", 3)])

    result = await stock_ingest.ingest_intake_history(db_session, reader=reader, settings=settings)

    assert result.applied == 0
    assert result.last_row == 3  # шапка + две строки
    assert await _quantity(db_session, account.id, "SKU-1") == 0


async def test_new_events_after_watermark_are_applied_once(db_session: AsyncSession, monkeypatch):
    """Приёмка доезжает в PG — и повтор прохода её не задваивает."""
    settings = _settings(monkeypatch)
    account = await _account(db_session, 1301, sheet_key="Магазин")
    rows = [_event("Магазин", "SKU-1", 5)]
    reader, worksheet = _reader(rows)

    await stock_ingest.ingest_intake_history(db_session, reader=reader, settings=settings)
    worksheet.rows.append(_event("Магазин", "SKU-1", 7))
    worksheet.rows.append(_event("Магазин", "SKU-2", 2))

    first = await stock_ingest.ingest_intake_history(db_session, reader=reader, settings=settings)
    assert first.applied == 2
    assert await _quantity(db_session, account.id, "SKU-1") == 7
    assert await _quantity(db_session, account.id, "SKU-2") == 2

    # Второй проход по тому же журналу: новых строк нет — значит и дельт нет.
    second = await stock_ingest.ingest_intake_history(db_session, reader=reader, settings=settings)
    assert second.applied == 0
    assert await _quantity(db_session, account.id, "SKU-1") == 7


async def test_movement_records_honest_before_and_after(db_session: AsyncSession, monkeypatch):
    """Приёмка пишется движением `intake` с восстановимой историей."""
    settings = _settings(monkeypatch)
    account = await _account(db_session, 1302, sheet_key="Магазин")
    reader, worksheet = _reader([])
    await stock_ingest.ingest_intake_history(db_session, reader=reader, settings=settings)

    worksheet.rows.append(_event("Магазин", "SKU-1", 4))
    worksheet.rows.append(_event("Магазин", "SKU-1", 6))
    await stock_ingest.ingest_intake_history(db_session, reader=reader, settings=settings)

    movements = await StockBalanceRepository(db_session).ledger_matches_balance(account.id)
    assert movements == [], "журнал движений обязан сходиться с остатком"
    assert await _quantity(db_session, account.id, "SKU-1") == 10

    from app.db.models.stock_movement import StockMovement
    from sqlalchemy import select

    rows = list(
        await db_session.scalars(
            select(StockMovement)
            .where(StockMovement.account_id == account.id)
            .order_by(StockMovement.created_at, StockMovement.quantity_before)
        )
    )
    assert [m.movement_type for m in rows] == [StockMovementType.intake] * 2
    assert [(m.quantity_before, m.quantity_after) for m in rows] == [(0, 4), (4, 10)]
    # Каждое внесение — отдельным движением с автором: схлопни их в одно, и журнал
    # перестанет отвечать на вопрос «кто это внёс».
    assert all("склад@example.com" in (m.comment or "") for m in rows)


async def test_changed_watermark_row_halts_ingest_entirely(db_session: AsyncSession, monkeypatch):
    """Сдвинулся журнал — ингест не выполняется ВОВСЕ (fail closed).

    Если человек удалил или вставил строки в «Історія», номер водораздела
    указывает не туда. Продолжать по нему — значит либо потерять приёмку, либо
    задвоить её, и оба исхода молчаливы. Поэтому останавливаемся и зовём человека.
    """
    settings = _settings(monkeypatch)
    account = await _account(db_session, 1303, sheet_key="Магазин")
    reader, worksheet = _reader([_event("Магазин", "SKU-1", 5)])
    await stock_ingest.ingest_intake_history(db_session, reader=reader, settings=settings)

    cursors = StockIntakeCursorRepository(db_session)
    cursor = await cursors.get(book_id="book-1", tab=HISTORY_TAB)
    assert cursor is not None and cursor.last_row == 2

    # Кто-то удалил первую строку журнала: строка №2 теперь другая.
    worksheet.rows.pop(1)
    worksheet.rows.append(_event("Магазин", "SKU-9", 100))

    result = await stock_ingest.ingest_intake_history(db_session, reader=reader, settings=settings)

    assert result.halted_reason is not None
    assert result.applied == 0
    assert await _quantity(db_session, account.id, "SKU-9") == 0
    await db_session.refresh(cursor)
    assert cursor.last_row == 2, "водораздел не должен двигаться при остановке"


async def test_unknown_tab_is_skipped_but_does_not_block_the_watermark(
    db_session: AsyncSession, monkeypatch
):
    """Чужая вкладка пропускается, а водораздел через неё проезжает.

    Иначе одна строка от несуществующего аккаунта встала бы намертво: каждый
    проход перечитывал бы её и не двигался дальше, а вся приёмка после неё —
    включая приёмку живых аккаунтов — не доехала бы никогда.
    """
    settings = _settings(monkeypatch)
    account = await _account(db_session, 1304, sheet_key="Магазин")
    reader, worksheet = _reader([])
    await stock_ingest.ingest_intake_history(db_session, reader=reader, settings=settings)

    worksheet.rows.append(_event("Невідомий Лист", "SKU-X", 50))
    worksheet.rows.append(_event("Магазин", "SKU-1", 3))

    result = await stock_ingest.ingest_intake_history(db_session, reader=reader, settings=settings)

    assert result.applied == 1
    assert result.skipped_unknown_tab == 1
    assert result.unknown_tabs == ("Невідомий Лист",)
    assert await _quantity(db_session, account.id, "SKU-1") == 3

    # И следующий проход не перечитывает пропущенную строку заново.
    again = await stock_ingest.ingest_intake_history(db_session, reader=reader, settings=settings)
    assert (again.applied, again.skipped_unknown_tab) == (0, 0)


async def test_trailing_broken_rows_do_not_stall_the_watermark(
    db_session: AsyncSession, monkeypatch
):
    """Битая строка в хвосте не должна перечитываться каждым проходом.

    Водораздел двигается по последней ПРОЧИТАННОЙ строке, а не по последнему
    применённому событию. Двигай его по событию — хвост из пустых или битых строк
    перечитывался бы вечно, а метрика отставания врала бы.
    """
    settings = _settings(monkeypatch)
    await _account(db_session, 1305, sheet_key="Магазин")
    reader, worksheet = _reader([])
    await stock_ingest.ingest_intake_history(db_session, reader=reader, settings=settings)

    worksheet.rows.append(_event("Магазин", "SKU-1", 2))
    worksheet.rows.append(["01.08.2026 10:05:00", "Магазин", "", "", "", "хтось"])

    first = await stock_ingest.ingest_intake_history(db_session, reader=reader, settings=settings)
    assert first.applied == 1
    assert first.last_row == 3, "водораздел обязан переехать через битую строку"

    cursor = await StockIntakeCursorRepository(db_session).get(book_id="book-1", tab=HISTORY_TAB)
    assert cursor is not None and cursor.last_row == 3


async def test_batch_limit_leaves_backlog_flag(db_session: AsyncSession, monkeypatch):
    """Пачка ограничена — остаток журнала виден метрикой, а не молча теряется."""
    settings = _settings(monkeypatch, STOCK_INGEST_BATCH_LIMIT=2)
    account = await _account(db_session, 1306, sheet_key="Магазин")
    reader, worksheet = _reader([])
    await stock_ingest.ingest_intake_history(db_session, reader=reader, settings=settings)

    for _ in range(5):
        worksheet.rows.append(_event("Магазин", "SKU-1", 1))

    first = await stock_ingest.ingest_intake_history(db_session, reader=reader, settings=settings)
    assert first.applied == 2 and first.backlog is True

    while True:
        result = await stock_ingest.ingest_intake_history(
            db_session, reader=reader, settings=settings
        )
        if result.applied == 0:
            break
    assert await _quantity(db_session, account.id, "SKU-1") == 5


async def test_halt_is_reported_once_per_process(monkeypatch):
    """Сигнал об остановке — один на процесс, а не 1440 сообщений в сутки."""
    stock_ingest.reset_halt_notifications()
    assert stock_ingest.should_notify_halt("book-1", "журнал змінився") is True
    assert stock_ingest.should_notify_halt("book-1", "журнал змінився") is False
    # Другая книга — свой сигнал.
    assert stock_ingest.should_notify_halt("book-2", "журнал змінився") is True
    stock_ingest.reset_halt_notifications()
    assert stock_ingest.should_notify_halt("book-1", "журнал змінився") is True
