"""ORM-модели. Импорт этого пакета регистрирует все таблицы в `Base.metadata`
(используется Alembic autogenerate и тестовой фикстурой `create_all`).
"""

from app.db.models.audit import AuditLog
from app.db.models.enums import OrgType, ShipmentStatus, UserRole, UserStatus
from app.db.models.sender_profile import SenderProfile
from app.db.models.shipment import Shipment, ShipmentItem
from app.db.models.user import User

__all__ = [
    "AuditLog",
    "OrgType",
    "SenderProfile",
    "Shipment",
    "ShipmentItem",
    "ShipmentStatus",
    "User",
    "UserRole",
    "UserStatus",
]
