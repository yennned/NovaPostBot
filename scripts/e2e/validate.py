"""Единый валидатор прогона: один отчёт по всем персонам сразу.

Персоны отдают **сырые логи** и вердиктов не выносят — иначе каждая мерила бы
успех по-своему, а «зелёный» отчёт означал бы лишь, что сценарий дошёл до конца.
Вердикт выносится здесь, из трёх независимых источников: логи харнесса, состояние
БД «до/после» и сама Нова Пошта.

`.venv/bin/python -m scripts.e2e.validate --run-id run1 [--cleanup] [--keep 2]`
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
from pathlib import Path
from typing import Any

from scripts.e2e.env import ROOT, load_stand_env

# Окружение стенда поднимаем ТОЛЬКО при запуске скриптом. `load_stand_env` делает
# `load_dotenv(override=True)` по `.env.prod`, то есть импорт этого модуля молча
# переводил бы весь процесс на боевую БД и боевой токен. Для CLI это и нужно, а
# вот тесту, которому нужна одна чистая функция отсюда, — категорически нет:
# `get_settings.cache_clear()` в любом последующем тесте подхватил бы прод.
# Поймано полным прогоном: два теста разъехались по чужому `np_sender_warehouse_ref`.
# Так же устроен `prod_health.py` — он поднимает окружение внутри своей функции.
if __name__ == "__main__":
    _env_file = None
    if "--env-file" in sys.argv:
        _env_file = sys.argv[sys.argv.index("--env-file") + 1]
        os.environ.setdefault("E2E_REDIS_URL", "redis://localhost:6379/9")
    load_stand_env(_env_file)

# Ниже блока выше намеренно: настройки кешированы (`lru_cache`), и при запуске
# скриптом окружение стенда обязано лечь в `os.environ` до первого их чтения.
from app.db.base import get_sessionmaker
from scripts.e2e.lib import ARTIFACTS, SUBMIT_FAILED_MARKER
from sqlalchemy import text

SNAPSHOT_BEFORE = ROOT / "scripts" / "e2e" / "artifacts" / "snapshot_before.json"


# --------------------------------------------------------------------------- #
# Сбор логов
# --------------------------------------------------------------------------- #
def _load_logs(run_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    steps: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    for path in sorted(run_dir.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                steps.append(json.loads(line))
    for path in sorted(run_dir.glob("*.summary.json")):
        summaries.append(json.loads(path.read_text(encoding="utf-8")))
    return steps, summaries


def _percentiles(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)

    def pct(q: float) -> float:
        idx = min(len(ordered) - 1, round(q * (len(ordered) - 1)))
        return round(ordered[idx], 1)

    return {
        "n": len(ordered),
        "p50": pct(0.5),
        "p95": pct(0.95),
        "max": round(ordered[-1], 1),
        "mean": round(statistics.fmean(ordered), 1),
    }


# --------------------------------------------------------------------------- #
# Состояние БД после прогона
# --------------------------------------------------------------------------- #
_AFTER_QUERIES = {
    # `insured_amount` в срезе не для полноты: это база страхового возмещения НП,
    # и её занижение (тем более ноль) по отчёту иначе не увидеть.
    "shipments": """
        select id::text, ttn_number, np_ref, sender_profile_id::text, status::text,
               account_id::text, created_by_user_id::text, recipient_city,
               insured_amount::text, created_at::text
        from shipments order by created_at desc
    """,
    "items": """
        select si.shipment_id::text, si.sku, si.quantity, s.status::text
        from shipment_items si join shipments s on s.id = si.shipment_id
    """,
    "movements": """
        select id::text, account_id::text, sku, movement_type::text, quantity_delta,
               shipment_id::text, created_at::text
        from stock_movements order by created_at desc
    """,
    "users": "select id::text, telegram_id, full_name, role::text, status::text from users",
    "memberships": """
        select account_id::text, user_id::text, role::text, status::text
        from client_account_memberships
    """,
    "audit_recent": """
        select action, count(*) as n from audit_logs
        where created_at > now() - interval '6 hours' group by action order by 2 desc
    """,
}


async def _state_after() -> dict[str, Any]:
    out: dict[str, Any] = {}
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        for name, sql in _AFTER_QUERIES.items():
            rows = (await session.execute(text(sql))).mappings().all()
            out[name] = [dict(r) for r in rows]
    return out


# --------------------------------------------------------------------------- #
# Инварианты
# --------------------------------------------------------------------------- #
OPEN_STATUSES = {"created", "confirmed", "dispatched", "in_transit"}


def _cascade_attempts(summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Все попытки создать ТТН из сводок каскада — и удачные, и нет."""
    attempts: list[dict[str, Any]] = []
    for summary in summaries:
        cascade = summary.get("cascade") or {}
        for key in ("created", "dry_runs"):
            rows = cascade.get(key)
            if isinstance(rows, list):
                attempts.extend(row | {"persona": summary.get("persona", "?")} for row in rows)
    return attempts


