"""Симметричное шифрование секретов (Fernet) — для ключей НП в `sender_profiles`.

Ключ берётся из `settings.fernet_key` (см. `.env`). Fernet инициализируется
лениво: при пустом ключе бросаем понятную ошибку, а не падаем на импорте.
Сгенерировать ключ: `python -c "from app.utils.crypto import generate_key; print(generate_key())"`.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


class DecryptionError(RuntimeError):
    """Не удалось расшифровать токен: битые данные или сменился `FERNET_KEY`."""


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
    """Расшифровать токен → исходная строка.

    При битом/несовместимом токене (повреждение или ротация `FERNET_KEY`) бросает
    `DecryptionError` вместо «сырого» `InvalidToken` — чтобы загрузка ORM падала
    предсказуемо. Ловит её глобальный errors-router бота
    (`app/bot/handlers/errors.py`): лог для ops + понятное сообщение пользователю.
    """
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise DecryptionError(
            "не удалось расшифровать ключ НП: повреждённый токен или сменён FERNET_KEY"
        ) from exc
