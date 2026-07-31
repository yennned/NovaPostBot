"""Callback без совпадений отвечает, а не уходит в тишину — на НАСТОЯЩЕМ диспетчере.

Юнит-вызов хендлера здесь бесполезен: проверяется именно то, что происходит,
когда **ни один** хендлер не сматчился. Неотвеченный callback Telegram показывает
крутящимся спиннером на кнопке секунд тридцать — самая частая жалоба «кнопка
зависла». E2E-прогон на боевых данных поймал это пять раз у двух персон: тап по
кнопке выбора ФОП, которая отфильтрована состоянием FSM (двойной тап и тап по
старому экрану).

Диспетчер собирается ОДИН раз на модуль: роутеры — модульные синглтоны, повторное
включение падает с «Router is already attached».
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from aiogram import Dispatcher, F, Router
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TgUser
from app.bot.handlers.fallback import STALE_CALLBACK_TEXT
from app.bot.handlers.fallback import router as fallback_router

USER = TgUser(id=42, is_bot=False, first_name="Клієнт")
CHAT = Chat(id=42, type="private")


class _Flow(StatesGroup):
    step_one = State()


_probe = Router(name="fallback-probe")


@_probe.callback_query(_Flow.step_one, F.data == "flow:next")
async def _only_in_state(callback: CallbackQuery) -> None:
    """Хендлер, отфильтрованный состоянием, — как `ttn.cb_pick_sender`."""
    await callback.answer("крок пройдено")


class _RecordingBot:
    """Duck-typed бот: собирает исходящие вызовы, ничего не отправляет."""

    def __init__(self) -> None:
        self.calls: list[object] = []
        self.id = 1

    async def __call__(self, method, *args, **kwargs):
        self.calls.append(method)
        return True

    @property
    def answers(self) -> list[str | None]:
        return [
            getattr(call, "text", None)
            for call in self.calls
            if type(call).__name__ == "AnswerCallbackQuery"
        ]


@pytest.fixture(scope="module")
def dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(_probe)
    # Как в `build_dispatcher`: fallback — после всех предметных роутеров.
    dp.include_router(fallback_router)
    return dp


def _callback_update(data: str, update_id: int = 1) -> Update:
    message = Message(message_id=1, date=datetime.now(UTC), chat=CHAT, from_user=USER, text="екран")
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=f"cb-{update_id}",
            from_user=USER,
            chat_instance="ci",
            data=data,
            message=message,
        ),
    )


async def test_unmatched_callback_is_answered(dispatcher: Dispatcher) -> None:
    """Кнопка, которую никто не обрабатывает, всё равно получает ack."""
    bot = _RecordingBot()

    await dispatcher.feed_update(bot, _callback_update("cab:ttn:такого:немає"))

    assert bot.answers == [STALE_CALLBACK_TEXT]


async def test_callback_filtered_out_by_state_is_answered(dispatcher: Dispatcher) -> None:
    """Главный боевой случай: хендлер есть, но состояние уже другое.

    Ровно это и происходит при нервном двойном тапе «оберіть ФОП»: первый тап
    переводит FSM, второй не матчится ничем.
    """
    bot = _RecordingBot()

    await dispatcher.feed_update(bot, _callback_update("flow:next", update_id=2))

    assert bot.answers == [STALE_CALLBACK_TEXT]


async def test_matching_handler_still_wins(dispatcher: Dispatcher) -> None:
    """Fallback не должен перехватывать то, что обработано штатно."""
    bot = _RecordingBot()
    update = _callback_update("flow:next", update_id=3)
    await dispatcher.fsm.get_context(bot, chat_id=CHAT.id, user_id=USER.id).set_state(
        _Flow.step_one
    )

    await dispatcher.feed_update(bot, update)

    assert bot.answers == ["крок пройдено"]
