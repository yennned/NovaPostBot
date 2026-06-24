"""Модель `sender_profiles` — ФОП-отправители клиента (много на клиента).

`np_api_key` хранится зашифрованным (Fernet) через тип `EncryptedString`:
в Python — открытый ключ НП, в БД — шифртекст.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Enum, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import OrgType
from app.db.types import EncryptedString

if TYPE_CHECKING:
    from app.db.models.user import User


class SenderProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "sender_profiles"
    __table_args__ = (
        Index(
            "uq_sender_profiles_client_default",
            "client_id",
            unique=True,
            postgresql_where=text("is_default"),
        ),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Ключ НП — шифруется на запись, расшифровывается на чтение (см. EncryptedString).
    np_api_key: Mapped[str] = mapped_column(EncryptedString, nullable=False)

    sender_full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sender_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    org_type: Mapped[OrgType] = mapped_column(
        Enum(OrgType, name="org_type"),
        default=OrgType.fop,
        server_default=OrgType.fop.value,
        nullable=False,
    )
    edrpou: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # Кэш Reference'ов НП (заполняется при работе с API в следующих фазах).
    np_sender_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    np_contact_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    np_sender_warehouse: Mapped[str | None] = mapped_column(String(64), nullable=True)

    is_default: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )

    client: Mapped[User] = relationship(back_populates="sender_profiles")

    def __repr__(self) -> str:  # pragma: no cover - отладочное представление
        return f"<SenderProfile id={self.id!s} client={self.client_id!s} name={self.name!r}>"
