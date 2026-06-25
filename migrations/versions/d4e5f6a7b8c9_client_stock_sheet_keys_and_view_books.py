"""add client stock sheet key and read-only view book id

Revision ID: d4e5f6a7b8c9
Revises: f1a9c3d8e4b2
Create Date: 2026-06-25 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "f1a9c3d8e4b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("stock_sheet_key", sa.String(length=255), nullable=True))
    op.add_column("users", sa.Column("stock_view_book_id", sa.String(length=128), nullable=True))
    op.execute(
        "update users set stock_sheet_key = coalesce(nullif(full_name, ''), telegram_id::text)"
    )


def downgrade() -> None:
    op.drop_column("users", "stock_view_book_id")
    op.drop_column("users", "stock_sheet_key")
