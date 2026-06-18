"""phase4: shipments.size_preset + weight

Revision ID: 3f9a2b7c1d8e
Revises: 2c1d4e8f1a6b
Create Date: 2026-06-19 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3f9a2b7c1d8e"
down_revision: str | None = "2c1d4e8f1a6b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("shipments", sa.Column("size_preset", sa.String(length=32), nullable=True))
    op.add_column("shipments", sa.Column("weight", sa.Numeric(8, 3), nullable=True))


def downgrade() -> None:
    op.drop_column("shipments", "weight")
    op.drop_column("shipments", "size_preset")
