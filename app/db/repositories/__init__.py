"""Репозитории — тонкий слой доступа к данным над `AsyncSession`.

Отделён от хендлеров (API-first), чтобы будущий WebApp/API переиспользовал ту же
логику. Управление транзакциями (commit/rollback) — на стороне вызывающего
(middleware/сервис), репозитории делают `flush` для получения сгенерированных
значений.
"""

from app.db.repositories.audit import AuditRepository
from app.db.repositories.client_account import ClientAccountRepository
from app.db.repositories.low_stock_alert import LowStockAlertRepository
from app.db.repositories.notification_setting import NotificationSettingRepository
from app.db.repositories.reports import ReportsRepository
from app.db.repositories.sender_profile import SenderProfileRepository
from app.db.repositories.shipment import ShipmentItemDraft, ShipmentRepository
from app.db.repositories.stock_movement import StockMovementRepository
from app.db.repositories.support import SupportRepository
from app.db.repositories.user import UserRepository

__all__ = [
    "AuditRepository",
    "ClientAccountRepository",
    "LowStockAlertRepository",
    "NotificationSettingRepository",
    "ReportsRepository",
    "SenderProfileRepository",
    "ShipmentItemDraft",
    "ShipmentRepository",
    "StockMovementRepository",
    "SupportRepository",
    "UserRepository",
]
