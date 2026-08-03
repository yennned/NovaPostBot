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

import unicodedata
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any

from app.novaposhta.schemas import ParcelSpec, TTNDraft

# Способ оплаты за саму доставку — фиксированный (на функции бота не влияет,
# меняется одной константой). Не путать с COD (накладений платіж получателя).
PAYMENT_METHOD = "Cash"

# Коэффициент объёмного («габаритного») веса НП: 1 м³ = 250 кг. Эквивалент
# общеизвестной формулы Д×Ш×В(см)/4000. Вынесен константой: если НП поменяет
# коэффициент, правится одна строка, логика расчёта не меняется.
VOLUMETRIC_KG_PER_CUBIC_METER = Decimal("250")

# Минимальный вес места, который принимает НП. Квантование до 0.001 кг может
# схлопнуть очень лёгкое место в 0, а `OptionsSeat[].weight = 0` при непустом
# верхнеуровневом `Weight` НП отклоняет.
MIN_SEAT_WEIGHT = Decimal("0.001")


def money(value: Decimal | int | str) -> str:
    """Денежную сумму → строку для НП (НП ждёт строки, не числа).

    Через `str()` — иначе `Decimal(199.99)` затащил бы двоичный шум float
    (`199.9900000000…`), и НП отбраковала бы/неверно посчитала сумму.
    """
    return f"{Decimal(str(value)):f}"


def weight(value: Decimal | int | str) -> str:
    """Вес (кг) → строку для НП (через `str()` — защита от float-шума)."""
    return f"{Decimal(str(value)):f}"


# Что НП принимает в `Description`. Снято живым перебором по
# `InternetDocument.save` 2026-08-03: пробник создавал и тут же удалял документ на
# каждый символ. НП валидирует поле по закрытому белому списку и на всё вне него
# отвечает `Description is not valid`, не называя виновника.
#
# Принято:   латиница ASCII, цифры, кириллица (рус.+укр., включая їґё), пробел,
#            пунктуация ниже — и, неожиданно, эмодзи (☕ ✅ прошли).
# Отвергнуто: % { } ~ ^ @ $ ° × – — … € ₴ ™ © § ± ≥ · •  и любая буква вне
#            ASCII/кириллицы: é ü ß ў ² ½ α 中.
#
# Список именно разрешённого, а не запрещённого: перечень НП закрытый, и любой
# неучтённый символ обязан отсеяться сам, а не уронить создание ТТН у клиента.
#
# Апостроф `ʼ` (U+02BC) здесь не для красоты: это украинский апостроф из
# «м'ясо», и вырезать его из названия товара нельзя. `&` и `#` тоже приняты —
# сверка с боевым листом показала 7 названий с амперсандом, которые первый
# вариант этого списка резал зря.
NP_DESCRIPTION_PUNCTUATION = "()[]/\\+*=<>|`'\";:,.!?№_-«»’‘“”&#ʼ´ʹ‑"

# Кириллица, которую НП принимает: основной блок (А-я покрывает и Ъъ Ыы Ээ) плюс
# буквы, лежащие вне него.
NP_DESCRIPTION_CYRILLIC_EXTRA = "ЁёІіЇїЄєҐґ"

# Замены для отвергнутого, у чего есть внятный эквивалент: молча выбросить тире из
# «Кава – 250 г» значит склеить слова, а `×` в «10×20» несёт смысл. Дробная черта
# нужна для `½`, которая ниже разложится в «1 ⁄ 2».
NP_DESCRIPTION_REPLACEMENTS = {
    "–": "-",
    "—": "-",
    "−": "-",
    "…": "...",
    "×": "x",
    "·": "-",
    "•": "-",
    "⁄": "/",
    "₴": " грн",
    "€": " EUR",
    "$": " USD",
    "°": " град",
    "±": "+-",
    "ß": "ss",
    "%": " відс.",
}

# Максимум, который принимает НП. Совпадает с прежним срезом в хендлере ТТН.
NP_DESCRIPTION_LIMIT = 100

# Что подставить, если после чистки не осталось ничего (описание из одних 中).
NP_DESCRIPTION_FALLBACK = "Товари"


def _np_allows(char: str) -> bool:
    """Символ входит в белый список НП."""
    return (
        "a" <= char <= "z"
        or "A" <= char <= "Z"
        or "0" <= char <= "9"
        or "А" <= char <= "я"
        or char in NP_DESCRIPTION_CYRILLIC_EXTRA
        or char in NP_DESCRIPTION_PUNCTUATION
        or char.isspace()
    )


