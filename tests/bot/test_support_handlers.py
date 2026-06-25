"""Интеграционные тесты хендлеров поддержки (Фаза 6) — на Postgres.

Фейковые message/callback/bot, реальная сессия и сервис `app/services/support`.
Релей-сценарии строят треды напрямую через репозиторий, чтобы не зависеть от
текущего времени/расписания.
"""

from __future__ import annotations

from aiogram.types import ReplyKeyboardRemove
from app.bot import permissions as perm
from app.bot.handlers.support import (
    cb_open,
    client_chat_exit,
    client_chat_message,
    client_open,
    staff_open,
    staff_reply_exit,
    staff_reply_message,
)
from app.bot.types import EffectiveContext
from app.db.models.enums import SupportThreadStatus, UserRole, UserStatus
from app.db.repositories import SupportRepository, UserRepository
from sqlalchemy.ext.asyncio import AsyncSession


class FakeState:
    def __init__(self, data: dict | None = None) -> None:
        self.cleared = False
        self.state = None
        self._data = data or {}

    async def clear(self) -> None:
        self.cleared = True
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


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, telegram_id: int, text: str, parse_mode=None) -> None:
        self.sent.append((telegram_id, text))


class FakeCallback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = FakeMessage()
        self.acks: list[dict] = []

    async def answer(self, text=None, show_alert=False) -> None:
        self.acks.append({"text": text, "show_alert": show_alert})


def _ctx(user, role: UserRole) -> EffectiveContext:
    return EffectiveContext(actor_user=user, effective_user=user, effective_role=role, is_dev=False)


async def _client(session: AsyncSession, telegram_id: int = 100):
    return await UserRepository(session).create(
        telegram_id=telegram_id,
        phone=f"+38050{telegram_id}",
        full_name="Іван Клієнт",
        role=UserRole.client,
        status=UserStatus.active,
    )


async def _manager(session: AsyncSession, telegram_id: int = 9):
    return await UserRepository(session).create(
        telegram_id=telegram_id, role=UserRole.manager, status=UserStatus.active
    )


async def test_client_open_shows_duty_card(db_session: AsyncSession):
    client = await _client(db_session)
    msg = FakeMessage()
    await client_open(msg, _ctx(client, UserRole.client), db_session, FakeState())
    assert msg.answers
    assert msg.answers[0]["reply_markup"] is not None  # inline «Почати чат»


async def test_client_message_relays_to_assigned_manager(db_session: AsyncSession):
    client = await _client(db_session)
    manager = await _manager(db_session)
    thread = await SupportRepository(db_session).create_thread(
        client_id=client.id, assigned_manager_id=manager.id, status=SupportThreadStatus.open
    )
    state = FakeState({"support_thread_id": str(thread.id)})
    bot = FakeBot()

    await client_chat_message(
        FakeMessage("Де моя посилка?"), _ctx(client, UserRole.client), db_session, state, bot
    )

    assert any(tid == manager.telegram_id for tid, _ in bot.sent)
    assert any("Де моя посилка?" in text for _, text in bot.sent)


async def test_client_message_queues_when_no_manager(db_session: AsyncSession):
    client = await _client(db_session)
    thread = await SupportRepository(db_session).create_thread(
        client_id=client.id, status=SupportThreadStatus.waiting
    )
    state = FakeState({"support_thread_id": str(thread.id)})
    bot = FakeBot()
    msg = FakeMessage("Чекаю відповіді")

    await client_chat_message(msg, _ctx(client, UserRole.client), db_session, state, bot)

    assert bot.sent == []  # некому релеить — в очереди
    assert msg.answers  # клиенту — подтверждение «збережено»


async def test_staff_reply_relays_to_client_and_claims(db_session: AsyncSession):
    client = await _client(db_session)
    manager = await _manager(db_session)
    waiting = await SupportRepository(db_session).create_thread(
        client_id=client.id, status=SupportThreadStatus.waiting
    )
    state = FakeState({"support_thread_id": str(waiting.id)})
    bot = FakeBot()

    await staff_reply_message(
        FakeMessage("Вже відправили"), _ctx(manager, UserRole.manager), db_session, state, bot
    )

    assert any(tid == client.telegram_id for tid, _ in bot.sent)
    refreshed = await SupportRepository(db_session).get_with_messages(waiting.id)
    assert refreshed.assigned_manager_id == manager.id  # waiting забран дежурным
    assert refreshed.status is SupportThreadStatus.open


