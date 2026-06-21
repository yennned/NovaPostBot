"""phase6: support threads/messages + users.duty_since

Revision ID: c9e2f7a1b3d4
Revises: b7d2f4a1c9e0
Create Date: 2026-06-21 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9e2f7a1b3d4"
down_revision: str | None = "b7d2f4a1c9e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("duty_since", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "support_threads",
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("assigned_manager_id", sa.Uuid(), nullable=True),
        sa.Column("shipment_id", sa.Uuid(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("open", "waiting", "closed", name="support_thread_status"),
            server_default=sa.text("'open'"),
            nullable=False,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["assigned_manager_id"],
            ["users.id"],
            name=op.f("fk_support_threads_assigned_manager_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["users.id"],
            name=op.f("fk_support_threads_client_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["shipment_id"],
            ["shipments.id"],
            name=op.f("fk_support_threads_shipment_id_shipments"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_support_threads")),
    )
    op.create_index(
        op.f("ix_support_threads_assigned_manager_id"),
        "support_threads",
        ["assigned_manager_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_support_threads_client_id"), "support_threads", ["client_id"], unique=False
    )
    op.create_index(
        op.f("ix_support_threads_shipment_id"), "support_threads", ["shipment_id"], unique=False
    )
    op.create_index(op.f("ix_support_threads_status"), "support_threads", ["status"], unique=False)

    op.create_table(
        "support_messages",
        sa.Column("thread_id", sa.Uuid(), nullable=False),
        sa.Column("sender_role", sa.String(length=16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["thread_id"],
            ["support_threads.id"],
            name=op.f("fk_support_messages_thread_id_support_threads"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_support_messages")),
    )
    op.create_index(
        op.f("ix_support_messages_thread_id"), "support_messages", ["thread_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_support_messages_thread_id"), table_name="support_messages")
    op.drop_table("support_messages")

    op.drop_index(op.f("ix_support_threads_status"), table_name="support_threads")
    op.drop_index(op.f("ix_support_threads_shipment_id"), table_name="support_threads")
    op.drop_index(op.f("ix_support_threads_client_id"), table_name="support_threads")
    op.drop_index(op.f("ix_support_threads_assigned_manager_id"), table_name="support_threads")
    op.drop_table("support_threads")

    op.drop_column("users", "duty_since")

    bind = op.get_bind()
    sa.Enum(name="support_thread_status").drop(bind, checkfirst=True)
