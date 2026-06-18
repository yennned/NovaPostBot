"""Структуры запросов/ответов НП.

Конверт ответа (`NPEnvelope`) + доменные frozen-структуры для методов и
маппинга полей `InternetDocument.save`. Все значения — то, что отдаём/получаем
от НП; денежные и весовые поля НП ждёт строками.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(frozen=True, slots=True)
class NPEnvelope:
    """Разобранный конверт ответа НП.

    НП всегда отвечает HTTP 200 с телом
    `{success, data, errors, errorCodes, warnings, ...}`. Бизнес-сбой выражен
    `success=false`, а не HTTP-кодом.
    """

    success: bool
    data: list[dict[str, Any]]
    errors: list[str]
    error_codes: list[str]
    warnings: list[str]

    @classmethod
    def from_payload(cls, payload: Any) -> NPEnvelope:
        """Построить конверт из распарсенного JSON-тела (терпимо к форме)."""
        if not isinstance(payload, dict):
            return cls(
                success=False,
                data=[],
                errors=["неожиданный формат відповіді НП"],
                error_codes=[],
                warnings=[],
            )
        raw_data = payload.get("data")
        data = (
            [row for row in raw_data if isinstance(row, dict)] if isinstance(raw_data, list) else []
        )
        return cls(
            success=bool(payload.get("success")),
            data=data,
            errors=_as_str_list(payload.get("errors")),
            error_codes=_as_str_list(payload.get("errorCodes")),
            warnings=_as_str_list(payload.get("warnings")),
        )


def _as_str_list(value: Any) -> list[str]:
    """Привести произвольное поле НП (`errors`/`errorCodes`/...) к списку строк.

    Терпимо к форме: список → строки поэлементно; dict (НП иногда отдаёт
    объект-карту) → его значения; пусто/None → []; иначе одна строка.
    """
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, dict):
        return [str(item) for item in value.values()]
    if value in (None, ""):
        return []
    return [str(value)]


# --- Справочники НП ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class City:
    """Город из `Address.getCities`."""

    ref: str
    name: str
    area: str | None = None


@dataclass(frozen=True, slots=True)
class Warehouse:
    """Відділення из `Address.getWarehouses`."""

    ref: str
    number: str
    description: str
    city_ref: str | None = None


# --- Стороны отправления -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class SenderIdentity:
    """Реквизиты отправителя (ФОП) для `InternetDocument.save`.

    Ref'ы подтягиваются при валидации ключа (PR 4) и кэшируются в
    `sender_profiles`. `city_ref`/`warehouse_ref` — наш склад-отправитель.
    """

    counterparty_ref: str
    contact_ref: str
    city_ref: str
    warehouse_ref: str
    phone: str


@dataclass(frozen=True, slots=True)
class SenderValidation:
    """Результат проверки ключа ФОП: Ref контрагента-отправителя и контакта.

    Город/склад отправителя дорезолвит `sender_profile`-сервис (PR 4) из
    конфига/аккаунта — здесь только то, что однозначно есть у контрагента.
    """

    counterparty_ref: str
    contact_ref: str | None


@dataclass(frozen=True, slots=True)
class RecipientSpec:
    """Получатель ТТН. `kind` — `person`/`organization` (PrivatePerson/Organization).

    `counterparty_ref`/`contact_ref` создаются в write-сервисе (PR 6) через
    `Counterparty.save` перед `InternetDocument.save`.
    """

    kind: str
    name: str
    phone: str
    city_ref: str
    warehouse_ref: str
    counterparty_ref: str
    contact_ref: str
    edrpou: str | None = None


@dataclass(frozen=True, slots=True)
class ParcelSpec:
    """Габариты/вес посылки. Размеры — для «Власних розмірів» (опционально)."""

    weight: Decimal
    seats_amount: int = 1
    volume_general: Decimal | None = None


# --- Черновик и результат ТТН ------------------------------------------------


@dataclass(frozen=True, slots=True)
class TTNDraft:
    """Полностью разрешённый черновик ТТН — вход для `mapping.to_save_props`."""

    sender: SenderIdentity
    recipient: RecipientSpec
    parcel: ParcelSpec
    description: str
    cost: Decimal  # страховая (оцінкова) сумма → NP `Cost`
    payer_type: str = "Recipient"  # `Recipient` (по умолч.) / `Sender`
    cod_amount: Decimal | None = None  # задано → COD (BackwardDeliveryData)
    cargo_type: str = "Cargo"
    service_type: str = "WarehouseWarehouse"


@dataclass(frozen=True, slots=True)
class TTNResult:
    """Результат `InternetDocument.save`."""

    ref: str
    int_doc_number: str  # № ТТН
    cost: Decimal | None = None
    estimated_delivery_date: str | None = None


@dataclass(frozen=True, slots=True)
class PriceQuote:
    """Результат `InternetDocument.getDocumentPrice` (онлайн-ценообразование)."""

    cost: Decimal
    cost_redelivery: Decimal | None = None  # комиссия COD
    estimated_delivery_date: str | None = None


@dataclass(frozen=True, slots=True)
class TrackingStatus:
    """Строка `TrackingDocument.getStatusDocuments`."""

    number: str
    status: str
    status_code: str
    raw: dict[str, Any] = field(default_factory=dict)
