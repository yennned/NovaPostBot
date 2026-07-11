"""Состояние low-stock алертов для антиспама воркера."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.db.models.client_account import ClientAccount


class LowStockAlert(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "low_stock_alerts"
    __table_args__ = (UniqueConstraint("client_id", "sku", name="uq_low_stock_alerts_client_sku"),)

    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("client_accounts.id", ondelete="CASCADE"), index=True, nullable=True
    )
    sku: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    is_low: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_available: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    account: Mapped[ClientAccount | None] = relationship()
