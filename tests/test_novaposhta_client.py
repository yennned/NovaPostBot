"""Тесты транспортного ядра НП (PR1) — без сети и ключей, через MockTransport."""

from __future__ import annotations

import json

import httpx
import pytest
from app.config import Settings
from app.novaposhta import (
    NovaPoshtaAuthError,
    NovaPoshtaClient,
    NovaPoshtaError,
    NovaPoshtaNotFound,
    NovaPoshtaUnavailable,
    NovaPoshtaValidationError,
)


def _settings(**over) -> Settings:
    # _env_file=None — не подмешивать локальный .env (герметичность);
    # backoff=0 — ретраи без реальных пауз (тесты не спят).
    settings = Settings(_env_file=None)
    settings.np_retry_backoff = 0.0
    for key, value in over.items():
        setattr(settings, key, value)
    return settings


def _client(handler, **over) -> NovaPoshtaClient:
    return NovaPoshtaClient(settings=_settings(**over), transport=httpx.MockTransport(handler))


def _ok(data: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "data": data, "errors": [], "errorCodes": []})


def _fail(errors: list[str], codes: list[str] | None = None) -> httpx.Response:
    return httpx.Response(
        200, json={"success": False, "data": [], "errors": errors, "errorCodes": codes or []}
    )


async def test_call_returns_data_and_sends_envelope():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _ok([{"Ref": "abc"}])

    client = _client(handler)
    data = await client.call(api_key="K", model="Address", method="getCities", props={"q": "Київ"})

    assert data == [{"Ref": "abc"}]
    assert seen["body"] == {
        "apiKey": "K",
        "modelName": "Address",
        "calledMethod": "getCities",
        "methodProperties": {"q": "Київ"},
    }
    await client.aclose()


async def test_business_error_maps_to_validation():
    client = _client(lambda r: _fail(["Cost is below minimum"]))
    with pytest.raises(NovaPoshtaValidationError) as exc:
        await client.call(api_key="K", model="InternetDocument", method="save")
    assert exc.value.errors == ["Cost is below minimum"]


async def test_auth_keyword_maps_to_auth_error():
    client = _client(lambda r: _fail(["API key is invalid"]))
    with pytest.raises(NovaPoshtaAuthError):
        await client.call(api_key="bad", model="Counterparty", method="getCounterparties")


async def test_not_found_keyword_maps_to_not_found():
    client = _client(lambda r: _fail(["Document not found"]))
    with pytest.raises(NovaPoshtaNotFound):
        await client.call(api_key="K", model="InternetDocument", method="delete")


async def test_5xx_is_retried_then_unavailable():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(502, text="bad gateway")

    client = _client(handler, np_max_retries=3)
    with pytest.raises(NovaPoshtaUnavailable):
        await client.call(api_key="K", model="Address", method="getCities")
    assert calls["n"] == 3  # исчерпали все попытки


async def test_network_error_is_retried():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("boom")

    client = _client(handler, np_max_retries=2)
    with pytest.raises(NovaPoshtaUnavailable):
        await client.call(api_key="K", model="Address", method="getCities")
    assert calls["n"] == 2


async def test_transient_then_success_recovers():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return _ok([{"Ref": "ok"}])

    client = _client(handler, np_max_retries=3)
    data = await client.call(api_key="K", model="Address", method="getCities")
    assert data == [{"Ref": "ok"}]
    assert calls["n"] == 2


async def test_business_error_is_not_retried():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _fail(["bad field"])

    client = _client(handler, np_max_retries=3)
    with pytest.raises(NovaPoshtaValidationError):
        await client.call(api_key="K", model="InternetDocument", method="save")
    assert calls["n"] == 1  # бизнес-ошибку НП не ретраим


async def test_4xx_status_is_error_and_not_retried():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, text="nope")

    client = _client(handler, np_max_retries=3)
    with pytest.raises(NovaPoshtaError) as exc:
        await client.call(api_key="K", model="Address", method="getCities")
    assert not isinstance(exc.value, NovaPoshtaUnavailable)  # 4xx ≠ временный сбой
    assert calls["n"] == 1


async def test_429_rate_limit_is_transient_and_retried():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, text="slow down")

    client = _client(handler, np_max_retries=3)
    with pytest.raises(NovaPoshtaUnavailable):  # 429 — временный сбой
        await client.call(api_key="K", model="Address", method="getCities")
    assert calls["n"] == 3  # ретраим


async def test_non_json_body_raises_error():
    client = _client(lambda r: httpx.Response(200, text="<html>oops</html>"))
    with pytest.raises(NovaPoshtaError):
        await client.call(api_key="K", model="Address", method="getCities")


async def test_non_dict_json_payload_is_treated_as_failure():
    client = _client(lambda r: httpx.Response(200, json=[1, 2, 3]))
    with pytest.raises(NovaPoshtaValidationError):
        await client.call(api_key="K", model="Address", method="getCities")


async def test_auth_error_code_without_text_maps_to_auth():
    body = {"success": False, "data": [], "errors": [], "errorCodes": ["20000200068"]}
    client = _client(lambda r: httpx.Response(200, json=body))
    with pytest.raises(NovaPoshtaAuthError):
        await client.call(api_key="bad", model="Counterparty", method="getCounterparties")
