"""Errors-router на НАСТОЯЩЕМ диспетчере, а не вызовом хендлера напрямую.

Проверяем то, что юнит-вызовом проверить нельзя: aiogram матчит error-хендлеры в
порядке регистрации, поэтому обработчик без фильтра обязан стоять последним и не должен
перехватывать то, что уже обработано точечными хендлерами. Ошибка в порядке —
это либо молчащий бот (как было), либо, наоборот, съеденные штатные ветки.

Диспетчер собирается ОДИН раз на модуль: `errors_router` — модульный синглтон,
и повторное включение во второй Dispatcher падает с «Router is already attached».
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from aiogram import Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TgUser
from app.bot.handlers.errors import _STOCK_UNAVAILABLE_TEXT, _UNEXPECTED_TEXT
from app.bot.handlers.errors import router as errors_router
from app.sheets import StockSourceUnavailable

USER = TgUser(id=42, is_bot=False, first_name="Клієнт")
CHAT = Chat(id=42, type="private")

_handled: list[str] = []
_probe = Router(name="errors-probe")


@_probe.callback_query(F.data == "boom:stock")
async def _raise_stock(callback: CallbackQuery) -> None:
    raise StockSourceUnavailable("Магазин", 429)


@_probe.callback_query(F.data == "boom:any")
async def _raise_any(callback: CallbackQuery) -> None:
    raise ValueError("что-то пошло не так")


@_probe.callback_query(F.data == "boom:notmod")
async def _raise_not_modified(callback: CallbackQuery) -> None:
    raise TelegramBadRequest(method=None, message="message is not modified: ничего")


@_probe.callback_query(F.data == "ok")
async def _ok(callback: CallbackQuery) -> None:
    _handled.append("ok")


class _RecordingBot:
    """Ловит вызовы Bot API вместо сети: сюда прилетает `answerCallbackQuery`."""

    def __init__(self) -> None:
        self.calls: list[object] = []
        self.id = 1

    async def __call__(self, method, *args, **kwargs):
        self.calls.append(method)
        return True

    @property
    def texts(self) -> list[str | None]:
        return [getattr(call, "text", None) for call in self.calls]


@pytest.fixture(scope="module")
def dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(_probe)
    dp.include_router(errors_router)  # как в build_dispatcher — errors всегда последний
    return dp


def _callback_update(data: str) -> Update:
    message = Message(message_id=1, date=datetime.now(UTC), chat=CHAT, from_user=USER, text="екран")
    return Update(
        update_id=1,
        callback_query=CallbackQuery(
            id="cb-1", from_user=USER, chat_instance="ci", data=data, message=message
        ),
    )


async def test_stock_unavailable_is_answered_not_silent(dispatcher: Dispatcher):
    bot = _RecordingBot()
    await dispatcher.feed_update(bot, _callback_update("boom:stock"))
    assert _STOCK_UNAVAILABLE_TEXT in bot.texts


async def test_unexpected_exception_is_answered_not_silent(dispatcher: Dispatcher):
    """Именно этого не хватало: любая неожиданная ошибка = висящий спиннер."""
    bot = _RecordingBot()
    await dispatcher.feed_update(bot, _callback_update("boom:any"))
    assert _UNEXPECTED_TEXT in bot.texts


async def test_message_not_modified_stays_silent_after_catchall(dispatcher: Dispatcher):
    """Дабл-тап по-прежнему глушится: точечный хендлер зарегистрирован раньше
    обработчика без фильтра, иначе пользователь получал бы «сталася помилка» на
    пустом месте."""
    bot = _RecordingBot()
    await dispatcher.feed_update(bot, _callback_update("boom:notmod"))
    assert _UNEXPECTED_TEXT not in bot.texts


async def test_successful_handler_is_untouched(dispatcher: Dispatcher):
    """Обработчик без фильтра не должен вмешиваться в штатный путь."""
    _handled.clear()
    bot = _RecordingBot()
    await dispatcher.feed_update(bot, _callback_update("ok"))
    assert _handled == ["ok"]
    assert _UNEXPECTED_TEXT not in bot.texts
