"""phase5: stock_movements, notification_settings, shipment sla fields

Revision ID: 8b3a9d7e4c1f
Revises: 3f9a2b7c1d8e
Create Date: 2026-06-19 23:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8b3a9d7e4c1f"
down_revision: str | None = "3f9a2b7c1d8e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "shipments", sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("shipments", sa.Column("sla_deadline", sa.DateTime(timezone=True), nullable=True))
    op.add_column("shipments", sa.Column("sla_met", sa.Boolean(), nullable=True))
    op.add_column("shipments", sa.Column("fee_amount", sa.Numeric(12, 2), nullable=True))
    op.add_column(
        "shipments",
        sa.Column("fee_free", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )

    op.create_table(
        "notification_settings",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_notification_settings_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_settings")),
        sa.UniqueConstraint("user_id", "key", name="uq_notification_settings_user_key"),
    )
    op.create_index(
        op.f("ix_notification_settings_user_id"),
        "notification_settings",
        ["user_id"],
        unique=False,
    )

    op.create_table(
        "stock_movements",
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("shipment_id", sa.Uuid(), nullable=True),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("sku", sa.String(length=64), nullable=False),
        sa.Column(
            "movement_type",
            sa.Enum(
                "ttn_reserve",
                "ttn_dispatch",
                "ttn_cancel",
                "ttn_return",
                "manual",
                name="stock_movement_type",
            ),
            nullable=False,
        ),
        sa.Column("quantity_delta", sa.Integer(), nullable=False),
        sa.Column("quantity_before", sa.Integer(), nullable=False),
        sa.Column("quantity_after", sa.Integer(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name=op.f("fk_stock_movements_actor_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["users.id"],
            name=op.f("fk_stock_movements_client_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["shipment_id"],
            ["shipments.id"],
            name=op.f("fk_stock_movements_shipment_id_shipments"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_movements")),
    )
    op.create_index(
        op.f("ix_stock_movements_actor_user_id"), "stock_movements", ["actor_user_id"], unique=False
    )
    op.create_index(
        op.f("ix_stock_movements_client_id"), "stock_movements", ["client_id"], unique=False
    )
    op.create_index(
        op.f("ix_stock_movements_movement_type"), "stock_movements", ["movement_type"], unique=False
    )
    op.create_index(
        op.f("ix_stock_movements_shipment_id"), "stock_movements", ["shipment_id"], unique=False
    )
    op.create_index(op.f("ix_stock_movements_sku"), "stock_movements", ["sku"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_stock_movements_sku"), table_name="stock_movements")
    op.drop_index(op.f("ix_stock_movements_shipment_id"), table_name="stock_movements")
    op.drop_index(op.f("ix_stock_movements_movement_type"), table_name="stock_movements")
    op.drop_index(op.f("ix_stock_movements_client_id"), table_name="stock_movements")
    op.drop_index(op.f("ix_stock_movements_actor_user_id"), table_name="stock_movements")
    op.drop_table("stock_movements")

    op.drop_index(op.f("ix_notification_settings_user_id"), table_name="notification_settings")
    op.drop_table("notification_settings")

    op.drop_column("shipments", "fee_free")
    op.drop_column("shipments", "fee_amount")
    op.drop_column("shipments", "sla_met")
    op.drop_column("shipments", "sla_deadline")
    op.drop_column("shipments", "dispatched_at")

    bind = op.get_bind()
    sa.Enum(name="stock_movement_type").drop(bind, checkfirst=True)
