"""Интеграционные тесты хендлеров поддержки (Фаза 6) — на Postgres.

Фейковые message/callback/bot, реальная сессия и сервис `app/services/support`.
Релей-сценарии строят треды напрямую через репозиторий, чтобы не зависеть от
текущего времени/расписания.
"""

from __future__ import annotations

from app.bot.handlers.support import (
    client_chat_message,
    client_open,
    staff_open,
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

    async def answer(self, text, reply_markup=None, parse_mode=None) -> None:
        self.answers.append({"text": text, "reply_markup": reply_markup})


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, telegram_id: int, text: str, parse_mode=None) -> None:
        self.sent.append((telegram_id, text))


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
