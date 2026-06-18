"""Репозитории — тонкий слой доступа к данным над `AsyncSession`.

Отделён от хендлеров (API-first), чтобы будущий WebApp/API переиспользовал ту же
логику. Управление транзакциями (commit/rollback) — на стороне вызывающего
(middleware/сервис), репозитории делают `flush` для получения сгенерированных
значений.
"""

from app.db.repositories.audit import AuditRepository
from app.db.repositories.sender_profile import SenderProfileRepository
from app.db.repositories.shipment import ShipmentItemDraft, ShipmentRepository
from app.db.repositories.user import UserRepository

__all__ = [
    "AuditRepository",
    "SenderProfileRepository",
    "ShipmentItemDraft",
    "ShipmentRepository",
    "UserRepository",
]
