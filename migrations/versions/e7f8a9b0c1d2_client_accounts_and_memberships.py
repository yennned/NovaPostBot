"""client accounts, memberships and account-scoped ownership backfill.

The old ``client_id`` columns are intentionally kept for one rollout window as
compatibility columns.  New code reads/writes ``account_id`` and actor columns;
the next cleanup migration can remove the legacy columns after all workers have
been deployed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e7f8a9b0c1d2"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    account_status = sa.Enum("active", "blocked", "archived", name="client_account_status")
    membership_role = sa.Enum("account_owner", "employee", name="membership_role")
    membership_status = sa.Enum("invited", "active", "blocked", name="membership_status")
    op.create_table(
        "client_accounts",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            account_status,
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column("stock_sheet_key", sa.String(length=255), nullable=True),
        sa.Column("stock_view_book_id", sa.String(length=128), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_client_accounts")),
    )
    op.create_index(op.f("ix_client_accounts_status"), "client_accounts", ["status"], unique=False)

    # One membership per user is the database-level invariant preventing a
    # person from being attached to two client accounts.
    op.create_table(
        "client_account_memberships",
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", membership_role, nullable=False),
        sa.Column("status", membership_status, server_default=sa.text("'invited'"), nullable=False),
        sa.Column("invited_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("blocked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["client_accounts.id"],
            name=op.f("fk_client_account_memberships_account_id_client_accounts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_client_account_memberships_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["invited_by_user_id"],
            ["users.id"],
            name=op.f("fk_client_account_memberships_invited_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_client_account_memberships")),
        sa.UniqueConstraint("user_id", name="uq_client_account_memberships_user"),
        sa.UniqueConstraint(
            "account_id", "user_id", name="uq_client_account_memberships_account_user"
        ),
    )
    op.create_index(
        op.f("ix_client_account_memberships_account_id"),
        "client_account_memberships",
        ["account_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_client_account_memberships_user_id"),
        "client_account_memberships",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_client_account_memberships_status"),
        "client_account_memberships",
        ["status"],
        unique=False,
    )

    # Add the new ownership/author columns.  They are nullable only while the
    # backfill runs; all client-owned rows must be complete before the migration
    # finishes.
    for table in (
        "sender_profiles",
        "shipments",
        "stock_movements",
        "low_stock_alerts",
        "support_threads",
    ):
        op.add_column(table, sa.Column("account_id", sa.Uuid(), nullable=True))
        op.create_index(op.f(f"ix_{table}_account_id"), table, ["account_id"], unique=False)
        op.create_foreign_key(
            op.f(f"fk_{table}_account_id_client_accounts"),
            table,
            "client_accounts",
            ["account_id"],
            ["id"],
            ondelete="CASCADE",
        )

    op.add_column("shipments", sa.Column("created_by_user_id", sa.Uuid(), nullable=True))
    op.create_index(
        op.f("ix_shipments_created_by_user_id"), "shipments", ["created_by_user_id"], unique=False
    )
    op.create_foreign_key(
        op.f("fk_shipments_created_by_user_id_users"),
        "shipments",
        "users",
        ["created_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column("support_messages", sa.Column("sender_user_id", sa.Uuid(), nullable=True))
    op.create_index(
        op.f("ix_support_messages_sender_user_id"),
        "support_messages",
        ["sender_user_id"],
        unique=False,
    )
    op.create_foreign_key(
        op.f("fk_support_messages_sender_user_id_users"),
        "support_messages",
        "users",
        ["sender_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column("audit_logs", sa.Column("account_id", sa.Uuid(), nullable=True))
    op.create_index(op.f("ix_audit_logs_account_id"), "audit_logs", ["account_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_audit_logs_account_id_client_accounts"),
        "audit_logs",
        "client_accounts",
        ["account_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # ``client_id`` was historically the owner of the resource, but old data
    # can contain rows created before a user was promoted to an internal role.
    # Build the compatibility account from both current clients and every user
    # referenced by a legacy client_id. Restricting this to role=client leaves
    # those rows orphaned and makes the NOT NULL conversion below abort the
    # whole deployment transaction.
    op.execute(
        """
        with legacy_owners as (
            select id from users where role = 'client'::user_role
            union
            select client_id from sender_profiles
            union
            select client_id from shipments
            union
            select client_id from stock_movements
            union
            select client_id from low_stock_alerts
            union
            select client_id from support_threads
        )
        insert into client_accounts (id, name, status, stock_sheet_key, stock_view_book_id)
        select u.id, coalesce(nullif(u.full_name, ''), u.phone, 'Клієнт ' || u.id::text),
               case when u.status = 'blocked'::user_status then 'blocked'::client_account_status
                    when u.status = 'archived'::user_status then 'archived'::client_account_status
                    else 'active'::client_account_status end,
               u.stock_sheet_key, u.stock_view_book_id
        from users u
        join legacy_owners o on o.id = u.id
        on conflict (id) do nothing
        """
    )
    op.execute(
        """
        with legacy_owners as (
            select id from users where role = 'client'::user_role
            union
            select client_id from sender_profiles
            union
            select client_id from shipments
            union
            select client_id from stock_movements
            union
            select client_id from low_stock_alerts
            union
            select client_id from support_threads
        )
        insert into client_account_memberships
            (id, account_id, user_id, role, status, joined_at)
        select u.id, u.id, u.id, 'account_owner'::membership_role,
               case when u.status = 'blocked'::user_status then 'blocked'::membership_status
                    else 'active'::membership_status end,
               u.created_at
        from users u
        join legacy_owners o on o.id = u.id
        on conflict (user_id) do nothing
        """
    )
    for table in (
        "sender_profiles",
        "shipments",
        "stock_movements",
        "low_stock_alerts",
        "support_threads",
    ):
        op.execute(
            sa.text(
                f"update {table} t set account_id = u.id from users u "  # noqa: S608
                "where t.client_id = u.id and u.role = 'client'::user_role"
            )
        )

    op.execute(
        "update shipments set created_by_user_id = client_id where created_by_user_id is null"
    )
    op.execute(
        """
        update support_messages m
        set sender_user_id = case
            when m.sender_role = 'client' then t.client_id
            when m.sender_role in ('manager', 'dev') then t.assigned_manager_id
            else null end
        from support_threads t
        where m.thread_id = t.id
        """
    )
    op.execute(
        """
        update audit_logs a
        set account_id = m.account_id
        from client_account_memberships m
        where a.user_id = m.user_id and m.status in ('active'::membership_status, 'blocked'::membership_status)
        """
    )
    for table in (
        "sender_profiles",
        "shipments",
        "stock_movements",
        "low_stock_alerts",
        "support_threads",
    ):
        op.alter_column(table, "account_id", nullable=False, existing_type=sa.Uuid())

    op.execute(
        """
        do $$
        declare orphan_count bigint;
        begin
            if (select count(*) from client_accounts) <
               (select count(*) from users where role = 'client'::user_role) then
                raise exception 'client account backfill lost users';
            end if;
            if (select count(*) from client_account_memberships) <
               (select count(*) from users where role = 'client'::user_role) then
                raise exception 'client membership backfill lost users';
            end if;
            if exists (
                select 1
                from (values
                    ((select count(*) from sender_profiles where account_id is null)),
                    ((select count(*) from shipments where account_id is null)),
                    ((select count(*) from stock_movements where account_id is null)),
                    ((select count(*) from low_stock_alerts where account_id is null)),
                    ((select count(*) from support_threads where account_id is null))
                ) as orphan_counts(orphan_count)
                where orphan_count > 0
            ) then
                raise exception 'account scoped rows remain orphaned';
            end if;
            if exists (
                select 1 from shipments where created_by_user_id is null
            ) then
                raise exception 'historical shipment authors were not backfilled';
            end if;
        end $$;
        """
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_audit_logs_account_id_client_accounts"), "audit_logs", type_="foreignkey"
    )
    op.drop_index(op.f("ix_audit_logs_account_id"), table_name="audit_logs")
    op.drop_column("audit_logs", "account_id")

    op.drop_constraint(
        op.f("fk_support_messages_sender_user_id_users"), "support_messages", type_="foreignkey"
    )
    op.drop_index(op.f("ix_support_messages_sender_user_id"), table_name="support_messages")
    op.drop_column("support_messages", "sender_user_id")
    op.drop_constraint(
        op.f("fk_shipments_created_by_user_id_users"), "shipments", type_="foreignkey"
    )
    op.drop_index(op.f("ix_shipments_created_by_user_id"), table_name="shipments")
    op.drop_column("shipments", "created_by_user_id")
    for table in (
        "support_threads",
        "low_stock_alerts",
        "stock_movements",
        "shipments",
        "sender_profiles",
    ):
        op.drop_constraint(
            op.f(f"fk_{table}_account_id_client_accounts"), table, type_="foreignkey"
        )
        op.drop_index(op.f(f"ix_{table}_account_id"), table_name=table)
        op.drop_column(table, "account_id")

    op.drop_table("client_account_memberships")
    op.drop_index(op.f("ix_client_accounts_status"), table_name="client_accounts")
    op.drop_table("client_accounts")
    bind = op.get_bind()
    for enum_name in ("membership_status", "membership_role", "client_account_status"):
        sa.Enum(name=enum_name).drop(bind, checkfirst=True)
