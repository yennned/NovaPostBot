"""Чистые unit-тесты manager-side UI возвратов, без БД."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.bot.keyboards.clients import build_client_returns_kb, build_return_card_kb
from app.bot.texts.clients import client_returns_text, manager_return_card_text
from app.db.models.enums import ShipmentStatus
from app.services.manager_returns import (
    ManagerReturnCard,
    ManagerReturnListItem,
    ManagerReturnPage,
)
from app.services.shipments import ShipmentCard, ShipmentItemView


def _callbacks(markup) -> list[str]:
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data is not None
    ]


def test_manager_returns_text_and_callbacks_fit_limits():
    client_id = uuid4()
    shipment_id = uuid4()
    page = ManagerReturnPage(
        client_id=client_id,
        client_name="Клієнт",
        items=[
            ManagerReturnListItem(
                id=shipment_id,
                ttn_number="TTN-R1",
                recipient_name="Іван",
                status=ShipmentStatus.returning,
                items_count=2,
                can_receive=True,
            )
        ],
        total=1,
        limit=5,
        offset=0,
    )

    text = client_returns_text(page)
    callbacks = _callbacks(build_client_returns_kb(page, "active"))

    assert "Повернення клієнта" in text
    assert callbacks
    assert all(len(item) <= 64 for item in callbacks)


def test_manager_return_card_shows_receive_action_when_available():
    client_id = uuid4()
    shipment_id = uuid4()
    card = ManagerReturnCard(
        client_id=client_id,
        client_name="Клієнт",
        shipment=ShipmentCard(
            id=shipment_id,
            ttn_number="TTN-R2",
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
                    sku="SKU-1", name="Кава", category=None, quantity=1, unit_price=None
                )
            ],
            can_cancel=False,
        ),
        can_receive=True,
    )

    text = manager_return_card_text(card)
    callbacks = _callbacks(build_return_card_kb(card, "active", 0))

    assert "Повернення ще треба прийняти" in text
    assert any("cl:retrecv:" in item for item in callbacks)
