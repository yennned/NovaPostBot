"""Устойчивость чтения листа «Склад» к панели-итогу «Зведення» справа от таблицы.

Панель (provision_sheets.write_side_summary) добавляет колонки справа → в строке-шапке
появляются пустые ячейки-заголовки. Без `expected_headers` gspread.get_all_records
падает на их дубликатах; с ним — читает канонические 5 колонок и игнорирует остальное.
"""

from __future__ import annotations

import gspread
import pytest
from app.sheets.client import _STOCK_EXPECTED_HEADERS, SheetsClient
from app.sheets.inventory import GoogleSheetsStockSource
from app.sheets.source import StockDelta
from scripts.provision_sheets import _PANEL_VALUE_A1, side_summary_cells

# Шапка: канонические 5 колонок + Резерв/Доступно + разрыв (H, '') + панель «Зведення»
# (I='📊 Зведення', J=''). Две пустые ячейки-заголовка ('' в H и J) — то, на чём
# gspread спотыкается без expected_headers. Строки 2–3 — товары; строка 4 — «фантом»
# панели (пустой Артикул, число итога справа).
_GRID = [
    [
        "Артикул",
        "Назва",
        "Категорія",
        "Кількість",
        "Ціна",
        "Резерв",
        "Доступно",
        "",
        "📊 Зведення",
        "",
    ],
    ["COF-1", "Кава", "Кава", "5", "100", "", "", "", "Позицій", "2"],
    ["WHL-1", "Колесо", "Колеса", "3", "250", "", "", "", "Одиниць", "8"],
    ["", "", "", "", "", "", "", "", "Вартість, ₴", "1250"],
]


class _FakeWorksheet:
    """Фейк-лист: get_all_records — реальная реализация gspread поверх нашего grid."""

    def __init__(self, grid: list[list[str]]) -> None:
        self._grid = grid

    def get(self, **kwargs):
        return self._grid

    get_all_records = gspread.Worksheet.get_all_records


class _FakeStockWorksheet(_FakeWorksheet):
    """Фейк-лист с операциями записи — для apply_deltas/write_reserved."""

    def __init__(self, grid: list[list[str]]) -> None:
        super().__init__(grid)
        self.cell_updates: list[tuple[int, int, object]] = []
        self.appended: list[list] = []
        self.range_updates: list[tuple[str, list]] = []

    def row_values(self, row: int) -> list:
        return list(self._grid[row - 1])

    def col_values(self, col: int) -> list:
        out = [r[col - 1] if col - 1 < len(r) else "" for r in self._grid]
        while out and out[-1] == "":  # gspread обрезает хвостовые пустые
            out.pop()
        return out

    def update_cell(self, row: int, col: int, value: object) -> None:
        self.cell_updates.append((row, col, value))

    def append_row(self, values: list) -> None:
        self.appended.append(values)

    def update(self, values=None, range_name=None, **kwargs) -> None:
        self.range_updates.append((range_name, values))


def _source_over(ws) -> GoogleSheetsStockSource:
    class _Client:
        def get_stock_worksheet(self, client_key):
            return ws

    return GoogleSheetsStockSource(client=_Client())


def test_apply_deltas_survives_side_panel_and_updates_quantity():
    """apply_deltas не падает на панельных пустых заголовках и правит Кількість по SKU."""
    ws = _FakeStockWorksheet([list(r) for r in _GRID])
    _source_over(ws).apply_deltas("Вася", [StockDelta(sku="COF-1", quantity_delta=-2)])
    # COF-1 (строка 2), Кількість — колонка D (4); было 5 → стало 3
    assert (2, 4, 3) in ws.cell_updates


def test_write_reserved_maps_reserved_by_sku_into_reserve_column():
    ws = _FakeStockWorksheet([list(r) for r in _GRID])
    _source_over(ws).write_reserved("Вася", {"COF-1": 2, "WHL-1": 1})
    # «Резерв» — колонка F; вектор по строкам 2..3 в порядке SKU
    assert ws.range_updates == [("F2:F3", [[2], [1]])]


