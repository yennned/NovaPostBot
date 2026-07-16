"""Клавиатуры управления персоналом (👔, Фаза 6)."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.keyboards.common import nav_footer
from app.db.models.enums import UserStatus
from app.services.staff import StaffCard, StaffPage

PAGE_SIZE = 8


def build_list_kb(page: StaffPage) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="➕ Додати", callback_data="stf:add"),
            InlineKeyboardButton(text="🔎 Пошук", callback_data="stf:search"),
        ]
    ]
    for item in page.items:
        status_mark = "🚫" if item.status is UserStatus.blocked else ("🟢" if item.on_duty else "•")
        # ПІБ → телефон → Telegram-ID: у менеджера, заведённого по телефону, ещё нет
        # ни имени, ни telegram_id — показываем номер, а не «None».
        name = item.full_name or item.phone or str(item.telegram_id or "—")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{status_mark} {name}", callback_data=f"stf:card:{item.id}"
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if page.offset > 0:
        nav.append(
            InlineKeyboardButton(
                text="◀", callback_data=f"stf:list:{max(page.offset - page.limit, 0)}"
            )
        )
    if page.offset + page.limit < page.total:
        nav.append(
            InlineKeyboardButton(text="▶", callback_data=f"stf:list:{page.offset + page.limit}")
        )
    if nav:
        rows.append(nav)
    rows.extend(nav_footer())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_card_kb(card: StaffCard) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for index, flag in enumerate(card.permissions):
        mark = "✅" if flag.enabled else "⬜"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} {flag.label}", callback_data=f"stf:flag:{index}:{card.id}"
                )
            ]
        )
    # Блокировка — обратимая альтернатива удалению. `block_manager`/`unblock_manager`
    # в сервисе есть с самого начала, но кнопок не было: из UI они были недостижимы,
    # и единственным способом «убрать» менеджера оставалось удаление.
    if card.status is UserStatus.blocked:
        rows.append(
            [InlineKeyboardButton(text="✅ Розблокувати", callback_data=f"stf:unblock:{card.id}")]
        )
    else:
        rows.append(
            [InlineKeyboardButton(text="🚫 Заблокувати", callback_data=f"stf:block:{card.id}")]
        )
    rows.append(
        [InlineKeyboardButton(text="🗑 Видалити менеджера", callback_data=f"stf:delete:{card.id}")]
    )
    rows.extend(nav_footer(back="stf:list:0", back_label="До списку"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_delete_confirm_kb(card: StaffCard) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Так, видалити", callback_data=f"stf:deleteok:{card.id}"
                )
            ],
            *nav_footer(back=f"stf:card:{card.id}", back_label="Скасувати"),
        ]
    )
