"""Низкоуровневый async-клиент API Нової Пошти.

Единый POST-эндпоинт НП: тело `{apiKey, modelName, calledMethod,
methodProperties}`, ответ — конверт `{success, data, errors, ...}` (всегда
HTTP 200 на бизнес-сбое). Ключ — **per-ФОП**, поэтому передаётся на вызов, а не
в конструктор: один общий клиент обслуживает все ФОП.

Ретраи (tenacity) — только для временных сбоев (`NovaPoshtaUnavailable`:
сеть/таймаут/5xx). Бизнес-ошибки НП (`success=false`) не ретраятся.
"""

from __future__ import annotations

from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import Settings, get_settings
from app.novaposhta.exceptions import (
    NovaPoshtaAuthError,
    NovaPoshtaError,
    NovaPoshtaNotFound,
    NovaPoshtaUnavailable,
    NovaPoshtaValidationError,
)
from app.novaposhta.schemas import NPEnvelope

# Классификация ошибок НП. Надёжнее всего — по `errorCodes` (машинные коды);
# текстовые подстроки — фолбэк, когда кода нет. Коды НП относительно стабильны.
_AUTH_ERROR_CODES = frozenset({"20000200068"})  # невалидный/недійсний API-ключ
_NOT_FOUND_ERROR_CODES: frozenset[str] = frozenset()

# Подстроки-фолбэк. Без слишком широких стемов (напр. голого «ключ»), чтобы не
# ловить обычные валидационные сообщения, где слово встречается не про auth.
_AUTH_HINTS = ("api key", "apikey", "недійсний ключ", "невірний ключ", "не існує або заблокован")
_NOT_FOUND_HINTS = ("not found", "не знайдено", "відсутн")


class NovaPoshtaClient:
    """Тонкая обёртка над `httpx.AsyncClient` для одного эндпоинта НП.

    Для тестов можно передать `transport` (`httpx.MockTransport`) — реальной
    сети и ключей не требуется.
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._url = self._settings.np_api_url
        self._client = httpx.AsyncClient(
            timeout=self._settings.np_timeout_seconds,
            transport=transport,
        )

    async def aclose(self) -> None:
        """Закрыть нижележащий HTTP-клиент (вызывается при остановке процесса)."""
        await self._client.aclose()

    async def call(
        self,
        *,
        api_key: str,
        model: str,
        method: str,
        props: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Вызвать метод НП и вернуть `data` при успехе.

        Бросает подтип `NovaPoshtaError` на сбое (после ретраев временных).
        """
        payload = {
            "apiKey": api_key,
            "modelName": model,
            "calledMethod": method,
            "methodProperties": props or {},
        }
        retries = max(1, self._settings.np_max_retries)
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type(NovaPoshtaUnavailable),
            stop=stop_after_attempt(retries),
            wait=wait_exponential(multiplier=self._settings.np_retry_backoff, max=4),
            reraise=True,
        ):
            with attempt:
                return await self._request(payload)
        raise AssertionError("unreachable")  # pragma: no cover

    async def _request(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Один сетевой вызов + декод конверта (без ретраев)."""
        try:
            response = await self._client.post(self._url, json=payload)
        except httpx.HTTPError as exc:
            raise NovaPoshtaUnavailable(f"мережева помилка НП: {exc}") from exc

        # 5xx + 408 (timeout) + 429 (rate limit) — временные: ретраим и пускаем в
        # stale-fallback кэша; прочие не-200 (4xx) — постоянные, без ретраев.
        if response.status_code >= 500 or response.status_code in (408, 429):
            raise NovaPoshtaUnavailable(f"НП відповіла {response.status_code}")
        if response.status_code != 200:
            raise NovaPoshtaError(f"НП відповіла {response.status_code}")

        try:
            body = response.json()
        except ValueError as exc:
            raise NovaPoshtaError("НП повернула не-JSON відповідь") from exc

        envelope = NPEnvelope.from_payload(body)
        if not envelope.success:
            raise _classify(envelope)
        return envelope.data


def _classify(envelope: NPEnvelope) -> NovaPoshtaError:
    """Подобрать тип исключения: сначала по кодам НП, затем по тексту."""
    message = (
        "; ".join(envelope.errors)
        or "; ".join(envelope.error_codes)  # текст пуст — диагностика хотя бы из кодов
        or "НП відхилила запит"
    )
    kwargs = {"errors": envelope.errors, "error_codes": envelope.error_codes}
    codes = set(envelope.error_codes)
    if codes & _AUTH_ERROR_CODES:
        return NovaPoshtaAuthError(message, **kwargs)
    if codes & _NOT_FOUND_ERROR_CODES:
        return NovaPoshtaNotFound(message, **kwargs)

    haystack = message.lower()
    if any(hint in haystack for hint in _AUTH_HINTS):
        return NovaPoshtaAuthError(message, **kwargs)
    if any(hint in haystack for hint in _NOT_FOUND_HINTS):
        return NovaPoshtaNotFound(message, **kwargs)
    return NovaPoshtaValidationError(message, **kwargs)
