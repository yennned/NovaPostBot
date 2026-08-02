"""Индексы под списки, очереди и поиск.

Revision ID: d4e5f8a1b2c3
Revises: c3d4e5f8a1b2
Create Date: 2026-08-02

Ни одного составного индекса в схеме не было. Все списки кабинета и очереди
менеджера фильтруют по скоупу и сортируют по свежести, а одиночного индекса на
`account_id` для этого мало: Postgres берёт по нему строки и сортирует их целиком.
На 15 000 ТТН/мес это десятки тысяч строк на каждый тап пагинации.

Поиск идёт `ILIKE '%…%'` — обычный B-tree к нему неприменим в принципе, поэтому
`pg_trgm` + GIN.

Замерено `EXPLAIN (ANALYZE, BUFFERS)` на 200 000 засеянных ТТН (20 аккаунтов),
до и после, на одних и тех же данных:

| Запрос | Было | Стало |
|---|---|---|
| список кабинета (`account_id` + `created_at DESC`) | Bitmap Heap Scan, 2202 буфера, 2,9 мс | Index Scan, **6 буферов, 0,014 мс** |
| поиск по ТТН `ILIKE '%…%'` | Parallel Seq Scan, 8316 буферов, 85,7 мс | Bitmap+GIN, **104 буфера, 0,75 мс** |
| поиск по получателю `ILIKE '%…%'` | Parallel Seq Scan, 8316 буферов, 30,7 мс | Bitmap+GIN, **496 буферов, 4,1 мс** |
| поиск по дате (диапазоном) | Bitmap Heap Scan, 2202 буфера, 2,8 мс | Index Scan, **3 буфера, 0,010 мс** |

Отдельно про дату: старая форма `cast(created_at, Date) = :d` не sargable и на
тех же данных читала 3931 буфер против 3 у диапазона — индекс к ней неприменим,
сколько его ни добавляй. Поэтому вместе с индексом переписан и сам предикат
(`_shipment_search_filters`).
"""

from __future__ import annotations

from alembic import op

revision = "d4e5f8a1b2c3"
down_revision = "c3d4e5f8a1b2"
branch_labels = None
depends_on = None

#: `(имя, таблица, выражение)` — создаём сырым SQL, потому что часть из них
#: сортирует по убыванию, а часть использует операторный класс trgm.
_INDEXES = [
    ("ix_shipments_account_created", "shipments", "(account_id, created_at DESC)"),
    ("ix_shipments_client_created", "shipments", "(client_id, created_at DESC)"),
    ("ix_shipments_account_status", "shipments", "(account_id, status)"),
    ("ix_stock_movements_account_sku", "stock_movements", "(account_id, sku)"),
    ("ix_stock_movements_shipment_type", "stock_movements", "(shipment_id, movement_type)"),
    ("ix_users_role_status", "users", "(role, status)"),
]

_TRGM_INDEXES = [
    ("ix_shipments_ttn_trgm", "shipments", "ttn_number"),
    ("ix_shipments_recipient_trgm", "shipments", "recipient_name"),
    ("ix_users_full_name_trgm", "users", "full_name"),
    ("ix_users_phone_trgm", "users", "phone"),
]


def upgrade() -> None:
    for name, table, expression in _INDEXES:
        op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} {expression}")

    # Расширение ставится один раз на базу. На Neon доступно; если прав нет —
    # миграция упадёт здесь явно, а не оставит поиск молча несиндексированным.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for name, table, column in _TRGM_INDEXES:
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {name} ON {table} USING gin ({column} gin_trgm_ops)"
        )


def downgrade() -> None:
    for name, _, _ in _TRGM_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
    # Расширение НЕ удаляем: им может пользоваться что-то ещё в базе, а
    # `DROP EXTENSION` уронил бы это молча.
    for name, _, _ in _INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
