"""Ингест приёмки: лист «Історія» → `stock_balances`.

Приёмку по-прежнему делают работники склада вручную в книге «Приймання» и жмут
«Внести» — этот путь не меняется вовсе. Меняется только то, что применённые
позиции доезжают ещё и в Postgres, чтобы остаток можно было спрашивать у БД, а не
у Google на каждом экране.

**Идемпотентность держится на двух вещах сразу**, и обе обязательны:

1. *Водораздел в той же транзакции.* `stock_intake_cursor.last_row` двигается тем
   же коммитом, что и применение дельт. Сбой на середине пачки откатывает и то и
   другое, повтор переигрывает пачку целиком и ничего не задваивает.
2. *Отпечаток строки-водораздела.* Номер строки сам по себе ничего не гарантирует:
   если человек удалил или вставил строки в «Історія», `last_row` начинает
   указывать не туда. Несовпадение отпечатка = **ингест не выполняется вовсе**
   (fail closed) плюс сигнал владельцу. Угадывать здесь нечего: и «потерять
   приёмку», и «задвоить приёмку» — молчаливая порча остатка.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models.client_account import ClientAccount
from app.db.models.enums import StockMovementType
from app.db.repositories import StockBalanceRepository, StockIntakeCursorRepository
from app.services.inventory_backend import stock_sheet_key
from app.sheets.history import HISTORY_TAB, IntakeEvent, IntakeHistoryReader
from app.sheets.runtime import run_sheets_read

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Итог одного прохода ингеста."""

    applied: int = 0
    #: События, чей лист не сопоставился ни с одним аккаунтом. Водораздел через них
    #: всё равно проезжает — иначе одна чужая строка встала бы намертво.
    skipped_unknown_tab: int = 0
    last_row: int = 1
    #: В журнале осталось ещё — следующий проход подхватит. Метрика отставания.
    backlog: bool = False
    #: Непустая строка = ингест НЕ выполнялся. Единственная причина сегодня —
    #: разошёлся отпечаток строки-водораздела.
    halted_reason: str | None = None
    unknown_tabs: tuple[str, ...] = field(default=())
    #: С какого номера водораздел пришлось переставить: журнал прибрали, строка
    #: уехала, мы нашли её по отпечатку. `None` — штатный проход.
    reanchored_from: int | None = None


#: Об остановке ингеста владельцу сообщаем один раз на процесс, а не каждый проход:
#: при интервале в минуту это было бы 1440 сообщений в сутки. Перезапуск воркера
#: сигнал повторит — и это правильно: неразобранная остановка обязана всплыть снова.
_halt_notified: set[str] = set()


def reset_halt_notifications() -> None:
    """Сбросить память об уже отправленных сигналах (тесты)."""
    _halt_notified.clear()


async def _accounts_by_sheet_key(session: AsyncSession) -> dict[str, ClientAccount]:
    """Карта «имя вкладки → аккаунт».

    Ключ считается ТОЙ ЖЕ функцией, что и у читателя остатка (`stock_sheet_key`):
    разойдись они — ингест раскладывал бы приёмку по аккаунтам иначе, чем экран её
    показывает. Замороженные аккаунты берём тоже: их приёмка физически приезжает на
    склад, и пропуск означал бы дрейф остатка.
    """
    accounts = (await session.scalars(select(ClientAccount))).all()
    mapping: dict[str, ClientAccount] = {}
    for account in accounts:
        key = stock_sheet_key(account)
        if key in mapping:
            logger.warning(
                "stock_ingest.duplicate_sheet_key",
                key=key,
                kept=str(mapping[key].id),
                dropped=str(account.id),
            )
            continue
        mapping[key] = account
    return mapping


