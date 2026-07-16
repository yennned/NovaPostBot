"""Интеграционные тесты хендлеров «👔 Персонал» (Фаза 6) — на Postgres."""

from __future__ import annotations

from app.bot import permissions as perm
from app.bot.handlers.staff import (
    cb_block,
    cb_delete_ok,
    cb_flag,
    cb_unblock,
    staff_add_input,
    staff_open,
)
from app.bot.types import EffectiveContext
from app.db.models.enums import SupportThreadStatus, UserRole, UserStatus
from app.db.repositories import SupportRepository, UserRepository
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


async def test_staff_add_input_by_bare_phone_promotes(db_session: AsyncSession):
    owner = await _owner(db_session)
    users = UserRepository(db_session)
    await users.create(
        telegram_id=772,
        phone="380671112233",  # формат НП, как хранит register_contact
        role=UserRole.client,
        status=UserStatus.pending,
    )
    # Ввод без «+» (0-формат) — handler нормализует и НЕ примет за Telegram-ID.
    await staff_add_input(
        FakeMessage("0671112233"), _owner_ctx(owner), db_session, FakeState(), FakeBot()
    )
    assert (await users.get_by_telegram_id(772)).role is UserRole.manager


async def test_staff_add_input_by_phone_with_separators_precreates(db_session: AsyncSession):
    """Номер с пробелами/дефисами и незнакомый боту → предзаготовка менеджера."""
    owner = await _owner(db_session)
    users = UserRepository(db_session)
    await staff_add_input(
        FakeMessage("+380 50 999 88 77"), _owner_ctx(owner), db_session, FakeState(), FakeBot()
    )
    precreated = await users.get_by_phone("380509998877")
    assert precreated is not None
    assert precreated.telegram_id is None
    assert precreated.role is UserRole.manager


async def test_staff_add_input_rejects_garbage(db_session: AsyncSession):
    owner = await _owner(db_session)
    msg = FakeMessage("не телефон")
    await staff_add_input(msg, _owner_ctx(owner), db_session, FakeState(), FakeBot())
    assert msg.answers and "телефон" in str(msg.answers[0]["text"]).lower()


async def test_cb_flag_toggles_permission(db_session: AsyncSession):
    owner = await _owner(db_session)
    manager = await _manager(db_session)
    flag_key = perm.PERMISSION_FLAGS[0].key
    cb = FakeCallback(data=f"stf:flag:0:{manager.id}")

    await cb_flag(cb, _owner_ctx(owner), db_session)

    refreshed = await UserRepository(db_session).get_by_id(manager.id)
    assert refreshed.permissions.get(flag_key) is False  # было on по умолчанию → выключили
    assert cb.message.edits  # карточка перерисована


async def test_cb_block_and_unblock_manager(db_session: AsyncSession):
    """Обратимая альтернатива удалению доехала до UI.

    `block_manager`/`unblock_manager` жили в сервисе с самого начала, но кнопок и
    хендлеров не было — из бота они были недостижимы, и «убрать» менеджера можно
    было только безвозвратно.
    """
    owner = await _owner(db_session)
    manager = await _manager(db_session)

    await cb_block(FakeCallback(data=f"stf:block:{manager.id}"), _owner_ctx(owner), db_session)
    assert (await UserRepository(db_session).get_by_id(manager.id)).status is UserStatus.blocked

    await cb_unblock(FakeCallback(data=f"stf:unblock:{manager.id}"), _owner_ctx(owner), db_session)
    assert (await UserRepository(db_session).get_by_id(manager.id)).status is UserStatus.active


async def test_cb_block_manager_denied_for_non_owner(db_session: AsyncSession):
    manager = await _manager(db_session)
    other = await _manager(db_session, telegram_id=42)
    ctx = EffectiveContext(
        actor_user=other, effective_user=other, effective_role=UserRole.manager, is_dev=False
    )

    cb = FakeCallback(data=f"stf:block:{manager.id}")
    await cb_block(cb, ctx, db_session)

    assert (await UserRepository(db_session).get_by_id(manager.id)).status is UserStatus.active
    assert cb.acks[-1]["show_alert"] is True


async def test_cb_delete_ok_removes_manager_from_staff(db_session: AsyncSession):
    owner = await _owner(db_session)
    manager = await _manager(db_session)
    client = await UserRepository(db_session).create(
        telegram_id=200,
        full_name="Клієнт",
        role=UserRole.client,
        status=UserStatus.active,
    )
    await UserRepository(db_session).set_duty(manager, on_duty=True, duty_since=None)
    thread = await SupportRepository(db_session).create_thread(
        client_id=client.id,
        assigned_manager_id=manager.id,
        status=SupportThreadStatus.open,
    )
    manager_id = manager.id
    cb = FakeCallback(data=f"stf:deleteok:{manager.id}")

    await cb_delete_ok(cb, _owner_ctx(owner), db_session)

    # Удаление физическое: раньше здесь оставался «клиент» role=client/blocked.
    # Освобождение номера проверяет сервисный тест — здешний `_manager` без телефона.
    assert await UserRepository(db_session).get_by_id(manager_id) is None
    thread_refreshed = await SupportRepository(db_session).get_with_messages(thread.id)
    assert thread_refreshed.status is SupportThreadStatus.waiting
    assert thread_refreshed.assigned_manager_id is None
    assert cb.message.edits
    assert cb.acks[-1]["text"] == "Менеджера видалено"
