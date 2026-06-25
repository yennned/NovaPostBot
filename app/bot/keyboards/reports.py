"""Клавиатуры отчётов/аналитики (Фаза 6)."""

from __future__ import annotations

from datetime import date, timedelta

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.keyboards.common import nav_footer

PERIOD_LABELS = {"today": "Сьогодні", "week": "Тиждень", "month": "Місяць"}
_QUICK_DAYS = 3  # сколько последних дней показывать кнопками быстрого выбора


def build_period_kb(
    prefix: str, active: str, *, selected_day: date | None = None, today: date | None = None
) -> InlineKeyboardMarkup:
    """Период-переключатель. `prefix` — namespace callback (`rep` / `an`).

    Помимо пресетов (today/week/month) — ряд последних дней + «📅 Обрати дату»
    для произвольной даты. `selected_day` подсвечивает выбранный день; при выборе
    дня пресеты не подсвечиваются (`active` им не совпадает).
    """
    period_row = [
        InlineKeyboardButton(
            text=f"• {label}" if period == active and selected_day is None else label,
            callback_data=f"{prefix}:p:{period}",
        )
        for period, label in PERIOD_LABELS.items()
    ]
    base = today or date.today()
    days_row = [
        _day_button(prefix, base - timedelta(days=shift), selected_day)
        for shift in range(_QUICK_DAYS)
    ]
    pick_row = [InlineKeyboardButton(text="📅 Обрати дату", callback_data=f"{prefix}:pick")]
    return InlineKeyboardMarkup(inline_keyboard=[period_row, days_row, pick_row, *nav_footer()])


def _day_button(prefix: str, day: date, selected_day: date | None) -> InlineKeyboardButton:
    marker = "• " if day == selected_day else ""
    return InlineKeyboardButton(
        text=f"{marker}{day.strftime('%d.%m')}",
        callback_data=f"{prefix}:day:{day.isoformat()}",
    )
