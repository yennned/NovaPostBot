"""Тесты глобального errors-router'а.

Хендлеры дёргаем напрямую с дак-типизированным `ErrorEvent` (как в остальных
bot-тестах — без реального aiogram-апдейта и без БД).
"""

from __future__ import annotations

from types import SimpleNamespace

from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.exceptions import TelegramBadRequest
from app.bot.handlers.errors import (
    _KEY_UNREADABLE_TEXT,
    _STOCK_UNAVAILABLE_TEXT,
    _UNEXPECTED_TEXT,
    on_key_decryption_error,
    on_message_not_modified,
    on_stock_source_unavailable,
    on_unhandled_error,
)
from app.sheets import StockSourceUnavailable
from app.utils.crypto import DecryptionError


class _FakeMessage:
    def __init__(self) -> None:
        self.answers: list[str] = []

    async def answer(self, text: str) -> None:
        self.answers.append(text)


class _FakeCallback:
    def __init__(self, message=None, data: str | None = None) -> None:
        self.message = message
        self.data = data
        self.acks: list[dict] = []

    async def answer(self, text=None, show_alert=False) -> None:
        self.acks.append({"text": text, "show_alert": show_alert})


def _event(exc: Exception, *, message=None, callback=None) -> SimpleNamespace:
    return SimpleNamespace(
        update=SimpleNamespace(
            update_id=1,
            message=message,
            callback_query=callback,
            event_from_user=SimpleNamespace(id=42),
        ),
        exception=exc,
    )


_DECRYPT = DecryptionError("сменён FERNET_KEY")


async def test_decrypt_error_replies_to_message():
    msg = _FakeMessage()
    await on_key_decryption_error(_event(_DECRYPT, message=msg))
    assert msg.answers == [_KEY_UNREADABLE_TEXT]


async def test_decrypt_error_answers_the_callback():
    """Регрессия: раньше писали в `callback.message`, но сам callback не отвечали —
    Telegram оставлял на кнопке крутящийся спиннер, и она выглядела сломанной."""
    cb = _FakeCallback(message=_FakeMessage())
    await on_key_decryption_error(_event(_DECRYPT, callback=cb))
    assert cb.acks == [{"text": _KEY_UNREADABLE_TEXT, "show_alert": True}]
    assert cb.message.answers == []  # ack вместо дубля текстом


async def test_decrypt_error_without_target_does_not_crash():
    # нет ни message, ни callback (напр. inline-callback без сообщения) — просто лог
    await on_key_decryption_error(_event(_DECRYPT))


async def test_stock_unavailable_answers_callback_with_alert():
    cb = _FakeCallback(message=_FakeMessage(), data="cab:products:0")
    await on_stock_source_unavailable(_event(StockSourceUnavailable("Магазин", 429), callback=cb))
    assert cb.acks == [{"text": _STOCK_UNAVAILABLE_TEXT, "show_alert": True}]


async def test_unhandled_exception_backstop_replies():
    """Без backstop'а любое неожиданное исключение не отвечало пользователю НИЧЕГО."""
    msg = _FakeMessage()
    await on_unhandled_error(_event(ValueError("бум"), message=msg))
    assert msg.answers == [_UNEXPECTED_TEXT]


async def test_unhandled_backstop_acks_callback():
    cb = _FakeCallback(message=_FakeMessage(), data="cab:ttn:pick:0")
    await on_unhandled_error(_event(RuntimeError("бум"), callback=cb))
    assert cb.acks == [{"text": _UNEXPECTED_TEXT, "show_alert": True}]


def _bad_request_event(message: str) -> SimpleNamespace:
    return SimpleNamespace(exception=TelegramBadRequest(method=None, message=message))


async def test_message_not_modified_is_swallowed():
    # дабл-тап inline-кнопки → возврат не-UNHANDLED помечает событие обработанным (лог не пишется)
    event = _bad_request_event("message is not modified: ничего не поменялось")
    result = await on_message_not_modified(event)
    assert result is None


async def test_other_bad_request_is_passed_through():
    # реальная ошибка edit (нет сообщения/устарел callback) → UNHANDLED → пробрасывается в лог
    event = _bad_request_event("message to edit not found")
    result = await on_message_not_modified(event)
    assert result is UNHANDLED
