"""Клавиатуры отчётов/аналитики (Фаза 6)."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

PERIOD_LABELS = {"today": "Сьогодні", "week": "Тиждень", "month": "Місяць"}


def build_period_kb(prefix: str, active: str) -> InlineKeyboardMarkup:
    """Период-переключатель. `prefix` — namespace callback (`rep` / `an`)."""
    row = [
        InlineKeyboardButton(
            text=f"• {label}" if period == active else label,
            callback_data=f"{prefix}:p:{period}",
        )
        for period, label in PERIOD_LABELS.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row])
