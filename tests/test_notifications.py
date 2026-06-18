"""Тесты пуш-уведомлений (`app/services/notifications.py`) с фейковым Notifier."""

from __future__ import annotations

from app.db.models.enums import UserRole, UserStatus
from app.db.repositories import UserRepository
from app.services import notifications
from sqlalchemy.ext.asyncio import AsyncSession


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, telegram_id: int, text: str) -> None:
        self.sent.append((telegram_id, text))


async def test_notify_new_client_goes_to_owners_and_duty_managers(db_session: AsyncSession):
    users = UserRepository(db_session)
    await users.create(telegram_id=1, role=UserRole.owner, status=UserStatus.active)
    on_duty = await users.create(telegram_id=2, role=UserRole.manager, status=UserStatus.active)
    on_duty.on_duty = True
    await db_session.flush()
    # дежурный выкл — не получает
    await users.create(telegram_id=3, role=UserRole.manager, status=UserStatus.active)
    client = await users.create(
        telegram_id=100, phone="+380001", full_name="Іван", role=UserRole.client
    )

    notifier = FakeNotifier()
    await notifications.notify_new_client_registered(db_session, notifier, client=client)

    recipients = {tid for tid, _ in notifier.sent}
    assert recipients == {1, 2}
    assert "Нова заявка" in notifier.sent[0][1]


async def test_notify_client_approved(db_session: AsyncSession):
    users = UserRepository(db_session)
    client = await users.create(telegram_id=100, role=UserRole.client, status=UserStatus.active)

    notifier = FakeNotifier()
    await notifications.notify_client_approved(notifier, client=client)

    assert notifier.sent == [(100, notifications.client_approved_text())]
