"""Чистые unit-тесты manager shipment queue UI."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.bot.keyboards.manager_shipments import (
    build_card_kb,
    build_queue_kb,
    build_return_inspection_kb,
)
from app.bot.texts.manager_shipments import card_text, queue_text, return_inspection_text
from app.db.models.enums import ShipmentStatus
from app.services.manager_shipments import (
    ManagerShipmentCard,
    ManagerShipmentListItem,
    ManagerShipmentPage,
)
from app.services.shipments import ShipmentCard, ShipmentItemView


def _callbacks(markup) -> list[str]:
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data is not None
    ]


def test_manager_queue_text_and_callbacks_fit_limit():
    shipment_id = uuid4()
    page = ManagerShipmentPage(
        items=[
            ManagerShipmentListItem(
                id=shipment_id,
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

    text = queue_text(page)
    callbacks = _callbacks(build_queue_kb(page))

    assert "Створені" in text
    assert callbacks
    assert all(len(item) <= 64 for item in callbacks)


def test_manager_card_shows_expected_actions():
    shipment_id = uuid4()
    card = ManagerShipmentCard(
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
                    quantity=1,
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

    text = card_text(card)
    callbacks = _callbacks(build_card_kb("returns", 0, card))

    assert "Повернення можна оглянути" in text
    assert any("mq:return:" in item for item in callbacks)
    assert any("mq:lost:" in item for item in callbacks)
    assert any("mq:damaged:" in item for item in callbacks)


def test_return_inspection_text_and_callbacks_fit_limit():
    shipment_id = uuid4()
    card = ManagerShipmentCard(
        client_name="Клієнт",
        sender_profile_name="ФОП-1",
        shipment=ShipmentCard(
            id=shipment_id,
            ttn_number="TTN-M3",
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
                    quantity=1,
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

    text = return_inspection_text(card, {"SKU-1": False})
    callbacks = _callbacks(build_return_inspection_kb("returns", 0, card, {"SKU-1": False}))

    assert "Огляд повернення" in text
    assert "Брак" in text
    assert callbacks
    assert all(len(item) <= 64 for item in callbacks)