def test_write_reserved_skips_sheet_without_reserve_column():
    grid = [
        ["Артикул", "Назва", "Категорія", "Кількість", "Ціна"],
        ["COF-1", "Кава", "", "5", "100"],
    ]
    ws = _FakeStockWorksheet(grid)
    _source_over(ws).write_reserved("Вася", {"COF-1": 2})
    assert ws.range_updates == []  # нет колонки «Резерв» → тихо пропускаем


def test_get_all_records_raises_on_side_panel_without_expected_headers():
    """Контроль: без expected_headers дублирующиеся пустые заголовки справа ломают чтение."""
    ws = _FakeWorksheet(_GRID)
    with pytest.raises(gspread.exceptions.GSpreadException):
        ws.get_all_records(default_blank="")


def test_get_all_records_survives_side_panel_with_expected_headers():
    ws = _FakeWorksheet(_GRID)
    records = ws.get_all_records(default_blank="", expected_headers=_STOCK_EXPECTED_HEADERS)
    assert len(records) == 3  # 2 товара + фантом-строка панели
    assert records[0]["Артикул"] == "COF-1"
    assert records[0]["Кількість"] == 5


def test_read_rows_forwards_expected_headers(monkeypatch):
    """read_rows передаёт expected_headers в get_all_records."""
    captured: dict = {}

    class _WS:
        def get_all_records(self, **kwargs):
            captured.update(kwargs)
            return [{"Артикул": "COF-1"}]

    client = SheetsClient.__new__(SheetsClient)  # без авторизации/настроек
    monkeypatch.setattr(client, "get_stock_worksheet", lambda key: _WS())
    rows = client.read_rows("Вася")

    assert captured["expected_headers"] == _STOCK_EXPECTED_HEADERS
    assert rows == [{"Артикул": "COF-1"}]


def test_read_stock_skips_side_panel_phantom_rows():
    """Источник склада парсит товары и пропускает «фантом»-строки панели справа."""
    records = _FakeWorksheet(_GRID).get_all_records(
        default_blank="", expected_headers=_STOCK_EXPECTED_HEADERS
    )

    class _Client:
        def read_rows(self, client_key):
            return list(records)

    source = GoogleSheetsStockSource(client=_Client())
    stock = source.read_stock("Вася")

    assert [r.sku for r in stock] == ["COF-1", "WHL-1"]  # пустой Артикул панели пропущен
    assert stock[0].quantity == 5
    assert str(stock[0].price) == "100"


def test_side_summary_cells_structure_and_formulas():
    cells = side_summary_cells()
    assert len(cells) == 18
    # три секции: всього / за категорією / за товаром
    assert cells[0][0].endswith("Зведення")
    assert cells[5][0] == "За категорією"
    assert cells[11][0] == "За товаром"
    # всього (открытые диапазоны → авто-захват новых строк)
    assert cells[1] == ["Позицій", "=COUNTA(A2:A)"]
    assert cells[2] == ["Одиниць", "=SUM(D2:D)"]
    assert cells[3] == ["Вартість, ₴", "=SUMPRODUCT(D2:D;E2:E)"]
    # ячейки-селекторы с дефолтами
    assert cells[6] == ["Категорія", "Всі"]
    assert cells[12] == ["Артикул", ""]
    # фильтр по категории завязан на селектор (колонка значений панели) и опцию «Всі»
    cat_ref, sku_ref = f"${_PANEL_VALUE_A1}$7", f"${_PANEL_VALUE_A1}$13"
    assert all(cat_ref in cells[i][1] for i in (7, 8, 9))
    assert all('"Всі"' in cells[i][1] for i in (7, 8, 9))
    # фильтр по товару завязан на селектор товара (VLOOKUP/SUMIF)
    assert all(sku_ref in cells[i][1] for i in (13, 14, 15, 16, 17))
    assert "VLOOKUP" in cells[13][1]
    # Локаль книги с запятой → разделитель аргументов «;», а не «,».
    assert ";" in cells[3][1] and "," not in cells[3][1]
    assert ";" in cells[8][1]
