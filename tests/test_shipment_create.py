"""Тесты write-сервиса создания ТТН (Фаза 4, PR 6) — Postgres + фейковый NP."""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
from app.config import Settings
from app.db.models.enums import ShipmentStatus, StockMovementType, UserRole, UserStatus
from app.db.repositories import (
    SenderProfileRepository,
    ShipmentRepository,
    StockMovementRepository,
    UserRepository,
)
from app.novaposhta.client import NovaPoshtaClient
from app.novaposhta.exceptions import NovaPoshtaNotFound
from app.services import shipment
from app.services.exceptions import (
    InsufficientStock,
    SenderDispatchNotConfigured,
    SenderProfileIncomplete,
    SenderProfileNotConfigured,
    SenderProfileNotValidated,
    TtnCancelFailed,
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


def _sender_settings() -> Settings:
    """Конфиг с заданным складом-отправителем (Ref города/відділення) — иначе гейт
    `ensure_sender_dispatchable` справедливо бросит `SenderDispatchNotConfigured`."""
    settings = Settings(_env_file=None)
    settings.np_retry_backoff = 0.0
    settings.np_sender_city_ref = "sender-city"
    settings.np_sender_warehouse_ref = "sender-wh"
    return settings


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


def _exploding_np_client() -> NovaPoshtaClient:
    """NP-клиент, падающий на любом запросе — для проверки, что гейт отправителя
    срабатывает ДО обращения к НП (save_ttn не должен вызываться)."""
    settings = Settings(_env_file=None)
    settings.np_retry_backoff = 0.0

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("НП не повинна викликатися — гейт відправника має спрацювати раніше")

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
        sender_phone="+380501112233",
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
        "settings": _sender_settings(),
    }
    kwargs.update(over)
    return await shipment.create_shipment(session, **kwargs)


async def test_create_shipment_happy_writes_row_and_reserves(db_session: AsyncSession):
    client = await _active_client(db_session)
    await _validated_profile(db_session, client)

    card = await _create(db_session, client, _np_client(_OK_ROUTES))

    assert card.ttn_number == "59000999"
    assert card.sla_deadline is not None
    assert card.fee_amount == Decimal("22")
    assert card.fee_free is False
    # резерв активен (status=created учитывается reserved_by_sku)
    reserved = await ShipmentRepository(db_session).reserved_by_sku(client.id)
    assert reserved == {"COF-1": 3}
    movement = (await StockMovementRepository(db_session).list_for_shipment(card.id))[0]
    assert movement.movement_type is StockMovementType.ttn_reserve


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


async def test_create_shipment_incomplete_profile_no_phone_raises(db_session: AsyncSession):
    client = await _active_client(db_session, telegram_id=509)
    # ключ валиден (есть Ref'ы), но не заполнен sender_phone → НП-сабмит упал бы
    await SenderProfileRepository(db_session).create(
        client_id=client.id,
        name="ФОП",
        np_api_key="k",
        np_sender_ref="sender-cp",
        np_contact_ref="sender-ct",
        is_default=True,
    )
    with pytest.raises(SenderProfileIncomplete):
        await _create(db_session, client, _exploding_np_client())  # НП не должна вызываться
    assert await ShipmentRepository(db_session).reserved_by_sku(client.id) == {}


async def test_create_shipment_incomplete_profile_no_contact_raises(db_session: AsyncSession):
    client = await _active_client(db_session, telegram_id=510)
    await SenderProfileRepository(db_session).create(
        client_id=client.id,
        name="ФОП",
        np_api_key="k",
        np_sender_ref="sender-cp",
        sender_phone="+380501112233",
        is_default=True,
    )  # без np_contact_ref
    with pytest.raises(SenderProfileIncomplete):
        await _create(db_session, client, _exploding_np_client())
    assert await ShipmentRepository(db_session).reserved_by_sku(client.id) == {}


async def test_create_shipment_garbage_sender_phone_raises(db_session: AsyncSession):
    # Легаси-профиль с мусором в телефоне (старый путь правки писал его без валидации).
    # Гейт обязан отбить ДО НП: иначе НП ответит своим «Вкажіть коректний номер
    # телефону», и клиент увидит его вместо понятной ошибки бота.
    client = await _active_client(db_session, telegram_id=512)
    await SenderProfileRepository(db_session).create(
        client_id=client.id,
        name="ФОП",
        np_api_key="k",
        np_sender_ref="sender-cp",
        np_contact_ref="sender-ct",
        sender_phone="Тест ФОП",
        is_default=True,
    )
    with pytest.raises(SenderProfileIncomplete):
        await _create(db_session, client, _exploding_np_client())  # НП не должна вызываться
    assert await ShipmentRepository(db_session).reserved_by_sku(client.id) == {}


async def test_create_shipment_sender_dispatch_unconfigured_raises(db_session: AsyncSession):
    client = await _active_client(db_session, telegram_id=511)
    await _validated_profile(db_session, client)  # профиль полный
    # но склад-отправитель системы не задан (пустые NP_SENDER_* в конфиге)
    with pytest.raises(SenderDispatchNotConfigured):
        await _create(db_session, client, _exploding_np_client(), settings=Settings(_env_file=None))
    assert await ShipmentRepository(db_session).reserved_by_sku(client.id) == {}


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


# --------------------------------------------------- NP-aware відміна (PR 9d follow-up)


async def _created_for_cancel(session, telegram_id):
    client = await _active_client(session, telegram_id=telegram_id)
    await _validated_profile(session, client)
    card = await _create(session, client, _np_client(_OK_ROUTES))  # np_ref=doc-ref, резерв COF-1:3
    return client, card


async def test_cancel_np_delete_and_release(db_session: AsyncSession):
    client, card = await _created_for_cancel(db_session, 520)
    cancelled = await shipment.cancel_shipment(
        db_session,
        client=client,
        shipment_id=card.id,
        np_client=_np_client({("InternetDocument", "delete"): [{"Ref": "doc-ref"}]}),
    )
    assert cancelled.status == ShipmentStatus.cancelled
    assert await ShipmentRepository(db_session).reserved_by_sku(client.id) == {}  # резерв снят


async def test_cancel_np_error_keeps_reserve(db_session: AsyncSession):
    client, card = await _created_for_cancel(db_session, 521)
    fail = httpx.Response(
        200, json={"success": False, "data": [], "errors": ["НП недоступна"], "errorCodes": []}
    )
    with pytest.raises(TtnCancelFailed):
        await shipment.cancel_shipment(
            db_session,
            client=client,
            shipment_id=card.id,
            np_client=_np_client({("InternetDocument", "delete"): fail}),
        )
    # NP-first: при сбое НП статус не трогаем → резерв держится (нет oversell).
    assert await ShipmentRepository(db_session).reserved_by_sku(client.id) == {"COF-1": 3}


async def test_cancel_already_deleted_idempotent(db_session: AsyncSession, monkeypatch):
    client, card = await _created_for_cancel(db_session, 522)

    async def _raise_not_found(*args, **kwargs):
        raise NovaPoshtaNotFound("вже видалено")

    monkeypatch.setattr(shipment.methods, "delete_ttn", _raise_not_found)
    cancelled = await shipment.cancel_shipment(
        db_session, client=client, shipment_id=card.id, np_client=_np_client(_OK_ROUTES)
    )
    assert cancelled.status == ShipmentStatus.cancelled  # «уже удалено» = успех
