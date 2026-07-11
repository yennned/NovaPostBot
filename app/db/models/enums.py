"""Перечисления домена.

Значения — это строки, которые попадают в Postgres-enum, поэтому их **нельзя
менять** после миграции без отдельной миграции типа. Остальные enum (статусы
ТТН, движения склада, типы уведомлений) добавляются вместе с их таблицами.
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


class ClientAccountStatus(StrEnum):
    """Стан бізнес-акаунта клієнта."""

    active = "active"
    blocked = "blocked"
    archived = "archived"


class MembershipRole(StrEnum):
    """Роль користувача всередині клієнтського акаунта."""

    account_owner = "account_owner"
    employee = "employee"


class MembershipStatus(StrEnum):
    """Стан членства користувача в клієнтському акаунті."""

    invited = "invited"
    active = "active"
    blocked = "blocked"


class OrgType(StrEnum):
    """Организационно-правовая форма ФОП-отправителя."""

    fop = "fop"
    tov = "tov"


class ShipmentStatus(StrEnum):
    """Статус отправления/ТТН.

    Фаза 3 использует подмножество жизненного цикла: создано/подтверждено/
    отправлено/возвраты/потери — этого достаточно для кабинета клиента и
    статистики. Следующие фазы продолжат использовать те же значения.
    """

    created = "created"
    confirmed = "confirmed"
    dispatched = "dispatched"
    in_transit = "in_transit"
    arrived = "arrived"
    delivered = "delivered"
    returning = "returning"
    returned = "returned"
    lost = "lost"
    damaged = "damaged"
    cancelled = "cancelled"


class StockMovementType(StrEnum):
    """Тип движения склада (append-only журнал)."""

    ttn_reserve = "ttn_reserve"
    ttn_dispatch = "ttn_dispatch"
    ttn_cancel = "ttn_cancel"
    ttn_return = "ttn_return"
    manual = "manual"


class SupportThreadStatus(StrEnum):
    """Статус обращения клиента в поддержку (Фаза 6)."""

    open = "open"  # назначено дежурному, активный диалог
    waiting = "waiting"  # в очереди (нет дежурного / вне рабочих часов)
    closed = "closed"  # закрыто менеджером
