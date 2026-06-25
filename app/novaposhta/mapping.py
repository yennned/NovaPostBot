"""Чистый маппинг доменного черновика ТТН → `methodProperties` НП.

Вынесено отдельно от `methods.py` (I/O) намеренно: это самая рисковая часть
интеграции (точный набор обязательных полей `InternetDocument.save` — открытый
вопрос Фазы 0) и одновременно максимально тестируемая (чистые функции, ноль
сети). Любая правка контракта НП — здесь, с табличными тестами.

Решения (docs/09-novaposhta-api.md «решение F»):
- `PaymentMethod = Cash` — захардкожено, клиенту не показывается.
- `PayerType` — выбор клиента (`Recipient` по умолч. / `Sender`).
- `Cost` — страховая (оцінкова) сумма.
- COD (накладений платіж) → `AfterpaymentOnGoodsCost=<сумма>` — услуга
  «Контроль оплати» (NovaPay) для ФОП/юр-особи; передоплата → поле не шлём.
  НЕ `BackwardDeliveryData{CargoType:Money}` — то классическая Післяплата для
  фіз-осіб, на наших ФОП-ключах недоступна («Передана послуга Післяплата
  недоступна»). Скалярная форма не требует номера счёта — НП маршрутизирует по
  договору NovaPay (боем подтверждено на ключе ФОП).
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
        # Накладений платіж через «Контроль оплати» (NovaPay): отримувач платить
        # за товар, кошти йдуть на бізнес-рахунок ФОП за договором. Скалярна
        # форма (без номера рахунку) — НП сам маршрутизує. Взаимоисключающа с
        # BackwardDeliveryData{CargoType:Money} (класична Післяплата) — её не шлём.
        props["AfterpaymentOnGoodsCost"] = money(draft.cod_amount)
    return props


def split_full_name(name: str) -> tuple[str, str, str]:
    """Разбить ПІБ на (Прізвище, Ім'я, По-батькові) — укр. порядок.

    НП для PrivatePerson ждёт LastName/FirstName/MiddleName раздельно. Эвристика
    по позициям токенов: 1 токен → только прізвище; 2 → прізвище+ім'я; 3+ →
    остаток в по-батькові. Изолировано и под табличными тестами — открытый
    контракт-вопрос НП (точные требования к ПІБ получателя).
    """
    parts = name.split()
    if not parts:
        return "", "", ""
    last = parts[0]
    first = parts[1] if len(parts) > 1 else ""
    middle = " ".join(parts[2:]) if len(parts) > 2 else ""
    return last, first, middle


def to_recipient_counterparty_props(
    *, kind: str, name: str, phone: str, edrpou: str | None = None
) -> dict[str, Any]:
    """`methodProperties` для `Counterparty.save` получателя (фіз/юр).

    Контрагента-получателя создаём перед `InternetDocument.save` (НП требует Ref
    получателя). Поля по стандарту НП v2.0 — **требуют боевой сверки**.
    """
    if kind == "organization":
        return {
            "CounterpartyType": "Organization",
            "CounterpartyProperty": "Recipient",
            "CompanyName": name,
            "EDRPOU": edrpou or "",
        }
    last, first, middle = split_full_name(name)
    return {
        "CounterpartyType": "PrivatePerson",
        "CounterpartyProperty": "Recipient",
        "FirstName": first,
        "MiddleName": middle,
        "LastName": last,
        "Phone": phone,
    }


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
        # Оценка комиссии COD. NB: save идёт через «Контроль оплати»
        # (AfterpaymentOnGoodsCost), а тут RedeliveryCalculate — это лишь
        # орієнтовний прогноз НП (фактичну комісію NovaPay підтверджує менеджер).
        props["RedeliveryCalculate"] = {"CargoType": "Money", "Amount": money(cod_amount)}
    return props
