"""enforce single default sender profile per client

Revision ID: f1a9c3d8e4b2
Revises: c9e2f7a1b3d4
Create Date: 2026-06-24 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1a9c3d8e4b2"
down_revision: str | None = "c9e2f7a1b3d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_sender_profiles_client_default",
        "sender_profiles",
        ["client_id"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )


def downgrade() -> None:
    op.drop_index("uq_sender_profiles_client_default", table_name="sender_profiles")
