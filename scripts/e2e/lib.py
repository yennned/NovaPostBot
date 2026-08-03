"""Ядро E2E-харнесса: настоящий `Update` → настоящий `build_dispatcher`.

Три принципа, каждый оплачен реальным багом:

1. **Тапаем по видимому тексту кнопки, а не по `callback_data`.** Захардкоженный
   `cab:ttn:pick:3` прошёл бы мимо бага `7ede28f`, где индекс отфильтрованной
   страницы резолвился по нефильтрованному списку. Персона видит экран так же,
   как человек: текст + клавиатура.
2. **Молчание — дефект.** Ноль исходящих вызовов на тап означает необработанное
   исключение в теле хендлера: aiogram пишет трейс в лог и не отвечает, а для
   пользователя это «кнопка не реагирует» (`18b1995`).
3. **Один процесс — одна персона.** Роутеры в `app/bot/handlers` — модульные
   синглтоны, второй `build_dispatcher` в том же процессе падает с «Router is
   already attached». Разнесение по процессам заодно даёт настоящую конкуренцию
   к Postgres/НП/Sheets, а не имитацию через asyncio.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.methods import TelegramMethod
from aiogram.types import (
    CallbackQuery,
    Chat,
    Contact,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    Update,
    User,
)
from scripts.e2e.env import ROOT, outbox_chat_id

ARTIFACTS = ROOT / "scripts" / "e2e" / "artifacts"

#: Отказ НП на самом сабмите (`_submit_error_text` в хендлере ТТН). Хендлер при
#: этом отработал штатно — заглушки нет, экран нарисован, — но документа клиент
#: не получил. Вынесен константой, потому что на него ссылается вердикт.
SUBMIT_FAILED_MARKER = "Не вдалося створити ТТН"

#: Тексты, появление которых означает, что клиент получил отказ. Два источника:
#: глобальный errors-router (`app/bot/handlers/errors.py`) — хендлер упал и
#: показана заглушка; и `SUBMIT_FAILED_MARKER` — форма пройдена, ТТН нет.
#: Второй добавлен после прогона 2026-08-03, где три таких отказа не попали в
#: отчёт вовсе и он вышел ложно-зелёным.
ERROR_MARKERS = (
    "Сталася помилка",
    "Технічна помилка з ключами ФОП",
    "Склад тимчасово недоступний",
    SUBMIT_FAILED_MARKER,
)


# --------------------------------------------------------------------------- #
# Экран
# --------------------------------------------------------------------------- #
@dataclass
class Button:
    text: str
    data: str | None  # None → кнопка нижней (reply) панели


@dataclass
class Screen:
    """Что персона «видит» прямо сейчас."""

    text: str = ""
    message_id: int = 0
    inline: list[Button] = field(default_factory=list)
    reply: list[Button] = field(default_factory=list)

    def find(self, pattern: str) -> Button | None:
        rx = re.compile(pattern, re.IGNORECASE)
        for button in self.inline:
            if rx.search(button.text):
                return button
        return None

    def find_data(self, prefix: str) -> Button | None:
        """Кнопка по префиксу `callback_data` — для шагов, где текст кнопки
        плавает (товар, місто, відділення), а семантика шага фиксирована."""
        for button in self.inline:
            if button.data and button.data.startswith(prefix):
                return button
        return None

    def find_reply(self, pattern: str) -> Button | None:
        rx = re.compile(pattern, re.IGNORECASE)
        for button in self.reply:
            if rx.search(button.text):
                return button
        return None

    @property
    def buttons(self) -> list[str]:
        return [b.text for b in self.inline]


def _parse_markup(markup: Any) -> tuple[list[Button], list[Button]]:
    inline: list[Button] = []
    reply: list[Button] = []
    if isinstance(markup, InlineKeyboardMarkup):
        for row in markup.inline_keyboard:
            for cell in row:
                inline.append(Button(text=cell.text or "", data=cell.callback_data))
    elif isinstance(markup, ReplyKeyboardMarkup):
        for row in markup.keyboard:
            for cell in row:
                text = cell if isinstance(cell, str) else (cell.text or "")
                reply.append(Button(text=text, data=None))
    return inline, reply


# --------------------------------------------------------------------------- #
# Бот-приёмник
# --------------------------------------------------------------------------- #
class RecordingBot(Bot):
    """Настоящий `aiogram.Bot`, но каждый исходящий вызов перехватывается.

    `mode="real"` — реально отправляет в Telegram, переписав `chat_id` на чат
    владельца и пометив, кому сообщение предназначалось. `mode="stub"` — ничего
    не отправляет и синтезирует ответы: нужно для нагрузочного каскада, где
    per-chat лимит Telegram (~1 сообщение/сек) исказил бы замеры латентности.
    """

    def __init__(
        self,
        token: str,
        *,
        mode: str = "stub",
        label: str = "persona",
        sink: list[dict[str, Any]] | None = None,
    ) -> None:
        if mode != "real" and not token:
            # В stub-режиме токен не используется (наружу ничего не уходит), но
            # конструктор `Bot` требует синтаксически валидный. Так харнесс можно
            # гонять на стенде без токена — например, обкатывать сам харнесс локально.
            token = "111111:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        super().__init__(token=token, default=DefaultBotProperties(parse_mode=None))
        self.e2e_mode = mode
        self.e2e_label = label
        self.outgoing: list[dict[str, Any]] = sink if sink is not None else []
        self._next_message_id = 10_000
        self.screen = Screen()
        self.telegram_seconds = 0.0

    async def __call__(self, method: TelegramMethod, request_timeout: int | None = None) -> Any:
        name = type(method).__name__
        text = getattr(method, "text", None) or getattr(method, "caption", None)
        markup = getattr(method, "reply_markup", None)
        record: dict[str, Any] = {
            "method": name,
            "text": text,
            "callback_answer": getattr(method, "text", None)
            if name == "AnswerCallbackQuery"
            else None,
        }

        if name in ("SendMessage", "EditMessageText"):
            inline, reply = _parse_markup(markup)
            record["buttons"] = [b.text for b in inline] or [b.text for b in reply]

        started = time.perf_counter()
        try:
            if self.e2e_mode == "real":
                result = await self._send_for_real(method, name)
            else:
                result = self._synthesize(method, name)
        except Exception as exc:
            record["transport_error"] = f"{type(exc).__name__}: {exc}"
            result = self._synthesize(method, name)
        self.telegram_seconds += time.perf_counter() - started

        # Экран обновляем ПОСЛЕ отправки — message_id берём настоящий.
        if name in ("SendMessage", "EditMessageText"):
            inline, reply = _parse_markup(markup)
            self.screen = Screen(
                text=text or "",
                message_id=getattr(result, "message_id", self.screen.message_id),
                inline=inline,
                reply=reply or self.screen.reply,
            )
            record["message_id"] = self.screen.message_id

        self.outgoing.append(record)
        return result

    async def _send_for_real(self, method: TelegramMethod, name: str) -> Any:
        if hasattr(method, "chat_id"):
            method.chat_id = outbox_chat_id()
        if name == "SendMessage" and getattr(method, "text", None):
            method.text = f"[{self.e2e_label}]\n{method.text}"
        return await super().__call__(method)

    def _synthesize(self, method: TelegramMethod, name: str) -> Any:
        if name in ("SendMessage", "EditMessageText"):
            self._next_message_id += 1
            return Message(
                message_id=self._next_message_id,
                date=datetime.now(UTC),
                chat=Chat(id=outbox_chat_id(), type="private"),
                text=getattr(method, "text", "") or "",
            )
        return True


# --------------------------------------------------------------------------- #
# Счётчик обращений к Google Sheets
# --------------------------------------------------------------------------- #
class SheetsMeter:
    """Считает и хронометрирует чтения листа склада.

    Главный вопрос нагрузки — не «сколько миллисекунд рисуется экран», а
    **сколько раз за сценарий бот перечитывает лист**: квота Google (60 чтений
    в минуту на пользователя) выбирается именно этим, и упирается в неё даже
    один человек, создающий ТТН подряд. Считаем без правок `app/` — подменой
    метода на время прогона.
    """

    def __init__(self) -> None:
        self.reads = 0
        self.seconds = 0.0
        self.failures = 0
        self._original = None

    def install(self) -> None:
        from app.sheets.client import SheetsClient

        if self._original is not None:
            return
        self._original = SheetsClient.read_rows
        meter = self

        def counted(self_client, client_key):
            meter.reads += 1
            started = time.perf_counter()
            try:
                return meter._original(self_client, client_key)
            except Exception:
                meter.failures += 1
                raise
            finally:
                meter.seconds += time.perf_counter() - started

        SheetsClient.read_rows = counted

    def restore(self) -> None:
        if self._original is None:
            return
        from app.sheets.client import SheetsClient

        SheetsClient.read_rows = self._original
        self._original = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "sheets_reads": self.reads,
            "sheets_seconds": round(self.seconds, 2),
            "sheets_failures": self.failures,
        }


# --------------------------------------------------------------------------- #
# Персона
# --------------------------------------------------------------------------- #
class Persona:
    """Один «живой человек»: свой процесс, свой диспетчер, свой лог."""

    def __init__(
        self,
        *,
        name: str,
        telegram_id: int,
        dispatcher: Any,
        bot: RecordingBot,
        log_path: Path,
        sheets: SheetsMeter | None = None,
        chat_id: int | None = None,
    ) -> None:
        self.name = name
        self.telegram_id = telegram_id
        # Свой чат нужен нагрузочному прогону: там N персон живут в ОДНОМ процессе
        # (роутеры — модульные синглтоны, второй диспетчер собрать нельзя), и общий
        # `outbox_chat_id` склеил бы их состояния — ключ FSM строится из
        # `(bot_id, chat_id, user_id)`. У e2e поведение прежнее: `None` → чат
        # владельца, иначе в режиме `real` не сработает `edit_message_text`.
        self.chat_id = chat_id
        self.dp = dispatcher
        self.bot = bot
        self.sheets = sheets
        self.log_path = log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = self.log_path.open("w", encoding="utf-8")
        self._update_id = 1
        self.steps = 0
        self.defects: list[dict[str, Any]] = []

    # -- инфраструктура ----------------------------------------------------- #
    @property
    def screen(self) -> Screen:
        return self.bot.screen

    def _user(self) -> User:
        return User(id=self.telegram_id, is_bot=False, first_name=self.name)

    def _chat(self) -> Chat:
        # Чат — владельца: в режиме `real` иначе не сработает `edit_message_text`
        # (message_id принадлежит именно этому чату). Нагрузочный прогон задаёт
        # свой: см. комментарий у `chat_id` в `__init__`.
        return Chat(id=self.chat_id or outbox_chat_id(), type="private")

    def _next(self) -> int:
        self._update_id += 1
        return self._update_id

    def record(self, entry: dict[str, Any]) -> None:
        entry["persona"] = self.name
        entry["ts"] = datetime.now(UTC).isoformat()
        self._log.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._log.flush()

    def close(self) -> None:
        self._log.close()

    # -- ядро прогона ------------------------------------------------------- #
    async def _feed(self, update: Update, *, action: str, target: str) -> dict[str, Any]:
        before = len(self.bot.outgoing)
        tg_before = self.bot.telegram_seconds
        sheets_before = self.sheets.reads if self.sheets else 0
        started = time.perf_counter()
        error: str | None = None
        try:
            await self.dp.feed_update(self.bot, update)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        elapsed = time.perf_counter() - started
        telegram = self.bot.telegram_seconds - tg_before
        produced = self.bot.outgoing[before:]

        sheets_reads = self.sheets.reads - sheets_before if self.sheets else 0
        entry: dict[str, Any] = {
            "action": action,
            "target": target,
            "total_ms": round(elapsed * 1000, 1),
            "telegram_ms": round(telegram * 1000, 1),
            "app_ms": round((elapsed - telegram) * 1000, 1),
            "sheets_reads": sheets_reads,
            "outgoing": produced,
            "screen_text": self.screen.text[:400],
            "screen_buttons": self.screen.buttons,
        }
        if error:
            entry["exception"] = error
            self.defects.append(
                {"kind": "exception", "action": action, "target": target, "error": error}
            )

        # Молчание = почти всегда упавший хендлер (см. докстринг модуля).
        if not produced:
            entry["silent"] = True
            self.defects.append({"kind": "silence", "action": action, "target": target})

        for call in produced:
            payload = f"{call.get('text') or ''} {call.get('callback_answer') or ''}"
            for marker in ERROR_MARKERS:
                if marker in payload:
                    entry["error_screen"] = marker
                    self.defects.append(
                        {
                            "kind": "error_screen",
                            "action": action,
                            "target": target,
                            "marker": marker,
                        }
                    )

        self.steps += 1
        self.record(entry)
        return entry

    # -- действия человека -------------------------------------------------- #
    async def send(self, text: str) -> dict[str, Any]:
        """Написать в чат (в т.ч. тап кнопки нижней панели — это обычное сообщение)."""
        message = Message(
            message_id=self._next(),
            date=datetime.now(UTC),
            chat=self._chat(),
            from_user=self._user(),
            text=text,
        )
        return await self._feed(
            Update(update_id=self._next(), message=message), action="type", target=text
        )

    async def send_contact(self, phone: str) -> dict[str, Any]:
        message = Message(
            message_id=self._next(),
            date=datetime.now(UTC),
            chat=self._chat(),
            from_user=self._user(),
            contact=Contact(phone_number=phone, first_name=self.name, user_id=self.telegram_id),
        )
        return await self._feed(
            Update(update_id=self._next(), message=message), action="contact", target=phone
        )

    async def press(self, pattern: str) -> dict[str, Any]:
        """Тап кнопки нижней панели по видимому тексту."""
        button = self.screen.find_reply(pattern)
        if button is None:
            self.defects.append(
                {
                    "kind": "missing_reply_button",
                    "target": pattern,
                    "available": [b.text for b in self.screen.reply],
                }
            )
            self.record(
                {
                    "action": "press",
                    "target": pattern,
                    "missing": True,
                    "available": [b.text for b in self.screen.reply],
                }
            )
            return {}
        return await self.send(button.text)

    async def tap(self, pattern: str, *, data: str | None = None) -> dict[str, Any]:
        """Тап inline-кнопки по видимому тексту (или явным `callback_data`)."""
        target = data
        label = data or pattern
        if target is None:
            button = self.screen.find(pattern)
            if button is None or button.data is None:
                self.defects.append(
                    {"kind": "missing_button", "target": pattern, "available": self.screen.buttons}
                )
                self.record(
                    {
                        "action": "tap",
                        "target": pattern,
                        "missing": True,
                        "available": self.screen.buttons,
                    }
                )
                return {}
            target = button.data
            label = button.text

        message = Message(
            message_id=self.screen.message_id or self._next(),
            date=datetime.now(UTC),
            chat=self._chat(),
            from_user=User(id=self.bot.id if self.bot.id else 1, is_bot=True, first_name="bot"),
            text=self.screen.text or "екран",
        )
        callback = CallbackQuery(
            id=f"cb-{self._next()}",
            from_user=self._user(),
            chat_instance=f"ci-{self.telegram_id}",
            data=target,
            message=message,
        )
        return await self._feed(
            Update(update_id=self._next(), callback_query=callback),
            action="tap",
            target=f"{label} [{target}]",
        )

    async def tap_data(self, prefix: str, *, nth: int = 0) -> dict[str, Any]:
        """Тап по префиксу `callback_data` — но только среди кнопок, реально
        нарисованных на экране (человек не может нажать то, чего не видит)."""
        matches = [b for b in self.screen.inline if b.data and b.data.startswith(prefix)]
        if len(matches) <= nth:
            self.defects.append(
                {
                    "kind": "missing_button",
                    "target": prefix,
                    "available": [b.data for b in self.screen.inline],
                }
            )
            self.record(
                {
                    "action": "tap",
                    "target": prefix,
                    "missing": True,
                    "available": [b.data for b in self.screen.inline],
                }
            )
            return {}
        button = matches[nth]
        return await self.tap(button.text, data=button.data)

    async def become(self, target_telegram_id: int) -> dict[str, Any]:
        """Стать другим пользователем через штатный god-mode бота (`/as_user`).

        Апдейты подаются от dev-аккаунта владельца, а `effective_user` подменяет
        сам бот — то есть используется продуктовая функция, а не подделка
        отправителя. Цена: `is_dev=True` проходит сквозь гейты прав
        (`app/bot/permissions.py` проверяет dev первым), поэтому отказы по правам
        такой персоной не проверяются — для них нужен вход своим `telegram_id`.
        """
        entry = await self.send(f"/as_user {target_telegram_id}")
        self.record(
            {
                "action": "impersonate",
                "target": str(target_telegram_id),
                "screen_text": (self.screen.text or "")[:200],
            }
        )
        return entry

    async def as_role(self, role: str) -> dict[str, Any]:
        """`/as client|manager|owner|off` — роль без привязки к человеку."""
        return await self.send(f"/as {role}")

    async def idle(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


# --------------------------------------------------------------------------- #
# Сборка стенда
# --------------------------------------------------------------------------- #
async def build_persona(
    *,
    name: str,
    telegram_id: int,
    mode: str = "stub",
    run_id: str = "run",
    np_transport: Any = None,
    log_path: Path | None = None,
    chat_id: int | None = None,
    install_sheets_meter: bool = True,
) -> tuple[Persona, Any, Any]:
    """Собрать диспетчер и персону. Диспетчер — ОДИН на процесс.

    `np_transport` — подменить транспорт НП (`httpx.MockTransport`) для
    нагрузочного прогона; `NovaPoshtaClient` его принимает, но раньше сюда не
    прокидывался. `install_sheets_meter=False` — не вешать хронометраж на
    настоящий `SheetsClient`: у нагрузки свой счётчик с квотой.

    **FSM берётся из Redis, как в проде.** Раньше сюда не передавался redis-клиент,
    и харнесс жил на `MemoryStorage` — то есть не мог увидеть целый класс дефектов:
    `RedisStorage` json-дампит FSM-data, и первое же непригодное для JSON значение
    (`date`, `Decimal`, `UUID`) валит форму в проде, оставаясь невидимым здесь.
    Redis харнессу и так обязателен — на нём кэш справочников НП строкой ниже.
    """
    from app.bot import build_dispatcher
    from app.config import get_settings
    from app.novaposhta.cache import NPReferenceCache
    from app.novaposhta.client import NovaPoshtaClient
    from redis.asyncio import from_url as redis_from_url

    settings = get_settings()
    np_client = NovaPoshtaClient(settings=settings, transport=np_transport)
    redis_client = redis_from_url(settings.redis_url)
    np_cache = NPReferenceCache(redis_client, settings=settings)
    dispatcher = build_dispatcher(
        settings, np_client=np_client, np_cache=np_cache, redis=redis_client
    )

    sheets: SheetsMeter | None = None
    if install_sheets_meter:
        sheets = SheetsMeter()
        sheets.install()

    bot = RecordingBot(settings.bot_token, mode=mode, label=name)
    persona = Persona(
        name=name,
        telegram_id=telegram_id,
        dispatcher=dispatcher,
        bot=bot,
        log_path=log_path or (ARTIFACTS / run_id / f"{name}.jsonl"),
        sheets=sheets,
        chat_id=chat_id,
    )
    return persona, np_client, redis_client


def attach_persona(
    *,
    name: str,
    telegram_id: int,
    dispatcher: Any,
    log_path: Path,
    chat_id: int,
) -> Persona:
    """Ещё одна персона на УЖЕ собранном диспетчере.

    `build_dispatcher` в процессе можно позвать один раз (роутеры в
    `app/bot/handlers` — модульные синглтоны), но персон на нём может жить сколько
    угодно: `dp.feed_update(bot, update)` принимает бота аргументом, а ключ FSM
    строится из `(bot_id, chat_id, user_id)`. Именно так работает прод — один
    процесс, много людей, — и именно эту топологию обязан воспроизводить
    нагрузочный прогон. Разносить персон по процессам нельзя: у каждого был бы
    свой пул коннектов и свой single-worker executor, а значит ожидание в пуле и
    глубина очереди оказались бы тождественно нулевыми.
    """
    from app.config import get_settings

    settings = get_settings()
    bot = RecordingBot(settings.bot_token, mode="stub", label=name)
    return Persona(
        name=name,
        telegram_id=telegram_id,
        dispatcher=dispatcher,
        bot=bot,
        log_path=log_path,
        chat_id=chat_id,
    )
