"""Доменные исключения сервисного слоя.

Бот-слой (`app/bot/*`) ловит эти типы и рендерит uk-сообщения пользователю —
сервисы не знают про aiogram и не формируют UI-тексты ошибок.
"""

from __future__ import annotations

from datetime import datetime

from app.db.models.enums import ShipmentStatus, UserStatus


class ClientServiceError(Exception):
    """База для ошибок управления клиентами."""


class OfficeClosed(ClientServiceError):
    """Відділення зачинене — поза робочими годинами зміну відкрити не можна.

    `next_open` — ближайшее открытие по расписанию (для подсказки пользователю).
    """

    def __init__(self, *, next_open: datetime | None = None) -> None:
        self.next_open = next_open
        super().__init__("відділення зараз зачинене")


class ClientNotFound(ClientServiceError):
    """Клиент с таким id не найден."""


class SenderProfileNotFound(ClientServiceError):
    """ФОП-профиль с таким id не найден."""


class SenderProfileKeyInvalid(ClientServiceError):
    """Ключ ФОП отклонён Новой Поштой (невалидный/нет контрагента-отправителя).

    Транзитный сбой НП (`NovaPoshtaUnavailable`) сюда НЕ относится — он
    пробрасывается, чтобы не клеймить рабочий ключ невалидным.
    """


class ShipmentNotFound(ClientServiceError):
    """Отправление с таким id/ТТН не найдено."""


class SenderProfileNotConfigured(ClientServiceError):
    """У клиента нет ФОП (или дефолтного) для создания ТТН."""


class SenderProfileNotValidated(ClientServiceError):
    """ФОП есть, но ключ не валидирован в НП (нет `np_sender_ref`) — ТТН нельзя."""


class SenderProfileIncomplete(ClientServiceError):
    """ФОП провалидирован, но не хватает данных отправителя для `save_ttn`.

    Нет `np_contact_ref` (контакт-отправитель в НП) или `sender_phone`. Без них
    реальный НП-запрос ушёл бы с пустыми `ContactSender`/`SendersPhone` и упал на
    стороне НП. Правится менеджером/клиентом (дозаполнить профиль)."""


class SenderDispatchNotConfigured(ClientServiceError):
    """Не настроен склад-отправитель системы (Ref города/відділення НП).

    Пусто `settings.np_sender_city_ref` и/или эффективный warehouse-ref
    (`profile.np_sender_warehouse or settings.np_sender_warehouse_ref`). Это
    конфиг-проблема (dev/owner ставит `NP_SENDER_*` в `.env`), не вина клиента."""


class InsufficientStock(ClientServiceError):
    """Запрошено больше, чем доступно (`available`) по позиции."""

    def __init__(self, sku: str, requested: int, available: int) -> None:
        self.sku = sku
        self.requested = requested
        self.available = available
        super().__init__(f"{sku}: запрошено {requested}, доступно {available}")


class TtnCreationFailed(ClientServiceError):
    """НП отклонила создание ТТН (или временно недоступна) — ничего не зарезервировано."""


class TtnCancelFailed(ClientServiceError):
    """НП отклонила удаление ТТН — статус не меняем, резерв не трогаем."""


class ShipmentActionForbidden(ClientServiceError):
    """Действие с отправлением недопустимо в текущем статусе."""

    def __init__(self, action: str, status: ShipmentStatus) -> None:
        self.action = action
        self.status = status
        super().__init__(f"дія {action} недоступна для статусу {status}")


class InvalidNotificationSetting(ClientServiceError):
    """Запрошен неизвестный ключ настройки уведомлений."""


class PermissionDenied(ClientServiceError):
    """У актёра нет прав на действие (иерархия `can_manage` или per-flag)."""


class PhoneAlreadyTaken(ClientServiceError):
    """Телефон уже занят другим пользователем (нарушит UNIQUE)."""


class TransitionForbidden(ClientServiceError):
    """Переход статуса недопустим (напр. блокировать архивного)."""

    def __init__(self, from_status: UserStatus, to_status: UserStatus) -> None:
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(f"переход {from_status} → {to_status} недопустим")


class AlreadyInStatus(TransitionForbidden):
    """Клиент уже в целевом статусе (напр. подтверждение уже активного)."""

    def __init__(self, status: UserStatus) -> None:
        super().__init__(status, status)
        self.args = (f"клиент уже в статусе {status}",)
