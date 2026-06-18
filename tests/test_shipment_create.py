"""Тесты write-сервиса создания ТТН (Фаза 4, PR 6) — Postgres + фейковый NP."""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
from app.config import Settings
from app.db.models.enums import UserRole, UserStatus
from app.db.repositories import SenderProfileRepository, ShipmentRepository, UserRepository
from app.novaposhta.client import NovaPoshtaClient
from app.services import shipment
from app.services.exceptions import (
    InsufficientStock,
    SenderProfileNotConfigured,
    SenderProfileNotValidated,
    TtnCreationFailed,
)
from app.sheets.inventory import StockRow
from sqlalchemy.ext.asyncio import AsyncSession


class _FakeReader:
    def read_stock(self, client_key: str):
        return [
            StockRow(sku="COF-1", name="Кава", category="Кава", quantity=10, price=Decimal("100")),
            StockRow(sku="TEA-1", name="Чай", category="Чай", quantity=2, price=Decimal("80")),
        ]


class _CollectingNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, telegram_id: int, text: str) -> None:
        self.sent.append((telegram_id, text))


_OK_ROUTES = {
    ("Counterparty", "save"): [{"Ref": "rcpt-cp", "ContactPerson": {"data": [{"Ref": "rcpt-ct"}]}}],
    ("InternetDocument", "save"): [
        {"Ref": "doc-ref", "IntDocNumber": "59000999", "CostOnSite": 70}
    ],
}


def _np_client(routes: dict[tuple[str, str], object]) -> NovaPoshtaClient:
    settings = Settings(_env_file=None)
    settings.np_retry_backoff = 0.0

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        result = routes[(body["modelName"], body["calledMethod"])]
        if isinstance(result, httpx.Response):
            return result
        return httpx.Response(
            200, json={"success": True, "data": result, "errors": [], "errorCodes": []}
        )

    return NovaPoshtaClient(settings=settings, transport=httpx.MockTransport(handler))


async def _active_client(session: AsyncSession, telegram_id: int = 500):
    return await UserRepository(session).create(
        telegram_id=telegram_id,
        phone=f"+3800{telegram_id}",
        full_name="Клієнт",
        role=UserRole.client,
        status=UserStatus.active,
    )


async def _validated_profile(session: AsyncSession, client):
    return await SenderProfileRepository(session).create(
        client_id=client.id,
        name="ФОП",
        np_api_key="np-key",
        np_sender_ref="sender-cp",
        np_contact_ref="sender-ct",
        is_default=True,
    )


async def _create(session, client, np_client, *, items=None, notifier=None, **over):
    kwargs = {
        "client": client,
        "items": items if items is not None else [("COF-1", 3)],
        "recipient_kind": "person",
        "recipient_name": "Іван Петренко",
        "recipient_phone": "380671234567",
        "recipient_city_ref": "city-ref",
        "recipient_city_name": "Київ",
        "recipient_warehouse_ref": "wh-ref",
        "recipient_warehouse_name": "Відділення №1",
        "weight": Decimal("2"),
        "size_preset": "mala",
        "description": "Кава",
        "insured_amount": Decimal("500"),
        "np_client": np_client,
        "reader": _FakeReader(),
        "notifier": notifier,
    }
    kwargs.update(over)
    return await shipment.create_shipment(session, **kwargs)


async def test_create_shipment_happy_writes_row_and_reserves(db_session: AsyncSession):
    client = await _active_client(db_session)
    await _validated_profile(db_session, client)

    card = await _create(db_session, client, _np_client(_OK_ROUTES))

    assert card.ttn_number == "59000999"
    # резерв активен (status=created учитывается reserved_by_sku)
    reserved = await ShipmentRepository(db_session).reserved_by_sku(client.id)
    assert reserved == {"COF-1": 3}


async def test_create_shipment_sends_manager_push(db_session: AsyncSession):
    owner = await UserRepository(db_session).create(
        telegram_id=1, role=UserRole.owner, status=UserStatus.active
    )
    client = await _active_client(db_session, telegram_id=501)
    await _validated_profile(db_session, client)
    notifier = _CollectingNotifier()

    await _create(db_session, client, _np_client(_OK_ROUTES), notifier=notifier)

    assert owner.telegram_id in {tid for tid, _ in notifier.sent}


async def test_create_shipment_np_failure_writes_nothing(db_session: AsyncSession):
    client = await _active_client(db_session, telegram_id=502)
    await _validated_profile(db_session, client)
    routes = {
        ("Counterparty", "save"): _OK_ROUTES[("Counterparty", "save")],
        ("InternetDocument", "save"): httpx.Response(
            200, json={"success": False, "data": [], "errors": ["bad field"], "errorCodes": []}
        ),
    }
    with pytest.raises(TtnCreationFailed):
        await _create(db_session, client, _np_client(routes))

    # NP-first: ничего не записано → резерва нет
    reserved = await ShipmentRepository(db_session).reserved_by_sku(client.id)
    assert reserved == {}


async def test_create_shipment_over_reserve_raises_before_np(db_session: AsyncSession):
    client = await _active_client(db_session, telegram_id=503)
    await _validated_profile(db_session, client)
    # TEA-1 доступно 2, просим 5
    with pytest.raises(InsufficientStock):
        await _create(db_session, client, _np_client(_OK_ROUTES), items=[("TEA-1", 5)])
    assert await ShipmentRepository(db_session).reserved_by_sku(client.id) == {}


async def test_create_shipment_duplicate_sku_lines_aggregate_against_available(
    db_session: AsyncSession,
):
    client = await _active_client(db_session, telegram_id=507)
    await _validated_profile(db_session, client)
    # COF-1 доступно 10; две строки по 6 = 12 > 10 → суммарно превышение
    with pytest.raises(InsufficientStock):
        await _create(
            db_session, client, _np_client(_OK_ROUTES), items=[("COF-1", 6), ("COF-1", 6)]
        )
    assert await ShipmentRepository(db_session).reserved_by_sku(client.id) == {}


async def test_create_shipment_cod_without_amount_raises(db_session: AsyncSession):
    client = await _active_client(db_session, telegram_id=508)
    await _validated_profile(db_session, client)
    with pytest.raises(TtnCreationFailed):
        await _create(
            db_session, client, _np_client(_OK_ROUTES), payment_method="cod", cod_amount=None
        )
    assert await ShipmentRepository(db_session).reserved_by_sku(client.id) == {}


async def test_create_shipment_no_profile_raises_not_configured(db_session: AsyncSession):
    client = await _active_client(db_session, telegram_id=504)
    with pytest.raises(SenderProfileNotConfigured):
        await _create(db_session, client, _np_client(_OK_ROUTES))


async def test_create_shipment_unvalidated_profile_raises(db_session: AsyncSession):
    client = await _active_client(db_session, telegram_id=505)
    # профиль без np_sender_ref (не валидирован)
    await SenderProfileRepository(db_session).create(
        client_id=client.id, name="ФОП", np_api_key="k", is_default=True
    )
    with pytest.raises(SenderProfileNotValidated):
        await _create(db_session, client, _np_client(_OK_ROUTES))


async def test_create_shipment_recipient_failure_writes_nothing(db_session: AsyncSession):
    client = await _active_client(db_session, telegram_id=506)
    await _validated_profile(db_session, client)
    routes = {
        ("Counterparty", "save"): httpx.Response(
            200, json={"success": False, "data": [], "errors": ["bad recipient"], "errorCodes": []}
        ),
    }
    with pytest.raises(TtnCreationFailed):
        await _create(db_session, client, _np_client(routes))
    assert await ShipmentRepository(db_session).reserved_by_sku(client.id) == {}
