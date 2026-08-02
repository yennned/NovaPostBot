"""Трёхфазный сабмит: бронь до НП, снятие при сбое, привязка при успехе.

Гонку двух коннектов проверяет `tests/test_stock_holds.py` — там она поставлена
честно, с двумя транзакциями. Здесь проверяется **обвязка**: что бронь вообще
берётся до похода в НП, что она не остаётся висеть после отказа НП и что после
успеха её снимают, иначе остаток вычитается дважды.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import httpx
import pytest
from app.config import Settings, get_settings
from app.db.models.client_account import ClientAccount
from app.db.models.enums import StockMovementType, UserRole, UserStatus
from app.db.models.stock_hold import StockHold
from app.db.models.user import User
from app.db.repositories import (
    InsufficientAvailable,
    SenderProfileRepository,
    ShipmentItemDraft,
    ShipmentRepository,
    StockBalanceRepository,
    StockHoldRepository,
    UserRepository,
)
from app.novaposhta.client import NovaPoshtaClient
from app.services import shipment
from app.services.exceptions import InsufficientStock, TtnCreationFailed
from app.sheets.source import StockRow
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import account_of

_OK_ROUTES = {
    ("Counterparty", "save"): [{"Ref": "rcpt-cp", "ContactPerson": {"data": [{"Ref": "rcpt-ct"}]}}],
    ("InternetDocument", "save"): [
        {"Ref": "doc-ref", "IntDocNumber": "59000999", "CostOnSite": 70}
    ],
}


class _SheetReader:
    def read_stock(self, client_key: str):
        return [
            StockRow(sku="COF-1", name="Кава", category="Кава", quantity=10, price=Decimal("100"))
        ]


def _np_client(routes) -> NovaPoshtaClient:
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


def _failing_np_client() -> NovaPoshtaClient:
    return _np_client(
        {
            ("Counterparty", "save"): [
                {"Ref": "rcpt-cp", "ContactPerson": {"data": [{"Ref": "rcpt-ct"}]}}
            ],
            ("InternetDocument", "save"): httpx.Response(
                200,
                json={
                    "success": False,
                    "data": [],
                    "errors": ["Не вдалося створити накладну"],
                    "errorCodes": [],
                },
            ),
        }
    )


@pytest.fixture
def pg_inventory(monkeypatch):
    monkeypatch.setenv("INVENTORY_SOURCE", "pg")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _settings() -> Settings:
    settings = Settings(_env_file=None)
    settings.np_retry_backoff = 0.0
    settings.np_sender_city_ref = "sender-city"
    settings.np_sender_warehouse_ref = "sender-wh"
    return settings


async def _client_with_stock(session: AsyncSession, telegram_id: int, *, quantity: int):
    client = await UserRepository(session).create(
        telegram_id=telegram_id,
        phone=f"+3800{telegram_id}",
        full_name="Клієнт",
        role=UserRole.client,
        status=UserStatus.active,
    )
    await SenderProfileRepository(session).create(
        client_id=client.id,
        name="ФОП",
        np_api_key="np-key",
        np_sender_ref="sender-cp",
        np_contact_ref="sender-ct",
        sender_phone="+380501112233",
        is_default=True,
    )
    account = await account_of(session, client)
    repo = StockBalanceRepository(session)
    await repo.apply_movement(
        account_id=account.id,
        sku="COF-1",
        delta=quantity,
        movement_type=StockMovementType.intake,
    )
    await repo.upsert_meta(account_id=account.id, sku="COF-1", name="Кава", price=Decimal("100"))
    return client, account


async def _create(session, client, account, np_client, *, items=None, reader=None):
    return await shipment.create_shipment(
        session,
        client=client,
        account=account,
        account_id=account.id,
        items=items if items is not None else [("COF-1", 3)],
        recipient_kind="person",
        recipient_name="Іван Петренко",
        recipient_phone="380671234567",
        recipient_city_ref="city-ref",
        recipient_city_name="Київ",
        recipient_warehouse_ref="wh-ref",
        recipient_warehouse_name="Відділення №1",
        weight=Decimal("2"),
        size_preset="mala",
        description="Кава",
        insured_amount=Decimal("500"),
        np_client=np_client,
        reader=reader,
        settings=_settings(),
    )


async def test_successful_submit_releases_the_hold(db_session: AsyncSession, pg_inventory):
    """После создания ТТН бронь обязана уйти.

    Остаток дальше держит статус отправления (`RESERVING_STATUSES`). Оставь бронь
    активной — то же количество вычлось бы дважды, и клиент увидел бы вдвое
    меньший доступный остаток, чем есть на самом деле.
    """
    client, account = await _client_with_stock(db_session, 1700, quantity=10)

    card = await _create(db_session, client, account, _np_client(_OK_ROUTES))

    assert card.ttn_number == "59000999"
    holds = StockHoldRepository(db_session)
    assert await holds.active_by_sku(account.id) == {}
    # Резерв теперь выводится из статуса ТТН — как и раньше.
    assert await ShipmentRepository(db_session).reserved_by_account(account.id) == {"COF-1": 3}
    # Связь брони с ТТН сохранена: по ней разбирают, откуда взялся захват.
    attached = await holds.by_submit_key(
        shipment.submit_key_for(
            client_id=client.id,
            account_id=account.id,
            drafts=[ShipmentItemDraft(sku="COF-1", name="Кава", quantity=3)],
        )
    )
    assert [h.shipment_id for h in attached] == [card.id], (
        "ключ попытки обязан быть детерминированным по корзине — иначе двойной тап "
        "взял бы вторую бронь на ту же корзину"
    )


async def test_np_failure_releases_the_hold(db_session: AsyncSession, pg_inventory):
    """НП отказала — товар обязан снова стать доступным.

    Бронь закоммичена до вызова НП, поэтому откат транзакции её не уберёт: снятие
    должно быть явным и тоже закоммиченным. Иначе клиент не смог бы продать
    собственный остаток до срабатывания дворника.
    """
    client, account = await _client_with_stock(db_session, 1701, quantity=10)

    with pytest.raises(TtnCreationFailed):
        await _create(db_session, client, account, _failing_np_client())

    assert await StockHoldRepository(db_session).active_by_sku(account.id) == {}
    # И следующая попытка на весь остаток проходит гейт.
    card = await _create(db_session, client, account, _np_client(_OK_ROUTES), items=[("COF-1", 10)])
    assert card.ttn_number == "59000999"


async def test_gate_refuses_when_not_enough_available(db_session: AsyncSession, pg_inventory):
    """Отказ приходит доменным `InsufficientStock` — экраны его уже умеют показывать."""
    client, account = await _client_with_stock(db_session, 1702, quantity=2)

    with pytest.raises(InsufficientStock) as exc:
        await _create(db_session, client, account, _np_client(_OK_ROUTES), items=[("COF-1", 5)])

    assert exc.value.sku == "COF-1"
    assert await StockHoldRepository(db_session).active_by_sku(account.id) == {}


async def test_gate_is_inert_while_source_is_the_sheet(db_session: AsyncSession):
    """При `INVENTORY_SOURCE=sheets` броней не появляется вовсе.

    У Google нет ни транзакций, ни строчных локов: гейт поверх листа изображал бы
    защиту, которой нет, и при этом занижал бы остаток на время каждого сабмита.
    """
    client, account = await _client_with_stock(db_session, 1703, quantity=10)

    card = await _create(db_session, client, account, _np_client(_OK_ROUTES), reader=_SheetReader())

    assert card.ttn_number == "59000999"
    rows = list(
        await db_session.scalars(select(StockHold).where(StockHold.account_id == account.id))
    )
    assert rows == [], "на пути Sheets брони не заводятся ни активные, ни снятые"


async def test_hold_is_visible_to_another_connection_during_the_np_call(engine, pg_inventory):
    """Фаза 1 обязана **закоммитить** бронь до похода в НП.

    Это и есть весь смысл трёхфазного сабмита, и одной сессией его не проверить:
    незакоммиченная бронь прекрасно видна самой себе. Поэтому здесь два настоящих
    коннекта — первый парковаается внутри вызова НП, второй в этот момент пытается
    занять тот же товар и обязан получить отказ.

    Убери `commit()` из фазы 1 — и второй коннект либо увидит остаток нетронутым,
    либо встанет на локе до конца чужого похода в НП. Оба исхода тест валят.
    """
    in_np = asyncio.Event()
    let_go = asyncio.Event()
    client_id = account_id = None
    try:
        async with AsyncSession(engine, expire_on_commit=False) as setup:
            client, account = await _client_with_stock(setup, 1799, quantity=10)
            await setup.commit()
            client_id, account_id = client.id, account.id

        async def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if (body["modelName"], body["calledMethod"]) == ("InternetDocument", "save"):
                in_np.set()
                await let_go.wait()
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": _OK_ROUTES[(body["modelName"], body["calledMethod"])],
                    "errors": [],
                    "errorCodes": [],
                },
            )

        async def submit():
            np_settings = Settings(_env_file=None)
            np_settings.np_retry_backoff = 0.0
            async with AsyncSession(engine, expire_on_commit=False) as session:
                client = await session.get(User, client_id)
                account = await session.get(ClientAccount, account_id)
                card = await _create(
                    session,
                    client,
                    account,
                    NovaPoshtaClient(settings=np_settings, transport=httpx.MockTransport(handler)),
                )
                await session.commit()
                return card

        task = asyncio.create_task(submit())
        await asyncio.wait_for(in_np.wait(), timeout=15)

        async with AsyncSession(engine, expire_on_commit=False) as other:
            reserved = await ShipmentRepository(other).reserved_by_account(account_id)
            with pytest.raises(InsufficientAvailable):
                # `wait_for`, потому что без коммита фазы 1 этот вызов не упадёт, а
                # встанет на локе строки остатка до конца чужого похода в НП.
                await asyncio.wait_for(
                    StockHoldRepository(other).hold(
                        account_id=account_id,
                        client_id=None,
                        submit_key="other-attempt",
                        wanted={"COF-1": 8},
                        reserved=reserved,
                        ttl_seconds=300,
                    ),
                    timeout=8,
                )
            await other.rollback()

        let_go.set()
        card = await asyncio.wait_for(task, timeout=20)
        assert card.ttn_number == "59000999"
    finally:
        let_go.set()
        async with AsyncSession(engine, expire_on_commit=False) as cleanup:
            if account_id is not None:
                await cleanup.execute(delete(ClientAccount).where(ClientAccount.id == account_id))
            if client_id is not None:
                await cleanup.execute(delete(User).where(User.id == client_id))
            await cleanup.commit()
