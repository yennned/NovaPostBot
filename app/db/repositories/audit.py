"""Репозиторий аудита (`audit_logs`) — только запись (append-only)."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.db.models.audit import AuditLog
from app.db.models.client_account import ClientAccountMembership
from app.db.repositories.base import BaseRepository


class AuditRepository(BaseRepository):
    async def log(
        self,
        action: str,
        *,
        user_id: uuid.UUID | None = None,
        account_id: uuid.UUID | None = None,
        affected_entity: str | None = None,
        before: dict | None = None,
        after: dict | None = None,
        notes: str | None = None,
    ) -> AuditLog:
        if account_id is None and user_id is not None:
            account_id = await self.session.scalar(
                select(ClientAccountMembership.account_id).where(
                    ClientAccountMembership.user_id == user_id
                )
            )
        entry = AuditLog(
            action=action,
            user_id=user_id,
            account_id=account_id,
            affected_entity=affected_entity,
            before=before,
            after=after,
            notes=notes,
        )
        await self._add(entry)
        return entry
