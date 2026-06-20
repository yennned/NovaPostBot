"""Модели отправлений клиента (Фаза 3).

В Phase 3 нам нужен read-side для кабинета клиента: список ТТН, карточка,
резервы под ещё не отправленные заказы и статистика по статусам. Поэтому
модель пока хранит только те поля, которые уже нужны UI и сервисам.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Numeric, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import ShipmentStatus

if TYPE_CHECKING:
    from app.db.models.sender_profile import SenderProfile
    from app.db.models.stock_movement import StockMovement
    from app.db.models.user import User


class Shipment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "shipments"

    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    sender_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sender_profiles.id", ondelete="SET NULL"), index=True, nullable=True
    )

    ttn_number: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True)
    np_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)

    recipient_name: Mapped[str] = mapped_column(String(255), nullable=False)
    recipient_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    recipient_city: Mapped[str | None] = mapped_column(String(255), nullable=True)
    recipient_warehouse: Mapped[str | None] = mapped_column(String(255), nullable=True)
    recipient_kind: Mapped[str] = mapped_column(String(32), server_default="person", nullable=False)

    payer_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payment_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cod_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    insured_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)

    # Габариты посылки. Пресет НП (документи/мала/...) и фактический вес (кг).
    # «Власні розміри» (Д×Ш×В) — транзитом в НП, не персистим. Вес полезен для
    # карточки и синка перевзвешивания НП в Фазе 5.
    size_preset: Mapped[str | None] = mapped_column(String(32), nullable=True)
    weight: Mapped[Decimal | None] = mapped_column(Numeric(8, 3), nullable=True)

    status: Mapped[ShipmentStatus] = mapped_column(
        Enum(ShipmentStatus, name="shipment_status"),
        default=ShipmentStatus.created,
        server_default=ShipmentStatus.created.value,
        index=True,
        nullable=False,
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    status_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tracking_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sla_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sla_met: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    fee_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    fee_free: Mapped[bool] = mapped_column(Boolean, server_default=text("false"), nullable=False)

    client: Mapped[User] = relationship()
    sender_profile: Mapped[SenderProfile | None] = relationship()
    items: Mapped[list[ShipmentItem]] = relationship(
        back_populates="shipment",
        cascade="all, delete-orphan",
        order_by="ShipmentItem.created_at",
    )
    stock_movements: Mapped[list[StockMovement]] = relationship(
        back_populates="shipment",
        order_by="StockMovement.created_at",
    )


class ShipmentItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "shipment_items"

    shipment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shipments.id", ondelete="CASCADE"), index=True, nullable=False
    )
    sku: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str | None] = mapped_column(String(255), nullable=True)
    quantity: Mapped[int] = mapped_column(nullable=False)
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)

    shipment: Mapped[Shipment] = relationship(back_populates="items")
