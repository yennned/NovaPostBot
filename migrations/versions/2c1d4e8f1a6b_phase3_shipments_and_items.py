"""phase3: shipments and shipment_items

Revision ID: 2c1d4e8f1a6b
Revises: 5166e81501e8
Create Date: 2026-06-18 11:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2c1d4e8f1a6b"
down_revision: str | None = "5166e81501e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "shipments",
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("sender_profile_id", sa.Uuid(), nullable=True),
        sa.Column("ttn_number", sa.String(length=32), nullable=True),
        sa.Column("np_ref", sa.String(length=64), nullable=True),
        sa.Column("recipient_name", sa.String(length=255), nullable=False),
        sa.Column("recipient_phone", sa.String(length=32), nullable=True),
        sa.Column("recipient_city", sa.String(length=255), nullable=True),
        sa.Column("recipient_warehouse", sa.String(length=255), nullable=True),
        sa.Column("recipient_kind", sa.String(length=32), server_default="person", nullable=False),
        sa.Column("payer_type", sa.String(length=32), nullable=True),
        sa.Column("payment_method", sa.String(length=32), nullable=True),
        sa.Column("cod_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("insured_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "created",
                "confirmed",
                "dispatched",
                "in_transit",
                "arrived",
                "delivered",
                "returning",
                "returned",
                "lost",
                "damaged",
                "cancelled",
                name="shipment_status",
            ),
            server_default="created",
            nullable=False,
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "status_changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("tracking_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["users.id"],
            name=op.f("fk_shipments_client_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["sender_profile_id"],
            ["sender_profiles.id"],
            name=op.f("fk_shipments_sender_profile_id_sender_profiles"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shipments")),
        sa.UniqueConstraint("ttn_number", name=op.f("uq_shipments_ttn_number")),
    )
    op.create_index(op.f("ix_shipments_client_id"), "shipments", ["client_id"], unique=False)
    op.create_index(
        op.f("ix_shipments_sender_profile_id"), "shipments", ["sender_profile_id"], unique=False
    )
    op.create_index(op.f("ix_shipments_status"), "shipments", ["status"], unique=False)

    op.create_table(
        "shipment_items",
        sa.Column("shipment_id", sa.Uuid(), nullable=False),
        sa.Column("sku", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("category", sa.String(length=255), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["shipment_id"],
            ["shipments.id"],
            name=op.f("fk_shipment_items_shipment_id_shipments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shipment_items")),
    )
    op.create_index(
        op.f("ix_shipment_items_shipment_id"), "shipment_items", ["shipment_id"], unique=False
    )
    op.create_index(op.f("ix_shipment_items_sku"), "shipment_items", ["sku"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_shipment_items_sku"), table_name="shipment_items")
    op.drop_index(op.f("ix_shipment_items_shipment_id"), table_name="shipment_items")
    op.drop_table("shipment_items")
    op.drop_index(op.f("ix_shipments_status"), table_name="shipments")
    op.drop_index(op.f("ix_shipments_sender_profile_id"), table_name="shipments")
    op.drop_index(op.f("ix_shipments_client_id"), table_name="shipments")
    op.drop_table("shipments")
    sa.Enum(name="shipment_status").drop(op.get_bind(), checkfirst=True)
