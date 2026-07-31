"""Preflight боевого прогона: доступы, ключи НП, срез «до».

Запуск:  `.venv/bin/python -m scripts.e2e.preflight`

Ничего не меняет — только читает. Секретов не печатает: ключ НП показывается
хвостом из 4 символов, строка подключения не выводится вовсе.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from scripts.e2e.env import ROOT, load_stand_env

load_stand_env()

from app.config import get_settings  # noqa: E402
from app.db.base import get_sessionmaker  # noqa: E402
from app.novaposhta.client import NovaPoshtaClient  # noqa: E402
from app.novaposhta.methods import validate_key_and_get_sender  # noqa: E402
from sqlalchemy import text  # noqa: E402

SNAPSHOT_PATH = ROOT / "scripts" / "e2e" / "artifacts" / "snapshot_before.json"

_SNAPSHOT_QUERIES: dict[str, str] = {
    "users": """
        select id::text, telegram_id, full_name, role::text, status::text,
               phone, permissions::text
        from users order by role, created_at
    """,
    "accounts": """
        select id::text, name, status::text, stock_sheet_key, stock_view_book_id
        from client_accounts order by created_at
    """,
    "memberships": """
        select account_id::text, user_id::text, role::text, status::text
        from client_account_memberships
    """,
    "sender_profiles": """
        select id::text, account_id::text, name, sender_full_name, org_type::text,
               is_default, (np_sender_ref is not null) as has_sender_ref,
               (np_contact_ref is not null) as has_contact_ref,
               (np_sender_warehouse is not null) as has_warehouse
        from sender_profiles order by account_id, name
    """,
    "shipments": """
        select id::text, ttn_number, status::text, account_id::text,
               created_by_user_id::text, created_at::text
        from shipments order by created_at desc
    """,
    "stock_movements": """
        select id::text, account_id::text, sku, movement_type::text, quantity_delta,
               shipment_id::text, created_at::text
        from stock_movements order by created_at desc limit 200
    """,
    "support_threads": "select id::text, status::text, created_at::text from support_threads",
    "counters": """
        select (select count(*) from audit_logs) as audit_rows,
               (select max(created_at) from audit_logs)::text as audit_last,
               (select count(*) from shipments) as shipments,
               (select count(*) from stock_movements) as movements
    """,
}


async def _snapshot() -> dict[str, Any]:
    sessionmaker = get_sessionmaker()
    out: dict[str, Any] = {}
    async with sessionmaker() as session:
        for name, sql in _SNAPSHOT_QUERIES.items():
            rows = (await session.execute(text(sql))).mappings().all()
            out[name] = [dict(r) for r in rows]
    return out


async def _check_np_keys(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Проверить ключ каждого ФОП живым вызовом НП.

    Ключ расшифровывается ORM-моделью прозрачно (`EncryptedString`), поэтому
    здесь достаточно прочитать профиль через сессию.
    """
    from app.db.models import SenderProfile
    from sqlalchemy import select

    results: list[dict[str, Any]] = []
    client = NovaPoshtaClient()
    sessionmaker = get_sessionmaker()
    try:
        async with sessionmaker() as session:
            profiles = (await session.execute(select(SenderProfile))).scalars().all()
            for profile in profiles:
                entry: dict[str, Any] = {
                    "profile_id": str(profile.id),
                    "name": profile.name,
                    "account_id": str(profile.account_id),
                    "key_tail": (profile.np_api_key or "")[-4:],
                }
                try:
                    validation = await validate_key_and_get_sender(
                        client, api_key=profile.np_api_key
                    )
                    entry["ok"] = True
                    entry["counterparty_ref"] = validation.counterparty_ref
                    entry["contact_ref"] = validation.contact_ref
                    entry["ref_matches_profile"] = (
                        validation.counterparty_ref == profile.np_sender_ref
                    )
                except Exception as exc:
                    entry["ok"] = False
                    entry["error"] = f"{type(exc).__name__}: {exc}"
                results.append(entry)
    finally:
        await client.aclose()
    return results


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preflight боевого E2E-прогона")
    parser.add_argument("--skip-np", action="store_true", help="не дёргать НП (только срез БД)")
    parser.add_argument(
        "--force", action="store_true", help="перезаписать эталонный срез «до» (осознанно)"
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    print(f"environment      : {settings.environment}")
    print(f"inventory source : {settings.inventory_source}")
    print(f"stock book       : {settings.sheets_stock_book_id}")
    print(f"dev ids          : {settings.dev_telegram_ids}")
    print(f"owner ids        : {settings.owner_telegram_ids}")
    print(f"bot token        : {'задан' if settings.bot_token else 'ПУСТОЙ'}")

    snapshot = await _snapshot()
    print("\n— срез «до» —")
    for name, rows in snapshot.items():
        print(f"  {name:16} {len(rows)}")

    if not args.skip_np:
        print("\n— проверка ключей НП —")
        snapshot["np_keys"] = await _check_np_keys(snapshot)
        for entry in snapshot["np_keys"]:
            status = "OK " if entry.get("ok") else "FAIL"
            extra = (
                f"ref={entry.get('counterparty_ref')} matches={entry.get('ref_matches_profile')}"
                if entry.get("ok")
                else entry.get("error")
            )
            print(f"  [{status}] {entry['name']:34} ...{entry['key_tail']}  {extra}")

    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(snapshot, ensure_ascii=False, indent=2)

    # Срез «до» — ЭТАЛОН: по нему валидатор считает «что появилось», а
    # `restore_names.py` возвращает затёртые значения. Повторный preflight
    # посреди прогона перезаписал бы эталон уже испорченными данными, и
    # восстановление откатило бы починку. Поэтому baseline пишется один раз;
    # повторные срезы ложатся рядом.
    if SNAPSHOT_PATH.exists() and not args.force:
        alt = SNAPSHOT_PATH.with_name(f"snapshot_{int(SNAPSHOT_PATH.stat().st_mtime)}.json")
        alt.write_text(payload)
        print(f"\n⚠️  эталон {SNAPSHOT_PATH.name} уже есть — НЕ переписан.")
        print(f"    текущий срез: {alt.relative_to(ROOT)}")
        print("    перезаписать эталон осознанно: --force")
        return 0

    SNAPSHOT_PATH.write_text(payload)
    print(f"\nэталонный срез сохранён: {SNAPSHOT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
