"""Остаток склада в Postgres: балансы, брони, водораздел ингеста.

Revision ID: c3d4e5f8a1b2
Revises: b2c3d4e5f8a1
Create Date: 2026-08-02

Аддитивная миграция: новые таблицы, читателей у них пока нет. Переключение
источника остатка идёт отдельно, через `INVENTORY_SOURCE`, чтобы откат был
сменой переменной окружения, а не миграцией вниз.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c3d4e5f8a1b2"
down_revision = "b2c3d4e5f8a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stock_balances",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("sku", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("category", sa.String(length=128), nullable=True),
        sa.Column("price", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("quantity", sa.Integer(), server_default="0", nullable=False),
        sa.Column("mirrored_quantity", sa.Integer(), nullable=True),
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
            ["account_id"],
            ["client_accounts.id"],
            name="fk_stock_balances_account_id_client_accounts",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_stock_balances"),
        sa.UniqueConstraint("account_id", "sku", name="uq_stock_balances_account_sku"),
        sa.CheckConstraint("quantity >= 0", name="ck_stock_balances_quantity_non_negative"),
    )
    op.create_index("ix_stock_balances_account_id", "stock_balances", ["account_id"])
    op.create_index(
        "ix_stock_balances_account_sort", "stock_balances", ["account_id", "category", "name"]
    )

    op.create_table(
        "stock_holds",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("shipment_id", sa.Uuid(), nullable=True),
        sa.Column("sku", sa.String(length=64), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("submit_key", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
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
            ["account_id"],
            ["client_accounts.id"],
            name="fk_stock_holds_account_id_client_accounts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["client_id"], ["users.id"], name="fk_stock_holds_client_id_users", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["shipment_id"],
            ["shipments.id"],
            name="fk_stock_holds_shipment_id_shipments",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_stock_holds"),
        sa.CheckConstraint("quantity > 0", name="ck_stock_holds_quantity_positive"),
    )
    op.create_index("ix_stock_holds_account_id", "stock_holds", ["account_id"])
    op.create_index("ix_stock_holds_submit_key", "stock_holds", ["submit_key"])
    # Частичные: снятые брони живут только ради аудита и в горячий путь не входят.
    op.create_index(
        "ix_stock_holds_active",
        "stock_holds",
        ["account_id", "sku"],
        postgresql_where=sa.text("released_at IS NULL"),
    )
    op.create_index(
        "ix_stock_holds_expiry",
        "stock_holds",
        ["expires_at"],
        postgresql_where=sa.text("released_at IS NULL"),
    )

    op.create_table(
        "stock_intake_cursor",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("book_id", sa.String(length=128), nullable=False),
        sa.Column("tab", sa.String(length=255), nullable=False),
        sa.Column("last_row", sa.Integer(), server_default="1", nullable=False),
        sa.Column("last_row_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("last_ingested_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_stock_intake_cursor"),
        sa.UniqueConstraint("book_id", "tab", name="uq_stock_intake_cursor_book_tab"),
    )


def downgrade() -> None:
    op.drop_table("stock_intake_cursor")
    op.drop_index("ix_stock_holds_expiry", table_name="stock_holds")
    op.drop_index("ix_stock_holds_active", table_name="stock_holds")
    op.drop_index("ix_stock_holds_submit_key", table_name="stock_holds")
    op.drop_index("ix_stock_holds_account_id", table_name="stock_holds")
    op.drop_table("stock_holds")
    op.drop_index("ix_stock_balances_account_sort", table_name="stock_balances")
    op.drop_index("ix_stock_balances_account_id", table_name="stock_balances")
    op.drop_table("stock_balances")
