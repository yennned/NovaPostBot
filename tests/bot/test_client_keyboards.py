"""Тесты callback_data клавиатур кабинета клиента."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.bot.keyboards.client import (
    build_inventory_kb,
    build_sender_profile_kb,
    build_settings_kb,
    build_shipment_card_kb,
    build_shipments_kb,
)
from app.db.models.enums import OrgType
from app.services.client_settings import ClientSettingsView, NotificationSettingView
from app.services.inventory import InventoryPage
from app.services.sender_profile import SenderProfileView
from app.services.shipments import ShipmentPage


def _all_callbacks(markup) -> list[str]:
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data is not None
    ]


def test_settings_callbacks_fit_telegram_limit():
    view = ClientSettingsView(
        full_name="Клієнт",
        phone="+380001",
        notifications=[
            NotificationSettingView(
                key="notify_registration_approved",
                label="Підтвердження реєстрації",
                enabled=True,
            ),
            NotificationSettingView(
                key="notify_shipment_status",
                label="Статуси відправлень",
                enabled=True,
            ),
            NotificationSettingView(
                key="notify_low_stock",
                label="Залишки та low-stock",
                enabled=True,
            ),
        ],
        sender_profiles_count=1,
        default_sender_name="ФОП-1",
    )

    callbacks = _all_callbacks(build_settings_kb(view))

    assert callbacks
    assert all(len(item) <= 64 for item in callbacks)


def test_sender_profile_callbacks_fit_telegram_limit():
    profile = SenderProfileView(
        id=uuid4(),
        client_id=uuid4(),
        name="ФОП-1",
        org_type=OrgType.fop,
        edrpou="12345678",
        sender_full_name="Іван",
        sender_phone="+380001",
        is_default=False,
        has_api_key=True,
        is_np_validated=False,
        created_at=datetime.now(UTC),
    )

    callbacks = _all_callbacks(build_sender_profile_kb(profile))

    assert callbacks
    assert all(len(item) <= 64 for item in callbacks)


def test_shipment_card_cancel_callback_contains_shipment_id():
    shipment_id = uuid4()
    callbacks = _all_callbacks(build_shipment_card_kb("created", 0, shipment_id, can_cancel=True))

    assert f"cab:cancel:created:0:{shipment_id}" in callbacks
    assert all(len(item) <= 64 for item in callbacks)


def _shipment_page() -> ShipmentPage:
    return ShipmentPage(items=[], total=0, limit=10, offset=0)


def _inventory_page() -> InventoryPage:
    return InventoryPage(items=[], total=0, limit=10, offset=0, categories=[])


def test_shipments_reset_button_only_with_active_search():
    # Без активного поиска «Скинути» не показываем (иначе сброс — no-op-редактирование).
    assert all(
        "cab:sclear" not in cb
        for cb in _all_callbacks(build_shipments_kb(_shipment_page(), "created"))
    )
    # С активным поиском «Скинути» появляется.
    assert any(
        "cab:sclear" in cb
        for cb in _all_callbacks(build_shipments_kb(_shipment_page(), "created", query="TTN-1"))
    )


def test_inventory_reset_button_only_with_active_filter():
    # Ни поиска, ни категории → кнопки нет.
    no_filter = _all_callbacks(build_inventory_kb(_inventory_page()))
    assert all("cab:pclear" not in cb for cb in no_filter)
    # Активный поиск → кнопка есть.
    assert any(
        "cab:pclear" in cb
        for cb in _all_callbacks(build_inventory_kb(_inventory_page(), query="товар"))
    )
    # Активная категория → кнопка есть.
    assert any(
        "cab:pclear" in cb
        for cb in _all_callbacks(build_inventory_kb(_inventory_page(), active_category="Одяг"))
    )


def test_inventory_shows_sheet_link_only_when_url_present():
    # Без книги-зеркала ссылки нет.
    no_link = build_inventory_kb(_inventory_page())
    urls = [b.url for row in no_link.inline_keyboard for b in row if b.url]
    assert urls == []
    # С url — появляется кнопка-ссылка.
    with_link = build_inventory_kb(
        _inventory_page(), sheet_url="https://docs.google.com/spreadsheets/d/BOOK"
    )
    urls = [b.url for row in with_link.inline_keyboard for b in row if b.url]
    assert urls == ["https://docs.google.com/spreadsheets/d/BOOK"]
