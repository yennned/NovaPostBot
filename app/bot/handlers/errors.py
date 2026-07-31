"""Глобальный errors-router: понятный ответ вместо молчания бота.

Общий принцип: пользователь ВСЕГДА должен получить реакцию. Необработанное
исключение в хендлере оставляет callback без ответа, Telegram держит на кнопке
крутящийся спиннер, и это выглядит как «кнопка сломалась» — самая частая жалоба.
Поэтому ответ на callback здесь важнее текста (см. `_reply`).

Что ловим:

* `crypto.DecryptionError` — непрочитанный ключ ФОП. `np_api_key` хранится
  Fernet-зашифрованным и расшифровывается на чтении строки ORM (`EncryptedString`).
  Если `FERNET_KEY` сменили/потеряли, любая загрузка `SenderProfile` бросит его,
  а профили читаются во многих местах (создание/цена/адреса/отмена/кабинет/список
  клиентов). Это всегда «всё разом» (ключ глобальный), поэтому ловим не точечно в
  каждом сервисе, а одним обработчиком уровня диспетчера.
* `StockSourceUnavailable` — Google Sheets недоступен (квота 429, 5xx). Честное
  сообщение вместо экрана с нулевыми остатками.
* `TelegramBadRequest` «message is not modified» — дабл-тап, глушим без шума.
* Всё остальное — последний рубеж без фильтра, строго последним в роутере.

Транзакция к этому моменту уже откатана `ServicesMiddleware`.
"""

from __future__ import annotations

import contextlib
from typing import Any

import structlog
from aiogram import Router
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import ExceptionTypeFilter
from aiogram.types import ErrorEvent, Message

from app.sheets import StockSourceUnavailable
from app.utils.crypto import DecryptionError

logger = structlog.get_logger(__name__)

router = Router(name="errors")

_KEY_UNREADABLE_TEXT = (
    "⚠️ Технічна помилка з ключами ФОП (Нова Пошта). "
    "Ми вже сповіщені — зверніться, будь ласка, до підтримки."
)
_STOCK_UNAVAILABLE_TEXT = "⚠️ Склад тимчасово недоступний. Спробуйте, будь ласка, за хвилину."
_UNEXPECTED_TEXT = "⚠️ Сталася помилка. Спробуйте ще раз або зверніться до підтримки."


def _event_message(event: ErrorEvent) -> Message | None:
    """Сообщение, в ответ на которое можно написать (из message или callback)."""
    update = event.update
    if update.message is not None:
        return update.message
    if update.callback_query is not None:
        return update.callback_query.message
    return None


async def _reply(event: ErrorEvent, text: str) -> None:
    """Ответить пользователю, ОБЯЗАТЕЛЬНО закрыв callback.

    Раньше на ошибке в callback-хендлере писали в `callback_query.message`, но сам
    callback не отвечали — Telegram оставлял на кнопке крутящийся спиннер, и для
    пользователя это выглядело как «кнопка сломалась». Ack важнее текста, поэтому
    сначала `callback.answer`.
    """
    callback = event.update.callback_query
    if callback is not None:
        with contextlib.suppress(TelegramAPIError):
            await callback.answer(text, show_alert=True)
        return
    message = _event_message(event)
    if message is not None:
        with contextlib.suppress(TelegramAPIError):
            await message.answer(text)


@router.errors(ExceptionTypeFilter(DecryptionError))
async def on_key_decryption_error(event: ErrorEvent) -> None:
    """Сбой расшифровки ключа НП → лог + понятный ответ (если есть куда отвечать)."""
    logger.error("fernet_decrypt_failed", error=str(event.exception))
    await _reply(event, _KEY_UNREADABLE_TEXT)


@router.errors(ExceptionTypeFilter(StockSourceUnavailable))
async def on_stock_source_unavailable(event: ErrorEvent) -> None:
    """Sheets недоступен (квота 429/5xx) → честное сообщение, а не пустой склад."""
    exc = event.exception
    logger.warning(
        "stock_source_unavailable",
        client_key=getattr(exc, "client_key", None),
        status=getattr(exc, "status", None),
    )
    await _reply(event, _STOCK_UNAVAILABLE_TEXT)


@router.errors(ExceptionTypeFilter(TelegramBadRequest))
async def on_message_not_modified(event: ErrorEvent) -> Any:
    """Дабл-тап inline-кнопки: `edit_*` тем же контентом → Telegram «message is not
    modified». Сообщение уже в нужном состоянии — глушим без шума (возврат не-UNHANDLED
    помечает событие обработанным, лог не пишется). Прочие `TelegramBadRequest` (нет
    сообщения, устаревший callback и т.п.) — реальные, отдаём дальше через `UNHANDLED`.
    """
    if "message is not modified" in str(event.exception):
        return None
    return UNHANDLED


@router.errors()
async def on_unhandled_error(event: ErrorEvent) -> None:
    """Последний рубеж: любое НЕОЖИДАННОЕ исключение.

    Без него хендлер, упавший на чём угодно, не отвечал пользователю НИЧЕГО: тап по
    кнопке оставлял висящий спиннер, сообщение оставалось без реакции, а разработчик
    видел ошибку только если специально смотрел логи. «Кнопка не работает» в 90%
    случаев была именно этим.

    Регистрируется ПОСЛЕДНИМ и без фильтра: aiogram матчит error-хендлеры в порядке
    регистрации, поэтому точечные обработчики выше сохраняют приоритет, а
    `on_message_not_modified` продолжает глушить дабл-тапы. `errors_router` уже
    последний в `build_dispatcher`, так что этот обработчик — действительно край.
    """
    callback = event.update.callback_query
    # Только `getattr` с дефолтом: у `Update` нет `event_from_user` (это ключ в
    # `data`, а не поле модели), и прямое обращение роняло сам обработчик — то есть
    # снова возвращало молчание, которое он и должен был устранить.
    source = callback or event.update.message
    logger.error(
        "unhandled_handler_error",
        exc_info=event.exception,
        update_id=getattr(event.update, "update_id", None),
        user_id=getattr(getattr(source, "from_user", None), "id", None),
        callback_data=getattr(callback, "data", None),
    )
    await _reply(event, _UNEXPECTED_TEXT)
