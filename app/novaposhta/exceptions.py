"""Исключения транспортного слоя Нової Пошти.

Это ошибки уровня API НП (не сервисные). Сервисный слой (`app/services/*`)
ловит их и транслирует в `ClientServiceError`-подтипы, чтобы бот ловил одно
семейство. Аналогия — `crypto.DecryptionError` как низкоуровневая ошибка,
отличная от сервисных.
"""

from __future__ import annotations


class NovaPoshtaError(Exception):
    """База для ошибок обращения к API НП.

    Несёт список человекочитаемых ошибок НП и их коды (`errorCodes`).
    """

    def __init__(
        self,
        message: str,
        *,
        errors: list[str] | None = None,
        error_codes: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.errors = errors or []
        self.error_codes = error_codes or []


class NovaPoshtaUnavailable(NovaPoshtaError):
    """Сеть/таймаут/5xx — временный сбой, имеет смысл ретраить."""


class NovaPoshtaAuthError(NovaPoshtaError):
    """Невалидный/заблокированный ключ ФОП (валидация ключа, доступ)."""


class NovaPoshtaValidationError(NovaPoshtaError):
    """Запрос отклонён НП: неверное поле, `Cost` ниже минимума и т.п."""


class NovaPoshtaNotFound(NovaPoshtaError):
    """Запрошенный объект НП не найден (напр. удаление неизвестного ТТН)."""