def description(value: str) -> str:
    """Описание вкладення → строку, которую примет НП.

    НП отбраковывает поле целиком из-за одного постороннего символа, её ответ
    (`Description is not valid`) клиенту ничего не объясняет и приходит уже после
    того, как он прошёл всю форму. Названия товаров с `100%`, `–` или `₴`
    встречаются сплошь, поэтому чистка — не косметика: без неё такой SKU нельзя
    отправить вовсе. Живой прогон 2026-08-03: 3 ТТН из 60 умерли ровно так.

    Буквы с диакритикой не выбрасываем, а разлагаем: `Café` → `Cafe` читается,
    `Caf` — нет. Разложение применяется **только** к тому, что НП не приняла:
    `ї`, `ё`, `ґ` в белом списке есть, а канонически они раскладываются на базовую
    букву со знаком, и безусловный NFD молча превратил бы украинский текст в
    русский.
    """
    cleaned: list[str] = []
    for char in value:
        if _np_allows(char):
            cleaned.append(char)
        elif char in NP_DESCRIPTION_REPLACEMENTS:
            cleaned.append(NP_DESCRIPTION_REPLACEMENTS[char])
        else:
            for part in unicodedata.normalize("NFD", char):
                if _np_allows(part):
                    cleaned.append(part)
                elif part in NP_DESCRIPTION_REPLACEMENTS:
                    cleaned.append(NP_DESCRIPTION_REPLACEMENTS[part])
    # Пробелы схлопываем после замен: выброшенный символ мог оставить двойной.
    text = " ".join("".join(cleaned).split())[:NP_DESCRIPTION_LIMIT].strip()
    return text or NP_DESCRIPTION_FALLBACK


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


def volumetric_weight_kg(volume_m3: Decimal) -> Decimal:
    """Объёмный вес одного места (кг) по правилу НП: 1 м³ = 250 кг.

    НП тарифицирует по **максимуму** из фактического и объёмного веса, поэтому
    ориентировочная оценка обязана считать объёмный вес так же, как посчитает НП
    при приёме посылки. Иначе клиент видит цену за фактический вес, а платит за
    объёмный (лёгкий товар в крупной коробке).
    """
    return volume_m3 * VOLUMETRIC_KG_PER_CUBIC_METER


def billable_weight_kg(
    weight_kg: Decimal | int | str,
    *,
    length_cm: Decimal | int | str | None = None,
    width_cm: Decimal | int | str | None = None,
    height_cm: Decimal | int | str | None = None,
    seats_amount: int = 1,
) -> Decimal:
    """Тарифный вес = max(фактический, объёмный по габаритам всех мест).

    Габариты необязательны: без них тарифный вес равен фактическому (поведение
    до появления объёмного расчёта). Валидацию габаритов (все три вместе,
    строго > 0) переиспользуем из `_parcel_geometry`, чтобы оценка и `save`
    считали геометрию одинаково.
    """
    actual = Decimal(str(weight_kg))
    if all(value is None for value in (length_cm, width_cm, height_cm)):
        return actual
    _, volume = _parcel_geometry(
        ParcelSpec(
            weight=actual,
            length_cm=length_cm,
            width_cm=width_cm,
            height_cm=height_cm,
        )
    )
    volumetric = volumetric_weight_kg(volume) * max(int(seats_amount), 1)
    # normalize() убирает хвостовые нули расчёта (12.000 → 12): вес уходит в НП
    # строкой, и «12» читается человеком в карточке лучше, чем «12.000».
    return max(actual, volumetric.normalize())


def to_save_props(draft: TTNDraft) -> dict[str, Any]:
    """Собрать `methodProperties` для `InternetDocument.save` из черновика."""
    seats_amount = max(int(draft.parcel.seats_amount), 1)
    (length, width, height), volume = _parcel_geometry(draft.parcel)
    # InternetDocument.save отклоняет запрос только с SeatsAmount/Weight:
    # OptionsSeat обязателен даже для одного места. Для нескольких мест
    # делим общий вес и ограничиваем точность до 3 знаков после запятой.
    # Клемп до MIN_SEAT_WEIGHT: очень лёгкое место (≤ 0.0004 кг) иначе
    # округлилось бы в 0, и НП отклонила бы ТТН с непустым `Weight`.
    seat_weight = max(
        (Decimal(str(draft.parcel.weight)) / seats_amount).quantize(MIN_SEAT_WEIGHT),
        MIN_SEAT_WEIGHT,
    )
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
        # Чистим здесь, а не только в хендлере: это последняя точка перед НП, и
        # она прикрывает любого вызывающего — воркер, скрипты, будущий код.
        "Description": description(draft.description),
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
    length_cm: Decimal | int | str | None = None,
    width_cm: Decimal | int | str | None = None,
    height_cm: Decimal | int | str | None = None,
) -> dict[str, Any]:
    """`methodProperties` для `InternetDocument.getDocumentPrice` (онлайн-цена).

    Габариты сами по себе в запрос не уходят — они влияют только на `Weight`:
    НП тарифицирует по максимуму из фактического и объёмного веса, а объёмный мы
    воспроизводим локально (`billable_weight_kg`). Так оценка совпадает с тем,
    что НП посчитает по реальным габаритам из `OptionsSeat` при `save`, и не
    зависит от того, принимает ли `getDocumentPrice` нашей версии API
    `OptionsSeat`.
    """
    props: dict[str, Any] = {
        "CitySender": city_sender_ref,
        "CityRecipient": city_recipient_ref,
        "Weight": weight(
            billable_weight_kg(
                weight_kg,
                length_cm=length_cm,
                width_cm=width_cm,
                height_cm=height_cm,
                seats_amount=seats_amount,
            )
        ),
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
