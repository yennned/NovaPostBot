"""Коннект к Postgres не удерживается через вызов Нової Пошти.

Ради этого и правка: `InternetDocument.save` — это p50 2,5 с, а при флаки-НП до
45 с с ретраями. Всё это время коннект из пула Neon был бы занят ничем, а под
всплеском их 50 на процесс — то есть десяток одновременных отправок выбирает пул,
и следующий апдейт ждёт, хотя работать ему не мешает никто.

Проверяется изнутри самого вызова НП: в момент, когда транспорт получает запрос,
у сессии не должно быть открытой транзакции.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
from app.config import Settings
from app.db.models.enums import UserRole, UserStatus
from app.db.repositories import ClientAccountRepository, SenderProfileRepository, UserRepository
from app.novaposhta.client import NovaPoshtaClient
from app.services import shipment
from app.sheets.source import StockRow
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import account_of

_OK_ROUTES = {
    ("Counterparty", "save"): [{"Ref": "rcpt-cp", "ContactPerson": {"data": [{"Ref": "rcpt-ct"}]}}],
    ("InternetDocument", "save"): [
        {"Ref": "doc-ref", "IntDocNumber": "59000999", "CostOnSite": 70}
    ],
}


class _Reader:
    def read_stock(self, client_key: str):
        return [
            StockRow(sku="COF-1", name="Кава", category="Кава", quantity=10, price=Decimal("100"))
        ]


def _settings() -> Settings:
    settings = Settings(_env_file=None)
    settings.np_retry_backoff = 0.0
    settings.np_sender_city_ref = "sender-city"
    settings.np_sender_warehouse_ref = "sender-wh"
    return settings


async def _client(session: AsyncSession, telegram_id: int):
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
    return client, await account_of(session, client)


async def _create(session, client, account, np_client):
    return await shipment.create_shipment(
        session,
        client=client,
        account=account,
        account_id=account.id,
        items=[("COF-1", 3)],
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
        reader=_Reader(),
        settings=_settings(),
    )


async def test_no_open_transaction_while_calling_nova_poshta(db_session: AsyncSession):
    """В момент запроса к НП транзакция сессии обязана быть закрыта.

    Проверяем изнутри транспорта, а не после факта: «коммит был где-то раньше» —
    это не то же самое, что «коннект свободен именно сейчас».
    """
    client, account = await _client(db_session, 1900)
    seen: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(db_session.in_transaction())
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": _OK_ROUTES[(body["modelName"], body["calledMethod"])],
                "errors": [],
                "errorCodes": [],
            },
        )

    np_settings = Settings(_env_file=None)
    np_settings.np_retry_backoff = 0.0
    card = await _create(
        db_session,
        client,
        account,
        NovaPoshtaClient(settings=np_settings, transport=httpx.MockTransport(handler)),
    )

    assert card.ttn_number == "59000999"
    assert seen and not any(seen), (
        "коннект удерживается через вызов НП: под всплеском это выбирает пул на "
        f"пустом месте (транзакция открыта в {sum(seen)} из {len(seen)} запросов)"
    )


async def test_active_account_is_not_reloaded_from_db(db_session: AsyncSession):
    """Активный аккаунт уже загружен мидлварью — второй раз его тянуть незачем.

    Членство грузилось по два-три раза за апдейт, и каждый раз это round-trip
    внутри того же апдейта, при котором коннект держится через внешнее I/O.
    """
    client, account = await _client(db_session, 1901)

    calls = 0
    original = ClientAccountRepository.get_membership

    async def counting(self, **kwargs):
        nonlocal calls
        calls += 1
        return await original(self, **kwargs)

    ClientAccountRepository.get_membership = counting
    try:
        np_settings = Settings(_env_file=None)
        np_settings.np_retry_backoff = 0.0

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": _OK_ROUTES[(body["modelName"], body["calledMethod"])],
                    "errors": [],
                    "errorCodes": [],
                },
            )

        await _create(
            db_session,
            client,
            account,
            NovaPoshtaClient(settings=np_settings, transport=httpx.MockTransport(handler)),
        )
    finally:
        ClientAccountRepository.get_membership = original

    assert calls == 0, f"членство перезагружалось {calls} раз(а) при уже переданном аккаунте"
