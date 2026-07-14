"""Интеграционные тесты хендлеров поддержки (Фаза 6) — на Postgres.

Фейковые message/callback/bot, реальная сессия и сервис `app/services/support`.
Релей-сценарии строят треды напрямую через репозиторий, чтобы не зависеть от
текущего времени/расписания.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED, SkipHandler
from aiogram.types import Chat, Message, ReplyKeyboardMarkup
from aiogram.types import User as TgUser
from app.bot import permissions as perm
from app.bot.handlers.support import (
    _can_handle_support,
    _is_staff,
    cb_open,
    client_chat_exit,
    client_chat_exit_stale,
    client_chat_message,
    client_open,
    client_start,
    staff_open,
    staff_reply_exit,
    staff_reply_message,
)
from app.bot.handlers.support import router as support_router
from app.bot.keyboards.menus import CLIENT_TEAM_BUTTON
from app.bot.states import SupportState
from app.bot.types import EffectiveContext
from app.db.models.client_account import ClientAccount, ClientAccountMembership
from app.db.models.enums import SupportThreadStatus, UserRole, UserStatus
from app.db.models.support import SupportMessage, SupportThread
from app.db.models.user import User
from app.db.repositories import ClientAccountRepository, SupportRepository, UserRepository
from app.services import support as support_service
from app.services.support import DutyContact
from sqlalchemy import delete, select
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


async def test_blocked_client_cannot_continue_existing_chat(db_session: AsyncSession):
    client = await _client(db_session)
    client.status = UserStatus.blocked
    thread = await SupportRepository(db_session).create_thread(
        client_id=client.id, status=SupportThreadStatus.open
    )
    state = FakeState({"support_thread_id": str(thread.id)})
    bot = FakeBot()

    await client_chat_message(
        FakeMessage("Спроба після блокування"),
        _ctx(client, UserRole.client),
        db_session,
        state,
        bot,
    )

    assert state.cleared is True
    assert bot.sent == []


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


async def _owner(session: AsyncSession, telegram_id: int = 1):
    return await UserRepository(session).create(
        telegram_id=telegram_id, role=UserRole.owner, status=UserStatus.active
    )


async def test_owner_has_no_support_access(db_session: AsyncSession):
    # Поддержка — функция менеджера; владелец её не обрабатывает.
    owner = await _owner(db_session)
    ctx = _ctx(owner, UserRole.owner)
    assert _is_staff(ctx) is False
    assert _can_handle_support(ctx) is False


async def test_staff_open_skips_for_owner(db_session: AsyncSession):
    owner = await _owner(db_session, telegram_id=2)
    with pytest.raises(SkipHandler):
        await staff_open(FakeMessage(), _ctx(owner, UserRole.owner), db_session, FakeState())


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


async def test_client_chat_exit_closes_thread_and_notifies_manager(db_session: AsyncSession):
    client = await _client(db_session)
    manager = await _manager(db_session)
    thread = await SupportRepository(db_session).create_thread(
        client_id=client.id, assigned_manager_id=manager.id, status=SupportThreadStatus.open
    )
    state = FakeState({"support_thread_id": str(thread.id)})
    bot = FakeBot()
    msg = FakeMessage("⬅️ Завершити чат")

    await client_chat_exit(msg, _ctx(client, UserRole.client), db_session, state, bot)

    refreshed = await SupportRepository(db_session).get_with_messages(thread.id)
    assert refreshed.status is SupportThreadStatus.closed  # клиент реально закрыл тред
    assert any(tid == manager.telegram_id for tid, _ in bot.sent)  # дежурный уведомлён
    assert state.cleared
    assert isinstance(msg.answers[-1]["reply_markup"], ReplyKeyboardMarkup)


async def test_client_chat_exit_without_thread_restores_role_menu(db_session: AsyncSession):
    client = await _client(db_session)
    msg = FakeMessage("⬅️ Завершити чат")
    state = FakeState()

    await client_chat_exit(msg, _ctx(client, UserRole.client), db_session, state, FakeBot())

    assert state.cleared
    # Выход из чата возвращает нижнюю reply-панель меню роли (она же заменяет
    # «exit»-клавиатуру) — одним сообщением, без ReplyKeyboardRemove.
    assert isinstance(msg.answers[-1]["reply_markup"], ReplyKeyboardMarkup)


async def test_client_chat_exit_stale_returns_home(db_session: AsyncSession):
    # «Завершити чат» вне активного чата (стейт потерян) — не молчим, а возвращаем домой.
    client = await _client(db_session)
    msg = FakeMessage("⬅️ Завершити чат")
    state = FakeState()

    await client_chat_exit_stale(msg, _ctx(client, UserRole.client), state)

    assert state.cleared
    assert isinstance(msg.answers[-1]["reply_markup"], ReplyKeyboardMarkup)


async def test_staff_reply_exit_restores_role_menu(db_session: AsyncSession):
    manager = await _manager(db_session)
    msg = FakeMessage("/exit")
    state = FakeState()

    await staff_reply_exit(msg, _ctx(manager, UserRole.manager), state)

    assert state.cleared
    assert isinstance(msg.answers[-1]["reply_markup"], ReplyKeyboardMarkup)


def _patch_duty(monkeypatch, *, manager=None, office_open: bool = True) -> None:
    """Зафиксировать дежурство: иначе маршрутизация треда зависит от часов работы."""

    async def _fake(session, *, settings=None, now=None) -> DutyContact:
        return DutyContact(manager=manager, window=None, office_open=office_open)

    monkeypatch.setattr("app.services.support.get_duty_contact", _fake)


async def test_client_start_creates_no_thread_and_asks_to_write(db_session: AsyncSession):
    # Тап «Почати чат» раньше сразу писал пустой тред и отвечал «Повідомлення
    # збережено», хотя клиент ничего не написал: в инбоксе висели пустышки.
    client = await _client(db_session)
    cb = FakeCallback(data="sup:start")
    state = FakeState()

    await client_start(cb, _ctx(client, UserRole.client), state)

    assert await SupportRepository(db_session).get_active_thread_for_client(client.id) is None
    assert state.state is SupportState.client_chatting
    answer = str(cb.message.answers[-1]["text"])
    assert "Напишіть повідомлення" in answer
    assert "збережено" not in answer


async def test_first_message_creates_thread_and_pings_managers(
    db_session: AsyncSession, monkeypatch
):
    # Рабочее время, дежурного нет: тред рождается вместе с текстом, и только
    # теперь менеджеры получают сигнал — а не на пустом тапе.
    client = await _client(db_session)
    manager = await _manager(db_session)
    _patch_duty(monkeypatch, manager=None, office_open=True)
    state = FakeState({"support_thread_id": ""})
    bot = FakeBot()

    await client_chat_message(
        FakeMessage("Де моя посилка?"), _ctx(client, UserRole.client), db_session, state, bot
    )

    thread = await SupportRepository(db_session).get_active_thread_for_client(client.id)
    assert thread is not None
    assert thread.status is SupportThreadStatus.waiting
    stored = await SupportRepository(db_session).get_with_messages(thread.id)
    assert [m.text for m in stored.messages] == ["Де моя посилка?"]  # тред не пустой
    assert any(tid == manager.telegram_id for tid, _ in bot.sent)  # менеджеров позвали
    assert (await state.get_data())["support_thread_id"] == str(thread.id)


async def test_client_start_denies_inactive_client(db_session: AsyncSession):
    # Заблокированный клиент должен получить отказ на входе, а не после того,
    # как напишет сообщение в чат, куда его позвали.
    client = await _client(db_session)
    client.status = UserStatus.blocked
    await db_session.flush()
    cb = FakeCallback(data="sup:start")

    await client_start(cb, _ctx(client, UserRole.client), FakeState())

    assert cb.acks[-1]["show_alert"] is True
    assert cb.message.answers == []  # в чат не зовём


async def test_first_message_routes_to_duty_manager(db_session: AsyncSession, monkeypatch):
    client = await _client(db_session)
    manager = await _manager(db_session)
    _patch_duty(monkeypatch, manager=manager, office_open=True)
    state = FakeState({"support_thread_id": ""})
    bot = FakeBot()

    await client_chat_message(
        FakeMessage("Вітаю!"), _ctx(client, UserRole.client), db_session, state, bot
    )

    thread = await SupportRepository(db_session).get_active_thread_for_client(client.id)
    assert thread.status is SupportThreadStatus.open
    assert thread.assigned_manager_id == manager.id
    assert any("Вітаю!" in text for tid, text in bot.sent if tid == manager.telegram_id)


async def test_concurrent_first_messages_create_single_thread(engine, monkeypatch):
    """Два первых сообщения из одного `getUpdates`-батча → один тред, не два.

    Тред теперь рождается на первом сообщении, а aiogram обрабатывает апдейты
    параллельными тасками с отдельными сессиями — без advisory-lock в
    `open_or_get_thread` обе таски не находят активный тред и создают по своему.
    Идёт на реальных соединениях (а не на общей savepoint-сессии), иначе
    транзакционный лок нечему сериализовать; поэтому за собой прибираем руками.
    Барьер в `get_duty_contact` сводит обе таски к get-or-create одновременно:
    без него `gather` успевает выполнить их последовательно, и гонка не
    воспроизводится — тест проходил бы и на сломанном коде.
    """
    barrier = asyncio.Barrier(2)

    async def _fake_duty(session, *, settings=None, now=None) -> DutyContact:
        await barrier.wait()
        return DutyContact(manager=None, window=None, office_open=True)

    monkeypatch.setattr("app.services.support.get_duty_contact", _fake_duty)

    async with AsyncSession(engine, expire_on_commit=False) as setup:
        client = await UserRepository(setup).create(
            telegram_id=777001,
            phone="+380507770010",
            full_name="Гонка Клієнт",
            role=UserRole.client,
            status=UserStatus.active,
        )
        await setup.commit()
        client_id = client.id

    async def _first_message() -> uuid.UUID:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            user = await session.get(User, client_id)
            result = await support_service.open_or_get_thread(session, client=user)
            await support_service.post_message(
                session, thread=result.thread, sender_role="client", text="Вітаю"
            )
            await session.commit()
            return result.thread.id

    try:
        first, second = await asyncio.gather(_first_message(), _first_message())
        assert first == second  # обе таски пишут в один тред

        async with AsyncSession(engine) as check:
            threads = (
                await check.scalars(
                    select(SupportThread).where(SupportThread.client_id == client_id)
                )
            ).all()
        assert len(threads) == 1
    finally:
        async with AsyncSession(engine) as cleanup:
            await cleanup.execute(
                delete(SupportMessage).where(
                    SupportMessage.thread_id.in_(
                        select(SupportThread.id).where(SupportThread.client_id == client_id)
                    )
                )
            )
            await cleanup.execute(delete(SupportThread).where(SupportThread.client_id == client_id))
            await cleanup.execute(
                delete(ClientAccountMembership).where(ClientAccountMembership.user_id == client_id)
            )
            await cleanup.execute(delete(ClientAccount).where(ClientAccount.id == client_id))
            await cleanup.execute(delete(User).where(User.id == client_id))
            await cleanup.commit()


@pytest.mark.parametrize("text", ["⚙️ Налаштування", "📦 Товари", CLIENT_TEAM_BUTTON])
async def test_menu_button_escapes_support_router(text: str):
    """Кнопка меню обязана ПОКИНУТЬ support_router, а не осесть в релее.

    Проверяем на реальном роутере через диспетчер: `SkipHandler` не выбрасывает
    событие из роутера, а лишь передаёт следующему хендлеру того же роутера, —
    поэтому релей сам исключает `CLIENT_MENU_TEXTS`. Если это условие потерять,
    тап «Налаштування» снова уйдёт менеджеру как сообщение обращения, а до
    `client_cabinet` (подключён после `support`) не доедет.
    """
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=1, type="private"),
        from_user=TgUser(id=1, is_bot=False, first_name="Клієнт"),
        text=text,
    )
    state = FakeState({"support_thread_id": ""})

    result = await support_router.propagate_event(
        "message",
        message,
        state=state,
        raw_state=SupportState.client_chatting.state,
    )

    assert result is UNHANDLED  # ушло дальше по роутерам — к своему хендлеру


async def test_exit_chat_keeps_team_button_for_account_owner(db_session: AsyncSession):
    # Выход из чата пересобирал нижнюю панель без «👥 Команда», и кнопка пропадала
    # до следующего /start (reply-клавиатура живёт до замены).
    client = await _client(db_session)
    # `UserRepository.create` заводит клиенту аккаунт и владельческое членство —
    # берём готовое ровно так же, как это делает middleware.
    account, membership = await ClientAccountRepository(db_session).get_context_for_user(client.id)
    ctx = EffectiveContext(
        actor_user=client,
        effective_user=client,
        effective_role=UserRole.client,
        is_dev=False,
        account=account,
        membership=membership,
    )
    msg = FakeMessage("⬅️ Завершити чат")

    await client_chat_exit(msg, ctx, db_session, FakeState(), FakeBot())

    keyboard = msg.answers[-1]["reply_markup"]
    buttons = [button.text for row in keyboard.keyboard for button in row]
    assert CLIENT_TEAM_BUTTON in buttons


async def test_exit_chat_has_no_team_button_without_membership(db_session: AsyncSession):
    client = await _client(db_session)
    msg = FakeMessage("⬅️ Завершити чат")

    await client_chat_exit(msg, _ctx(client, UserRole.client), db_session, FakeState(), FakeBot())

    keyboard = msg.answers[-1]["reply_markup"]
    buttons = [button.text for row in keyboard.keyboard for button in row]
    assert CLIENT_TEAM_BUTTON not in buttons


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
