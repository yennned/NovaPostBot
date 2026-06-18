"""Адаптеры API Нової Пошти.

Транспортный слой (httpx/redis живут здесь, не в `app/services`) — сервисы
видят только эти абстракции, поэтому API-first граница сохраняется.
"""

from app.novaposhta.client import NovaPoshtaClient
from app.novaposhta.exceptions import (
    NovaPoshtaAuthError,
    NovaPoshtaError,
    NovaPoshtaNotFound,
    NovaPoshtaUnavailable,
    NovaPoshtaValidationError,
)
from app.novaposhta.schemas import NPEnvelope

__all__ = [
    "NPEnvelope",
    "NovaPoshtaAuthError",
    "NovaPoshtaClient",
    "NovaPoshtaError",
    "NovaPoshtaNotFound",
    "NovaPoshtaUnavailable",
    "NovaPoshtaValidationError",
]
