"""Тесты сервиса расчёта стоимости ТТН (Фаза 4, PR 9c) — Postgres + фейковый NP."""

from __future__ import annotations

import uuid
from decimal import Decimal

import httpx
import pytest
from app.config import Settings
from app.db.models.enums import UserRole, UserStatus
from app.db.repositories import SenderProfileRepository, UserRepository
from app.novaposhta.client import NovaPoshtaClient
from app.novaposhta.exceptions import NovaPoshtaValidationError
from app.services import pricing
from app.services.exceptions import SenderProfileNotConfigured
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import account_of, employee_of


def _np_client(price_row: dict | None):
    settings = Settings(_env_file=None)
    settings.np_retry_backoff = 0.0

    def handler(request: httpx.Request) -> httpx.Response:
        data = [price_row] if price_row is not None else []
        return httpx.Response(
            200, json={"success": True, "data": data, "errors": [], "errorCodes": []}
        )

    return NovaPoshtaClient(settings=settings, transport=httpx.MockTransport(handler))


def _settings() -> Settings:
    s = Settings(_env_file=None)
    s.np_sender_city_ref = "sender-city"
    return s


async def _client_with_profile(session: AsyncSession, telegram_id: int = 700):
    client = await UserRepository(session).create(
        telegram_id=telegram_id, role=UserRole.client, status=UserStatus.active
    )
    profile = await SenderProfileRepository(session).create(
        client_id=client.id, name="ФОП", np_api_key="np-key", is_default=True, np_sender_ref="cp"
    )
    return client, profile


async def test_employee_quotes_with_owner_profile(db_session: AsyncSession):
    """Регрессия: работник получает цену по ФОП владельца аккаунта.

    `pricing` скоупился по `client_id` — у работника он свой, а не владельца, —
    поэтому расчёт стоимости падал «ФОП не знайдено» так же, как поиск города.
    """
    owner = await UserRepository(db_session).create(
        telegram_id=710, phone="380507710001", role=UserRole.client, status=UserStatus.active
    )
    profile = await SenderProfileRepository(db_session).create(
        client_id=owner.id, name="ФОП", np_api_key="np-key", is_default=True, np_sender_ref="cp"
    )
    employee = await employee_of(db_session, owner, phone="0507710002", telegram_id=711)
    account = await account_of(db_session, owner)

    quote = await pricing.quote_ttn(
        db_session,
        client=employee,
        sender_profile_id=profile.id,
        account_id=account.id,
        city_recipient_ref="rcpt-city",
        weight=Decimal("1"),
        cost=Decimal("100"),
        np_client=_np_client(
            {"Cost": 55, "CostRedelivery": 0, "EstimatedDeliveryDate": "2026-07-20"}
        ),
        settings=_settings(),
    )
    assert quote.cost == Decimal("55")


async def test_quote_ttn_foreign_account_profile_raises(db_session: AsyncSession):
    """Скоуп переехал на аккаунт, а не отключён: чужой ФОП по-прежнему не отдаётся."""
    owner, _ = await _client_with_profile(db_session, telegram_id=712)
    account = await account_of(db_session, owner)
    _stranger, stranger_profile = await _client_with_profile(db_session, telegram_id=713)

    with pytest.raises(SenderProfileNotConfigured):
        await pricing.quote_ttn(
            db_session,
            client=owner,
            sender_profile_id=stranger_profile.id,
            account_id=account.id,
            city_recipient_ref="rcpt-city",
            weight=Decimal("1"),
            cost=Decimal("100"),
            np_client=_np_client({"Cost": 50}),
            settings=_settings(),
        )


async def test_quote_ttn_returns_price(db_session: AsyncSession):
    client, profile = await _client_with_profile(db_session)
    np_client = _np_client(
        {"Cost": 70, "CostRedelivery": 20, "EstimatedDeliveryDate": "2026-06-25"}
    )
    quote = await pricing.quote_ttn(
        db_session,
        client=client,
        sender_profile_id=profile.id,
        city_recipient_ref="rcpt-city",
        weight=Decimal("2.5"),
        cost=Decimal("300"),
        np_client=np_client,
        cod_amount=Decimal("300"),
        settings=_settings(),
    )
    assert quote.cost == Decimal("70")
    assert quote.cost_redelivery == Decimal("20")
    assert quote.estimated_delivery_date == "2026-06-25"


async def test_quote_ttn_missing_cost_raises(db_session: AsyncSession):
    client, profile = await _client_with_profile(db_session, telegram_id=701)
    np_client = _np_client({"CostRedelivery": 20})  # без Cost
    with pytest.raises(NovaPoshtaValidationError):
        await pricing.quote_ttn(
            db_session,
            client=client,
            sender_profile_id=profile.id,
            city_recipient_ref="rcpt-city",
            weight=Decimal("1"),
            cost=Decimal("100"),
            np_client=np_client,
            settings=_settings(),
        )


async def test_quote_ttn_foreign_profile_raises(db_session: AsyncSession):
    client, _ = await _client_with_profile(db_session, telegram_id=702)
    with pytest.raises(SenderProfileNotConfigured):
        await pricing.quote_ttn(
            db_session,
            client=client,
            sender_profile_id=uuid.uuid4(),  # чужой/несуществующий профиль
            city_recipient_ref="rcpt-city",
            weight=Decimal("1"),
            cost=Decimal("100"),
            np_client=_np_client({"Cost": 50}),
            settings=_settings(),
        )
