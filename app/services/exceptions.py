"""Доменные исключения сервисного слоя.

Бот-слой (`app/bot/*`) ловит эти типы и рендерит uk-сообщения пользователю —
сервисы не знают про aiogram и не формируют UI-тексты ошибок.
"""

from __future__ import annotations

from app.db.models.enums import UserStatus


class ClientServiceError(Exception):
    """База для ошибок управления клиентами."""


class ClientNotFound(ClientServiceError):
    """Клиент с таким id не найден."""


class SenderProfileNotFound(ClientServiceError):
    """ФОП-профиль с таким id не найден."""


class PermissionDenied(ClientServiceError):
    """У актёра нет прав на действие (иерархия `can_manage` или per-flag)."""


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
