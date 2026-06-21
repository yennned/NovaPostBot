"""ORM-модели. Импорт этого пакета регистрирует все таблицы в `Base.metadata`
(используется Alembic autogenerate и тестовой фикстурой `create_all`).
"""

from app.db.models.audit import AuditLog
from app.db.models.enums import (
    OrgType,
    ShipmentStatus,
    StockMovementType,
    SupportThreadStatus,
    UserRole,
    UserStatus,
)
from app.db.models.low_stock_alert import LowStockAlert
from app.db.models.notification_setting import NotificationSetting
from app.db.models.sender_profile import SenderProfile
from app.db.models.shipment import Shipment, ShipmentItem
from app.db.models.stock_movement import StockMovement
from app.db.models.support import SupportMessage, SupportThread
from app.db.models.user import User

__all__ = [
    "AuditLog",
    "LowStockAlert",
    "NotificationSetting",
    "OrgType",
    "SenderProfile",
    "Shipment",
    "ShipmentItem",
    "ShipmentStatus",
    "StockMovement",
    "StockMovementType",
    "SupportMessage",
    "SupportThread",
    "SupportThreadStatus",
    "User",
    "UserRole",
    "UserStatus",
]
