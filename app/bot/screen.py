"""Helpers для single-window рендера экранов."""

from __future__ import annotations

import contextlib

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

_SCREEN_CHAT_ID = "_screen_chat_id"
_SCREEN_MESSAGE_ID = "_screen_message_id"


async def remember_screen(state: FSMContext, message: Message) -> None:
    chat = getattr(message, "chat", None)
    message_id = getattr(message, "message_id", None)
    if chat is None or message_id is None:
        return
    await state.update_data(**{_SCREEN_CHAT_ID: chat.id, _SCREEN_MESSAGE_ID: message_id})


async def answer_latest_screen(
    bot: Bot,
    message: Message,
    state: FSMContext,
    text: str,
    *,
    reply_markup=None,
    parse_mode: str | None = None,
    disable_web_page_preview: bool | None = None,
) -> Message:
    """Отправить новый актуальный экран внизу чата и отключить старый."""
    data = await state.get_data()
    chat_id = data.get(_SCREEN_CHAT_ID)
    message_id = data.get(_SCREEN_MESSAGE_ID)
    edit_markup = getattr(bot, "edit_message_reply_markup", None)
    if chat_id is not None and message_id is not None and edit_markup is not None:
        with contextlib.suppress(TelegramAPIError):
            await edit_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
    kwargs = {"reply_markup": reply_markup, "parse_mode": parse_mode}
    if disable_web_page_preview is not None:
        kwargs["disable_web_page_preview"] = disable_web_page_preview
    screen = await message.answer(text, **kwargs)
    await remember_screen(state, screen)
    return screen


async def edit_stored_screen(
    bot: Bot,
    state: FSMContext,
    *,
    text: str,
    reply_markup=None,
    parse_mode: str | None = None,
    disable_web_page_preview: bool | None = None,
) -> bool:
    data = await state.get_data()
    chat_id = data.get(_SCREEN_CHAT_ID)
    message_id = data.get(_SCREEN_MESSAGE_ID)
    if chat_id is None or message_id is None:
        return False
    try:
        await bot.edit_message_text(
            text=text,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            return False
    return True
