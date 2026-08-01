"""Кнопка нижней панели всегда доходит до своего хендлера, в любом FSM-состоянии.

Регрессия на реальный баг: клиент вводил телефон сотрудника («👥 Команда» →
«Додати»), тапал «⚙️ Налаштування» — и `invite_submit` съедал тап как «ввод
телефона», отвечая «вкажіть коректний номер». Кнопка выглядела сломанной. То же
самое делали поиск товаров/отправлений и релей поддержки, а у менеджера/власника
— поиск и ввод даты.

Тест гоняет РЕАЛЬНЫЕ роутеры в порядке `dispatcher.py` и проверяет, какой хендлер
поймал событие. Зависимости не подставляем: хендлер-назначение упадёт на `None`,
и имя его фрейма как раз показывает, кто перехватил. Это ловит именно то, что
пропускал юнит-вызов хендлера: порядок роутеров и кэш `raw_state`.
"""

from __future__ import annotations

import traceback
from datetime import UTC, datetime

import pytest
from aiogram import Router
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import Chat, Message
from aiogram.types import User as TgUser
from app.bot.handlers.account_team import router as account_team_router
from app.bot.handlers.analytics import router as analytics_router
from app.bot.handlers.client_cabinet import router as client_cabinet_router
from app.bot.handlers.manager_shipments import router as manager_shipments_router
from app.bot.handlers.menu_escape import router as menu_escape_router
from app.bot.handlers.reports import router as reports_router
from app.bot.handlers.staff import router as staff_router
from app.bot.handlers.support import router as support_router
from app.bot.keyboards.menus import MENU_TEXTS
from app.bot.states import (
    AccountTeamState,
    AnalyticsState,
    ClientCabinetState,
    ManagerShipmentState,
    ReportsState,
    StaffState,
    SupportState,
)

SETTINGS = "⚙️ Налаштування"
SHIPMENTS = "📬 Відправлення"

# Порядок = порядок include_router в app/bot/dispatcher.py.
ROUTERS: list[Router] = [
    menu_escape_router,
    manager_shipments_router,
    support_router,
    staff_router,
    reports_router,
    analytics_router,
    account_team_router,
    client_cabinet_router,
]

CASES = [
    pytest.param(None, SETTINGS, "open_settings", id="без-стейта"),
    pytest.param(SupportState.client_chatting.state, SETTINGS, "open_settings", id="чат-поддержки"),
    pytest.param(
        ClientCabinetState.waiting_for_product_search.state,
        SETTINGS,
        "open_settings",
        id="поиск-товаров",
    ),
    pytest.param(
        ClientCabinetState.waiting_for_shipment_search.state,
        SETTINGS,
        "open_settings",
        id="поиск-отправлений",
    ),
    pytest.param(
        AccountTeamState.waiting_for_phone.state,
        SETTINGS,
        "open_settings",
        id="ввод-телефона-сотрудника",
    ),
    pytest.param(
        ManagerShipmentState.waiting_for_search.state,
        SHIPMENTS,
        "open_queue",
        id="менеджер-поиск",
    ),
    pytest.param(SupportState.manager_replying.state, SHIPMENTS, "open_queue", id="менеджер-ответ"),
    pytest.param(
        StaffState.waiting_for_search.state, SHIPMENTS, "open_queue", id="владелец-поиск-персонала"
    ),
    pytest.param(StaffState.waiting_for_add.state, SHIPMENTS, "open_queue", id="владелец-найм"),
    pytest.param(ReportsState.waiting_for_date.state, SHIPMENTS, "open_queue", id="отчёты-дата"),
    pytest.param(
        AnalyticsState.waiting_for_date.state, SHIPMENTS, "open_queue", id="аналитика-дата"
    ),
]


class _FakeState:
    def __init__(self) -> None:
        self.cleared = False
        self._data: dict = {}

    async def clear(self) -> None:
        self.cleared = True
        self._data = {}

    async def set_state(self, value) -> None:
        pass

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


async def _first_handler(text: str, raw_state: str | None, state: _FakeState) -> str | None:
    """Имя хендлера, который первым поймал тап (None — никто)."""
    for router in ROUTERS:
        try:
            result = await router.propagate_event(
                "message",
                _message(text),
                state=state,
                raw_state=raw_state,
                effective_context=None,
                db_session=None,
                bot=None,
            )
        except Exception as exc:
            frames = [
                f.name
                for f in traceback.extract_tb(exc.__traceback__)
                if "app/bot/handlers" in f.filename
            ]
            return frames[0] if frames else f"?({type(exc).__name__})"
        if result is not UNHANDLED:
            return "обработал-молча"
    return None


@pytest.mark.parametrize(("raw_state", "text", "expected"), CASES)
async def test_menu_button_reaches_its_handler(raw_state, text, expected):
    state = _FakeState()

    caught = await _first_handler(text, raw_state, state)

    assert caught == expected
    assert state.cleared is True  # брошенный сценарий сброшен


async def test_menu_texts_cover_every_role_button():
    # Источник правды один: кнопка, добавленная в панель, обязана попасть в
    # MENU_TEXTS — иначе её снова начнут глотать хендлеры со свободным текстом.
    for button in (SETTINGS, SHIPMENTS, "👥 Команда", "📦 Товари", "👔 Персонал", "💬 Підтримка"):
        assert button in MENU_TEXTS
