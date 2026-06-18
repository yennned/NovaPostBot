"""Тесты обёрток methods.py (PR2) — через httpx.MockTransport, без сети/ключей."""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
from app.config import Settings
from app.novaposhta import methods
from app.novaposhta.client import NovaPoshtaClient
from app.novaposhta.exceptions import NovaPoshtaAuthError, NovaPoshtaValidationError
from app.novaposhta.mapping import to_save_props
from app.novaposhta.schemas import ParcelSpec, RecipientSpec, SenderIdentity, TTNDraft


def _client(handler) -> NovaPoshtaClient:
    settings = Settings(_env_file=None)
    settings.np_retry_backoff = 0.0
    return NovaPoshtaClient(settings=settings, transport=httpx.MockTransport(handler))


def _ok(data: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "data": data, "errors": [], "errorCodes": []})


def _fail(errors: list[str], codes: list[str] | None = None) -> httpx.Response:
    return httpx.Response(
        200, json={"success": False, "data": [], "errors": errors, "errorCodes": codes or []}
    )


def _router(routes: dict[tuple[str, str], object]):
    """Хендлер, диспатчащий по (modelName, calledMethod); собирает отправленные тела."""
    captured: dict = {"bodies": []}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured["body"] = body
        captured["bodies"].append(body)
        result = routes[(body["modelName"], body["calledMethod"])]
        return result if isinstance(result, httpx.Response) else _ok(result)

    return handler, captured


def _draft() -> TTNDraft:
    return TTNDraft(
        sender=SenderIdentity(
            counterparty_ref="s-cp",
            contact_ref="s-ct",
            city_ref="s-city",
            warehouse_ref="s-wh",
            phone="380501112233",
        ),
        recipient=RecipientSpec(
            kind="person",
            name="Іван",
            phone="380671234567",
            city_ref="r-city",
            warehouse_ref="r-wh",
            counterparty_ref="r-cp",
            contact_ref="r-ct",
        ),
        parcel=ParcelSpec(weight=Decimal("2")),
        description="Товар",
        cost=Decimal("400"),
    )


async def test_get_cities_parses_and_sends_query():
    handler, captured = _router(
        {
            ("Address", "getCities"): [
                {"Ref": "c1", "Description": "Київ", "AreaDescription": "Київська"},
                {"Description": "no-ref drop"},  # без Ref — отбрасывается
            ]
        }
    )
    cities = await methods.get_cities(_client(handler), api_key="K", query="Київ")
    assert len(cities) == 1
    assert cities[0].ref == "c1"
    assert cities[0].name == "Київ"
    assert cities[0].area == "Київська"
    assert captured["body"]["methodProperties"] == {"FindByString": "Київ", "Limit": "20"}


async def test_get_warehouses_with_and_without_query():
    handler, captured = _router(
        {("Address", "getWarehouses"): [{"Ref": "w1", "Number": 5, "Description": "Відділення №5"}]}
    )
    whs = await methods.get_warehouses(_client(handler), api_key="K", city_ref="c1", query="5")
    assert whs[0].ref == "w1"
    assert whs[0].number == "5"  # число НП нормализуем в строку
    assert captured["body"]["methodProperties"] == {
        "CityRef": "c1",
        "Limit": "50",
        "FindByString": "5",
    }

    handler2, captured2 = _router({("Address", "getWarehouses"): [{"Ref": "w1", "Number": "5"}]})
    await methods.get_warehouses(_client(handler2), api_key="K", city_ref="c1")
    assert "FindByString" not in captured2["body"]["methodProperties"]


