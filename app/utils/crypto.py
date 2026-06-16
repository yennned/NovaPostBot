"""Симметричное шифрование секретов (Fernet) — для ключей НП в `sender_profiles`.

Ключ берётся из `settings.fernet_key` (см. `.env`). Fernet инициализируется
лениво: при пустом ключе бросаем понятную ошибку, а не падаем на импорте.
Сгенерировать ключ: `python -c "from app.utils.crypto import generate_key; print(generate_key())"`.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet

from app.config import get_settings


def generate_key() -> str:
    """Сгенерировать новый Fernet-ключ (base64, 32 байта)."""
    return Fernet.generate_key().decode()


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = get_settings().fernet_key
    if not key:
        raise RuntimeError(
            "FERNET_KEY не задан — невозможно шифровать/расшифровывать ключи НП. "
            "Сгенерируйте ключ через app.utils.crypto.generate_key()."
        )
    return Fernet(key.encode())


def encrypt(plaintext: str) -> str:
    """Зашифровать строку → токен (str)."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Расшифровать токен → исходная строка."""
    return _fernet().decrypt(token.encode()).decode()
