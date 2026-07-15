"""drop legacy users.stock_sheet_key / users.stock_view_book_id.

Тот самый cleanup, который `e7f8a9b0c1d2` отложил «на один rollout window»
(см. её докстринг). Window прошёл: склад — свойство АККАУНТА, а не человека, и
источник правды теперь один — `client_accounts.stock_sheet_key`.

Копия на `users` была не просто дублем, а ловушкой: у работника аккаунта
`users.stock_sheet_key` = его телефон, листа с таким именем нет. Экран менеджера
читал именно эту копию и рисовал фантомную строку (починено в #107), а работник
заблокированного аккаунта проскакивал гейт и видел лживое «склад порожній»
(починено в этом же PR — `shipments.require_client_account`).

Безопасность сноса проверена на проде: у всех владельцев аккаунтов
`users.stock_sheet_key == client_accounts.stock_sheet_key`, дрейфа нет. Единственное
расхождение было у работника — то есть у того, чью копию читать и не следовало.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c0"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("users", "stock_sheet_key")
    op.drop_column("users", "stock_view_book_id")


def downgrade() -> None:
    op.add_column("users", sa.Column("stock_sheet_key", sa.String(length=255), nullable=True))
    op.add_column("users", sa.Column("stock_view_book_id", sa.String(length=128), nullable=True))
    # Возвращаем копию из аккаунта через членство — ровно тот источник, из которого
    # `e7f8a9b0c1d2` её когда-то и забэкфиллила. Работник получит ключ своего
    # аккаунта (а не телефон, как было): точную «сломанную» копию не воспроизводим —
    # восстанавливаем осмысленное значение.
    op.execute(
        """
        update users u
        set stock_sheet_key = a.stock_sheet_key,
            stock_view_book_id = a.stock_view_book_id
        from client_account_memberships m
        join client_accounts a on a.id = m.account_id
        where m.user_id = u.id
        """
    )
    # Клиенты без членства (сломанное состояние) — фолбэк на прежнюю цепочку.
    op.execute(
        """
        update users
        set stock_sheet_key = coalesce(nullif(full_name, ''), phone, telegram_id::text)
        where stock_sheet_key is null
        """
    )