async def ingest_intake_history(
    session: AsyncSession,
    *,
    reader: IntakeHistoryReader | None = None,
    settings: Settings | None = None,
) -> IngestResult:
    """Перенести новые события приёмки из «Історія» в `stock_balances`.

    Коммит — на вызывающем: дельты и водораздел обязаны уехать одной транзакцией.
    """
    cfg = settings or get_settings()
    book_id = cfg.sheets_stock_book_id
    if not book_id:
        return IngestResult(halted_reason="SHEETS_STOCK_BOOK_ID не настроен")

    history = reader or IntakeHistoryReader()
    cursors = StockIntakeCursorRepository(session)
    cursor = await cursors.get(book_id=book_id, tab=HISTORY_TAB)

    if cursor is None:
        # Водораздел заводится на ТЕКУЩЕМ конце журнала, а не на нуле: прошлая
        # приёмка уже сидит в количествах листа, и переигрывание задвоило бы её.
        # Отсюда требование к выкатке: backfill балансов и заведение водораздела
        # должны попасть в одно окно, с замороженной кнопкой «Внести».
        end = await run_sheets_read(history.last_row)
        window = await run_sheets_read(history.read_window, end, 0)
        cursor = await cursors.create_at(
            book_id=book_id, tab=HISTORY_TAB, row=end, fingerprint=window.watermark_fingerprint
        )
        logger.info("stock_ingest.cursor_created", book_id=book_id, row=end)
        return IngestResult(last_row=end)

    batch_limit = max(1, cfg.stock_ingest_batch_limit)
    window = await run_sheets_read(history.read_window, cursor.last_row, batch_limit)

    expected = cursor.last_row_fingerprint
    reanchored_from: int | None = None
    if expected is not None and window.watermark_fingerprint != expected:
        # Номер водораздела разошёлся с его отпечатком. Самый частый повод —
        # прибирание журнала: удалили старые, давно перенесённые строки, и всё, что
        # ниже, поехало вверх. Сама строка при этом жива, и по отпечатку её видно.
        matches = await run_sheets_read(history.locate_fingerprint, expected)
        if len(matches) != 1:
            reason = (
                f"строка-водораздел {cursor.last_row} листа «{HISTORY_TAB}» змінилася "
                "(рядки видалили або вставили) — інгест зупинено"
            )
            logger.error(
                "stock_ingest.fingerprint_mismatch",
                book_id=book_id,
                row=cursor.last_row,
                expected=expected,
                actual=window.watermark_fingerprint,
                matches=len(matches),
            )
            return IngestResult(last_row=cursor.last_row, halted_reason=reason)

        # Ровно одно попадание — двусмысленности нет: продолжаем с той же строки по
        # новому адресу. Ноль попаданий (водораздел удалили) и несколько (журнал
        # содержит одинаковые строки) остаются остановкой: там угадывать нечего, а
        # ошибка в любую сторону — молчаливая порча остатка.
        reanchored_from, moved_to = cursor.last_row, matches[0]
        logger.warning(
            "stock_ingest.reanchored", book_id=book_id, was=reanchored_from, now=moved_to
        )
        await cursors.rebase(cursor, row=moved_to)
        # Перечитываем окно с нового адреса, а не ждём следующего прохода: это одно
        # событие, и обработать его наполовину значило бы оставить в БД водораздел,
        # про который никто не знает, перенёс он что-нибудь или нет.
        window = await run_sheets_read(history.read_window, moved_to, batch_limit)

    accounts = await _accounts_by_sheet_key(session)
    balances = StockBalanceRepository(session)
    applied = 0
    unknown: dict[str, int] = {}

    for event in window.events:
        account = accounts.get(event.sheet_tab)
        if account is None:
            unknown[event.sheet_tab] = unknown.get(event.sheet_tab, 0) + 1
            continue
        await balances.apply_movement(
            account_id=account.id,
            sku=event.sku,
            delta=event.quantity,
            movement_type=StockMovementType.intake,
            comment=_comment(event),
        )
        applied += 1

    # Двигаемся по последней ПРОЧИТАННОЙ строке, а не по последнему применённому
    # событию: пустые, битые и чужие строки журнала пропускаются, и водораздел не
    # должен застревать перед ними — иначе каждый проход перечитывал бы один хвост.
    last_row = max(cursor.last_row, window.last_row_read)
    if last_row > cursor.last_row:
        await cursors.advance(cursor, row=last_row, fingerprint=window.last_row_fingerprint)

    if unknown:
        logger.warning(
            "stock_ingest.unknown_tabs", tabs=sorted(unknown), events=sum(unknown.values())
        )
    logger.info(
        "stock_ingest.pass",
        applied=applied,
        skipped_unknown_tab=sum(unknown.values()),
        last_row=last_row,
        backlog=window.truncated,
        reanchored_from=reanchored_from,
    )
    # Проход дошёл до конца — значит прошлая остановка разобрана. Снимаем признак
    # (его читает зеркало) и забываем, что о ней уже сообщали: иначе следующая
    # такая же осталась бы без сигнала до перезапуска воркера, то есть тем тише,
    # чем чаще она повторяется.
    if cursor.halted_reason is not None:
        await cursors.set_halted(cursor, reason=None)
    _forget_halt(book_id)
    return IngestResult(
        applied=applied,
        skipped_unknown_tab=sum(unknown.values()),
        last_row=last_row,
        backlog=window.truncated,
        unknown_tabs=tuple(sorted(unknown)),
        reanchored_from=reanchored_from,
    )


def _comment(event: IntakeEvent) -> str:
    """Кто и когда внёс приёмку — по одному движению на событие журнала.

    События одного SKU намеренно не схлопываются в одно движение: ценность журнала
    как раз в том, что видно каждое внесение отдельно, с автором и временем.
    """
    parts = [f"приймання · рядок {event.row}"]
    if event.raw_time:
        parts.append(event.raw_time)
    if event.who:
        parts.append(event.who)
    return " · ".join(parts)


async def mark_halted(session: AsyncSession, *, book_id: str, reason: str) -> None:
    """Записать остановку в водораздел — ОТДЕЛЬНОЙ транзакцией, после отката прохода.

    Внутри `ingest_intake_history` это сделать нельзя: проход с остановкой
    откатывается целиком (`stock_ingest_job`), и признак уехал бы вместе с ним. А
    он нужен зеркалу, которое иначе примет приёмку за ручную правку.
    """
    cursors = StockIntakeCursorRepository(session)
    cursor = await cursors.get(book_id=book_id, tab=HISTORY_TAB)
    if cursor is None or cursor.halted_reason == reason:
        return
    await cursors.set_halted(cursor, reason=reason)


def should_notify_halt(book_id: str, reason: str) -> bool:
    """Сообщать ли владельцу об остановке (один раз на процесс на книгу)."""
    key = f"{book_id}\x1f{reason}"
    if key in _halt_notified:
        return False
    _halt_notified.add(key)
    return True


def _forget_halt(book_id: str) -> None:
    """Забыть отметки об остановках этой книги — её ингест снова здоров."""
    prefix = f"{book_id}\x1f"
    for key in [key for key in _halt_notified if key.startswith(prefix)]:
        _halt_notified.discard(key)
