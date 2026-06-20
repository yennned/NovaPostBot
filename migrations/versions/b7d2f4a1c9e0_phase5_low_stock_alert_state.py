"""phase5: persisted low stock alert state

Revision ID: b7d2f4a1c9e0
Revises: 8b3a9d7e4c1f
Create Date: 2026-06-19 23:58:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7d2f4a1c9e0"
down_revision: str | None = "8b3a9d7e4c1f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "low_stock_alerts",
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("sku", sa.String(length=64), nullable=False),
        sa.Column("is_low", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("last_available", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["users.id"],
            name=op.f("fk_low_stock_alerts_client_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_low_stock_alerts")),
        sa.UniqueConstraint("client_id", "sku", name="uq_low_stock_alerts_client_sku"),
    )
    op.create_index(
        op.f("ix_low_stock_alerts_client_id"), "low_stock_alerts", ["client_id"], unique=False
    )
    op.create_index(op.f("ix_low_stock_alerts_sku"), "low_stock_alerts", ["sku"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_low_stock_alerts_sku"), table_name="low_stock_alerts")
    op.drop_index(op.f("ix_low_stock_alerts_client_id"), table_name="low_stock_alerts")
    op.drop_table("low_stock_alerts")
