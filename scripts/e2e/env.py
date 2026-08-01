"""Загрузка окружения стенда — **до** первого импорта `app.*`.

`app.config.Settings` читает `.env` проекта, а `get_settings()` кеширован
(`lru_cache`), поэтому окружение нужно подменить в `os.environ` раньше, чем
кто-либо дёрнет настройки: переменные окружения в pydantic-settings имеют
приоритет над файлом `.env`.

Секреты (`BOT_TOKEN`, `FERNET_KEY`, строка Neon) живут только в `.env.prod`
(gitignored, `chmod 600`) и в лог/вывод не попадают.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = ROOT / ".env.prod"


def outbox_chat_id() -> int:
    """Куда перенаправляются ВСЕ исходящие сообщения бота во время прогона.

    Живой бот отправляет по-настоящему, но реальные клиенты тестовых сообщений
    не получают: `chat_id` переписывается на этот чат. По умолчанию — первый
    `DEV_TELEGRAM_IDS` стенда; перекрывается `E2E_OUTBOX_CHAT_ID`. Захардкоженным
    id тут быть не должно — репозиторий публичный.
    """
    explicit = os.environ.get("E2E_OUTBOX_CHAT_ID")
    if explicit:
        return int(explicit)
    dev_ids = os.environ.get("DEV_TELEGRAM_IDS", "").replace(";", ",").split(",")
    for raw in dev_ids:
        if raw.strip():
            return int(raw.strip())
    raise SystemExit(
        "Не задано, куди слати вихідні прогону: виставте E2E_OUTBOX_CHAT_ID "
        "або DEV_TELEGRAM_IDS у файлі оточення стенда."
    )


def load_stand_env(env_file: Path | str | None = None) -> Path:
    """Поднять окружение стенда в `os.environ` и вернуть путь к файлу."""
    path = Path(env_file) if env_file else DEFAULT_ENV_FILE
    if not path.exists():
        raise SystemExit(
            f"Нет файла окружения {path}. Забери его с прода:\n"
            "  ssh novapostbot 'cat ~/NovaPostBot/.env' > .env.prod && chmod 600 .env.prod"
        )
    load_dotenv(path, override=True)

    # Redis прода живёт во внутренней сети compose (`redis://redis:6379`) и снаружи
    # недоступен. Он обслуживает ТОЛЬКО кэш справочников НП, поэтому подменяем его
    # локальным — на корректность прогона это не влияет, а холодный кэш даже полезен:
    # попадание в кэш пропускает резолв ФОП и сделало бы проверку бутафорией
    # (ровно эта ловушка описана в PROGRESS за 2026-07-15).
    os.environ["REDIS_URL"] = os.environ.get("E2E_REDIS_URL", "redis://localhost:6379/9")

    # Прогон никогда не поллит Telegram: апдейты подаются в диспетчер напрямую.
    # Явно фиксируем это в окружении, чтобы случайный `python -m app.main`
    # из этого же процесса не поднял второй поллер на боевом токене.
    os.environ["E2E_RUN"] = "1"
    return path


def dev_telegram_id() -> int:
    """Dev-аккаунт, от имени которого ходят все персоны (через `/as_user`)."""
    return outbox_chat_id()
