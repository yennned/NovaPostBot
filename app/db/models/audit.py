"""Модель `audit_logs` — append-only журнал чувствительных действий.

Записи не удаляются. Не редактируются — **кроме одного случая**: при физическом
удалении человека `AuditRepository.scrub_user_pii` вычищает из `before`/`after`
его ПИБ/телефон/Telegram ID. Строка при этом остаётся: тип действия, время и
`account_id` продолжают отвечать «что было», просто без персональных данных.
Иначе ПИИ пережили бы удалённого человека в payload'ах.

`user_id` nullable — действие могло выполнить система, произойти до создания
пользователя либо его автора физически удалили (`ondelete=SET NULL`).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import UUIDPrimaryKeyMixin


class AuditLog(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "audit_logs"

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("client_accounts.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # Тип действия, напр. "user_activated", "permission_changed", "dev_impersonate".
    action: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    # На какую сущность повлияло, напр. "user:<uuid>" / "sender_profile:<uuid>".
    affected_entity: Mapped[str | None] = mapped_column(String(128), nullable=True)

    before: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Append-only: только момент создания, без updated_at.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    def __repr__(self) -> str:  # pragma: no cover - отладочное представление
        return f"<AuditLog id={self.id!s} action={self.action!r}>"
