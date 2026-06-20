"""Персональные настройки уведомлений пользователя."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.db.models.user import User


class NotificationSetting(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "notification_settings"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_notification_settings_user_key"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, server_default=text("true"), default=True, nullable=False
    )

    user: Mapped[User] = relationship(back_populates="notification_settings")
