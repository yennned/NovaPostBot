"""Значение `intake` в enum `stock_movement_type`.

Revision ID: b2c3d4e5f8a1
Revises: a1b2c3d4e5f7
Create Date: 2026-08-02

Отдельной миграцией и БЕЗ единой вставки с этим значением. Postgres не даёт
использовать значение enum в той же транзакции, в которой оно добавлено, а
Alembic оборачивает миграцию в транзакцию — то есть «добавил и тут же применил»
упало бы на проде, а не на CI.

`IF NOT EXISTS` — чтобы повторный прогон на частично применённой базе не падал.
"""

from __future__ import annotations

from alembic import op

revision = "b2c3d4e5f8a1"
down_revision = "a1b2c3d4e5f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE stock_movement_type ADD VALUE IF NOT EXISTS 'intake'")


def downgrade() -> None:
    # Postgres не умеет удалять значение из enum. Пересоздавать тип ради отката
    # опаснее, чем оставить неиспользуемое значение: пришлось бы переписывать
    # колонку в таблице движений, которая append-only и растёт.
    pass
