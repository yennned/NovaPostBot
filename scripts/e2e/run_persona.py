"""Запуск одной персоны в отдельном процессе.

`.venv/bin/python -m scripts.e2e.run_persona --name client-owner --telegram-id <dev id>
    --run-id run1 --seed 7 --mode stub --scenario crawl`

Один процесс = одна персона: роутеры `app/bot/handlers` — модульные синглтоны,
второй `build_dispatcher` в том же процессе падает («Router is already attached»).
Побочная выгода — настоящая конкуренция к Postgres/НП/Sheets между персонами.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from scripts.e2e.env import load_stand_env

_env_file = None
if "--env-file" in sys.argv:
    _env_file = sys.argv[sys.argv.index("--env-file") + 1]
    os.environ.setdefault("E2E_REDIS_URL", "redis://localhost:6379/9")
load_stand_env(_env_file)

from scripts.e2e.cascade import run_cascade  # noqa: E402
from scripts.e2e.crawler import Crawler  # noqa: E402
from scripts.e2e.human import Human  # noqa: E402
from scripts.e2e.lib import ARTIFACTS, build_persona  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--telegram-id", type=int, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--mode", default="stub", choices=("stub", "real"))
    parser.add_argument("--scenario", default="crawl", choices=("crawl", "cascade", "both"))
    parser.add_argument("--ttn-budget", type=int, default=0, help="сколько ТТН создаёт эта персона")
    parser.add_argument(
        "--global-limit",
        type=int,
        default=10,
        help="общий на все процессы лимит реальных документов НП; 0 — только вхолостую",
    )
    parser.add_argument(
        "--pace-seconds",
        type=float,
        default=0.0,
        help="мин. интервал между началами ТТН у этой персоны (0 — встык, как раньше)",
    )
    parser.add_argument("--max-taps", type=int, default=200)
    parser.add_argument("--allow-destructive", action="store_true")
    parser.add_argument(
        "--as-user",
        type=int,
        default=None,
        help="стати іншим користувачем через god-mode бота (/as_user <telegram_id>)",
    )
    parser.add_argument("--as-role", default=None, choices=("client", "manager", "owner"))
    parser.add_argument("--env-file", default=None)
    args = parser.parse_args()

    persona, np_client, redis_client = await build_persona(
        name=args.name, telegram_id=args.telegram_id, mode=args.mode, run_id=args.run_id
    )
    human = Human(persona, seed=args.seed)
    summary: dict[str, object] = {"persona": args.name, "telegram_id": args.telegram_id}

    try:
        if args.as_user is not None:
            await persona.become(args.as_user)
            summary["impersonates"] = args.as_user
        elif args.as_role is not None:
            await persona.as_role(args.as_role)
            summary["as_role"] = args.as_role

        if args.scenario in ("crawl", "both"):
            crawler = Crawler(
                persona,
                human=human,
                max_taps=args.max_taps,
                allow_destructive=args.allow_destructive,
            )
            stats = await crawler.crawl_role()
            summary["crawl"] = {
                "presses": stats.presses,
                "taps": stats.taps,
                "visited": len(stats.visited),
                "silent": stats.silent,
                "errors": stats.errors,
                "skipped_destructive": sorted(set(stats.skipped_destructive)),
            }

        if args.scenario in ("cascade", "both") and args.ttn_budget > 0:
            summary["cascade"] = await run_cascade(
                persona,
                human=human,
                budget=args.ttn_budget,
                run_id=args.run_id,
                global_limit=args.global_limit,
                pace_seconds=args.pace_seconds,
            )
    finally:
        summary["steps"] = persona.steps
        summary["defects"] = persona.defects
        if persona.sheets is not None:
            summary["sheets"] = persona.sheets.snapshot()
            persona.sheets.restore()
        path = ARTIFACTS / args.run_id / f"{args.name}.summary.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        persona.close()
        await np_client.aclose()
        await redis_client.aclose()
        await persona.bot.session.close()

    print(json.dumps(summary, ensure_ascii=False, indent=2)[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
