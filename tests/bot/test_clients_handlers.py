"""Интеграционные тесты хендлеров раздела «Клієнти» (Фаза 2) — на Postgres.

Фейковые message/callback/bot, реальная сессия и сервис `app/services/clients`.
Без явного `@pytest.mark.asyncio` — loop сессии (как в остальных DB-тестах).
"""

from __future__ import annotations

from types import SimpleNamespace

from app.bot.handlers.clients_manage import (
    cb_action,
    cb_card,
    cb_edit_field,
    open_clients,
    receive_edit,
)
from app.bot.states import ClientManageState
from app.db.models.enums import UserRole, UserStatus
from app.db.repositories import UserRepository
from sqlalchemy.ext.asyncio import AsyncSession


class FakeState:
    def __init__(self, data: dict | None = None) -> None:
        self.cleared = False
        self.state = None
        self._data = data or {}

    async def clear(self) -> None:
        self.cleared = True

    async def set_state(self, value) -> None:
        self.state = value

    async def update_data(self, **kw) -> None:
        self._data.update(kw)

    async def get_data(self) -> dict:
        return self._data


class FakeMessage:
    def __init__(self) -> None:
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


async def _manager(session: AsyncSession, telegram_id: int = 9):
    return await UserRepository(session).create(
        telegram_id=telegram_id, role=UserRole.manager, status=UserStatus.active
    )


async def _pending(session: AsyncSession, telegram_id: int = 100):
    return await UserRepository(session).create(
        telegram_id=telegram_id,
        phone=f"+3800{telegram_id}",
        full_name="Іван Клієнт",
        role=UserRole.client,
        status=UserStatus.pending,
    )


async def test_open_clients_lists(db_session: AsyncSession):
    manager = await _manager(db_session)
    await _pending(db_session)
    msg = FakeMessage()
    await open_clients(msg, SimpleNamespace(actor_user=manager), db_session, FakeState())
    assert msg.answers
    assert "Клієнти" in str(msg.answers[0]["text"])
    assert msg.answers[0]["reply_markup"] is not None


async def test_open_clients_denies_non_staff(db_session: AsyncSession):
    client_actor = await UserRepository(db_session).create(
        telegram_id=200, role=UserRole.client, status=UserStatus.active
    )
    msg = FakeMessage()
    await open_clients(msg, SimpleNamespace(actor_user=client_actor), db_session, FakeState())
    assert "Недостатньо прав" in str(msg.answers[0]["text"])


async def test_cb_card_shows_card(db_session: AsyncSession):
    manager = await _manager(db_session)
    client = await _pending(db_session)
    cb = FakeCallback(data=f"cl:card:pending:{client.id}")
    await cb_card(cb, SimpleNamespace(actor_user=manager), db_session)
    assert cb.message.edits
    assert "Іван Клієнт" in str(cb.message.edits[0]["text"])
    assert cb.acks  # callback acknowledged


async def test_cb_action_approve_changes_status_and_notifies(db_session: AsyncSession):
    manager = await _manager(db_session)
    client = await _pending(db_session)
    bot = FakeBot()
    cb = FakeCallback(data=f"cl:act:approve:{client.id}")

    await cb_action(cb, SimpleNamespace(actor_user=manager), db_session, bot)

    refreshed = await UserRepository(db_session).get_by_id(client.id)
    assert refreshed.status is UserStatus.active
    assert any(tid == client.telegram_id for tid, _ in bot.sent)  # клиент оповещён
    assert cb.message.edits  # карточка перерисована


async def test_cb_edit_field_sets_state(db_session: AsyncSession):
    client = await _pending(db_session)
    state = FakeState()
    cb = FakeCallback(data=f"cl:editf:full_name:pending:{client.id}")
    await cb_edit_field(cb, state)
    assert state.state == ClientManageState.waiting_for_edit
    assert state._data["field"] == "full_name"
    assert state._data["client_id"] == str(client.id)


async def test_receive_edit_updates_name(db_session: AsyncSession):
    manager = await _manager(db_session)
    client = await _pending(db_session)
    state = FakeState({"client_id": str(client.id), "field": "full_name", "token": "pending"})
    msg = FakeMessage()
    msg.text = "Оновлене Імʼя"
    await receive_edit(msg, state, SimpleNamespace(actor_user=manager), db_session)
    refreshed = await UserRepository(db_session).get_by_id(client.id)
    assert refreshed.full_name == "Оновлене Імʼя"
    assert any("оновлено" in str(a["text"]).lower() for a in msg.answers)


async def test_receive_edit_phone_collision(db_session: AsyncSession):
    manager = await _manager(db_session)
    a = await _pending(db_session, telegram_id=500)
    b = await _pending(db_session, telegram_id=501)
    state = FakeState({"client_id": str(a.id), "field": "phone", "token": "pending"})
    msg = FakeMessage()
    msg.text = b.phone
    await receive_edit(msg, state, SimpleNamespace(actor_user=manager), db_session)
    assert any("зайнят" in str(x["text"]) for x in msg.answers)


async def test_cb_action_forbidden_transition_alerts(db_session: AsyncSession):
    manager = await _manager(db_session)
    active_client = await UserRepository(db_session).create(
        telegram_id=101, role=UserRole.client, status=UserStatus.active
    )
    bot = FakeBot()
    # approve активного → AlreadyInStatus → alert, статус не меняется
    cb = FakeCallback(data=f"cl:act:approve:{active_client.id}")
    await cb_action(cb, SimpleNamespace(actor_user=manager), db_session, bot)
    assert cb.acks and cb.acks[-1]["show_alert"] is True
    assert bot.sent == []
