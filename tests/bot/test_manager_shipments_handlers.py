"""Unit-тесты manager shipment handlers без БД."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from aiogram.dispatcher.event.bases import SkipHandler
from app.bot.handlers.manager_shipments import (
    cb_card,
    cb_queue,
    cb_return,
    cb_return_apply,
    open_queue,
)
from app.bot.states import ManagerShipmentState
from app.bot.types import EffectiveContext
from app.db.models.enums import ShipmentStatus, UserRole, UserStatus
from app.db.models.user import User
from app.services.manager_shipments import (
    ManagerShipmentCard,
    ManagerShipmentListItem,
    ManagerShipmentPage,
)
from app.services.shipments import ShipmentCard, ShipmentItemView


class FakeState:
    def __init__(self) -> None:
        self._data = {}
        self.state = None

    async def clear(self) -> None:
        self._data = {}

    async def update_data(self, **kwargs) -> None:
        self._data.update(kwargs)

    async def get_data(self) -> dict:
        return self._data

    async def set_state(self, value) -> None:
        self.state = value


class FakeMessage:
    def __init__(self) -> None:
        self.answers: list[dict] = []
        self.edits: list[dict] = []

    async def answer(self, text, reply_markup=None, parse_mode=None) -> None:
        self.answers.append({"text": text, "reply_markup": reply_markup, "parse_mode": parse_mode})

    async def edit_text(self, text, reply_markup=None, parse_mode=None) -> None:
        self.edits.append({"text": text, "reply_markup": reply_markup, "parse_mode": parse_mode})


class FakeCallback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = FakeMessage()
        self.acks: list[dict] = []

    async def answer(self, text=None, show_alert=False) -> None:
        self.acks.append({"text": text, "show_alert": show_alert})


def _ctx(role: UserRole) -> EffectiveContext:
    user = User(
        telegram_id=1,
        role=role,
        status=UserStatus.active,
        permissions={},
        full_name="Actor",
    )
    return EffectiveContext(
        actor_user=user,
        effective_user=user,
        effective_role=role,
        is_dev=False,
        dev_session=None,
    )


def _page() -> ManagerShipmentPage:
    return ManagerShipmentPage(
        items=[
            ManagerShipmentListItem(
                id=uuid4(),
                ttn_number="TTN-M1",
                client_name="Клієнт",
                recipient_name="Іван",
                status=ShipmentStatus.created,
                created_at=datetime.now(UTC),
                sla_deadline=datetime.now(UTC),
                sla_state="встигаємо",
            )
        ],
        total=1,
        limit=6,
        offset=0,
        bucket="created",
        query=None,
        counts={"created": 1, "confirmed": 0, "returns": 0},
    )


def _card(shipment_id) -> ManagerShipmentCard:
    return ManagerShipmentCard(
        client_name="Клієнт",
        sender_profile_name="ФОП-1",
        shipment=ShipmentCard(
            id=shipment_id,
            ttn_number="TTN-M2",
            recipient_name="Іван",
            recipient_phone="+380001",
            recipient_city="Київ",
            recipient_warehouse="Відділення 1",
            status=ShipmentStatus.created,
            created_at=datetime.now(UTC),
            status_changed_at=datetime.now(UTC),
            dispatched_at=None,
            sla_deadline=None,
            sla_met=None,
            payment_method="cod",
            payer_type="recipient",
            cod_amount=None,
            insured_amount=None,
            fee_amount=None,
            fee_free=False,
            items=[
                ShipmentItemView(
                    sku="SKU-1",
                    name="Кава",
                    category=None,
                    quantity=1,
                    unit_price=None,
                )
            ],
            can_cancel=True,
        ),
        can_confirm=True,
        can_cancel=True,
        can_receive_return=False,
        can_mark_lost=False,
        can_mark_damaged=False,
    )


async def test_open_queue_renders_for_manager(monkeypatch):
    message = FakeMessage()
    state = FakeState()
    page = _page()

    async def fake_list_queue(session, *, actor, bucket="created", query=None, limit=8, offset=0):
        return page

    monkeypatch.setattr(
        "app.bot.handlers.manager_shipments.manager_shipments.list_queue",
        fake_list_queue,
    )

    await open_queue(message, _ctx(UserRole.manager), object(), state)

    assert message.answers
    assert "Створені" in message.answers[0]["text"]


async def test_open_queue_skips_for_client():
    with pytest.raises(SkipHandler):
        await open_queue(FakeMessage(), _ctx(UserRole.client), object(), FakeState())


async def test_cb_queue_renders_page(monkeypatch):
    cb = FakeCallback("mq:list:created:0")
    state = FakeState()

    async def fake_list_queue(session, *, actor, bucket="created", query=None, limit=8, offset=0):
        return _page()

    monkeypatch.setattr(
        "app.bot.handlers.manager_shipments.manager_shipments.list_queue",
        fake_list_queue,
    )

    await cb_queue(cb, _ctx(UserRole.manager), object(), state)

    assert cb.message.edits
    assert cb.acks


async def test_cb_card_renders_card(monkeypatch):
    shipment_id = uuid4()
    cb = FakeCallback(f"mq:card:created:0:{shipment_id}")

    async def fake_get_card(session, *, actor, shipment_id):
        return _card(shipment_id)

    monkeypatch.setattr(
        "app.bot.handlers.manager_shipments.manager_shipments.get_card",
        fake_get_card,
    )

    await cb_card(cb, _ctx(UserRole.manager), object())

    assert cb.message.edits
    assert "Клієнт" in cb.message.edits[0]["text"]


async def test_cb_return_opens_inspection(monkeypatch):
    shipment_id = uuid4()
    cb = FakeCallback(f"mq:return:returns:0:{shipment_id}")
    state = FakeState()
    base = _card(shipment_id)
    card = ManagerShipmentCard(
        client_name=base.client_name,
        sender_profile_name=base.sender_profile_name,
        shipment=ShipmentCard(
            id=base.shipment.id,
            ttn_number=base.shipment.ttn_number,
            recipient_name=base.shipment.recipient_name,
            recipient_phone=base.shipment.recipient_phone,
            recipient_city=base.shipment.recipient_city,
            recipient_warehouse=base.shipment.recipient_warehouse,
            status=ShipmentStatus.returning,
            created_at=base.shipment.created_at,
            status_changed_at=base.shipment.status_changed_at,
            dispatched_at=base.shipment.dispatched_at,
            sla_deadline=base.shipment.sla_deadline,
            sla_met=base.shipment.sla_met,
            payment_method=base.shipment.payment_method,
            payer_type=base.shipment.payer_type,
            cod_amount=base.shipment.cod_amount,
            insured_amount=base.shipment.insured_amount,
            fee_amount=base.shipment.fee_amount,
            fee_free=base.shipment.fee_free,
            items=base.shipment.items,
            can_cancel=False,
        ),
        can_confirm=False,
        can_cancel=False,
        can_receive_return=True,
        can_mark_lost=True,
        can_mark_damaged=True,
    )

    async def fake_get_card(session, *, actor, shipment_id):
        return card

    monkeypatch.setattr(
        "app.bot.handlers.manager_shipments.manager_shipments.get_card",
        fake_get_card,
    )

    await cb_return(cb, _ctx(UserRole.manager), object(), state)

    assert cb.message.edits
    assert "Огляд повернення" in cb.message.edits[0]["text"]
    assert state.state == ManagerShipmentState.inspecting_return


async def test_cb_return_apply_calls_service_and_notifies(monkeypatch):
    shipment_id = uuid4()
    state = FakeState()
    await state.set_state(ManagerShipmentState.inspecting_return)
    await state.update_data(
        manager_return_shipment_id=str(shipment_id),
        manager_return_bucket="returns",
        manager_return_offset=0,
        manager_return_decisions={"SKU-1": False},
    )
    cb = FakeCallback("mq:ria")
    existing = ManagerShipmentCard(
        client_name="Клієнт",
        sender_profile_name="ФОП-1",
        shipment=ShipmentCard(
            id=shipment_id,
            ttn_number="TTN-M2",
            recipient_name="Іван",
            recipient_phone="+380001",
            recipient_city="Київ",
            recipient_warehouse="Відділення 1",
            status=ShipmentStatus.returning,
            created_at=datetime.now(UTC),
            status_changed_at=datetime.now(UTC),
            dispatched_at=None,
            sla_deadline=None,
            sla_met=None,
            payment_method="cod",
            payer_type="recipient",
            cod_amount=None,
            insured_amount=None,
            fee_amount=None,
            fee_free=False,
            items=[
                ShipmentItemView(
                    sku="SKU-1",
                    name="Кава",
                    category=None,
                    quantity=2,
                    unit_price=None,
                )
            ],
            can_cancel=False,
        ),
        can_confirm=False,
        can_cancel=False,
        can_receive_return=True,
        can_mark_lost=True,
        can_mark_damaged=True,
    )
    updated = ManagerShipmentCard(
        client_name=existing.client_name,
        sender_profile_name=existing.sender_profile_name,
        shipment=ShipmentCard(
            id=existing.shipment.id,
            ttn_number=existing.shipment.ttn_number,
            recipient_name=existing.shipment.recipient_name,
            recipient_phone=existing.shipment.recipient_phone,
            recipient_city=existing.shipment.recipient_city,
            recipient_warehouse=existing.shipment.recipient_warehouse,
            status=ShipmentStatus.returned,
            created_at=existing.shipment.created_at,
            status_changed_at=existing.shipment.status_changed_at,
            dispatched_at=existing.shipment.dispatched_at,
            sla_deadline=existing.shipment.sla_deadline,
            sla_met=existing.shipment.sla_met,
            payment_method=existing.shipment.payment_method,
            payer_type=existing.shipment.payer_type,
            cod_amount=existing.shipment.cod_amount,
            insured_amount=existing.shipment.insured_amount,
            fee_amount=existing.shipment.fee_amount,
            fee_free=existing.shipment.fee_free,
            items=existing.shipment.items,
            can_cancel=False,
        ),
        can_confirm=False,
        can_cancel=False,
        can_receive_return=False,
        can_mark_lost=False,
        can_mark_damaged=False,
    )
    captured = {}

    async def fake_get_card(session, *, actor, shipment_id):
        return existing

    async def fake_receive_return(session, *, actor, shipment_id, decisions=None):
        captured["decisions"] = decisions
        return updated

    async def fake_notify(session, notifier, *, shipment_id):
        captured["notified"] = shipment_id

    monkeypatch.setattr(
        "app.bot.handlers.manager_shipments.manager_shipments.get_card",
        fake_get_card,
    )
    monkeypatch.setattr(
        "app.bot.handlers.manager_shipments.manager_shipments.receive_return",
        fake_receive_return,
    )
    monkeypatch.setattr(
        "app.bot.handlers.manager_shipments.manager_shipments.notify_client_about_status",
        fake_notify,
    )

    class FakeSession:
        async def commit(self):
            captured["committed"] = True

    await cb_return_apply(cb, _ctx(UserRole.manager), FakeSession(), state, object())

    assert cb.message.edits
    assert captured["committed"] is True
    assert captured["notified"] == shipment_id
    assert captured["decisions"][0].accepted_quantity == 0
    assert captured["decisions"][0].rejected_quantity == 2
