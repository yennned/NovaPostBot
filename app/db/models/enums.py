"""Перечисления домена (фундамент Фазы 1).

Значения — это строки, которые попадают в Postgres-enum, поэтому их **нельзя
менять** после миграции без отдельной миграции типа. Остальные enum (статусы
ТТН, движения склада, типы уведомлений) добавим вместе с их таблицами в
Фазах 3–5.
"""

from __future__ import annotations

from enum import StrEnum


class UserRole(StrEnum):
    """Роль пользователя. Порядок объявления = иерархия (client < manager < owner)."""

    client = "client"
    manager = "manager"
    owner = "owner"


class UserStatus(StrEnum):
    """Статус учётной записи (гейтинг доступа)."""

    pending = "pending"  # ожидает подтверждения
    active = "active"  # активен, полный доступ
    blocked = "blocked"  # заблокирован
    archived = "archived"  # мягко удалён


class OrgType(StrEnum):
    """Организационно-правовая форма ФОП-отправителя."""

    fop = "fop"
    tov = "tov"
