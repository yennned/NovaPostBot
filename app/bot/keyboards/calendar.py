"""Переиспользуемый инлайн-календарь (без внешних зависимостей).

Месячная сетка 7×N с навигацией по месяцам и выбором дня. Поддерживает выбор
одного дня и диапазона «з — по» (первый клик — начало, второй — конец). Состояние
диапазона календарь сам не хранит — вызывающий передаёт `selected_from`, чтобы
подсветить начало и показать кнопку «Застосувати».

callback_data (namespace `cal:`):
- `cal:nav:YYYY-MM`  — перейти к месяцу;
- `cal:day:YYYY-MM-DD` — выбрать день;
- `cal:apply`        — применить (одиночная дата = начало диапазона);
- `cal:cancel`       — выйти из выбора;
- `cal:noop`         — пустая ячейка/заголовок (без действия).
"""

from __future__ import annotations

import calendar
from datetime import date

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

CAL_NAV = "cal:nav:"
CAL_DAY = "cal:day:"
CAL_APPLY = "cal:apply"
CAL_CANCEL = "cal:cancel"
CAL_NOOP = "cal:noop"

_WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд")
_MONTHS = (
    "Січень",
    "Лютий",
    "Березень",
    "Квітень",
    "Травень",
    "Червень",
    "Липень",
    "Серпень",
    "Вересень",
    "Жовтень",
    "Листопад",
    "Грудень",
)


def _month_shift(year: int, month: int, delta: int) -> tuple[int, int]:
    index = (year * 12 + (month - 1)) + delta
    return index // 12, index % 12 + 1


def build_calendar_kb(
    year: int, month: int, *, selected_from: date | None = None
) -> InlineKeyboardMarkup:
    """Клавиатура календаря на месяц `year-month`; `selected_from` подсвечивается."""
    prev_y, prev_m = _month_shift(year, month, -1)
    next_y, next_m = _month_shift(year, month, 1)
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="‹", callback_data=f"{CAL_NAV}{prev_y:04d}-{prev_m:02d}"),
            InlineKeyboardButton(text=f"{_MONTHS[month - 1]} {year}", callback_data=CAL_NOOP),
            InlineKeyboardButton(text="›", callback_data=f"{CAL_NAV}{next_y:04d}-{next_m:02d}"),
        ],
        [InlineKeyboardButton(text=w, callback_data=CAL_NOOP) for w in _WEEKDAYS],
    ]
    for week in calendar.Calendar(firstweekday=0).monthdayscalendar(year, month):
        row: list[InlineKeyboardButton] = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(text=" ", callback_data=CAL_NOOP))
                continue
            current = date(year, month, day)
            label = f"·{day}·" if current == selected_from else str(day)
            row.append(
                InlineKeyboardButton(text=label, callback_data=f"{CAL_DAY}{current.isoformat()}")
            )
        rows.append(row)
    footer: list[InlineKeyboardButton] = []
    if selected_from is not None:
        footer.append(InlineKeyboardButton(text="✅ Застосувати", callback_data=CAL_APPLY))
    footer.append(InlineKeyboardButton(text="◀ Назад", callback_data=CAL_CANCEL))
    rows.append(footer)
    return InlineKeyboardMarkup(inline_keyboard=rows)
