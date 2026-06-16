"""Репозиторий аудита (`audit_logs`) — только запись (append-only)."""

from __future__ import annotations

import uuid

from app.db.models.audit import AuditLog
from app.db.repositories.base import BaseRepository


class AuditRepository(BaseRepository):
    async def log(
        self,
        action: str,
        *,
        user_id: uuid.UUID | None = None,
        affected_entity: str | None = None,
        before: dict | None = None,
        after: dict | None = None,
        notes: str | None = None,
    ) -> AuditLog:
        entry = AuditLog(
            action=action,
            user_id=user_id,
            affected_entity=affected_entity,
            before=before,
            after=after,
            notes=notes,
        )
        await self._add(entry)
        return entry
