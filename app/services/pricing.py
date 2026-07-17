"""Сервис онлайн-расчёта стоимости ТТН (`getDocumentPrice`). Без aiogram.

Тонкая обёртка для FSM создания ТТН: резолвит ключ ФОП клиента и зовёт
`methods.get_price`. Город-отправитель — наш склад из конфига (`NP_SENDER_CITY_REF`).
Кэширование результата — забота вызывающего (FSM-data по хэшу влияющих полей);
здесь только один сетевой вызов. Ошибки НП (`NovaPoshtaError` / отсутствие `Cost`
→ `NovaPoshtaValidationError`) пробрасываем — бот переводит в graceful-degradation.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models.user import User
from app.novaposhta import methods
from app.novaposhta.client import NovaPoshtaClient
from app.novaposhta.schemas import PriceQuote
from app.services.sender_scope import resolve_scoped_profile


async def _resolve_key(
    session: AsyncSession,
    client: User,
    sender_profile_id: uuid.UUID,
    account_id: uuid.UUID | None = None,
) -> str:
    """Ключ НП явно заданного ФОП в скоупе актора (FSM держит его id)."""
    profile = await resolve_scoped_profile(
        session, client=client, sender_profile_id=sender_profile_id, account_id=account_id
    )
    return profile.np_api_key  # EncryptedString расшифровывает при чтении


async def quote_ttn(
    session: AsyncSession,
    *,
    client: User,
    sender_profile_id: uuid.UUID,
    city_recipient_ref: str,
    weight: Decimal,
    cost: Decimal,
    np_client: NovaPoshtaClient,
    cod_amount: Decimal | None = None,
    account_id: uuid.UUID | None = None,
    settings: Settings | None = None,
) -> PriceQuote:
    """Стоимость/срок доставки НП (склад-отправитель → відділення получателя)."""
    settings = settings or get_settings()
    api_key = await _resolve_key(session, client, sender_profile_id, account_id)
    return await methods.get_price(
        np_client,
        api_key=api_key,
        city_sender_ref=settings.np_sender_city_ref,
        city_recipient_ref=city_recipient_ref,
        weight_kg=weight,
        cost=cost,
        cod_amount=cod_amount,
    )
