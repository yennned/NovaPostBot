"""users.telegram_id nullable (hire manager by phone) + normalize existing phones

Revision ID: a1b2c3d4e5f6
Revises: d4e5f6a7b8c9
Create Date: 2026-07-05 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Найм менеджера по телефону работает и для того, кто ещё не запускал бота:
    # владелец заводит запись с одним телефоном (telegram_id пуст), а при первом
    # входе по контакту `register_contact` подхватывает её по номеру и проставляет
    # telegram_id. Поэтому колонка становится nullable (unique сохраняется —
    # в Postgres несколько NULL не конфликтуют).
    op.alter_column("users", "telegram_id", existing_type=sa.BigInteger(), nullable=True)

    # Нормализуем ранее сохранённые телефоны к формату НП (380XXXXXXXXX): найм по
    # телефону сверяет нормализованный ввод с колонкой точным равенством. Колонка
    # `phone` unique — при коллизии (две сырые записи сворачиваются в один номер)
    # обе оставляем как есть, чтобы миграция не упала на duplicate key.
    op.execute(
        r"""
        with norm as (
            select id,
                   phone as old,
                   case
                       when regexp_replace(phone, '\D', '', 'g') ~ '^0[0-9]{9}$'
                           then '38' || regexp_replace(phone, '\D', '', 'g')
                       when regexp_replace(phone, '\D', '', 'g') ~ '^380[0-9]{9}$'
                           then regexp_replace(phone, '\D', '', 'g')
                       else phone
                   end as p
            from users
            where phone is not null
        )
        update users u
        set phone = n.p
        from norm n
        where u.id = n.id
          and n.p is distinct from n.old
          and not exists (
              select 1 from norm m where m.id <> n.id and m.p = n.p
          )
        """
    )


def downgrade() -> None:
    # Записи с telegram_id IS NULL — это менеджеры, заведённые по телефону и ещё не
    # вошедшие в бота (существуют только благодаря этой фиче). При откате удаляем их,
    # иначе возврат колонки в NOT NULL упал бы на NULL-значениях.
    op.execute("DELETE FROM users WHERE telegram_id IS NULL")
    op.alter_column("users", "telegram_id", existing_type=sa.BigInteger(), nullable=False)
