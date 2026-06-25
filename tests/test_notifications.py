"""Тесты пуш-уведомлений (`app/services/notifications.py`) с фейковым Notifier."""

from __future__ import annotations

from decimal import Decimal

from app.bot import permissions as perm
from app.db.models.enums import UserRole, UserStatus
from app.db.repositories import ShipmentItemDraft, ShipmentRepository, UserRepository
from app.services import notifications
from app.services.inventory import InventoryItem
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


async def test_notify_support_queued_goes_to_support_managers_not_owner(db_session: AsyncSession):
    users = UserRepository(db_session)
    # Владелец — поддержку больше НЕ получает.
    await users.create(telegram_id=1, role=UserRole.owner, status=UserStatus.active)
    # Менеджер с правом — получает (даже если не на смене).
    await users.create(telegram_id=2, role=UserRole.manager, status=UserStatus.active)
    # Менеджер без права на поддержку — не получает.
    await users.create(
        telegram_id=3,
        role=UserRole.manager,
        status=UserStatus.active,
        permissions={perm.CAN_HANDLE_SUPPORT: False},
    )

    notifier = FakeNotifier()
    await notifications.notify_support_queued_to_managers(
        db_session, notifier, client_label="Іван (+380001)"
    )

    recipients = {tid for tid, _ in notifier.sent}
    assert recipients == {2}
    assert "черзі" in notifier.sent[0][1]


async def test_notify_client_approved(db_session: AsyncSession):
    users = UserRepository(db_session)
    client = await users.create(telegram_id=100, role=UserRole.client, status=UserStatus.active)

    notifier = FakeNotifier()
    await notifications.notify_client_approved(notifier, client=client)

    assert notifier.sent == [(100, notifications.client_approved_text())]


async def test_notify_shipment_status_changed_respects_client_toggle(db_session: AsyncSession):
    users = UserRepository(db_session)
    shipments = ShipmentRepository(db_session)
    client = await users.create(
        telegram_id=101,
        role=UserRole.client,
        status=UserStatus.active,
        permissions={"notify_shipment_status": False},
    )
    shipment = await shipments.create(
        client_id=client.id,
        recipient_name="Іван",
        ttn_number="59000111",
        items=[ShipmentItemDraft(sku="SKU-1", name="Товар", quantity=1)],
    )
    notifier = FakeNotifier()

    await notifications.notify_shipment_status_changed(
        db_session,
        notifier,
        client=client,
        shipment=shipment,
    )

    assert notifier.sent == []


async def test_notify_low_stock_goes_to_client_and_staff(db_session: AsyncSession):
    users = UserRepository(db_session)
    await users.create(telegram_id=1, role=UserRole.owner, status=UserStatus.active)
    manager = await users.create(telegram_id=2, role=UserRole.manager, status=UserStatus.active)
    manager.on_duty = True
    client = await users.create(telegram_id=102, role=UserRole.client, status=UserStatus.active)
    notifier = FakeNotifier()

    await notifications.notify_low_stock(
        db_session,
        notifier,
        client=client,
        items=[
            InventoryItem(
                sku="SKU-LOW",
                name="Кава",
                category="Кава",
                stock=4,
                reserved=2,
                available=2,
                price=Decimal("100"),
            )
        ],
    )

    recipients = {tid for tid, _ in notifier.sent}
    assert recipients == {1, 2, 102}
