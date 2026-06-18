"""Адаптеры API Нової Пошти.

Транспортный слой (httpx/redis живут здесь, не в `app/services`) — сервисы
видят только эти абстракции, поэтому API-first граница сохраняется.
"""

from app.novaposhta import mapping, methods
from app.novaposhta.cache import NPReferenceCache
from app.novaposhta.client import NovaPoshtaClient
from app.novaposhta.exceptions import (
    NovaPoshtaAuthError,
    NovaPoshtaError,
    NovaPoshtaNotFound,
    NovaPoshtaUnavailable,
    NovaPoshtaValidationError,
)
from app.novaposhta.schemas import (
    City,
    NPEnvelope,
    ParcelSpec,
    PriceQuote,
    RecipientSpec,
    SenderIdentity,
    SenderValidation,
    TrackingStatus,
    TTNDraft,
    TTNResult,
    Warehouse,
)

__all__ = [
    "City",
    "NPEnvelope",
    "NPReferenceCache",
    "NovaPoshtaAuthError",
    "NovaPoshtaClient",
    "NovaPoshtaError",
    "NovaPoshtaNotFound",
    "NovaPoshtaUnavailable",
    "NovaPoshtaValidationError",
    "ParcelSpec",
    "PriceQuote",
    "RecipientSpec",
    "SenderIdentity",
    "SenderValidation",
    "TTNDraft",
    "TTNResult",
    "TrackingStatus",
    "Warehouse",
    "mapping",
    "methods",
]
