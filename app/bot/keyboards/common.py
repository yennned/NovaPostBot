"""Общие элементы inline-клавиатур: единый футер навигации.

Single-window UX: на каждом экране любой роли должен быть выход в меню роли
(«⌂ Головна» → `home:open`, обрабатывается в `start.home_callback`, работает и в
dev-impersonation через `effective_role`) и, где есть осмысленный предыдущий шаг,
контекстный «◀ <куда>». Стрелка назад — везде одна (`◀`), без emoji-варіацій,
чтобы навигация не «разъезжалась» по разделам.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton

HOME_CALLBACK = "home:open"
BACK_ARROW = "◀"


def home_button(text: str = "⌂ Головна") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=HOME_CALLBACK)


def back_button(callback_data: str, label: str = "Назад") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=f"{BACK_ARROW} {label}", callback_data=callback_data)


def nav_footer(
    *, back: str | None = None, back_label: str = "Назад"
) -> list[list[InlineKeyboardButton]]:
    """Единый нижний ряд навигации: `[◀ <back_label>] [⌂ Головна]`.

    `back=None` — экран-корень раздела (открыт из меню): только «⌂ Головна».
    """
    row: list[InlineKeyboardButton] = []
    if back is not None:
        row.append(back_button(back, back_label))
    row.append(home_button())
    return [row]


def category_chips(
    categories: list[str],
    *,
    prefix: str,
    active: str | None = None,
    per_row: int = 3,
) -> list[list[InlineKeyboardButton]]:
    """Ряды чипов-категорий для фильтра товаров (как в «Товари» и в пикере ТТН).

    «Всі» + **все** категории (перенос по `per_row` в ряд — не обрезаем до 3).
    `prefix` — namespace callback (`cab:pcat` / `cab:ttn:pcat`): кладёт
    `<prefix>:all` и `<prefix>:<idx>`, где `idx` — позиция в `categories`
    (этот же список хендлер кладёт в FSM, выбор идёт по индексу). `active` —
    выбранная категория (None → активна «Всі»), помечается «• ».
    """
    if not categories:
        return []
    chips = [
        InlineKeyboardButton(
            text=("• Всі" if active is None else "Всі"), callback_data=f"{prefix}:all"
        )
    ]
    for idx, label in enumerate(categories):
        mark = "• " if active is not None and label == active else ""
        chips.append(InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"{prefix}:{idx}"))
    return [chips[i : i + per_row] for i in range(0, len(chips), per_row)]