async def test_staff_open_lists_inbox(db_session: AsyncSession):
    manager = await _manager(db_session)
    client = await _client(db_session)
    await SupportRepository(db_session).create_thread(
        client_id=client.id, assigned_manager_id=manager.id, status=SupportThreadStatus.open
    )
    msg = FakeMessage()
    await staff_open(msg, _ctx(manager, UserRole.manager), db_session, FakeState())
    assert msg.answers
    assert "Підтримка" in str(msg.answers[0]["text"])


async def test_staff_open_denies_manager_without_support_permission(db_session: AsyncSession):
    manager = await UserRepository(db_session).create(
        telegram_id=11,
        role=UserRole.manager,
        status=UserStatus.active,
        permissions={perm.CAN_HANDLE_SUPPORT: False},
    )
    msg = FakeMessage()

    await staff_open(msg, _ctx(manager, UserRole.manager), db_session, FakeState())

    assert msg.answers
    assert "Підтримка недоступна" in str(msg.answers[0]["text"])


async def test_staff_reply_message_denies_foreign_thread_access(db_session: AsyncSession):
    client = await _client(db_session)
    assigned = await _manager(db_session, telegram_id=12)
    foreign = await _manager(db_session, telegram_id=13)
    thread = await SupportRepository(db_session).create_thread(
        client_id=client.id, assigned_manager_id=assigned.id, status=SupportThreadStatus.open
    )
    state = FakeState({"support_thread_id": str(thread.id)})
    bot = FakeBot()
    msg = FakeMessage("Я не ваш менеджер")

    await staff_reply_message(msg, _ctx(foreign, UserRole.manager), db_session, state, bot)

    assert msg.answers
    assert "недоступне" in str(msg.answers[0]["text"])
    assert bot.sent == []
    refreshed = await SupportRepository(db_session).get_with_messages(thread.id)
    assert refreshed.assigned_manager_id == assigned.id


async def test_client_chat_exit_clears_reply_keyboard(db_session: AsyncSession):
    client = await _client(db_session)
    msg = FakeMessage("/exit")
    state = FakeState()

    await client_chat_exit(msg, _ctx(client, UserRole.client), state)

    assert state.cleared
    # 1-е сообщение гасит залипшую reply-клавиатуру «Вийти з чату»,
    assert isinstance(msg.answers[0]["reply_markup"], ReplyKeyboardRemove)
    # 2-е — inline-home (не reply-клавиатура).
    assert msg.answers[1]["reply_markup"] is not None
    assert not isinstance(msg.answers[1]["reply_markup"], ReplyKeyboardRemove)


async def test_staff_reply_exit_clears_reply_keyboard(db_session: AsyncSession):
    manager = await _manager(db_session)
    msg = FakeMessage("/exit")
    state = FakeState()

    await staff_reply_exit(msg, _ctx(manager, UserRole.manager), state)

    assert state.cleared
    assert isinstance(msg.answers[0]["reply_markup"], ReplyKeyboardRemove)
    assert not isinstance(msg.answers[1]["reply_markup"], ReplyKeyboardRemove)


async def test_cb_open_denies_foreign_thread_access(db_session: AsyncSession):
    client = await _client(db_session)
    assigned = await _manager(db_session, telegram_id=14)
    foreign = await _manager(db_session, telegram_id=15)
    thread = await SupportRepository(db_session).create_thread(
        client_id=client.id, assigned_manager_id=assigned.id, status=SupportThreadStatus.open
    )
    cb = FakeCallback(data=f"sup:open:{thread.id}")

    await cb_open(cb, _ctx(foreign, UserRole.manager), db_session)

    assert cb.acks
    assert cb.acks[-1]["show_alert"] is True
    assert "іншим менеджером" in str(cb.acks[-1]["text"])
    assert cb.message.edits == []
