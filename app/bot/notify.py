"""Адаптер `Notifier` поверх aiogram `Bot` (бот-слой).

Доменный `app/services/notifications.py` принимает `Notifier`-протокол; здесь
конкретная реализация, которая шлёт сообщения и **глотает** ошибки доставки
отдельным получателям (сбой одного не должен валить флоу регистрации/подтверждения).

**Почему здесь ограничитель.** Telegram лимитирует исходящие: ~30 сообщений в
секунду глобально и примерно одно в секунду в один чат. Превышение приходит
исключением `TelegramRetryAfter` с полем `retry_after` — «подожди столько секунд».

До этой правки такого обращения не было **нигде** в `app/`, и это не значило
«лимит нас не касается»: `TelegramRetryAfter` — подкласс `TelegramAPIError`
(`aiogram/exceptions.py`), поэтому он попадал в общий `except` ниже и записывался
как рядовой сбой доставки. То есть нас уже могли лимитировать, а в логах это
выглядело как «пуш не пришёл» — сообщение терялось навсегда, и отличить
«заблокировал бота» от «нас придержали на 3 секунды» было нельзя.

Веер усиливает это: `_send_many` шлёт всем получателям разом через
`asyncio.gather`, а проход трекинга, нашедший сотню новых `dispatched`, выдаёт
такие вееры пачками. Всплеск, который Telegram придержал бы на секунды, без
ограничителя превращается в пачку потерянных уведомлений.
"""

from __future__ import annotations

import asyncio
import time

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter

logger = structlog.get_logger(__name__)

#: Глобальный потолок исходящих. Ниже документированных ~30/с осознанно: лимит
#: считает Telegram по своим часам, и идти с ним впритык значит регулярно получать
#: `retry_after` вместо запаса.
GLOBAL_MESSAGES_PER_SECOND = 25
#: Потолок на один чат. Telegram придерживает примерно на одном сообщении в
#: секунду; наши вееры почти всегда «по одному сообщению разным людям», поэтому
#: упирается сюда только повторный пуш одному и тому же человеку.
PER_CHAT_INTERVAL_SECONDS = 1.0
#: Сколько раз повторить после `retry_after`. Второй попытки хватает: Telegram
#: называет точное время ожидания, и если и она не прошла — дело не в темпе.
RETRY_ATTEMPTS = 2


class _RateLimiter:
    """Глобальный темп + интервал на чат. Один на процесс, живёт в `BotNotifier`.

    Не токен-бакет с бюрократией, а два простых инварианта: между любыми двумя
    отправками не меньше `1/GLOBAL_MESSAGES_PER_SECOND`, а между двумя в один чат —
    не меньше `PER_CHAT_INTERVAL_SECONDS`. Лок общий: без него `asyncio.gather`
    выпускает весь веер одновременно, и оба инварианта нарушаются разом.
    """

    def __init__(
        self,
        *,
        messages_per_second: int = GLOBAL_MESSAGES_PER_SECOND,
        per_chat_interval: float = PER_CHAT_INTERVAL_SECONDS,
    ) -> None:
        self._min_gap = 1.0 / messages_per_second if messages_per_second else 0.0
        self._per_chat_interval = per_chat_interval
        self._lock = asyncio.Lock()
        self._last_global = 0.0
        self._last_by_chat: dict[int, float] = {}

    async def acquire(self, telegram_id: int) -> None:
        async with self._lock:
            now = time.monotonic()
            earliest = max(
                self._last_global + self._min_gap,
                self._last_by_chat.get(telegram_id, 0.0) + self._per_chat_interval,
            )
            delay = earliest - now
            if delay > 0:
                # Спим ПОД локом намеренно: иначе соседняя корутина проскочит в
                # образовавшееся окно и глобальный темп перестанет соблюдаться.
                await asyncio.sleep(delay)
                now = earliest
            self._last_global = now
            self._last_by_chat[telegram_id] = now


class BotNotifier:
    def __init__(self, bot: Bot, *, limiter: _RateLimiter | None = None) -> None:
        self.bot = bot
        self.limiter = limiter or _RateLimiter()

    async def send_message(self, telegram_id: int, text: str) -> None:
        for attempt in range(RETRY_ATTEMPTS + 1):
            await self.limiter.acquire(telegram_id)
            try:
                await self.bot.send_message(telegram_id, text, parse_mode="HTML")
                return
            except TelegramRetryAfter as exc:
                # Ловим ДО `TelegramAPIError`: он его подкласс, и без отдельной
                # ветки флуд-вейт уходил бы в «сбой доставки», а сообщение —
                # в никуда. Telegram называет точное время ожидания; ждём его.
                if attempt >= RETRY_ATTEMPTS:
                    logger.warning(
                        "notify_rate_limited_gave_up",
                        telegram_id=telegram_id,
                        retry_after=exc.retry_after,
                        attempts=attempt + 1,
                    )
                    return
                logger.info(
                    "notify_rate_limited",
                    telegram_id=telegram_id,
                    retry_after=exc.retry_after,
                    attempt=attempt + 1,
                )
                await asyncio.sleep(exc.retry_after)
            except TelegramAPIError as exc:
                logger.warning("notify_failed", telegram_id=telegram_id, error=str(exc))
                return
