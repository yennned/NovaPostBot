"""store the reason entered by staff when cancelling a shipment."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f7a8b9c0d1e2"
down_revision: str | None = "e5f6a7b8c1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("shipments", sa.Column("cancellation_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("shipments", "cancellation_reason")