def _check_throughput(summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Доля дошедших до документа — со знаменателем.

    Прежний вердикт смотрел только на список находок, а находки рождались из
    экранов ошибки. Прогон, где клиент раз за разом проходит всю форму и не
    получает ТТН, давал 🟢 ЧИСТО: 2026-08-03 так и вышло — 43 документа из 60
    попыток, отчёт чистый. Знаменатель здесь ровно для того, чтобы «сколько
    отказов» нельзя было не заметить.
    """
    attempts = _cascade_attempts(summaries)
    if not attempts:
        return []
    findings: list[dict[str, Any]] = []
    submitted = [a for a in attempts if a.get("submitted")]
    if not submitted:
        return [
            {
                "severity": "high",
                "rule": "ttn_created",
                "detail": (
                    f"жодної ТТН не створено за {len(attempts)} спроб — "
                    "решта метрик звіту нічого не означає"
                ),
            }
        ]
    reasons: dict[str, int] = {}
    for attempt in attempts:
        if attempt.get("submitted"):
            continue
        reason = attempt.get("reject_reason") or f"обірвано на кроці «{attempt.get('failed_at')}»"
        reasons[reason] = reasons.get(reason, 0) + 1
    for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
        findings.append(
            {
                "severity": "high" if SUBMIT_FAILED_MARKER in reason else "medium",
                "rule": "submit_rejected",
                "detail": f"{count} з {len(attempts)} спроб без ТТН: {reason}",
            }
        )
    return findings


def collect_findings(
    steps: list[dict[str, Any]],
    before: dict[str, Any],
    after: dict[str, Any],
    summaries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Все проверки прогона в одном месте.

    Собрано функцией, а не выражением в `main`, ради проверяемости самой
    разводки: выпавшую из цепочки проверку иначе не отличить от проверки,
    которой нечего сказать, — а именно так отчёт и становится ложно-зелёным.
    """
    return _check_logs(steps) + _check_invariants(before, after) + _check_throughput(summaries)


def _check_invariants(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []

    before_ids = {row["id"] for row in before.get("shipments", [])}
    new_shipments = [row for row in after["shipments"] if row["id"] not in before_ids]

    # 1. У каждой созданной ТТН должен быть номер НП.
    for row in new_shipments:
        if not row.get("ttn_number"):
            findings.append(
                {
                    "severity": "high",
                    "rule": "ttn_number_present",
                    "detail": f"відправлення {row['id']} без номера ТТН (status={row['status']})",
                }
            )

    # 2. Дублей номеров быть не должно (двойной тап «Відправити»).
    numbers = [row["ttn_number"] for row in after["shipments"] if row.get("ttn_number")]
    duplicates = {n for n in numbers if numbers.count(n) > 1}
    for number in duplicates:
        findings.append(
            {
                "severity": "high",
                "rule": "no_duplicate_ttn",
                "detail": f"номер {number} зустрічається {numbers.count(number)} разів",
            }
        )

    # 3. Резерв снят у всех закрытых отправлений.
    open_ids = {row["id"] for row in after["shipments"] if row["status"] in OPEN_STATUSES}
    closed_ids = {row["id"] for row in after["shipments"]} - open_ids
    reserved_by_shipment: dict[str, int] = {}
    for mv in after["movements"]:
        if mv.get("shipment_id"):
            reserved_by_shipment[mv["shipment_id"]] = reserved_by_shipment.get(
                mv["shipment_id"], 0
            ) + (mv["quantity_delta"] or 0)
    for shipment_id in closed_ids:
        balance = reserved_by_shipment.get(shipment_id)
        if balance is not None and balance != 0:
            findings.append(
                {
                    "severity": "high",
                    "rule": "reservation_released",
                    "detail": f"закрите відправлення {shipment_id} тримає резерв {balance}",
                }
            )

    # 4. Никто лишний не появился в БД.
    before_users = {row["id"] for row in before.get("users", [])}
    for row in after["users"]:
        if row["id"] not in before_users:
            findings.append(
                {
                    "severity": "info",
                    "rule": "new_user",
                    "detail": f"зʼявився користувач {row['full_name']} ({row['role']}) — прибрати в уборці",
                }
            )

    return findings


def _check_logs(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for step in steps:
        base = f"{step.get('persona')}: {step.get('action')} {step.get('target')}"
        if step.get("silent"):
            findings.append(
                {
                    "severity": "high",
                    "rule": "no_silence",
                    "detail": f"{base} — бот не відповів нічим (ознака падіння в хендлері)",
                }
            )
        if step.get("exception"):
            findings.append(
                {
                    "severity": "high",
                    "rule": "no_exception",
                    "detail": f"{base} — {step['exception']}",
                }
            )
        if step.get("error_screen"):
            findings.append(
                {
                    "severity": "medium",
                    "rule": "no_error_screen",
                    "detail": f"{base} — екран помилки «{step['error_screen']}»",
                }
            )
        if step.get("missing"):
            findings.append(
                {
                    "severity": "medium",
                    "rule": "button_present",
                    "detail": f"{base} — кнопки немає на екрані (було: {step.get('available')})",
                }
            )
        for call in step.get("outgoing", []):
            if call.get("transport_error"):
                findings.append(
                    {
                        "severity": "medium",
                        "rule": "telegram_transport",
                        "detail": f"{base} — {call['transport_error']}",
                    }
                )
    return findings


# --------------------------------------------------------------------------- #
# Сверка с Новой Поштой + уборка
# --------------------------------------------------------------------------- #
async def _cancel_in_db_only(shipment_id: Any) -> None:
    """Закрыть отправление и вернуть резерв БЕЗ обращения к НП.

    Только для починки рассогласования «в НП документа уже нет, в БД он
    `confirmed`». Идёт теми же доменными функциями, что и штатная отмена
    (`shipments.apply_cancel` + `record_for_items`), поэтому история движений
    остаётся правдивой; пропускается ровно шаг `InternetDocument.delete`.
    """
    import uuid as uuid_module

    from app.db.models import Shipment, User
    from app.db.models.enums import StockMovementType
    from app.db.repositories import StockMovementRepository
    from app.services import shipments
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = (
            await session.execute(
                select(Shipment)
                .options(selectinload(Shipment.items))
                .where(Shipment.id == uuid_module.UUID(str(shipment_id)))
            )
        ).scalar_one()
        owner = (await session.execute(select(User).where(User.id == row.client_id))).scalar_one()

        # Уборка идёт по «новым с прошлого среза», а срез живёт неделями — в выборку
        # попадают и ТТН, отменённые прошлыми прогонами. Второй `ttn_cancel` завысил
        # бы журнал ровно так же, как его отсутствие занижало, и ни одна проверка
        # этого не увидела бы: бронь не физический тип, инвариант остатка молчит.
        from app.db.repositories import ShipmentRepository

        if await ShipmentRepository(session).movement_exists(row.id, StockMovementType.ttn_cancel):
            return

        await shipments.apply_cancel(
            session, shipment=row, account_id=row.account_id, actor_user_id=owner.id
        )
        await StockMovementRepository(session).record_for_items(
            client_id=owner.id,
            account_id=row.account_id,
            shipment_id=row.id,
            items=row.items,
            movement_type=StockMovementType.ttn_cancel,
            sign=1,
            comment=f"E2E-прибирання: ТТН {row.ttn_number or '—'} вже видалено в НП",
        )
        await session.commit()


async def _np_reconcile(
    new_shipments: list[dict[str, Any]], *, cleanup: bool, keep: int
) -> dict[str, Any]:
    """Сверить созданные ТТН с НП и убрать лишние.

    Ключ берём у ФОП **того самого** отправления, а не «первый попавшийся»: в
    аккаунте несколько ФОП с разными ключами, и чужим ключом НП документ не
    отдаст и не удалит.

    Убираем **штатной отменой** (`cancel_shipment_np_first`), а не голым
    `InternetDocument.delete`. Голое удаление стирает документ в НП, но
    оставляет строку `shipments` в статусе `confirmed` и её резерв в
    `stock_movements` — то есть остаток клиента остаётся занят под отправление,
    которого уже нет. Штатный путь удаляет в НП, флипает статус и возвращает
    резерв одной транзакцией, а «уже удалено» (`NovaPoshtaNotFound`) считает
    идемпотентным успехом — поэтому повторный запуск уборки безопасен.
    """
    import uuid as uuid_module

    from app.db.models import SenderProfile, Shipment, User
    from app.novaposhta.client import NovaPoshtaClient
    from app.novaposhta.methods import get_status_documents
    from app.services.shipment import cancel_shipment_np_first
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload

    report: dict[str, Any] = {"checked": [], "deleted": [], "kept": [], "errors": []}
    tracked = [s for s in new_shipments if s.get("ttn_number")]
    if not tracked:
        return report

    client = NovaPoshtaClient()
    sessionmaker = get_sessionmaker()
    try:
        async with sessionmaker() as session:
            profiles = (await session.execute(select(SenderProfile))).scalars().all()
            keys = {str(p.id): p.np_api_key for p in profiles}

        to_keep = {s["ttn_number"] for s in tracked[:keep]}
        report["kept"] = sorted(to_keep)

        for shipment in tracked:
            number = shipment["ttn_number"]
            api_key = keys.get(str(shipment.get("sender_profile_id")))
            if not api_key:
                report["errors"].append(f"{number}: не знайдено ключ ФОП відправлення")
                continue
            try:
                statuses = await get_status_documents(client, api_key=api_key, numbers=[number])
                report["checked"].append({"number": number, "status": str(statuses)[:200]})
            except Exception as exc:
                report["errors"].append(f"{number}: статус — {type(exc).__name__}: {exc}")

            if not cleanup or number in to_keep:
                continue
            try:
                async with sessionmaker() as session:
                    # joinedload обязателен: `cancel_shipment_np_first` читает
                    # `shipment.items` и `shipment.account`, а ленивая загрузка в
                    # asyncio падает `MissingGreenlet` — уборка молча не работала,
                    # и созданные прогоном ТТН оставались висеть в НП с резервом.
                    row = (
                        (
                            await session.execute(
                                select(Shipment)
                                .where(Shipment.id == uuid_module.UUID(shipment["id"]))
                                .options(
                                    joinedload(Shipment.items),
                                    joinedload(Shipment.account),
                                    joinedload(Shipment.sender_profile),
                                    joinedload(Shipment.client),
                                )
                            )
                        )
                        .unique()
                        .scalar_one()
                    )
                    owner = (
                        await session.execute(select(User).where(User.id == row.client_id))
                    ).scalar_one()
                    await cancel_shipment_np_first(
                        session,
                        shipment=row,
                        client=owner,
                        np_client=client,
                        account_id=row.account_id,
                        actor_user_id=owner.id,
                        sync=False,
                    )
                    await session.commit()
                report["deleted"].append(number)
            except Exception as exc:
                # Документ уже удалён в НП, а строка в БД жива: НП отвечает
                # «No document changed DeletionMark», клиент поднимает
                # `NovaPoshtaError` → `TtnCancelFailed`, и штатная отмена
                # становится невозможной НАВСЕГДА — резерв висит вечно.
                # Для уборки добиваем состояние в БД напрямую и помечаем это в
                # отчёте: это не рядовой шаг, а починка рассогласования.
                if "DeletionMark" in str(exc) or "getting payment info" in str(exc):
                    try:
                        await _cancel_in_db_only(uuid_module.UUID(shipment["id"]))
                        report["deleted"].append(f"{number} (тільки в БД — у НП вже видалено)")
                    except Exception as inner:
                        report["errors"].append(
                            f"{number}: добивання в БД — {type(inner).__name__}: {inner}"
                        )
                else:
                    report["errors"].append(f"{number}: скасування — {type(exc).__name__}: {exc}")
    finally:
        await client.aclose()
    return report


# --------------------------------------------------------------------------- #
# Отчёт
# --------------------------------------------------------------------------- #
def _render(
    run_id: str,
    steps: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    np_report: dict[str, Any],
    new_shipments: list[dict[str, Any]],
) -> str:
    by_persona: dict[str, list[dict[str, Any]]] = {}
    for step in steps:
        by_persona.setdefault(step.get("persona", "?"), []).append(step)

    high = [f for f in findings if f["severity"] == "high"]
    medium = [f for f in findings if f["severity"] == "medium"]
    info = [f for f in findings if f["severity"] == "info"]

    lines: list[str] = []
    lines.append(f"# E2E-прогон `{run_id}` — підсумок\n")
    verdict = "🟢 ЧИСТО" if not high else f"🔴 {len(high)} критичних"
    lines.append(f"**Вердикт:** {verdict} · {len(medium)} середніх · {len(info)} інформаційних\n")
    lines.append(
        f"Кроків усього: **{len(steps)}** · персон: **{len(by_persona)}** · "
        f"нових відправлень: **{len(new_shipments)}**\n"
    )
    # Пропускная способность — в шапке, а не в глубине отчёта: без знаменателя
    # «нових відправлень: 43» читается как успех, хотя попыток было 60.
    attempts = _cascade_attempts(summaries)
    if attempts:
        done = sum(1 for a in attempts if a.get("submitted"))
        lines.append(
            f"Дійшло до документа: **{done} з {len(attempts)}** "
            f"({done / len(attempts):.0%}) спроб створити ТТН\n"
        )

    lines.append("\n## Персони\n")
    lines.append("| Персона | Кроків | Мовчань | Винятків | Екранів помилки |")
    lines.append("|---|---:|---:|---:|---:|")
    for name, entries in sorted(by_persona.items()):
        lines.append(
            f"| {name} | {len(entries)} "
            f"| {sum(1 for e in entries if e.get('silent'))} "
            f"| {sum(1 for e in entries if e.get('exception'))} "
            f"| {sum(1 for e in entries if e.get('error_screen'))} |"
        )

    lines.append("\n## Продуктивність (мс)\n")
    lines.append("| Зріз | n | p50 | p95 | max | mean |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    slices = {
        "усі кроки (наш код)": [s["app_ms"] for s in steps if "app_ms" in s],
        "усі кроки (разом з Telegram)": [s["total_ms"] for s in steps if "total_ms" in s],
    }
    for action in ("type", "tap", "contact"):
        slices[f"{action}"] = [
            s["app_ms"] for s in steps if s.get("action") == action and "app_ms" in s
        ]
    submit = [
        s["app_ms"] for s in steps if "cab:ttn:send" in str(s.get("target")) and "app_ms" in s
    ]
    if submit:
        slices["створення ТТН (InternetDocument.save)"] = submit
    for label, values in slices.items():
        stats = _percentiles(values)
        if stats:
            lines.append(
                f"| {label} | {stats['n']} | {stats['p50']} | {stats['p95']} "
                f"| {stats['max']} | {stats['mean']} |"
            )

    lines.append("\n## Знахідки\n")
    if not findings:
        lines.append("Порушень не знайдено.\n")
    for group, title in ((high, "Критичні"), (medium, "Середні"), (info, "Інформаційні")):
        if not group:
            continue
        lines.append(f"\n### {title}\n")
        for finding in group:
            lines.append(f"- **{finding['rule']}** — {finding['detail']}")

    lines.append("\n## Нова Пошта\n")
    lines.append(f"- перевірено номерів: {len(np_report.get('checked', []))}")
    lines.append(f"- залишено на перевірку: {', '.join(np_report.get('kept') or []) or '—'}")
    lines.append(f"- видалено: {', '.join(np_report.get('deleted') or []) or '—'}")
    for error in np_report.get("errors", []):
        lines.append(f"- ⚠️ {error}")

    if new_shipments:
        lines.append("\n## Створені відправлення\n")
        lines.append("| ТТН | Статус | Оголошена вартість | Створив |")
        lines.append("|---|---|---:|---|")
        for row in new_shipments:
            insured = row.get("insured_amount")
            # Ноль тут — не косметика: НП возмещает в пределах оголошеної вартості.
            insured_cell = "⚠️ 0" if insured in (None, "0", "0.00") else insured
            lines.append(
                f"| {row.get('ttn_number') or '—'} | {row['status']} | {insured_cell} | "
                f"{row.get('created_by_user_id')} |"
            )

    lines.append("\n---\n")
    lines.append(
        "_Звіт зібрано `scripts/e2e/validate.py` з трьох незалежних джерел: логи харнесса, "
        "стан БД «до/після», відповіді Нової Пошти._\n"
    )
    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser()
    # Один отчёт на всё: обходы ролей и волны каскада лежат в разных папках,
    # но вердикт владельцу нужен один.
    parser.add_argument("--run-id", required=True, nargs="+")
    parser.add_argument("--cleanup", action="store_true", help="видалити зайві ТТН у НП")
    parser.add_argument("--keep", type=int, default=2, help="скільки ТТН лишити на перевірку")
    parser.add_argument("--env-file", default=None)
    args = parser.parse_args()

    run_dirs = [ARTIFACTS / run_id for run_id in args.run_id]
    missing = [d for d in run_dirs if not d.exists()]
    if missing:
        raise SystemExit(f"немає логів прогону: {', '.join(str(d) for d in missing)}")

    steps: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    for run_dir in run_dirs:
        run_steps, run_summaries = _load_logs(run_dir)
        steps.extend(run_steps)
        summaries.extend(run_summaries)

    before = (
        json.loads(SNAPSHOT_BEFORE.read_text(encoding="utf-8")) if SNAPSHOT_BEFORE.exists() else {}
    )
    after = await _state_after()

    before_ids = {row["id"] for row in before.get("shipments", [])}
    new_shipments = [row for row in after["shipments"] if row["id"] not in before_ids]

    findings = collect_findings(steps, before, after, summaries)
    np_report = await _np_reconcile(new_shipments, cleanup=args.cleanup, keep=args.keep)

    report = _render(" + ".join(args.run_id), steps, summaries, findings, np_report, new_shipments)
    path = run_dirs[0] / "report.md"
    path.write_text(report, encoding="utf-8")
    (run_dir / "state_after.json").write_text(json.dumps(after, ensure_ascii=False, indent=2))
    print(report)
    print(f"\nзвіт: {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
