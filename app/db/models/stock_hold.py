"""Короткоживущая бронь остатка на время похода в Нову Пошту.

Гейт от oversell обязан пережить внешний вызов: порядок NP-first означает, что
между «проверили остаток» и «записали резерв» лежит `InternetDocument.save`
(p50 2.5 с, при флаки-НП до 45 с). Держать всё это время открытую транзакцию с
`FOR UPDATE` нельзя — это idle-in-transaction поверх сети на пуленном коннекте
Neon, и при десятке одновременных отправок одного аккаунта очередь встаёт.

Поэтому бронь фиксируется своей короткой транзакцией и **коммитится до** вызова
НП: только так её увидит второй коннект. При успехе она привязывается к ТТН, при
любом сбое — снимается, а если процесс упал между фазами, её добьёт дворник по
`expires_at`. Худший случай — заниженный `available` на несколько минут, то есть
недопродажа, а не oversell.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.db.models.client_account import ClientAccount
    from app.db.models.shipment import Shipment


class StockHold(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "stock_holds"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_stock_holds_quantity_positive"),
        # Горячий путь гейта: сумма активных броней по (аккаунт, SKU). Индекс
        # частичный — снятые брони живут только ради аудита и в расчёт не входят.
        Index(
            "ix_stock_holds_active",
            "account_id",
            "sku",
            postgresql_where="released_at IS NULL",
        ),
        # Дворник ищет просроченные среди активных.
        Index(
            "ix_stock_holds_expiry",
            "expires_at",
            postgresql_where="released_at IS NULL",
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("client_accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: Проставляется, когда ТТН создана: с этого момента остаток держит статус
    #: отправления, а не бронь.
    shipment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("shipments.id", ondelete="SET NULL"), nullable=True
    )

    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)

    #: Ключ попытки сабмита — чтобы повторный тап не плодил вторую бронь на ту же
    #: корзину и чтобы снятие било по всей попытке целиком, а не по одной позиции.
    submit_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    account: Mapped[ClientAccount] = relationship()
    shipment: Mapped[Shipment | None] = relationship()
