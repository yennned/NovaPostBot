"""Кастомные SQLAlchemy-типы.

`EncryptedString` — прозрачный шифр поверх `app.utils.crypto`: в Python поле
содержит открытый текст, в БД хранится Fernet-токен. Так модели/репозитории
остаются чистыми (без явных вызовов encrypt/decrypt), а ключ НП на диске —
всегда зашифрован.
"""

from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.types import TypeDecorator

from app.utils import crypto


class EncryptedString(TypeDecorator):
    """`String`-колонка, шифруемая Fernet на запись и расшифровываемая на чтение."""

    impl = String
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect) -> str | None:
        if value is None:
            return None
        return crypto.encrypt(value)

    def process_result_value(self, value: str | None, dialect) -> str | None:
        if value is None:
            return None
        return crypto.decrypt(value)
