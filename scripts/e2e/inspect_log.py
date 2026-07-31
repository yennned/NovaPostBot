"""Просмотр JSONL-лога персоны: что бот реально отправил на каждый шаг.

Валидатор даёт агрегат; здесь — покадровый разбор конкретного места, когда надо
понять, ПОЧЕМУ шаг не сработал.

`.venv/bin/python -m scripts.e2e.inspect_log --run-id cascade1 --persona worker-veronika \
    [--grep cab:ttn:send] [--tail 30]`
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--persona", required=True)
    parser.add_argument(
        "--grep", default=None, help="показать только шаги с этой подстрокой в target"
    )
    parser.add_argument("--tail", type=int, default=0, help="показать последние N шагов")
    parser.add_argument("--full", action="store_true", help="печатать все исходящие целиком")
    args = parser.parse_args()

    path = ROOT / "scripts" / "e2e" / "artifacts" / args.run_id / f"{args.persona}.jsonl"
    rows = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]

    selected = list(enumerate(rows))
    if args.grep:
        selected = [(i, r) for i, r in selected if args.grep in str(r.get("target"))]
    if args.tail:
        selected = selected[-args.tail :]

    for index, row in selected:
        flags = []
        if row.get("silent"):
            flags.append("МОВЧАННЯ")
        if row.get("missing"):
            flags.append("НЕМА КНОПКИ")
        if row.get("exception"):
            flags.append(f"EXC {row['exception']}")
        if row.get("error_screen"):
            flags.append(f"ERR {row['error_screen']}")
        head = f"#{index:3} {row.get('action'):10} {str(row.get('target'))[:60]}"
        print(f"{head}  {row.get('total_ms')}ms  {' '.join(flags)}")
        for call in row.get("outgoing", []):
            text = str(call.get("text") or "").replace("\n", " | ")
            limit = 100000 if args.full else 200
            print(f"      → {call.get('method')}: {text[:limit]}")
            if call.get("transport_error"):
                print(f"        transport_error: {call['transport_error']}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
