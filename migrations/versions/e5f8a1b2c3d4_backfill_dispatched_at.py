"""Бэкфилл `dispatched_at` и индекс под отчёты.

Revision ID: e5f8a1b2c3d4
Revises: d4e5f8a1b2c3
Create Date: 2026-08-02

Отчёты выбирали отправленные за период так:

    dispatched_at BETWEEN :s AND :e
    OR (dispatched_at IS NULL AND status IN (…) AND status_changed_at BETWEEN :s AND :e)

Вторая ветка — legacy-фолбэк для строк, заведённых до появления поля. Пока она в
запросе, индекс на `dispatched_at` неприменим: планировщик обязан проверить
второе условие на каждой строке, и никакой индекс этого не отменяет. Поэтому
правильно не индексировать вокруг фолбэка, а разово закрыть его причину.

Здесь: заполняем поле там, где оно пустое, а статус уже за отправкой, и ставим
индекс на диапазон. После этого фолбэк снят в коде (`reports.py`).

Бэкфилл идемпотентен (`WHERE dispatched_at IS NULL`) и не трогает строки, где
поле уже заполнено настоящим временем сканирования от НП.
"""

from __future__ import annotations

from alembic import op

revision = "e5f8a1b2c3d4"
down_revision = "d4e5f8a1b2c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Набор статусов совпадает с прежней legacy-веткой запроса — иначе бэкфилл
    # закрыл бы не тот набор строк, который фолбэк обслуживал.
    op.execute(
        """
        UPDATE shipments
           SET dispatched_at = status_changed_at
         WHERE dispatched_at IS NULL
           AND status IN ('dispatched', 'in_transit', 'arrived', 'delivered')
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_shipments_dispatched_at ON shipments (dispatched_at)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_shipments_dispatched_at")
    # Бэкфилл не откатываем: отличить проставленное здесь от пришедшего от НП
    # уже нельзя, а обнулять и то и другое — терять данные ради симметрии.
