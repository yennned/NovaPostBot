"""Бізнес-акаунт клієнта та його членства."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import ClientAccountStatus, MembershipRole, MembershipStatus

if TYPE_CHECKING:
    from app.db.models.user import User


class ClientAccount(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "client_accounts"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[ClientAccountStatus] = mapped_column(
        Enum(ClientAccountStatus, name="client_account_status"),
        default=ClientAccountStatus.active,
        server_default=ClientAccountStatus.active.value,
        index=True,
        nullable=False,
    )
    stock_sheet_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stock_view_book_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    memberships: Mapped[list[ClientAccountMembership]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class ClientAccountMembership(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "client_account_memberships"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_client_account_memberships_user"),
        UniqueConstraint(
            "account_id", "user_id", name="uq_client_account_memberships_account_user"
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("client_accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    role: Mapped[MembershipRole] = mapped_column(
        Enum(MembershipRole, name="membership_role"), nullable=False
    )
    status: Mapped[MembershipStatus] = mapped_column(
        Enum(MembershipStatus, name="membership_status"),
        default=MembershipStatus.invited,
        server_default=MembershipStatus.invited.value,
        index=True,
        nullable=False,
    )
    invited_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    account: Mapped[ClientAccount] = relationship(back_populates="memberships")
    user: Mapped[User] = relationship(foreign_keys=[user_id], back_populates="account_memberships")
    invited_by: Mapped[User | None] = relationship(foreign_keys=[invited_by_user_id])
