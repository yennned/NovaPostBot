"""Остаток склада в Postgres — источник правды по количеству.

До этого количеством владел лист Google («Склад»), а бот читал его на каждый шаг
создания ТТН. Одна отправка стоила ~9 HTTP-запросов к Google, все они шли через
единственный поток (`app/sheets/runtime.py`), и при квоте 60 read/min и
60 write/min на service-account потолок был ~8 ТТН/мин на весь бот. Отдельно от
скорости: у листа нет транзакций, поэтому гейт от oversell (прочитали остаток →
ушли в НП на 2.5 с → записали резерв) пропускал два одновременных сабмита.

Люди при этом продолжают работать в таблицах — приёмка и ручные правки остаются в
Google без изменений, см. `docs/04-warehouse-sheets.md`. Меняется только то, кто
читает и пишет лист: не хендлер в момент нажатия «Відправити», а воркер батчем.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.db.models.client_account import ClientAccount


class StockBalance(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Физический остаток позиции на аккаунт.

    `available` по-прежнему выводится, а не хранится: `quantity − reserved −
    активные холды`. Резерв не дублируем колонкой — он и так выводится из статуса
    ТТН (`ShipmentRepository.reserved_by_account`), а вторая копия была бы ровно
    тем дрейфом, ради устранения которого всё и делается.
    """

    __tablename__ = "stock_balances"
    __table_args__ = (
        UniqueConstraint("account_id", "sku", name="uq_stock_balances_account_sku"),
        # Остаток не может уйти в минус. Это последний рубеж под гейтом от oversell:
        # если холды где-то посчитались неверно, БД откажет, а не продаст чужое.
        CheckConstraint("quantity >= 0", name="ck_stock_balances_quantity_non_negative"),
        # Экран товаров сортирует по категории и названию — без индекса это filesort
        # по всему ассортименту аккаунта (у крупнейшего сейчас 1636 позиций).
        Index("ix_stock_balances_account_sort", "account_id", "category", "name"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("client_accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    sku: Mapped[str] = mapped_column(String(64), nullable=False)

    # Описательные поля принадлежат листу: человек правит их прямо в «Складі», а
    # ингест забирает сюда. Зеркало их не пишет и потому не может затереть правку.
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)

    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    #: Что зеркало записало в ячейку «Кількість» в прошлый раз.
    #:
    #: База трёхстороннего слияния: если значение в листе отличается от неё, значит
    #: ячейку правил человек, и это намеренная коррекция, которую надо принять, а не
    #: затереть. Без этой колонки «человек поправил» неотличимо от «PG изменился»,
    #: и зеркало молча откатывало бы ручные правки — то самое, что владелец просил
    #: сохранить рабочим.
    mirrored_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)

    account: Mapped[ClientAccount] = relationship()
