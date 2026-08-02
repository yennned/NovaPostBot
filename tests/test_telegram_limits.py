"""Исходящие Telegram: флуд-вейт не теряется, темп соблюдается.

`TelegramRetryAfter` — подкласс `TelegramAPIError`, и до правки он попадал в общий
`except` и записывался как рядовой сбой доставки. То есть нас уже могли
лимитировать, а в логах это выглядело как «пуш не пришёл»: сообщение терялось
навсегда, и отличить «заблокировал бота» от «нас придержали на 3 секунды» было
нельзя. Иерархия исключений делает такой дефект **невидимым** — поэтому тест
проверяет не текст лога, а факт доставки.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from app.bot.notify import BotNotifier, _RateLimiter


class _FakeBot:
    """Бот, который придерживает первые `flood_times` отправок."""

    def __init__(self, *, flood_times: int = 0, retry_after: int = 0) -> None:
        self.flood_times = flood_times
        self.retry_after = retry_after
        self.sent: list[tuple[int, str]] = []
        self.attempts = 0

    async def send_message(self, telegram_id: int, text: str, **_kw) -> None:
        self.attempts += 1
        if self.attempts <= self.flood_times:
            raise TelegramRetryAfter(
                method=None, message="Flood control exceeded", retry_after=self.retry_after
            )
        self.sent.append((telegram_id, text))


def _notifier(bot: _FakeBot) -> BotNotifier:
    # Ограничитель без задержек: здесь проверяется обработка флуд-вейта, а темп —
    # отдельным тестом. Иначе тест мерил бы `asyncio.sleep`.
    return BotNotifier(bot, limiter=_RateLimiter(messages_per_second=0, per_chat_interval=0.0))


async def test_flood_wait_is_retried_not_swallowed():
    """Придержанное сообщение обязано дойти со второй попытки.

    Мутация: убрать ветку `except TelegramRetryAfter` — исключение уйдёт в
    `except TelegramAPIError`, сообщение не отправится, `sent` останется пустым.
    """
    bot = _FakeBot(flood_times=1, retry_after=0)
    await _notifier(bot).send_message(555, "привіт")

    assert bot.sent == [(555, "привіт")], "флуд-вейт проглочен: сообщение потеряно"
    assert bot.attempts == 2


async def test_flood_wait_gives_up_after_retries():
    """Бесконечно ждать нельзя: Telegram называет точное время, и если и после
    него не прошло — дело не в темпе. Сдаёмся, но с отдельным логом."""
    bot = _FakeBot(flood_times=99, retry_after=0)
    await _notifier(bot).send_message(555, "привіт")

    assert bot.sent == []
    assert bot.attempts == 3, "одна попытка плюс два повтора"


async def test_ordinary_delivery_error_is_still_swallowed():
    """Заблокировавший бота пользователь не должен валить веер остальным."""

    class _Blocked(_FakeBot):
        async def send_message(self, telegram_id: int, text: str, **_kw) -> None:
            self.attempts += 1
            raise TelegramForbiddenError(method=None, message="bot was blocked by the user")

    bot = _Blocked()
    await _notifier(bot).send_message(555, "привіт")

    assert bot.attempts == 1, "обычный сбой доставки не ретраится"


async def test_global_rate_is_capped():
    """Веер не выпускается залпом: между отправками держится глобальный интервал.

    Без общего лока `asyncio.gather` выпустил бы весь веер одновременно — ровно
    то, что вызывает флуд-вейт.
    """
    bot = _FakeBot()
    notifier = BotNotifier(
        bot, limiter=_RateLimiter(messages_per_second=100, per_chat_interval=0.0)
    )

    started = time.monotonic()
    await asyncio.gather(*(notifier.send_message(i, "x") for i in range(10)))
    elapsed = time.monotonic() - started

    assert len(bot.sent) == 10
    # 10 сообщений при 100/с — не быстрее 90 мс (девять интервалов по 10 мс).
    assert elapsed >= 0.09, f"веер ушёл залпом за {elapsed * 1000:.0f} мс"


async def test_per_chat_interval_applies_to_the_same_chat():
    """Два подряд сообщения одному человеку разводятся по времени."""
    bot = _FakeBot()
    notifier = BotNotifier(
        bot, limiter=_RateLimiter(messages_per_second=1000, per_chat_interval=0.05)
    )

    started = time.monotonic()
    await notifier.send_message(777, "перше")
    await notifier.send_message(777, "друге")
    elapsed = time.monotonic() - started

    assert len(bot.sent) == 2
    assert elapsed >= 0.05, f"per-chat интервал не соблюдён: {elapsed * 1000:.0f} мс"


@pytest.mark.parametrize("chats", [2, 5])
async def test_different_chats_are_not_delayed_by_per_chat_interval(chats: int):
    """Per-chat интервал не должен тормозить веер разным людям.

    Иначе рассылка на 20 получателей растянулась бы на 20 секунд, и «уведомление»
    перестало бы быть уведомлением.
    """
    bot = _FakeBot()
    notifier = BotNotifier(
        bot, limiter=_RateLimiter(messages_per_second=1000, per_chat_interval=10.0)
    )

    started = time.monotonic()
    await asyncio.gather(*(notifier.send_message(i, "x") for i in range(chats)))
    elapsed = time.monotonic() - started

    assert len(bot.sent) == chats
    assert elapsed < 1.0, f"разные чаты ждут друг друга: {elapsed:.1f} с"


async def test_status_push_does_not_query_per_recipient(db_session):
    """Веер статуса не делает запрос на каждого получателя.

    Было `1 + (1..2)×N` SELECT **подряд**: задержка росла как N × RTT до Neon, а
    не как max(RTT). Внутри прохода трекинга, который выдаёт сотню таких вееров за
    раз, это и превращалось в минуты на ровном месте.

    Считаем запросы, а не поведение: числа сходились и у прежней реализации.
    Мутация: вернуть `_notification_enabled` в цикл — счётчик вырастет с 2 до 14.
    """
    from app.db.models.enums import MembershipStatus, UserRole, UserStatus
    from app.db.repositories import (
        ClientAccountRepository,
        ShipmentItemDraft,
        ShipmentRepository,
        UserRepository,
    )
    from app.services import notifications
    from sqlalchemy import event

    users = UserRepository(db_session)
    owner = await users.create(
        telegram_id=7000, full_name="Власник", role=UserRole.client, status=UserStatus.active
    )
    membership = await ClientAccountRepository(db_session).get_membership(user_id=owner.id)
    account = membership.account
    # `role=client` заводит человеку СВОЙ аккаунт (членство уникально по
    # пользователю), поэтому работников создаём как `manager`-ов и вручную вводим
    # в аккаунт владельца — так же, как это делает приглашение сотрудника.
    for i in range(5):
        employee = await users.create(
            telegram_id=7001 + i,
            full_name=f"Працівник {i}",
            role=UserRole.manager,
            status=UserStatus.active,
        )
        membership_row = await ClientAccountRepository(db_session).create_invited_membership(
            account_id=account.id, user=employee, invited_by_user_id=owner.id
        )
        # Веер идёт только по АКТИВНЫМ участникам — приглашённый в него не входит.
        membership_row.status = MembershipStatus.active
    shipment = await ShipmentRepository(db_session).create(
        client_id=owner.id,
        account_id=account.id,
        recipient_name="Іван",
        ttn_number="59001234",
        items=[ShipmentItemDraft(sku="A", name="Товар", quantity=1)],
    )
    await db_session.flush()

    statements: list[str] = []

    @event.listens_for(db_session.sync_session, "do_orm_execute")
    def _count(state):
        statements.append(str(state.statement)[:40])

    notifier = BotNotifier(
        _FakeBot(), limiter=_RateLimiter(messages_per_second=0, per_chat_interval=0.0)
    )
    await notifications.notify_shipment_status_changed(db_session, notifier, shipment=shipment)

    assert len(statements) <= 3, (
        f"{len(statements)} запросов на шесть получателей — вернулся запрос на каждого: "
        f"{statements}"
    )
