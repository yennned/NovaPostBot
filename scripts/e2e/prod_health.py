"""Пост-деплой проверка: боевой бот реально поллит Telegram.

Зачем отдельный скрипт. `deploy` в CI зелёный уже тогда, когда `docker compose up`
вернул управление, а снимок `docker compose ps` он делает через секунду после
старта — «Up Less than a second» не отличает поднявшийся контейнер от того, что
упадёт на первом же обращении к БД. Нужен признак живости *снаружи*.

Приём и **направление конфликта**. Соблазнительно послать короткий `getUpdates` и
считать 409 признаком живого бота — но это работает наоборот и даёт ложный
«труп»: при конкуренции за токен Telegram обрывает того, кто поллил *раньше*, и
обслуживает новый запрос. Короткий пробник поэтому получает `200 OK` всегда,
независимо от состояния прода.

Поэтому пробником становимся мы: открываем **длинный** `getUpdates` и ждём. Живой
боевой процесс в пределах своего цикла (aiogram поллит с `timeout=30`) придёт за
апдейтами и вытеснит уже нас — прилетит `409`. Так что здесь **409 = бот жив**, а
тихо отработавший до конца длинный поллинг = поллить некому.

Побочных эффектов нет: `offset` мы не передаём, значит ничего не подтверждаем и
не удаляем из очереди. Вытесненный на один цикл боевой поллинг не ломается —
aiogram ловит любую ошибку `getUpdates` и продолжает с backoff
(`aiogram/dispatcher/dispatcher.py`, `_listen_updates`).
"""

from __future__ import annotations

import asyncio
import os
import sys

import httpx
from scripts.e2e.env import load_stand_env

# Ждём дольше, чем цикл боевого поллера (aiogram: timeout=30), иначе живой бот
# просто не успеет прийти за апдейтами и вытеснить нас — получим ложный «труп».
_POLL_TIMEOUT_SECONDS = 40
_HTTP_TIMEOUT_SECONDS = float(_POLL_TIMEOUT_SECONDS + 20)

#: Коды возврата. Отделять «не знаю» от «лежит» здесь принципиально: пробник
#: обязан молчать о том, чего не проверил, иначе собственный сбой сети выглядит
#: как авария прода и провоцирует чинить работающее.
_ALIVE, _DOWN, _UNKNOWN = 0, 1, 2


async def _probe(token: str) -> int:
    base = f"https://api.telegram.org/bot{token}"
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as http:
            me = await http.get(f"{base}/getMe")
            me.raise_for_status()
            username = me.json()["result"]["username"]
            print(f"getMe: @{username}")

            hook = await http.get(f"{base}/getWebhookInfo")
            hook.raise_for_status()
            info = hook.json()["result"]
            webhook_url = info.get("url") or ""
            pending = info.get("pending_update_count", 0)
            print(f"webhook: {webhook_url or '(нет, режим polling)'}, в очереди {pending}")

            # 409 у Telegram означает не только «занят другим поллером», но и
            # «включён вебхук — getUpdates недоступен». В режиме вебхука бот вообще
            # не поллит, и наш признак живости неприменим: 409 пришёл бы и на
            # погашенном контейнере, дав ложное «жив». Поэтому не гадаем, а
            # честно отвечаем «не знаю» — направление ошибки здесь важнее охвата.
            if webhook_url:
                print("бот переведён на вебхук — проверка поллингом неприменима ?")
                return _UNKNOWN

            print(f"держим getUpdates {_POLL_TIMEOUT_SECONDS}с — ждём вытеснения...")
            updates = await http.get(
                f"{base}/getUpdates", params={"timeout": _POLL_TIMEOUT_SECONDS, "limit": 1}
            )
    except httpx.HTTPError as exc:
        # Сбой связи/таймаут — это отказ ПРОБНИКА, а не приговор проду. Без этой
        # ветки исключение вылетало наружу, интерпретатор завершался кодом 1 — тем
        # самым, которым мы обозначаем «бот не работает».
        print(f"пробник не смог доспросить Telegram ({type(exc).__name__}: {exc}) ?")
        return _UNKNOWN
    except (KeyError, ValueError) as exc:
        print(f"неожиданный формат ответа Telegram ({type(exc).__name__}: {exc}) ?")
        return _UNKNOWN

    if updates.status_code == 409:
        print("409 Conflict — нас вытеснил другой поллер: боевой процесс ЖИВ ✔")
        if pending > 5:
            print(f"  ⚠ в очереди {pending} апдейтов — бот поллит, но может отставать")
        return _ALIVE

    if updates.is_success:
        print("длинный поллинг отработал без конфликта — поллить некому, бот НЕ работает ✘")
        return _DOWN

    print(f"getUpdates: неожиданный ответ {updates.status_code} — {updates.text[:200]} ?")
    return _UNKNOWN


def main() -> int:
    load_stand_env()
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        print("BOT_TOKEN пуст — нечего проверять (см. .env.prod)")
        return _UNKNOWN
    return asyncio.run(_probe(token))


if __name__ == "__main__":
    sys.exit(main())
