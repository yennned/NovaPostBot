"""Типизированные обёртки над методами API НП.

Каждая функция принимает `NovaPoshtaClient` + `api_key` (per-ФОП) и
параметры, вызывает соответствующий `modelName/calledMethod` и разбирает
`data` в доменные структуры (`schemas.py`). I/O-слой; чистый маппинг полей
`save`/`price` — в `mapping.py`.
"""

from __future__ import annotations

from decimal import Decimal

from app.novaposhta.client import NovaPoshtaClient
from app.novaposhta.exceptions import NovaPoshtaValidationError
from app.novaposhta.mapping import to_price_props, to_recipient_counterparty_props, to_save_props
from app.novaposhta.schemas import (
    City,
    PriceQuote,
    SenderValidation,
    TrackingStatus,
    TTNDraft,
    TTNResult,
    Warehouse,
)


def _first(rows: list[dict], what: str) -> dict:
    """Первая строка ответа или доменная ошибка, если НП вернула пусто."""
    if not rows:
        raise NovaPoshtaValidationError(f"НП повернула порожній результат: {what}")
    return rows[0]


def _ref(row: dict, what: str) -> str:
    """Достать обязательный `Ref` из строки ответа или доменная ошибка.

    Прямой `row["Ref"]` бросил бы `KeyError` (не `NovaPoshtaError`) и проскочил бы
    мимо обработчиков сбоя НП — поэтому проверяем явно.
    """
    ref = row.get("Ref")
    if not ref:
        raise NovaPoshtaValidationError(f"НП не повернула Ref: {what}")
    return str(ref)


def _decimal(value: object) -> Decimal | None:
    """Аккуратно привести числовое поле НП к Decimal (через str)."""
    if value in (None, ""):
        return None
    return Decimal(str(value))


# --- Справочники -------------------------------------------------------------


async def get_cities(
    client: NovaPoshtaClient,
    *,
    api_key: str,
    query: str,
    limit: int = 20,
    attempts: int | None = None,
    timeout_seconds: float | None = None,
) -> list[City]:
    """`Address.getCities` — поиск города по подстроке."""
    rows = await client.call(
        api_key=api_key,
        model="Address",
        method="getCities",
        props={"FindByString": query, "Limit": str(limit)},
        attempts=attempts,
        timeout_seconds=timeout_seconds,
    )
    return [
        City(ref=row["Ref"], name=row.get("Description", ""), area=row.get("AreaDescription"))
        for row in rows
        if row.get("Ref")
    ]


async def get_warehouses(
    client: NovaPoshtaClient,
    *,
    api_key: str,
    city_ref: str,
    query: str | None = None,
    limit: int = 50,
    attempts: int | None = None,
    timeout_seconds: float | None = None,
) -> list[Warehouse]:
    """`Address.getWarehouses` — відділення в городе (опц. поиск по номеру/строке)."""
    props: dict[str, str] = {"CityRef": city_ref, "Limit": str(limit)}
    if query:
        props["FindByString"] = query
    rows = await client.call(
        api_key=api_key,
        model="Address",
        method="getWarehouses",
        props=props,
        attempts=attempts,
        timeout_seconds=timeout_seconds,
    )
    return [
        Warehouse(
            ref=row["Ref"],
            number=str(row.get("Number", "")),
            description=row.get("Description", ""),
            city_ref=row.get("CityRef"),
        )
        for row in rows
        if row.get("Ref")
    ]


# --- Цена / трекинг ----------------------------------------------------------


async def get_price(
    client: NovaPoshtaClient,
    *,
    api_key: str,
    city_sender_ref: str,
    city_recipient_ref: str,
    weight_kg: Decimal | int | str,
    cost: Decimal | int | str,
    seats_amount: int = 1,
    service_type: str = "WarehouseWarehouse",
    cargo_type: str = "Cargo",
    cod_amount: Decimal | int | str | None = None,
) -> PriceQuote:
    """`InternetDocument.getDocumentPrice` — онлайн-стоимость/срок."""
    rows = await client.call(
        api_key=api_key,
        model="InternetDocument",
        method="getDocumentPrice",
        props=to_price_props(
            city_sender_ref=city_sender_ref,
            city_recipient_ref=city_recipient_ref,
            weight_kg=weight_kg,
            cost=cost,
            seats_amount=seats_amount,
            service_type=service_type,
            cargo_type=cargo_type,
            cod_amount=cod_amount,
        ),
    )
    row = _first(rows, "getDocumentPrice")
    cost = _decimal(row.get("Cost"))
    if cost is None:
        # Цена без поля Cost — битый/изменённый ответ; не выдаём «0 грн» за доставку.
        raise NovaPoshtaValidationError("НП не повернула вартість доставки")
    return PriceQuote(
        cost=cost,
        cost_redelivery=_decimal(row.get("CostRedelivery")),
        estimated_delivery_date=row.get("EstimatedDeliveryDate") or None,
    )


