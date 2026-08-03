"""Чтение листа «Історія» книги «Склад» — потока событий приёмки.

**Почему именно «Історія», а не другие кандидаты.** Приёмку применяет Apps Script
(`scripts/intake_apps_script.gs`), которого мы не трогаем, и на каждую применённую
позицию он дописывает в «Історія» строку `[Час, Лист, Артикул, Кількість+,
Накладна, Хто]` (`applyToStock_`). Лист append-only и произведён неприкосновенным
скриптом — то есть готовый event stream.

Колонка «Оброблено» книги «Приймання» на эту роль не годится: `clearRows_` удаляет
перенесённые строки сразу после записи, и опрос гарантированно терял бы данные.
Диф «Склад.Кількість против PG» тоже не годится — он неотличим от ручной правки
человеком.

Читаем **одним** запросом окно `A{водораздел}:F{водораздел+N}`: первая строка окна —
сама строка-водораздел, по ней считается отпечаток, остальные — новые события. Так
проверка целостности не стоит лишнего обращения к Google.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.sheets.client import SheetsClient
from app.sheets.source import StockSheetNotFound

#: Имя листа-журнала. Должно совпадать с `HISTORY_TAB` в `scripts/intake_apps_script.gs`.
HISTORY_TAB = "Історія"

#: Колонки (`HISTORY_HEADERS` там же): Час, Лист (клієнт), Артикул, Кількість +,
#: Накладна, Хто. Читаем по индексам, а не по заголовкам: заголовок пишется один раз
#: при создании листа, а строки — машиной, и позиционный контракт здесь честнее.
_COL_TIME, _COL_TAB, _COL_SKU, _COL_QTY, _COL_TTN, _COL_WHO = range(6)
_LAST_COLUMN = "F"


@dataclass(frozen=True, slots=True)
class IntakeEvent:
    """Одна применённая позиция приёмки. `row` — номер строки в листе."""

    row: int
    sheet_tab: str
    sku: str
    quantity: int
    raw_time: str
    ttn: str
    who: str


@dataclass(frozen=True, slots=True)
class IntakeHistoryWindow:
    """Окно журнала: отпечаток водораздела + события после него.

    `watermark_fingerprint` — `None`, если строки-водораздела в листе больше нет
    (лист укоротили). Это не «нечего читать», а сигнал нарушенной целостности:
    решает вызывающий, здесь мы только сообщаем факт.

    `last_row_read`/`last_row_fingerprint` описывают строку, на которой окно
    закончилось, — новый водораздел. Считаются здесь же, из уже прочитанного, чтобы
    сдвиг водораздела не стоил второго обращения к Google.

    Двигаться надо именно по последней ПРОЧИТАННОЙ строке, а не по последнему
    валидному событию: пустые и битые строки журнала пропускаются, и застрять
    водораздел перед ними не должен — иначе каждый проход перечитывал бы один и тот
    же хвост.
    """

    watermark_row: int
    watermark_fingerprint: str | None
    last_row_read: int
    last_row_fingerprint: str | None
    events: list[IntakeEvent]
    truncated: bool


def fingerprint_row(values: list[Any]) -> str:
    """Отпечаток строки журнала.

    Нужен, потому что номер строки сам по себе ничего не гарантирует: если человек
    удалил или вставил строки в «Історія», `last_row` начинает указывать не туда, и
    продолжать по нему — значит тихо потерять или задвоить приёмку.
    """
    payload = "\x1f".join(str(value).strip() for value in values)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _parse_quantity(raw: Any) -> int:
    text = str(raw or "").strip().replace(" ", "").replace(",", ".")
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


class IntakeHistoryReader:
    """Синхронный читатель журнала приёмки (гоняется через Sheets-executor)."""

    def __init__(self, client: SheetsClient | None = None) -> None:
        self.client = client or SheetsClient()

    def read_window(self, watermark_row: int, limit: int) -> IntakeHistoryWindow:
        """Окно журнала начиная СО строки-водораздела (включительно).

        Лист ещё не создан (приёмку ни разу не применяли) — это пустой журнал, а не
        сбой: Apps Script заводит «Історія» лениво, при первом «Внести».
        """
        try:
            worksheet = self.client.get_stock_worksheet(HISTORY_TAB)
        except StockSheetNotFound:
            return _empty_window(watermark_row)

        first = max(1, watermark_row)
        last = first + max(1, limit)
        rows = worksheet.get(f"A{first}:{_LAST_COLUMN}{last}")

        if not rows:
            # Водораздел за пределами данных: лист укоротили (или он пуст).
            return _empty_window(watermark_row)

        events = [
            event
            for offset, raw in enumerate(rows[1:], start=first + 1)
            if (event := _to_event(offset, raw)) is not None
        ]
        return IntakeHistoryWindow(
            watermark_row=watermark_row,
            watermark_fingerprint=fingerprint_row(rows[0]),
            last_row_read=first + len(rows) - 1,
            last_row_fingerprint=fingerprint_row(rows[-1]),
            events=events,
            # Окно заполнено целиком — значит в журнале почти наверняка есть ещё.
            truncated=len(rows) > limit,
        )

    def last_row(self) -> int:
        """Номер последней заполненной строки журнала (1 — только шапка/пусто).

        Нужен ровно в одном месте: при заведении водораздела «читать с этого
        момента». Заводить его на нуле нельзя — вся прошлая приёмка уже учтена в
        количествах листа, и переигрывание её задвоило бы остаток.
        """
        try:
            worksheet = self.client.get_stock_worksheet(HISTORY_TAB)
        except StockSheetNotFound:
            return 1
        return max(1, len(worksheet.col_values(1)))

    def locate_fingerprint(self, fingerprint: str) -> list[int]:
        """Номера строк (1-based), чей отпечаток равен заданному.

        Нужен, когда номер водораздела перестал совпадать с его отпечатком: строку
        могли не тронуть вовсе, а просто сдвинуть, удалив что-то выше неё. Тогда
        сама строка в листе есть, и её можно найти — а найдя, продолжить ровно с
        того места, где остановились.

        Возвращается **список**, а не первое попадание: одинаковые строки в журнале
        реальны. `applyToStock_` берёт один `new Date()` на всю пачку, поэтому два
        «Внести» в одну секунду с теми же позициями дают побайтово равные строки, а
        значит и равные отпечатки. Выбрать из них «правильную» нельзя ничем —
        решает вызывающий, здесь мы только показываем, сколько их.
        """
        try:
            worksheet = self.client.get_stock_worksheet(HISTORY_TAB)
        except StockSheetNotFound:
            return []
        last = max(1, len(worksheet.col_values(1)))
        rows = worksheet.get(f"A1:{_LAST_COLUMN}{last}")
        return [
            number
            for number, raw in enumerate(rows, start=1)
            if fingerprint_row(raw) == fingerprint
        ]


def _empty_window(watermark_row: int) -> IntakeHistoryWindow:
    return IntakeHistoryWindow(
        watermark_row=watermark_row,
        watermark_fingerprint=None,
        last_row_read=watermark_row,
        last_row_fingerprint=None,
        events=[],
        truncated=False,
    )


def _to_event(row: int, raw: list[Any]) -> IntakeEvent | None:
    if len(raw) <= _COL_QTY:
        return None
    sku = str(raw[_COL_SKU] or "").strip()
    quantity = _parse_quantity(raw[_COL_QTY])
    if not sku or quantity == 0:
        # Пустая или битая строка журнала пропускается, но водораздел через неё
        # всё равно проезжает: иначе одна кривая строка встала бы намертво.
        return None
    return IntakeEvent(
        row=row,
        sheet_tab=str(raw[_COL_TAB] or "").strip(),
        sku=sku,
        quantity=quantity,
        raw_time=str(raw[_COL_TIME] or "").strip(),
        ttn=str(raw[_COL_TTN] or "").strip() if len(raw) > _COL_TTN else "",
        who=str(raw[_COL_WHO] or "").strip() if len(raw) > _COL_WHO else "",
    )


def parse_history_time(raw: str) -> datetime | None:
    """Время события из журнала. Формат зависит от локали книги — не гадаем.

    Значение диагностическое (комментарий к движению), на идемпотентность и на
    остаток оно не влияет: порядок задаёт номер строки, а не время.
    """
    for fmt in ("%d.%m.%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None
