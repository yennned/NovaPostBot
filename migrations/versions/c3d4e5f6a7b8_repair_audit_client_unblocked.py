"""repair audit_logs.account_id for client_unblocked (пропуск в b2c3d4e5f6a7).

`b2c3d4e5f6a7` перечислял клиентские переходы вручную и потерял `client_unblocked`
(`clients.py:247`) — единственное действие из пятёрки `_transition`, не попавшее в
список. Код уже проставляет аккаунт для всех переходов; чинятся только строки,
накопленные до этого.

Догоняющая миграция, потому что `b2c3d4e5f6a7` уже применена на проде.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Тот же приём, что и в 2a предыдущей миграции: субъект — из `affected_entity`.
    # Субъект без членства остаётся NULL: аккаунт не выдумываем.
    op.execute(
        r"""
        update audit_logs a
        set account_id = m.account_id
        from client_account_memberships m
        where a.account_id is null
          and a.action = 'client_unblocked'
          and a.affected_entity ~ '^user:[0-9a-fA-F-]{36}$'
          and m.user_id = substring(a.affected_entity from 6)::uuid
        """
    )


def downgrade() -> None:
    # Как и в b2c3d4e5f6a7: ремонт данных не откатывается.
    pass
