"""Чистые unit-тесты инлайн-календаря (`app/bot/keyboards/calendar.py`)."""

from __future__ import annotations

from datetime import date

from app.bot.keyboards.calendar import CAL_APPLY, CAL_DAY, build_calendar_kb


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def _labels(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def test_calendar_has_month_nav_and_all_days():
    markup = build_calendar_kb(2026, 7)
    cbs = _callbacks(markup)
    assert "cal:nav:2026-06" in cbs  # предыдущий месяц
    assert "cal:nav:2026-08" in cbs  # следующий месяц
    day_cbs = [c for c in cbs if c.startswith(CAL_DAY)]
    assert len(day_cbs) == 31  # июль
    assert f"{CAL_DAY}2026-07-15" in cbs


def test_calendar_without_selection_has_no_apply():
    assert CAL_APPLY not in _callbacks(build_calendar_kb(2026, 7))


def test_calendar_with_selected_from_highlights_and_shows_apply():
    markup = build_calendar_kb(2026, 7, selected_from=date(2026, 7, 5))
    assert CAL_APPLY in _callbacks(markup)
    assert "·5·" in _labels(markup)  # начало диапазона подсвечено
