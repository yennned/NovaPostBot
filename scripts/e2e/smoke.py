"""Смок харнесса: одна персона, только чтение экранов.

`.venv/bin/python -m scripts.e2e.smoke --telegram-id <telegram_id> --name client-owner`

Ничего не создаёт и не меняет: заходит в `/start`, обходит кнопки нижней панели
клиента и печатает, что бот реально нарисовал бы. Нужен, чтобы поймать поломку
самого харнесса до того, как за него сядут девять персон.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from scripts.e2e.env import load_stand_env

# `--env-file` разбираем до загрузки окружения: `app.config` кеширует настройки,
# подменить их позже уже нельзя.
_env_file = None
if "--env-file" in sys.argv:
    _env_file = sys.argv[sys.argv.index("--env-file") + 1]
    os.environ.setdefault("E2E_REDIS_URL", "redis://localhost:6379/9")
load_stand_env(_env_file)

from scripts.e2e.lib import build_persona  # noqa: E402

CLIENT_TOUR = ["📦 Товари", "📬 Відправлення", "📊 Статистика", "⚙️ Налаштування"]


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--telegram-id", type=int, required=True)
    parser.add_argument("--name", default="smoke")
    parser.add_argument("--run-id", default="smoke")
    parser.add_argument("--mode", default="stub", choices=("stub", "real"))
    parser.add_argument(
        "--env-file", default=None, help="окружение стенда (по умолчанию .env.prod)"
    )
    args = parser.parse_args()

    persona, np_client, redis_client = await build_persona(
        name=args.name, telegram_id=args.telegram_id, mode=args.mode, run_id=args.run_id
    )
    try:
        entry = await persona.send("/start")
        print(f"/start → {entry.get('total_ms')} ms")
        print("  текст:", (persona.screen.text or "")[:200].replace("\n", " | "))
        print("  нижня панель:", [b.text for b in persona.screen.reply])

        for label in CLIENT_TOUR:
            button = persona.screen.find_reply(label[:6])
            if button is None:
                print(f"\n[!] кнопки {label!r} немає в меню — пропускаю")
                continue
            entry = await persona.send(button.text)
            silent = " МОВЧАННЯ" if entry.get("silent") else ""
            err = f" ERROR:{entry['error_screen']}" if entry.get("error_screen") else ""
            print(f"\n{button.text} → {entry.get('total_ms')} ms{silent}{err}")
            print("  текст:", (persona.screen.text or "")[:220].replace("\n", " | "))
            print("  кнопки:", persona.screen.buttons[:12])

        print(f"\nкроків: {persona.steps}, дефектів: {len(persona.defects)}")
        for defect in persona.defects:
            print("  ", defect)
    finally:
        persona.close()
        await np_client.aclose()
        await redis_client.aclose()
        await persona.bot.session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
