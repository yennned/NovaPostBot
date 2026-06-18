"""FSM состояния bot-layer."""

from aiogram.fsm.state import State, StatesGroup


class StartStates(StatesGroup):
    waiting_for_contact = State()
