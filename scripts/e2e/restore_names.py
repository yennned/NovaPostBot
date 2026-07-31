"""Откат данных, затёртых обходом: `users.full_name` и поля ФОП.

История: первая версия краулера вводила мусор в любой экран, ожидающий текста, —
включая «⚙️ Налаштування → ✏️ Змінити ПІБ» и правку полей ФОП. На боевом стенде
это переименовало реальных клиентов и затёрло `name`/`sender_full_name`/`edrpou`
у части ФОП. Ключи НП, `np_*_ref`, телефоны и `is_default` не пострадали.

Причина закрыта в `crawler.py`: свободный текст вводится **только** в поисковые
поля (`SAFE_TEXT_INPUT`), правки полей — в `DESTRUCTIVE`.

Эталон берётся из среза «до» (`artifacts/snapshot_before.json`, снимается
`preflight.py`), а не из констант в коде: репозиторий публичный, ФИО и
telegram_id реальных людей в git попадать не должны.

`.venv/bin/python -m scripts.e2e.restore_names --snapshot <файл> [--apply]`
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from scripts.e2e.env import ROOT, load_stand_env

load_stand_env()

from app.db.base import get_sessionmaker  # noqa: E402
from sqlalchemy import text  # noqa: E402

DEFAULT_SNAPSHOT = ROOT / "scripts" / "e2e" / "artifacts" / "snapshot_before.json"


def _load(snapshot: Path) -> tuple[dict[str, str], dict[str, tuple[str, str, str | None]]]:
    if not snapshot.exists():
        raise SystemExit(
            f"немає среза «до»: {snapshot}\n"
            "Знімається `.venv/bin/python -m scripts.e2e.preflight` ПЕРЕД прогоном."
        )
    data: dict[str, Any] = json.loads(snapshot.read_text(encoding="utf-8"))
    names = {
        row["id"]: row["full_name"]
        for row in data.get("users", [])
        if row.get("role") == "client" and row.get("full_name")
    }
    profiles = {
        row["id"]: (row["name"], row.get("sender_full_name"), row.get("edrpou"))
        for row in data.get("sender_profiles", [])
        if row.get("name")
    }
    return names, profiles


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--apply", action="store_true", help="без флага — только показать")
    args = parser.parse_args()

    names, profiles = _load(args.snapshot)
    changed = 0
    sessionmaker = get_sessionmaker()

    async with sessionmaker() as session:
        print("— клієнти —")
        rows = (
            (
                await session.execute(
                    text("select id::text as id, full_name from users where id = any(:ids)"),
                    {"ids": list(names)},
                )
            )
            .mappings()
            .all()
        )
        for row in rows:
            expected = names[row["id"]]
            if row["full_name"] == expected:
                continue
            print(f"  FIX  {row['full_name']!r} → {expected!r}")
            changed += 1
            if args.apply:
                await session.execute(
                    text("update users set full_name = :name, updated_at = now() where id = :id"),
                    {"name": expected, "id": row["id"]},
                )

        print("— ФОП —")
        rows = (
            (
                await session.execute(
                    text(
                        "select id::text as id, name, sender_full_name, edrpou "
                        "from sender_profiles where id = any(:ids)"
                    ),
                    {"ids": list(profiles)},
                )
            )
            .mappings()
            .all()
        )
        for row in rows:
            expected = profiles[row["id"]]
            current = (row["name"], row["sender_full_name"], row["edrpou"])
            if current == expected:
                continue
            print(f"  FIX  {current} → {expected}")
            changed += 1
            if args.apply:
                await session.execute(
                    text(
                        "update sender_profiles set name = :name, sender_full_name = :fio, "
                        "edrpou = :edrpou, updated_at = now() where id = :id"
                    ),
                    {
                        "name": expected[0],
                        "fio": expected[1],
                        "edrpou": expected[2],
                        "id": row["id"],
                    },
                )

        if args.apply:
            await session.commit()
            print(f"\nвідновлено записів: {changed}")
        else:
            print(f"\nрозбіжностей: {changed} (застосувати — з --apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
