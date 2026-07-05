"""Тесты хелперов провижна/привязки книги-зеркала (`scripts/provision_sheets`)."""

from __future__ import annotations

import inspect

from scripts.provision_sheets import (
    _extract_book_id,
    readonly_summary_cells,
    side_summary_cells,
    write_readonly_summary,
)

_BOOK_ID = "1AbC_dEf-GhIjKlMnOpQrStUvWxYz0123456789"


def test_extract_book_id_from_full_url():
    url = f"https://docs.google.com/spreadsheets/d/{_BOOK_ID}/edit#gid=0"
    assert _extract_book_id(url) == _BOOK_ID


def test_extract_book_id_from_bare_id():
    assert _extract_book_id(_BOOK_ID) == _BOOK_ID
    assert _extract_book_id(f"  {_BOOK_ID}  ") == _BOOK_ID


# --- D1: основная панель, «За товаром» поиск по назві -----------------------


def test_side_summary_tovar_resolves_article_via_regexextract():
    rows = side_summary_cells()
    assert len(rows) == 19
    # J13 — комбинированный селектор «Товар», J14 — резолв-артикул из него.
    assert rows[12][0] == "Товар"
    assert rows[13][0] == "Артикул"
    assert "REGEXEXTRACT" in rows[13][1]
    assert "$J$13" in rows[13][1]  # резолв читает селектор


def test_side_summary_tovar_lookups_use_resolved_article_not_selector():
    rows = side_summary_cells()
    # Строки «Назва/Категорія/Кількість/Ціна/Вартість» ищут по резолв-артикулу J14,
    # а не по сырому комбинированному селектору J13.
    for label_idx in range(14, 19):
        formula = rows[label_idx][1]
        assert "$J$14" in formula
        assert "$J$13" not in formula


# --- D2: read-only-панель зеркала, статичный разрез по категориям ------------


def test_readonly_summary_row_per_category_with_totals():
    rows = readonly_summary_cells(["Кава", "Чай"])
    assert rows[6] == ["Категорія", "Позицій", "Одиниць", "Вартість, ₴"]
    assert rows[7] == [
        "Кава",
        '=COUNTIF(C2:C;"Кава")',
        '=SUMIF(C2:C;"Кава";D2:D)',
        '=SUMPRODUCT((C2:C="Кава")*D2:D*E2:E)',
    ]
    assert rows[8][0] == "Чай"
    # Итоговая строка «Разом» по всему листу.
    assert rows[-1] == ["Разом", "=COUNTA(A2:A)", "=SUM(D2:D)", "=SUMPRODUCT(D2:D;E2:E)"]
    assert len(rows) == 7 + 2 + 1


def test_readonly_summary_empty_categories_still_valid():
    rows = readonly_summary_cells([])
    assert rows[-1][0] == "Разом"
    assert len(rows) == 8  # 7 строк шапки/«Всього» + «Разом», без категорий


def test_readonly_summary_escapes_quotes_in_category_literal():
    rows = readonly_summary_cells(['Кабель "USB"'])
    assert rows[7][1] == '=COUNTIF(C2:C;"Кабель ""USB""")'


def test_readonly_summary_panel_has_no_dropdowns():
    # Read-only книга: панель зеркала не должна ставить ни одной валидации-дропдауна.
    assert "setDataValidation" not in inspect.getsource(write_readonly_summary)
