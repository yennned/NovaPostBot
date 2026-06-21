"""Интеграционные тесты хендлеров «👔 Персонал» (Фаза 6) — на Postgres."""

from __future__ import annotations

from app.bot import permissions as perm
from app.bot.handlers.staff import cb_flag, staff_add_input, staff_open
from app.bot.types import EffectiveContext
from app.db.models.enums import UserRole, UserStatus
from app.db.repositories import UserRepository
from sqlalchemy.ext.asyncio import AsyncSession


class FakeState:
    def __init__(self, data: dict | None = None) -> None:
        self.state = None
        self._data = data or {}

    async def clear(self) -> None:
        self._data = {}

    async def set_state(self, value) -> None:
        self.state = value

    async def update_data(self, **kw) -> None:
        self._data.update(kw)

    async def get_data(self) -> dict:
        return self._data


class FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.answers: list[dict] = []
        self.edits: list[dict] = []

    async def answer(self, text, reply_markup=None, parse_mode=None) -> None:
        self.answers.append({"text": text, "reply_markup": reply_markup})

    async def edit_text(self, text, reply_markup=None, parse_mode=None) -> None:
        self.edits.append({"text": text, "reply_markup": reply_markup})


class FakeCallback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = FakeMessage()
        self.acks: list[dict] = []

    async def answer(self, text=None, show_alert=False) -> None:
        self.acks.append({"text": text, "show_alert": show_alert})


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, telegram_id: int, text: str, parse_mode=None) -> None:
        self.sent.append((telegram_id, text))


def _owner_ctx(owner) -> EffectiveContext:
    return EffectiveContext(
        actor_user=owner, effective_user=owner, effective_role=UserRole.owner, is_dev=False
    )


async def _owner(session: AsyncSession, telegram_id: int = 1):
    return await UserRepository(session).create(
        telegram_id=telegram_id, role=UserRole.owner, status=UserStatus.active
    )


async def _manager(session: AsyncSession, telegram_id: int = 10):
    return await UserRepository(session).create(
        telegram_id=telegram_id,
        full_name="Олег",
        role=UserRole.manager,
        status=UserStatus.active,
    )


async def test_staff_open_lists(db_session: AsyncSession):
    owner = await _owner(db_session)
    await _manager(db_session)
    msg = FakeMessage()
    await staff_open(msg, _owner_ctx(owner), db_session, FakeState())
    assert msg.answers
    assert "Персонал" in str(msg.answers[0]["text"])


async def test_staff_add_input_creates_and_notifies(db_session: AsyncSession):
    owner = await _owner(db_session)
    bot = FakeBot()
    await staff_add_input(FakeMessage("555"), _owner_ctx(owner), db_session, FakeState(), bot)
    created = await UserRepository(db_session).get_by_telegram_id(555)
    assert created is not None and created.role is UserRole.manager
    assert any(tid == 555 for tid, _ in bot.sent)  # приветствие новому менеджеру


async def test_cb_flag_toggles_permission(db_session: AsyncSession):
    owner = await _owner(db_session)
    manager = await _manager(db_session)
    flag_key = perm.PERMISSION_FLAGS[0].key
    cb = FakeCallback(data=f"stf:flag:0:{manager.id}")

    await cb_flag(cb, _owner_ctx(owner), db_session)

    refreshed = await UserRepository(db_session).get_by_id(manager.id)
    assert refreshed.permissions.get(flag_key) is False  # было on по умолчанию → выключили
    assert cb.message.edits  # карточка перерисована
