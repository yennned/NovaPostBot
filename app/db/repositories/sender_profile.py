"""Репозиторий ФОП-отправителей (`sender_profiles`)."""

from __future__ import annotations

import uuid

from sqlalchemy import select, update

from app.db.models.enums import OrgType
from app.db.models.sender_profile import SenderProfile
from app.db.repositories.base import BaseRepository


class SenderProfileRepository(BaseRepository):
    async def get_by_id(self, profile_id: uuid.UUID) -> SenderProfile | None:
        return await self.session.get(SenderProfile, profile_id)

    async def list_for_client(self, client_id: uuid.UUID) -> list[SenderProfile]:
        stmt = (
            select(SenderProfile)
            .where(SenderProfile.client_id == client_id)
            .order_by(SenderProfile.created_at)
        )
        return list(await self.session.scalars(stmt))

    async def get_default_for_client(self, client_id: uuid.UUID) -> SenderProfile | None:
        stmt = select(SenderProfile).where(
            SenderProfile.client_id == client_id,
            SenderProfile.is_default.is_(True),
        )
        return await self.session.scalar(stmt)

    async def create(
        self,
        *,
        client_id: uuid.UUID,
        name: str,
        np_api_key: str,
        sender_full_name: str | None = None,
        sender_phone: str | None = None,
        org_type: OrgType = OrgType.fop,
        edrpou: str | None = None,
        is_default: bool = False,
    ) -> SenderProfile:
        profile = SenderProfile(
            client_id=client_id,
            name=name,
            np_api_key=np_api_key,
            sender_full_name=sender_full_name,
            sender_phone=sender_phone,
            org_type=org_type,
            edrpou=edrpou,
            is_default=is_default,
        )
        await self._add(profile)
        if is_default:
            await self.set_default(profile)
        return profile

    async def update(self, profile: SenderProfile, **fields) -> SenderProfile:
        for key, value in fields.items():
            setattr(profile, key, value)
        await self.session.flush()
        return profile

    async def set_default(self, profile: SenderProfile) -> SenderProfile:
        """Сделать ФОП дефолтным, сняв флаг с остальных ФОП этого клиента."""
        await self.session.execute(
            update(SenderProfile)
            .where(
                SenderProfile.client_id == profile.client_id,
                SenderProfile.id != profile.id,
                SenderProfile.is_default.is_(True),
            )
            .values(is_default=False)
        )
        profile.is_default = True
        await self.session.flush()
        return profile
