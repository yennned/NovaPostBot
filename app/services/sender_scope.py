"""Резолв ФОП в скоупе актора — общий для создания ТТН, адресов и расчёта цены.

Один предикат принадлежности на все пути НП. Раньше его копий было три, и они
разошлись: `shipment` сверял `account_id`, а `address`/`pricing` — `client_id`,
из-за чего работник входил в кошик, но на выборе города получал «ФОП не знайдено».

Скоуп — **аккаунт**, а не человек: `sender_profiles.client_id` значит «кто завёл
профиль» (владелец), а принадлежность компании держит `account_id`. Работник
пользуется ФОП владельца, поэтому сверка по `client_id` для него ложна всегда.
Legacy-ветка по `client_id` остаётся только для вызовов без `account_id`.

Гейт готовности к відправленню (`ensure_sender_dispatchable`) сюда не входит:
справочникам адресов и расчёту цены провалидированный ФОП не нужен — им нужен
только ключ. Его накладывает `shipment._resolve_sender` поверх.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.sender_profile import SenderProfile
from app.db.models.user import User
from app.db.repositories import SenderProfileRepository
from app.services.exceptions import SenderProfileNotConfigured


def _in_scope(profile: SenderProfile, *, client: User, account_id: uuid.UUID | None) -> bool:
    if account_id is not None:
        return profile.account_id == account_id
    return profile.client_id == client.id


async def resolve_scoped_profile(
    session: AsyncSession,
    *,
    client: User,
    sender_profile_id: uuid.UUID | None,
    account_id: uuid.UUID | None = None,
) -> SenderProfile:
    """ФОП актора: явный (с проверкой скоупа) или дефолтный по аккаунту."""
    repo = SenderProfileRepository(session)
    if sender_profile_id is not None:
        profile = await repo.get_by_id(sender_profile_id)
        if profile is None or not _in_scope(profile, client=client, account_id=account_id):
            raise SenderProfileNotConfigured("ФОП не знайдено")
        return profile
    profile = await repo.get_default_for_client(client.id, account_id=account_id)
    if profile is None:
        raise SenderProfileNotConfigured("ФОП ще не налаштований, зверніться до менеджера")
    return profile
