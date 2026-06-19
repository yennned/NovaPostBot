"""Тест глобального errors-router'а: непрочитанный ключ ФОП (DecryptionError).

Хендлер дёргаем напрямую с дак-типизированным `ErrorEvent` (как в остальных
bot-тестах — без реального aiogram-апдейта и без БД)."""

from __future__ import annotations

from types import SimpleNamespace

from app.bot.handlers.errors import _KEY_UNREADABLE_TEXT, on_key_decryption_error
from app.utils.crypto import DecryptionError


class _FakeMessage:
    def __init__(self) -> None:
        self.answers: list[str] = []

    async def answer(self, text: str) -> None:
        self.answers.append(text)


def _event(*, message=None, callback_message=None) -> SimpleNamespace:
    callback = SimpleNamespace(message=callback_message) if callback_message is not None else None
    return SimpleNamespace(
        update=SimpleNamespace(message=message, callback_query=callback),
        exception=DecryptionError("сменён FERNET_KEY"),
    )


async def test_decrypt_error_replies_to_message():
    msg = _FakeMessage()
    await on_key_decryption_error(_event(message=msg))
    assert msg.answers == [_KEY_UNREADABLE_TEXT]


async def test_decrypt_error_replies_via_callback_message():
    cb_msg = _FakeMessage()
    await on_key_decryption_error(_event(callback_message=cb_msg))
    assert cb_msg.answers == [_KEY_UNREADABLE_TEXT]


async def test_decrypt_error_without_target_does_not_crash():
    # нет ни message, ни callback (напр. inline-callback без сообщения) — просто лог
    await on_key_decryption_error(_event())