async def test_get_price_parses_cost():
    handler, captured = _router(
        {
            ("InternetDocument", "getDocumentPrice"): [
                {"Cost": 65, "CostRedelivery": 20, "EstimatedDeliveryDate": "2026-06-20"}
            ]
        }
    )
    quote = await methods.get_price(
        _client(handler),
        api_key="K",
        city_sender_ref="A",
        city_recipient_ref="B",
        weight_kg=Decimal("2"),
        cost=Decimal("400"),
        cod_amount=Decimal("400"),
    )
    assert quote.cost == Decimal("65")
    assert quote.cost_redelivery == Decimal("20")
    assert quote.estimated_delivery_date == "2026-06-20"
    assert captured["body"]["methodProperties"]["RedeliveryCalculate"] == {
        "CargoType": "Money",
        "Amount": "400",
    }


async def test_get_price_missing_cost_raises():
    handler, _ = _router({("InternetDocument", "getDocumentPrice"): [{"CostRedelivery": 20}]})
    with pytest.raises(NovaPoshtaValidationError):
        await methods.get_price(
            _client(handler),
            api_key="K",
            city_sender_ref="A",
            city_recipient_ref="B",
            weight_kg=Decimal("2"),
            cost=Decimal("400"),
        )


async def test_get_status_documents_batches_numbers():
    handler, captured = _router(
        {
            ("TrackingDocument", "getStatusDocuments"): [
                {"Number": "59000111", "Status": "Прибув", "StatusCode": "7"}
            ]
        }
    )
    statuses = await methods.get_status_documents(
        _client(handler), api_key="K", numbers=["59000111", "59000222"]
    )
    assert statuses[0].number == "59000111"
    assert statuses[0].status_code == "7"
    assert captured["body"]["methodProperties"]["Documents"] == [
        {"DocumentNumber": "59000111"},
        {"DocumentNumber": "59000222"},
    ]


async def test_save_ttn_sends_mapped_props_and_parses_result():
    draft = _draft()
    handler, captured = _router(
        {
            ("InternetDocument", "save"): [
                {"Ref": "doc-ref", "IntDocNumber": "59000333", "CostOnSite": 70}
            ]
        }
    )
    result = await methods.save_ttn(_client(handler), api_key="K", draft=draft)
    assert result.ref == "doc-ref"
    assert result.int_doc_number == "59000333"
    assert result.cost == Decimal("70")
    # отправленные props ровно те, что собрал чистый маппинг
    assert captured["body"]["methodProperties"] == to_save_props(draft)


async def test_delete_ttn_sends_document_refs():
    handler, captured = _router({("InternetDocument", "delete"): [{"Ref": "doc-ref"}]})
    await methods.delete_ttn(_client(handler), api_key="K", doc_ref="doc-ref")
    assert captured["body"]["methodProperties"] == {"DocumentRefs": "doc-ref"}


async def test_validate_key_returns_sender_and_contact_refs():
    handler, captured = _router(
        {
            ("Counterparty", "getCounterparties"): [{"Ref": "cp-1"}],
            ("Counterparty", "getCounterpartyContactPersons"): [{"Ref": "contact-1"}],
        }
    )
    result = await methods.validate_key_and_get_sender(_client(handler), api_key="K")
    assert result.counterparty_ref == "cp-1"
    assert result.contact_ref == "contact-1"
    # вторая вызов параметризован Ref контрагента
    second = captured["bodies"][1]
    assert second["calledMethod"] == "getCounterpartyContactPersons"
    assert second["methodProperties"]["Ref"] == "cp-1"


async def test_validate_key_invalid_raises_auth_error():
    # НП на плохой ключ отвечает success=false с auth-кодом → AuthError из клиента
    handler, _ = _router({("Counterparty", "getCounterparties"): _fail([], ["20000200068"])})
    with pytest.raises(NovaPoshtaAuthError):
        await methods.validate_key_and_get_sender(_client(handler), api_key="bad")


async def test_validate_key_no_sender_counterparty_raises_validation():
    handler, _ = _router({("Counterparty", "getCounterparties"): []})
    with pytest.raises(NovaPoshtaValidationError):
        await methods.validate_key_and_get_sender(_client(handler), api_key="K")
