"""Текст, набранный прямо на экране выбора товару, идёт в поиск, а не в тишину.

Экран пикера обещает «Шукайте за SKU/назвою/категорією або натисніть товар»
(`app/bot/texts/ttn.py`), но обработчик текста был подписан ТОЛЬКО на состояние
после кнопки «🔎 Пошук» (`entering_item_search`). Человек, набравший артикул на
самом экране, не получал ничего — ни результатов, ни отказа. E2E-прогон на боевых
данных дал 13 таких «текст → тишина» у всех ролей.

Проверяем на уровне фильтров роутера (без БД): достаточно ответа на вопрос
«поймал ли текст нужный хендлер», а тело хендлера покрыто отдельно.
"""

from __future__ import annotations

import traceback
from datetime import UTC, datetime

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import Chat, Message
from aiogram.types import User as TgUser
from app.bot.handlers.ttn import router as ttn_router
from app.bot.keyboards.menus import MENU_TEXTS
from app.bot.states import CreateTtnState


class _FakeState:
    def __init__(self) -> None:
        self._data: dict = {}
        self.state = None

    async def set_state(self, value) -> None:
        self.state = value

    async def update_data(self, **kw) -> None:
        self._data.update(kw)

    async def get_data(self) -> dict:
        return self._data


def _message(text: str) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=1, type="private"),
        from_user=TgUser(id=1, is_bot=False, first_name="Клієнт"),
        text=text,
    )


async def _matched_handler(text: str, raw_state: str | None) -> str | None:
    """Имя хендлера, поймавшего текст (`None` — не поймал никто).

    Именно имя, а не «поймал ли хоть кто-то»: кнопка «🚚 Створити ТТН» законно
    ловится входом в сценарий (`start_create_ttn`), и проверка «никто не взял»
    была бы неверной. Предмет теста — что поиск не съедает кнопки меню.
    """
    try:
        result = await ttn_router.propagate_event(
            "message",
            _message(text),
            state=_FakeState(),
            raw_state=raw_state,
            effective_context=None,
            db_session=None,
            bot=None,
        )
    except Exception as exc:
        # Тело зовёт `_effective_client(None)` и падает — но фильтры уже сматчились,
        # а нам нужно как раз это. Имя берём из первого кадра в `app/bot/handlers`.
        frames = [
            frame.name
            for frame in traceback.extract_tb(exc.__traceback__)
            if "app/bot/handlers" in frame.filename
        ]
        return frames[0] if frames else f"?({type(exc).__name__})"
    return None if result is UNHANDLED else "обробив-мовчки"


@pytest.mark.parametrize(
    "raw_state",
    [
        CreateTtnState.picking_items.state,
        CreateTtnState.entering_item_search.state,
    ],
)
async def test_text_on_picker_reaches_search(raw_state: str) -> None:
    assert await _matched_handler("Ide00005", raw_state) == "receive_item_search"


async def test_menu_button_is_not_treated_as_search_query() -> None:
    """Кнопка нижней панели — не поисковый запрос.

    `menu_escape` чистит состояние, но `raw_state` для фильтров уже вычислен,
    поэтому исключение по `MENU_TEXTS` обязано быть в самом фильтре (`802022a`).
    """
    for button in MENU_TEXTS:
        assert await _matched_handler(button, CreateTtnState.picking_items.state) != (
            "receive_item_search"
        ), f"кнопка меню {button!r} з'їдена пошуком товару"


async def test_command_is_not_treated_as_search_query() -> None:
    assert await _matched_handler("/start", CreateTtnState.picking_items.state) != (
        "receive_item_search"
    )


async def test_text_outside_ttn_flow_is_ignored_by_this_handler() -> None:
    """Без состояния створення ТТН хендлер поиска молчит — иначе он съедал бы
    свободный ввод чужих сценариев (поддержка, правка профиля)."""
    assert await _matched_handler("Ide00005", None) is None
