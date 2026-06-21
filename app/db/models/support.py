"""Модели поддержки: обращения клиентов и переписка (Фаза 6).

Релей-чат клиент↔дежурный менеджер хранится целиком в Postgres: тред
(`SupportThread`) держит участников, привязку к ТТН и статус; сообщения
(`SupportMessage`) — саму переписку. Прямого обмена Telegram-контактами нет —
всё идёт через бота, поэтому лог переписок неотчуждаем (виден owner/dev).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import SupportThreadStatus

if TYPE_CHECKING:
    from app.db.models.shipment import Shipment
    from app.db.models.user import User


class SupportThread(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "support_threads"

    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Дежурный менеджер, на которого маршрутизирован тред. NULL, пока обращение
    # лежит в очереди (`waiting`) без дежурного.
    assigned_manager_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # Опциональная привязка к ТТН (обращение из push нестандартной ситуации).
    shipment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("shipments.id", ondelete="SET NULL"), index=True, nullable=True
    )
    status: Mapped[SupportThreadStatus] = mapped_column(
        Enum(SupportThreadStatus, name="support_thread_status"),
        default=SupportThreadStatus.open,
        server_default=SupportThreadStatus.open.value,
        index=True,
        nullable=False,
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    client: Mapped[User] = relationship(foreign_keys=[client_id])
    assigned_manager: Mapped[User | None] = relationship(foreign_keys=[assigned_manager_id])
    shipment: Mapped[Shipment | None] = relationship()
    messages: Mapped[list[SupportMessage]] = relationship(
        back_populates="thread",
        cascade="all, delete-orphan",
        order_by="SupportMessage.created_at",
    )


class SupportMessage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "support_messages"

    thread_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("support_threads.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Роль автора реплики: значения `UserRole` + "dev" (god-mode). Не enum —
    # набор закрытый, но "dev" не входит в `user_role`, поэтому храним строкой.
    sender_role: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)

    thread: Mapped[SupportThread] = relationship(back_populates="messages")
