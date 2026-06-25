"""Табличные тесты чистого маппинга полей НП (PR2) — без сети.

Здесь пинятся решения по `InternetDocument.save` (PayerType/Cost/COD/габариты) —
открытый вопрос Фазы 0 изолирован в `mapping.py`, правка одним файлом.
"""

from __future__ import annotations

from decimal import Decimal

from app.novaposhta.mapping import (
    PAYMENT_METHOD,
    money,
    split_full_name,
    to_price_props,
    to_recipient_counterparty_props,
    to_save_props,
)
from app.novaposhta.schemas import ParcelSpec, RecipientSpec, SenderIdentity, TTNDraft

_SENDER = SenderIdentity(
    counterparty_ref="sender-cp",
    contact_ref="sender-contact",
    city_ref="sender-city",
    warehouse_ref="sender-wh",
    phone="380501112233",
)
_RECIPIENT = RecipientSpec(
    kind="person",
    name="Іван Петренко",
    phone="380671234567",
    city_ref="rcpt-city",
    warehouse_ref="rcpt-wh",
    counterparty_ref="rcpt-cp",
    contact_ref="rcpt-contact",
)


def _draft(**over) -> TTNDraft:
    base = {
        "sender": _SENDER,
        "recipient": _RECIPIENT,
        "parcel": ParcelSpec(weight=Decimal("2.5")),
        "description": "Кава мелена",
        "cost": Decimal("500"),
    }
    base.update(over)
    return TTNDraft(**base)


def test_save_props_base_fields():
    props = to_save_props(_draft())
    assert props["PayerType"] == "Recipient"  # дефолт
    assert props["PaymentMethod"] == PAYMENT_METHOD == "Cash"
    assert props["ServiceType"] == "WarehouseWarehouse"
    assert props["CargoType"] == "Cargo"
    assert props["SeatsAmount"] == "1"
    assert props["Weight"] == "2.5"  # строки, не числа
    assert props["Cost"] == "500"
    assert props["Description"] == "Кава мелена"
    # отправитель
    assert props["CitySender"] == "sender-city"
    assert props["Sender"] == "sender-cp"
    assert props["SenderAddress"] == "sender-wh"
    assert props["ContactSender"] == "sender-contact"
    assert props["SendersPhone"] == "380501112233"
    # получатель
    assert props["CityRecipient"] == "rcpt-city"
    assert props["RecipientAddress"] == "rcpt-wh"
    assert props["Recipient"] == "rcpt-cp"
    assert props["ContactRecipient"] == "rcpt-contact"
    assert props["RecipientsPhone"] == "380671234567"


def test_save_props_payer_sender():
    props = to_save_props(_draft(payer_type="Sender"))
    assert props["PayerType"] == "Sender"


def test_save_props_prepay_has_no_afterpayment():
    props = to_save_props(_draft())  # cod_amount=None
    assert "AfterpaymentOnGoodsCost" not in props
    # Класична Післяплата (BackwardDeliveryData) не используется в принципе.
    assert "BackwardDeliveryData" not in props


def test_save_props_cod_uses_afterpayment_on_goods_cost():
    # COD ФОП = «Контроль оплати» → скаляр AfterpaymentOnGoodsCost, НЕ BackwardDeliveryData.
    props = to_save_props(_draft(cod_amount=Decimal("750.50")))
    assert props["AfterpaymentOnGoodsCost"] == "750.50"
    assert "BackwardDeliveryData" not in props


def test_save_props_volume_optional():
    assert "VolumeGeneral" not in to_save_props(_draft())
    props = to_save_props(
        _draft(parcel=ParcelSpec(weight=Decimal("1"), volume_general=Decimal("0.004")))
    )
    assert props["VolumeGeneral"] == "0.004"


def test_save_props_seats_amount():
    props = to_save_props(_draft(parcel=ParcelSpec(weight=Decimal("10"), seats_amount=3)))
    assert props["SeatsAmount"] == "3"


def test_money_formats_decimal_as_string():
    assert money(Decimal("500.00")) == "500.00"
    assert money(Decimal("0.5")) == "0.5"
    assert money(3) == "3"
    assert money("12.30") == "12.30"


def test_money_does_not_leak_float_noise():
    # 199.99 не представимо в float точно — через str() шум не попадает в НП.
    assert money(199.99) == "199.99"
    assert money(1.5) == "1.5"


def test_price_props_without_cod():
    props = to_price_props(
        city_sender_ref="A", city_recipient_ref="B", weight_kg=Decimal("2"), cost=Decimal("300")
    )
    assert props == {
        "CitySender": "A",
        "CityRecipient": "B",
        "Weight": "2",
        "ServiceType": "WarehouseWarehouse",
        "Cost": "300",
        "CargoType": "Cargo",
        "SeatsAmount": "1",
    }


def test_split_full_name_ukrainian_order():
    assert split_full_name("Петренко Іван Богданович") == ("Петренко", "Іван", "Богданович")
    assert split_full_name("Петренко Іван") == ("Петренко", "Іван", "")
    assert split_full_name("Петренко") == ("Петренко", "", "")
    assert split_full_name("  ") == ("", "", "")


def test_recipient_counterparty_props_person():
    props = to_recipient_counterparty_props(
        kind="person", name="Петренко Іван", phone="380671234567"
    )
    assert props == {
        "CounterpartyType": "PrivatePerson",
        "CounterpartyProperty": "Recipient",
        "FirstName": "Іван",
        "MiddleName": "",
        "LastName": "Петренко",
        "Phone": "380671234567",
    }


def test_recipient_counterparty_props_organization():
    props = to_recipient_counterparty_props(
        kind="organization", name="ТОВ Ромашка", phone="380441112233", edrpou="12345678"
    )
    assert props["CounterpartyType"] == "Organization"
    assert props["CounterpartyProperty"] == "Recipient"
    assert props["EDRPOU"] == "12345678"


def test_price_props_with_cod_adds_redelivery():
    props = to_price_props(
        city_sender_ref="A",
        city_recipient_ref="B",
        weight_kg=1,
        cost=100,
        cod_amount=Decimal("250"),
    )
    assert props["RedeliveryCalculate"] == {"CargoType": "Money", "Amount": "250"}
