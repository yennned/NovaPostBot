"""Зеркало Postgres → лист «Склад»: чтение снимка и запись двух колонок.

Лист остаётся тем, на что люди смотрят и что они правят руками. Поэтому владение
делится **по колонкам**, а не по листу целиком:

| Колонка | Владелец | Почему |
|---|---|---|
| `Назва`, `Категорія`, `Ціна` | лист | описательные, конкуренции нет |
| `Кількість` | Postgres, правка ячейки принимается | под локом, от него зависит oversell |
| `Резерв` | Postgres | вычисляемое из статусов ТТН |
| `Доступно` | формула листа | `=Кількість − Резерв` |

Зеркало пишет **только** `Кількість` и `Резерв`. Описательные колонки оно не
трогает вовсе и потому физически не может откатить правку имени или цены.

Запись — полная перезапись вычисленной проекции этих двух колонок, а не дельта.
Это делает её **идемпотентной и ретраебельной**, в отличие от `apply_deltas`
(`app/sheets/runtime.py` запрещает ретраить записи именно потому, что дельта могла
примениться частично, и повтор удвоил бы её). Здесь повтор безопасен.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.sheets.client import SheetsClient
from app.sheets.source import StockSheetNotFound

#: Лист-журнал ручных правок «Кількість». Заполняет Apps Script книги «Склад»
#: (`scripts/stock_apps_script.gs`), один на всю книгу — колонка «Лист» говорит,
#: чей это склад. Должно совпадать с `EDITS_TAB` в скрипте.
EDITS_TAB = "_Правки"

_SKU_HEADERS = ("артикул", "sku")
_NAME_HEADERS = ("назва", "name")
_CATEGORY_HEADERS = ("категорія", "category")
_QUANTITY_HEADERS = ("кількість", "quantity", "qty")
_PRICE_HEADERS = ("ціна", "price")
_RESERVE_HEADERS = ("резерв", "reserved")


@dataclass(frozen=True, slots=True)
class SheetRow:
    """Строка листа как она есть сейчас, вместе с номером — адресом для записи."""

    row: int
    sku: str
    name: str
    category: str | None
    quantity: int
    price: Decimal | None
    #: Что сейчас нарисовано в колонке «Резерв». Читается вместе со всем остальным,
    #: чтобы зеркало писало только изменившиеся ячейки: без этого каждый цикл слал
    #: бы в Google по строке на каждую позицию (у крупного аккаунта их 1600).
    reserve: int


@dataclass(frozen=True, slots=True)
class SheetSnapshot:
    rows: list[SheetRow]
    quantity_col: int
    reserve_col: int | None


class MirrorSheetError(RuntimeError):
    """Лист не годится под зеркало: нет колонки «Артикул» или «Кількість».

    Отдельным типом, потому что реакция другая: это не блип и не пустой склад, а
    сломанная структура листа. Молча пропустить её — значит перестать зеркалить
    аккаунт и не сказать об этом никому.
    """


class StockSheetMirror:
    """Синхронный доступ к листу «Склад» для зеркала (через Sheets-executor)."""

    def __init__(self, client: SheetsClient | None = None) -> None:
        self.client = client or SheetsClient()

    def read_snapshot(self, client_key: str) -> SheetSnapshot:
        """Весь лист одним запросом: и позиции строк, и метаданные, и количества.

        Одним `get_values`, а не `get_all_records` + `row_values(1)`: то была бы
        двойная цена в квоте Google за те же данные.
        """
        worksheet = self.client.get_stock_worksheet(client_key)
        values = worksheet.get_values()
        if not values:
            raise MirrorSheetError(f"лист «{client_key}» порожній (немає навіть шапки)")

        header = [str(cell).strip().lower() for cell in values[0]]
        sku_col = _column(header, _SKU_HEADERS)
        quantity_col = _column(header, _QUANTITY_HEADERS)
        if sku_col is None or quantity_col is None:
            raise MirrorSheetError(f"у листі «{client_key}» немає колонок Артикул/Кількість")
        name_col = _column(header, _NAME_HEADERS)
        reserve_col = _column(header, _RESERVE_HEADERS)
        category_col = _column(header, _CATEGORY_HEADERS)
        price_col = _column(header, _PRICE_HEADERS)

        rows: list[SheetRow] = []
        for offset, raw in enumerate(values[1:], start=2):
            sku = _cell(raw, sku_col)
            if not sku:
                continue
            rows.append(
                SheetRow(
                    row=offset,
                    sku=sku,
                    name=_cell(raw, name_col) or sku,
                    category=_cell(raw, category_col) or None,
                    quantity=_to_int(_cell(raw, quantity_col)),
                    price=_to_decimal(_cell(raw, price_col)),
                    reserve=_to_int(_cell(raw, reserve_col)),
                )
            )
        return SheetSnapshot(rows=rows, quantity_col=quantity_col, reserve_col=reserve_col)

    def read_edit_authors(self, client_key: str) -> dict[tuple[str, int], str]:
        """Кто правил количество в этом листе: `{(артикул, новое значение): хто}`.

        Ключ включает само значение, а не только артикул: между циклами зеркала
        человек мог поправить одну позицию дважды, и приписать движению автора
        первой правки было бы хуже, чем не приписать никого.

        Листа нет — Apps Script в книге не установлен. Это не сбой: зеркало
        работало без него всегда, автор просто останется неизвестным.
        """
        try:
            worksheet = self.client.get_stock_worksheet(EDITS_TAB)
        except StockSheetNotFound:
            return {}
        values = worksheet.get_values()
        if len(values) < 2:
            return {}

        header = [str(cell).strip().lower() for cell in values[0]]
        tab_col = _column(header, ("лист",))
        sku_col = _column(header, _SKU_HEADERS)
        now_col = _column(header, ("стало",))
        who_col = _column(header, ("хто",))
        if not (tab_col and sku_col and now_col and who_col):
            return {}

        authors: dict[tuple[str, int], str] = {}
        for raw in values[1:]:
            if _cell(raw, tab_col) != client_key:
                continue
            sku = _cell(raw, sku_col)
            who = _cell(raw, who_col)
            if not sku or not who or who == "—":
                continue
            # Позже по журналу — вернее: перезаписываем, а не пропускаем.
            authors[(sku, _to_int(_cell(raw, now_col)))] = who
        return authors

    def write_columns(self, client_key: str, updates: list[tuple[int, int, int]]) -> None:
        """Записать ячейки `(строка, колонка, значение)` одним batch-запросом.

        Полная перезапись вычисленных значений, а не дельта, — поэтому повтор
        безопасен и запись можно ретраить.
        """
        if not updates:
            return
        from gspread.utils import rowcol_to_a1

        worksheet = self.client.get_stock_worksheet(client_key)
        worksheet.batch_update(
            [{"range": rowcol_to_a1(row, col), "values": [[value]]} for row, col, value in updates]
        )


def _column(header: list[str], aliases: tuple[str, ...]) -> int | None:
    for alias in aliases:
        if alias in header:
            return header.index(alias) + 1
    return None


def _cell(raw: list[Any], col: int | None) -> str:
    if col is None or len(raw) < col:
        return ""
    return str(raw[col - 1] or "").strip()


def _to_int(raw: str) -> int:
    text = raw.replace(" ", "").replace(" ", "").replace(",", ".")
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def _to_decimal(raw: str) -> Decimal | None:
    text = raw.replace(" ", "").replace(" ", "").replace(",", ".")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None
