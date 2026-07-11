"""Сумісне розв'язання legacy user-id та нового account-id."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.client_account import ClientAccountMembership
from app.db.models.enums import MembershipRole


async def resolve_account_scope(
    session: AsyncSession,
    *,
    client_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, uuid.UUID | None]:
    """Повернути `(legacy_client_id, account_id)` для старих і нових викликів."""
    if account_id is not None and client_id is not None:
        return client_id, account_id
    if client_id is not None:
        membership = await session.scalar(
            select(ClientAccountMembership).where(ClientAccountMembership.user_id == client_id)
        )
        return client_id, membership.account_id if membership is not None else account_id
    if account_id is None:
        raise ValueError("потрібен client_id або account_id")
    owner_id = await session.scalar(
        select(ClientAccountMembership.user_id).where(
            ClientAccountMembership.account_id == account_id,
            ClientAccountMembership.role == MembershipRole.account_owner,
        )
    )
    if owner_id is None:
        raise ValueError(f"акаунт {account_id} не має власника")
    return owner_id, account_id