async def get_status_documents(
    client: NovaPoshtaClient, *, api_key: str, numbers: list[str]
) -> list[TrackingStatus]:
    """`TrackingDocument.getStatusDocuments` — статусы по номерам ТТН (батч ≤100)."""
    documents = [{"DocumentNumber": number} for number in numbers]
    rows = await client.call(
        api_key=api_key,
        model="TrackingDocument",
        method="getStatusDocuments",
        props={"Documents": documents},
    )
    return [
        TrackingStatus(
            number=str(row.get("Number", "")),
            status=row.get("Status", ""),
            status_code=str(row.get("StatusCode", "")),
            raw=row,
        )
        for row in rows
    ]


# --- Создание / отмена ТТН ---------------------------------------------------


async def save_ttn(client: NovaPoshtaClient, *, api_key: str, draft: TTNDraft) -> TTNResult:
    """`InternetDocument.save` — создать ТТН по готовому черновику."""
    rows = await client.call(
        api_key=api_key,
        model="InternetDocument",
        method="save",
        props=to_save_props(draft),
    )
    row = _first(rows, "save")
    return TTNResult(
        ref=_ref(row, "InternetDocument.save"),
        int_doc_number=str(row.get("IntDocNumber", "")),
        cost=_decimal(row.get("CostOnSite")),
        estimated_delivery_date=row.get("EstimatedDeliveryDate") or None,
    )


async def delete_ttn(client: NovaPoshtaClient, *, api_key: str, doc_ref: str) -> None:
    """`InternetDocument.delete` — отменить ТТН (до отправки)."""
    await client.call(
        api_key=api_key,
        model="InternetDocument",
        method="delete",
        props={"DocumentRefs": doc_ref},
    )


def _extract_contact_ref(row: dict) -> str | None:
    """Достать Ref контактного лица из ответа `Counterparty.save` (вложенный объект)."""
    contact = row.get("ContactPerson")
    if isinstance(contact, dict):
        data = contact.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0].get("Ref")
    return None


async def ensure_recipient(
    client: NovaPoshtaClient,
    *,
    api_key: str,
    kind: str,
    name: str,
    phone: str,
    edrpou: str | None = None,
) -> tuple[str, str | None]:
    """Завести контрагента-получателя в НП → (Ref контрагента, Ref контакта).

    Нужно перед `InternetDocument.save` (НП принимает только Ref получателя, не
    инлайн-данные). Поля — стандарт НП v2.0 (см. `mapping`), **требуют боевой
    сверки**. Контакт может отсутствовать (юрособа) → `None`.
    """
    rows = await client.call(
        api_key=api_key,
        model="Counterparty",
        method="save",
        props=to_recipient_counterparty_props(kind=kind, name=name, phone=phone, edrpou=edrpou),
    )
    row = _first(rows, "Counterparty.save(Recipient)")
    return _ref(row, "Counterparty.save(Recipient)"), _extract_contact_ref(row)


# --- Валидация ключа ФОП -----------------------------------------------------


async def validate_key_and_get_sender(
    client: NovaPoshtaClient, *, api_key: str
) -> SenderValidation:
    """Проверить ключ ФОП и вернуть Ref контрагента-отправителя + контакта.

    Невалидный/заблокированный ключ → НП ответит `success=false` (клиент
    бросит `NovaPoshtaAuthError`). Пустой список отправителей → ключ валиден,
    но контрагент-отправитель не настроен → доменная ошибка валидации.
    """
    senders = await client.call(
        api_key=api_key,
        model="Counterparty",
        method="getCounterparties",
        props={"CounterpartyProperty": "Sender", "Page": "1"},
    )
    sender = _first(senders, "getCounterparties(Sender)")
    sender_ref = _ref(sender, "getCounterparties(Sender)")

    contacts = await client.call(
        api_key=api_key,
        model="Counterparty",
        method="getCounterpartyContactPersons",
        props={"Ref": sender_ref, "Page": "1"},
    )
    contact_ref = contacts[0]["Ref"] if contacts and contacts[0].get("Ref") else None
    return SenderValidation(counterparty_ref=sender_ref, contact_ref=contact_ref)
