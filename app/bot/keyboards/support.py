"""Клавиатуры поддержки (Фаза 6)."""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.db.models.support import SupportThread

PAGE_SIZE = 6

# Тексты reply-кнопок выхода (хендлеры ловят их в FSM-состояниях чата/ответа).
CLIENT_CHAT_EXIT = "⬅️ Завершити чат"
STAFF_REPLY_EXIT = "⬅️ Завершити відповідь"

_STATUS_MARK = {"open": "🟢", "waiting": "🟡", "closed": "⚪"}


def build_client_start_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="💬 Почати чат", callback_data="sup:start")]]
    )


def build_client_chat_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=CLIENT_CHAT_EXIT)]], resize_keyboard=True
    )


def build_staff_reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=STAFF_REPLY_EXIT)]], resize_keyboard=True
    )


def build_inbox_kb(
    threads: list[SupportThread],
    *,
    offset: int,
    total: int,
    limit: int = PAGE_SIZE,
    show_search: bool = True,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if show_search:
        rows.append([InlineKeyboardButton(text="🔎 Пошук", callback_data="sup:search")])
    for thread in threads:
        mark = _STATUS_MARK.get(thread.status.value, "•")
        name = (thread.client.full_name if thread.client else None) or "Клієнт"
        rows.append(
            [InlineKeyboardButton(text=f"{mark} {name}", callback_data=f"sup:open:{thread.id}")]
        )
    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(
            InlineKeyboardButton(text="◀️", callback_data=f"sup:inbox:{max(offset - limit, 0)}")
        )
    if offset + limit < total:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"sup:inbox:{offset + limit}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_thread_kb(
    thread: SupportThread,
    *,
    can_reply: bool,
    offset: int = 0,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if can_reply:
        rows.append(
            [
                InlineKeyboardButton(text="✍️ Відповісти", callback_data=f"sup:reply:{thread.id}"),
                InlineKeyboardButton(text="✅ Закрити", callback_data=f"sup:close:{thread.id}"),
            ]
        )
    rows.append([InlineKeyboardButton(text="◀️ До списку", callback_data=f"sup:inbox:{offset}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
