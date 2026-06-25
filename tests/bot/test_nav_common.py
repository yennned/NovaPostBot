"""Тесты общих элементов клавиатур: футер навигации + чипы категорий."""

from __future__ import annotations

from app.bot.keyboards.common import HOME_CALLBACK, category_chips, nav_footer


def _texts(rows):
    return [btn.text for row in rows for btn in row]


def _cbs(rows):
    return [btn.callback_data for row in rows for btn in row]


def test_nav_footer_home_only_when_root():
    rows = nav_footer()
    assert _cbs(rows) == [HOME_CALLBACK]
    assert _texts(rows) == ["⌂ Головна"]


def test_nav_footer_back_plus_home_single_row():
    rows = nav_footer(back="cl:list:all:0", back_label="До списку")
    assert len(rows) == 1  # одним рядом
    assert _texts(rows) == ["◀ До списку", "⌂ Головна"]
    assert _cbs(rows) == ["cl:list:all:0", HOME_CALLBACK]


def test_category_chips_shows_all_categories_not_truncated():
    cats = [f"Кат{i}" for i in range(7)]  # больше 3 — раньше резалось до 3
    rows = category_chips(cats, prefix="cab:ttn:pcat", active=None)
    cbs = _cbs(rows)
    assert "cab:ttn:pcat:all" in cbs
    assert "cab:ttn:pcat:6" in cbs  # последняя категория достижима
    # «Всі» помечена активной, когда категория не выбрана
    assert "• Всі" in _texts(rows)


def test_category_chips_marks_active():
    rows = category_chips(["Кава", "Чай"], prefix="cab:pcat", active="Чай")
    texts = _texts(rows)
    assert "• Чай" in texts
    assert "Всі" in texts and "• Всі" not in texts


def test_category_chips_empty_when_no_categories():
    assert category_chips([], prefix="cab:pcat", active=None) == []
