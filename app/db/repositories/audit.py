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
        account_id: uuid.UUID | None = None,
        affected_entity: str | None = None,
        before: dict | None = None,
        after: dict | None = None,
        notes: str | None = None,
    ) -> AuditLog:
        """Записать действие в аудит.

        `user_id` — **актор** (кто сделал). `account_id` — **субъект**: чей
        клиентский аккаунт затронут действием. Это разные вещи, и выводить одно
        из другого нельзя: менеджер подтверждает отправление клиента (актор —
        менеджер, субъект — аккаунт клиента), а дежурство менеджера не касается
        клиентских аккаунтов вовсе.

        Поэтому `account_id` проставляет вызывающий и только явно. У действия нет
        аккаунта-субъекта (дежурство, персонал, bootstrap, dev) — остаётся `None`.
        Раньше метод догружал членство актора и писал его аккаунт: staff-действия
        о клиенте получали `NULL`, а дежурство — чужой аккаунт.
        """
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
