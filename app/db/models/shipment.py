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

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import ShipmentStatus

if TYPE_CHECKING:
    from app.db.models.client_account import ClientAccount
    from app.db.models.sender_profile import SenderProfile
    from app.db.models.stock_movement import StockMovement
    from app.db.models.user import User


class Shipment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "shipments"
    __table_args__ = (
        # Все списки кабинета и очереди менеджера фильтруют по скоупу и сортируют
        # по свежести. Одиночного индекса на `account_id` для этого мало: Postgres
        # берёт по нему строки, а потом сортирует их целиком — на 15k ТТН/мес это
        # сортировка десятков тысяч строк на каждый тап пагинации.
        Index("ix_shipments_account_created", "account_id", text("created_at DESC")),
        Index("ix_shipments_client_created", "client_id", text("created_at DESC")),
        # Фильтр по «корзине» статусов идёт вместе со скоупом.
        Index("ix_shipments_account_status", "account_id", "status"),
        # Выборки воркера. Частичные: документ без номера трекать нечем.
        # Заведены миграцией `a1b2c3d4e5f7`, но в метаданных их не было — из-за
        # чего `alembic check` считал их «лишними в БД». Держим здесь, чтобы
        # модель оставалась источником правды по схеме.
        Index(
            "ix_shipments_tracking_scan",
            "status",
            "tracking_updated_at",
            postgresql_where=text("ttn_number IS NOT NULL"),
        ),
        Index(
            "ix_shipments_return_watch",
            "status",
            "dispatched_at",
            postgresql_where=text("ttn_number IS NOT NULL"),
        ),
        # Поиск идёт `ILIKE '%…%'` — B-tree к нему неприменим в принципе.
        Index(
            "ix_shipments_ttn_trgm",
            "ttn_number",
            postgresql_using="gin",
            postgresql_ops={"ttn_number": "gin_trgm_ops"},
        ),
        Index(
            "ix_shipments_recipient_trgm",
            "recipient_name",
            postgresql_using="gin",
            postgresql_ops={"recipient_name": "gin_trgm_ops"},
        ),
    )

    # «Кто завёл ТТН» (у ТТН работника — сам работник), а не скоуп: компанию держит
    # `account_id`. Переживает удаление человека как NULL — см. `e5f6a7b8c1d3`.
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("client_accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
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
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
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

    client: Mapped[User | None] = relationship(foreign_keys=[client_id])
    account: Mapped[ClientAccount] = relationship()
    created_by_user: Mapped[User | None] = relationship(foreign_keys=[created_by_user_id])
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
