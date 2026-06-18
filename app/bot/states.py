"""FSM состояния bot-layer."""

from aiogram.fsm.state import State, StatesGroup


class StartStates(StatesGroup):
    waiting_for_contact = State()


class ClientManageState(StatesGroup):
    waiting_for_search = State()  # ждём строку поиска (в data: status-токен)
