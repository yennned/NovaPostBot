"""История переживает удаление человека: shipments/stock_movements/support_threads.client_id → nullable SET NULL.

Предусловие физического удаления пользователей. Сейчас `client_id` в этих трёх
таблицах — `NOT NULL` + `ON DELETE CASCADE`, то есть `DELETE FROM users` снёс бы
каскадом ТТН, позиции (через `shipment_items`), движения склада и историю
поддержки. Требование обратное: история остаётся, ссылка на удалённого человека
обнуляется — ровно так, как уже сделано для `shipments.created_by_user_id`,
`stock_movements.actor_user_id`, `support_threads.assigned_manager_id`,
`audit_logs.user_id`.

Почему `client_id` теряет NOT NULL, а не `account_id`. `client_id` фактически
значит «кто завёл строку», а не «чья это компания»: ТТН, созданная работником,
держит в `client_id` id **работника** — `create_shipment(client=...)` получает
`_effective_client`, то есть залогиненного. Компанию держит `account_id`, он и
остаётся `NOT NULL`. `client_id` доживает как legacy и дропнется отдельной
cleanup-миграцией после стабильного релиза.

**Чего здесь намеренно нет.**

`sender_profiles.client_id` остаётся `CASCADE NOT NULL`. Перевод его в SET NULL
выглядел бы единообразно, но означал бы: владельца удалили, а профиль с
`np_api_key` (Fernet, расшифровывается на чтении) **остался жить**, потому что
`account_id` — NOT NULL, а аккаунт при удалении клиента сохраняется
анонимизированным. Это противоречит требованию «видалити ФОП-профілі та
зашифровані NP-ключі» и оставляет живой секрет. CASCADE уносит ключ гарантированно,
без надежды на то, что прикладной код не забудет удалить профиль явно. Профили
заводит только владелец акаунта (`owner_only` на create/update/set_default), так
что осиротить их удалением работника невозможно.

Раз `sender_profiles.client_id` остаётся NOT NULL, `uq_sender_profiles_client_default`
продолжает держать инвариант «один дефолтный ФОП» и никуда не переезжает.

`low_stock_alerts.client_id` тоже остаётся CASCADE: это состояние антиспама
воркера, а не история — переживать владельца ему незачем. Плюс на нём висит
`UniqueConstraint(client_id, sku)`, который от nullable молча деградировал бы
(NULL'ы в unique-индексе Postgres не конфликтуют).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5f6a7b8c1d3"
down_revision: str | None = "d4e5f6a7b8c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Только таблицы-история. Имена констрейнтов — из naming convention (`op.f`).
_TABLES = ("shipments", "stock_movements", "support_threads")


def _fk(table: str) -> str:
    return f"fk_{table}_client_id_users"


def upgrade() -> None:
    for table in _TABLES:
        op.alter_column(table, "client_id", existing_type=sa.Uuid(), nullable=True)
        op.drop_constraint(_fk(table), table, type_="foreignkey")
        op.create_foreign_key(
            _fk(table), table, "users", ["client_id"], ["id"], ondelete="SET NULL"
        )


def downgrade() -> None:
    for table in _TABLES:
        # Осиротевшие строки (человек физически удалён) возвращаем владельцу
        # аккаунта — единственное осмысленное значение, которое ещё восстановимо.
        # S608: `table` — не ввод, а элемент `_TABLES` из этого же файла;
        # идентификатор таблицы в SQL параметром не биндится в принципе.
        op.execute(
            sa.text(
                f"""
                UPDATE {table} t
                   SET client_id = m.user_id
                  FROM client_account_memberships m
                 WHERE t.client_id IS NULL
                   AND m.account_id = t.account_id
                   AND m.role = 'account_owner'
                """  # noqa: S608
            )
        )
        orphans = (
            op.get_bind()
            .execute(sa.text(f"SELECT count(*) FROM {table} WHERE client_id IS NULL"))  # noqa: S608
            .scalar()
        )
        if orphans:
            raise RuntimeError(
                f"{table}: {orphans} строк без client_id и без владельца аккаунта — "
                "откат невозможен без потери данных, восстановите владельцев вручную"
            )
        op.drop_constraint(_fk(table), table, type_="foreignkey")
        op.create_foreign_key(_fk(table), table, "users", ["client_id"], ["id"], ondelete="CASCADE")
        op.alter_column(table, "client_id", existing_type=sa.Uuid(), nullable=False)
