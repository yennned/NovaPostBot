"""Модель `users` — клиенты, менеджеры, владелец."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Enum, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import UserRole, UserStatus

if TYPE_CHECKING:
    from app.db.models.client_account import ClientAccountMembership
    from app.db.models.notification_setting import NotificationSetting
    from app.db.models.sender_profile import SenderProfile
    from app.db.models.stock_movement import StockMovement


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    # Telegram-идентификатор (основной ключ авторизации). Nullable: владелец может
    # завести менеджера по одному телефону (ещё не запускавшего бота) — telegram_id
    # проставится при первом входе по контакту (адопция по номеру в register_contact).
    telegram_id: Mapped[int | None] = mapped_column(
        BigInteger, unique=True, index=True, nullable=True
    )
    # Телефон появляется после request_contact → nullable до этого момента.
    phone: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role"),
        default=UserRole.client,
        server_default=UserRole.client.value,
        nullable=False,
    )
    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus, name="user_status"),
        default=UserStatus.pending,
        server_default=UserStatus.pending.value,
        nullable=False,
    )
    # Per-flag права менеджера (например, {"can_edit_clients": true}).
    permissions: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )

    account_memberships: Mapped[list[ClientAccountMembership]] = relationship(
        back_populates="user",
        foreign_keys="ClientAccountMembership.user_id",
        cascade="all, delete-orphan",
    )

    # Дежурство менеджера.
    on_duty: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    duty_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Момент открытия смены (нажатия «🟢 Я на зв'язку»). При нескольких дежурных
    # новый тред получает вставший последним; авто-снимается воркером.
    duty_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    sender_profiles: Mapped[list[SenderProfile]] = relationship(
        back_populates="client", cascade="all, delete-orphan"
    )
    notification_settings: Mapped[list[NotificationSetting]] = relationship(
        cascade="all, delete-orphan"
    )
    stock_movements: Mapped[list[StockMovement]] = relationship(
        foreign_keys="StockMovement.client_id"
    )

    def __repr__(self) -> str:  # pragma: no cover - отладочное представление
        return f"<User id={self.id!s} tg={self.telegram_id} role={self.role.value}>"
