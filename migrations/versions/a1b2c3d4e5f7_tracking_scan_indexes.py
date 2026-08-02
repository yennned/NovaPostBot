"""Индексы под выборки трекинга и позднего прохода возвратов.

Revision ID: a1b2c3d4e5f7
Revises: f7a8b9c0d1e2
Create Date: 2026-08-02

Горячий трекинг сортирует по `tracking_updated_at` (раньше — по
`status_changed_at`, из-за чего документ с неменяющимся статусом навсегда занимал
место в начале очереди). Поздний проход возвратов фильтрует по `dispatched_at`.
Ни одна из колонок не была проиндексирована, а составных индексов в `shipments`
не было вовсе — обе выборки шли через Sort по неиндексированному полю.

Оба индекса частичные (`ttn_number IS NOT NULL`): документ без номера не может
быть предметом трекинга, и таких строк в таблице заметная доля (черновики и
отменённые до отправки).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a1b2c3d4e5f7"
down_revision = "f7a8b9c0d1e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_shipments_tracking_scan",
        "shipments",
        ["status", "tracking_updated_at"],
        unique=False,
        postgresql_where=sa.text("ttn_number IS NOT NULL"),
    )
    op.create_index(
        "ix_shipments_return_watch",
        "shipments",
        ["status", "dispatched_at"],
        unique=False,
        postgresql_where=sa.text("ttn_number IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_shipments_return_watch", table_name="shipments")
    op.drop_index("ix_shipments_tracking_scan", table_name="shipments")
