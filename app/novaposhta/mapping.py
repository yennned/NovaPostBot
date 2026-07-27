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

from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any

from app.novaposhta.schemas import ParcelSpec, TTNDraft

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


def _positive_decimal(value: Decimal | int | str, *, field: str) -> Decimal:
    """Положительное конечное число для габарита/объёма НП."""
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} має бути числом більше 0") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{field} має бути числом більше 0")
    return parsed


def _parcel_geometry(parcel: ParcelSpec) -> tuple[tuple[str, str, str], Decimal]:
    """Вернуть габариты места и согласованный объём для `OptionsSeat`."""
    raw_dimensions = (parcel.length_cm, parcel.width_cm, parcel.height_cm)
    supplied = [value is not None for value in raw_dimensions]
    if any(supplied) and not all(supplied):
        raise ValueError("довжина, ширина та висота мають бути задані разом")

    if all(supplied):
        dimensions = tuple(
            _positive_decimal(value, field=field)
            for value, field in zip(raw_dimensions, ("довжина", "ширина", "висота"), strict=True)
        )
        calculated_volume = dimensions[0] * dimensions[1] * dimensions[2] / Decimal("1000000")
        if parcel.volume_general is not None:
            explicit_volume = _positive_decimal(parcel.volume_general, field="об'єм")
            if explicit_volume != calculated_volume:
                raise ValueError("volume_general не відповідає габаритам місця")
            volume = explicit_volume
        else:
            volume = calculated_volume
    elif parcel.volume_general is not None:
        # Совместимость со старыми вызывающими кодами: из явного объёма строим
        # кубическое место с округлением стороны вверх до целого сантиметра.
        explicit_volume = _positive_decimal(parcel.volume_general, field="объём")
        cubic_centimeters = explicit_volume * Decimal("1000000")
        side = ((cubic_centimeters.ln() / Decimal("3")).exp()).to_integral_value(
            rounding=ROUND_CEILING
        )
        side = max(side, Decimal("1"))
        dimensions = (side, side, side)
        volume = side**3 / Decimal("1000000")
    else:
        dimensions = (Decimal("10"), Decimal("10"), Decimal("10"))
        volume = Decimal("0.001")
    return tuple(f"{value:f}" for value in dimensions), volume


def to_save_props(draft: TTNDraft) -> dict[str, Any]:
    """Собрать `methodProperties` для `InternetDocument.save` из черновика."""
    seats_amount = max(int(draft.parcel.seats_amount), 1)
    (length, width, height), volume = _parcel_geometry(draft.parcel)
    # InternetDocument.save отклоняет запрос только с SeatsAmount/Weight:
    # OptionsSeat обязателен даже для одного места. Для нескольких мест
    # делим общий вес и ограничиваем точность до 3 знаков после запятой.
    seat_weight = (Decimal(str(draft.parcel.weight)) / seats_amount).quantize(Decimal("0.001"))
    options_seat = [
        {
            "volumetricVolume": money(volume),
            "volumetricWidth": width,
            "volumetricLength": length,
            "volumetricHeight": height,
            "weight": weight(seat_weight.normalize()),
        }
        for _ in range(seats_amount)
    ]
    props: dict[str, Any] = {
        "PayerType": draft.payer_type,
        "PaymentMethod": PAYMENT_METHOD,
        "CargoType": draft.cargo_type,
        "ServiceType": draft.service_type,
        "SeatsAmount": str(seats_amount),
        "Weight": weight(draft.parcel.weight),
        "OptionsSeat": options_seat,
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
