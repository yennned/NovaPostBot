"""Чистый маппинг доменного черновика ТТН → `methodProperties` НП.

Вынесено отдельно от `methods.py` (I/O) намеренно: это самая рисковая часть
интеграции (точный набор обязательных полей `InternetDocument.save` — открытый
вопрос Фазы 0) и одновременно максимально тестируемая (чистые функции, ноль
сети). Любая правка контракта НП — здесь, с табличными тестами.

Решения (docs/09-novaposhta-api.md «решение F»):
- `PaymentMethod = Cash` — захардкожено, клиенту не показывается.
- `PayerType` — выбор клиента (`Recipient` по умолч. / `Sender`).
- `Cost` — страховая (оцінкова) сумма.
- COD → `BackwardDeliveryData=[{PayerType:Recipient, CargoType:Money,
  RedeliveryString:<сумма>}]`; передоплата → поле не отправляется.
Полагаемся на стандартный контракт НП v2.0 (ServiceType=WarehouseWarehouse,
отправитель/получатель — по Ref'ам контрагентов).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.novaposhta.schemas import TTNDraft

# Способ оплаты за саму доставку — фиксированный (на функции бота не влияет,
# меняется одной константой). Не путать с COD (накладений платіж получателя).
PAYMENT_METHOD = "Cash"


def money(value: Decimal | int | str) -> str:
    """Денежную сумму → строку для НП (НП ждёт строки, не числа).

    Через `str()` — иначе `Decimal(199.99)` затащил бы двоичный шум float
    (`199.9900000000…`), и НП отбраковала бы/неверно посчитала сумму.
    """
    return f"{Decimal(str(value)):f}"


def weight(value: Decimal | int | str) -> str:
    """Вес (кг) → строку для НП (через `str()` — защита от float-шума)."""
    return f"{Decimal(str(value)):f}"


def to_save_props(draft: TTNDraft) -> dict[str, Any]:
    """Собрать `methodProperties` для `InternetDocument.save` из черновика."""
    props: dict[str, Any] = {
        "PayerType": draft.payer_type,
        "PaymentMethod": PAYMENT_METHOD,
        "CargoType": draft.cargo_type,
        "ServiceType": draft.service_type,
        "SeatsAmount": str(draft.parcel.seats_amount),
        "Weight": weight(draft.parcel.weight),
        "Description": draft.description,
        "Cost": money(draft.cost),
        # Отправитель (наш склад, контрагент = ФОП).
        "CitySender": draft.sender.city_ref,
        "Sender": draft.sender.counterparty_ref,
        "SenderAddress": draft.sender.warehouse_ref,
        "ContactSender": draft.sender.contact_ref,
        "SendersPhone": draft.sender.phone,
        # Получатель (контрагент создаётся в write-сервисе перед save).
        "CityRecipient": draft.recipient.city_ref,
        "RecipientAddress": draft.recipient.warehouse_ref,
        "Recipient": draft.recipient.counterparty_ref,
        "ContactRecipient": draft.recipient.contact_ref,
        "RecipientsPhone": draft.recipient.phone,
    }
    if draft.parcel.volume_general is not None:
        props["VolumeGeneral"] = money(draft.parcel.volume_general)
    if draft.cod_amount is not None:
        # Накладений платіж: комиссию платит отримувач.
        props["BackwardDeliveryData"] = [
            {
                "PayerType": "Recipient",
                "CargoType": "Money",
                "RedeliveryString": money(draft.cod_amount),
            }
        ]
    return props


def to_price_props(
    *,
    city_sender_ref: str,
    city_recipient_ref: str,
    weight_kg: Decimal | int | str,
    cost: Decimal | int | str,
    seats_amount: int = 1,
    service_type: str = "WarehouseWarehouse",
    cargo_type: str = "Cargo",
    cod_amount: Decimal | int | str | None = None,
) -> dict[str, Any]:
    """`methodProperties` для `InternetDocument.getDocumentPrice` (онлайн-цена)."""
    props: dict[str, Any] = {
        "CitySender": city_sender_ref,
        "CityRecipient": city_recipient_ref,
        "Weight": weight(weight_kg),
        "ServiceType": service_type,
        "Cost": money(cost),
        "CargoType": cargo_type,
        "SeatsAmount": str(seats_amount),
    }
    if cod_amount is not None:
        props["RedeliveryCalculate"] = {"CargoType": "Money", "Amount": money(cod_amount)}
    return props
