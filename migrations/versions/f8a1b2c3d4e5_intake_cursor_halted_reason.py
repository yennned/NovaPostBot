"""Признак остановки ингеста приёмки — в `stock_intake_cursor`.

Revision ID: f8a1b2c3d4e5
Revises: e5f8a1b2c3d4
Create Date: 2026-08-03

Ингест уже умел останавливаться на нарушенной целостности журнала, но знал об этом
только он сам, в памяти процесса. Читатель нужен другой — **зеркало**.

Пока ингест стоит, приёмка всё равно меняет «Кількість» в листе (её пишет Apps
Script по кнопке «Внести»). Зеркало видит расхождение ячейки с `mirrored_quantity`
и не может отличить приёмку от правки человека: дельта в пределах
`STOCK_MANUAL_DELTA_LIMIT` доезжает в PG движением `manual` вместо `intake`, а
превысившая лимит **отклоняется** — и ближайшая запись зеркала возвращает в ячейку
значение из PG, стирая приёмку из листа. То есть остановленный ингест не «замораживал
остаток», а тихо подменял тип движения и мог терять крупные приходы.

Поле nullable и без бэкфилла: `NULL` — «ингест здоров», и это верно для всех
существующих строк, потому что признак ставится только при следующей остановке.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f8a1b2c3d4e5"
down_revision = "e5f8a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "stock_intake_cursor",
        sa.Column("halted_reason", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("stock_intake_cursor", "halted_reason")
