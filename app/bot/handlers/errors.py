"""Глобальный errors-router: непрочитанный ключ ФОП (`crypto.DecryptionError`).

`np_api_key` хранится Fernet-зашифрованным и расшифровывается на чтении строки ORM
(`EncryptedString`). Если `FERNET_KEY` сменили/потеряли (или токен повреждён), любая
загрузка `SenderProfile` бросит `DecryptionError` — а профили читаются во многих
местах (создание/цена/адреса/отмена/кабинет/список клиентов). Это всегда «всё разом»
(ключ глобальный), поэтому ловим не точечно в каждом сервисе, а **одним dispatcher-level
backstop'ом**: громкий лог для ops + понятное uk-сообщение пользователю вместо опаковой
ошибки. Транзакция к этому моменту уже откатана `ServicesMiddleware`.
"""

from __future__ import annotations

import structlog
from aiogram import Router
from aiogram.filters import ExceptionTypeFilter
from aiogram.types import ErrorEvent, Message

from app.utils.crypto import DecryptionError

logger = structlog.get_logger(__name__)

router = Router(name="errors")

_KEY_UNREADABLE_TEXT = (
    "⚠️ Технічна помилка з ключами ФОП (Нова Пошта). "
    "Ми вже сповіщені — зверніться, будь ласка, до підтримки."
)


def _event_message(event: ErrorEvent) -> Message | None:
    """Сообщение, в ответ на которое можно написать (из message или callback)."""
    update = event.update
    if update.message is not None:
        return update.message
    if update.callback_query is not None:
        return update.callback_query.message
    return None


@router.errors(ExceptionTypeFilter(DecryptionError))
async def on_key_decryption_error(event: ErrorEvent) -> None:
    """Сбой расшифровки ключа НП → лог + понятный ответ (если есть куда отвечать)."""
    logger.error("fernet_decrypt_failed", error=str(event.exception))
    message = _event_message(event)
    if message is not None:
        await message.answer(_KEY_UNREADABLE_TEXT)
