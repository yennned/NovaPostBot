"""Ответ на callback, который не подобрал ни один хендлер.

Проблема, которую это закрывает. Многие callback-хендлеры отфильтрованы состоянием
FSM — например, выбор ФОП (`ttn.cb_pick_sender` при `CreateTtnState.picking_sender`).
Как только состояние сменилось, тот же `callback_data` не матчится **ничем**:
aiogram молча роняет апдейт, callback остаётся неотвеченным, и Telegram крутит
на кнопке спиннер секунд тридцать. Для пользователя это «кнопка зависла».

Достаточно двух совершенно бытовых действий:

* нервный **двойной тап** — первый переводит состояние, второй уже не матчится;
* тап по кнопке **прошлого экрана** (сообщение-то никуда не делось).

E2E-прогон по боевым данным поймал это пять раз у двух разных персон, и до сих
пор в проекте не было ни одного catch-all для `callback_query` — только для
исключений (`errors.py`). Это разные вещи: там хендлер упал, здесь его вовсе не
нашлось, поэтому errors-router не срабатывает.

Роутер подключается ПРЕДПОСЛЕДНИМ — после всех предметных и перед `errors_router`:
любой матч выше побеждает, а сюда попадает только то, что иначе ушло бы в тишину.
"""

from __future__ import annotations

import structlog
from aiogram import Router
from aiogram.types import CallbackQuery

logger = structlog.get_logger(__name__)

router = Router(name="fallback")

STALE_CALLBACK_TEXT = "Ця кнопка вже неактуальна — відкрийте екран заново."


@router.callback_query()
async def on_unmatched_callback(callback: CallbackQuery) -> None:
    """Закрыть спиннер и честно сказать, что кнопка устарела.

    Экран НЕ перерисовываем: сюда попадает и дубль тапа, и кнопка из старого
    сообщения, и подмена состояния — во всех случаях неизвестно, что человек
    сейчас видит, а лишний `edit`/`answer` затёр бы актуальный экран. Ack важнее:
    он снимает спиннер, а текст объясняет, что делать.
    """
    logger.info(
        "callback_unmatched",
        callback_data=callback.data,
        message_id=getattr(callback.message, "message_id", None),
    )
    await callback.answer(STALE_CALLBACK_TEXT, show_alert=False)
