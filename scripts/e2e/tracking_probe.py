"""Живой пробник `TrackingDocument.getStatusDocuments` — какие даты отдаёт НП.

Зачем: вердикт SLA закрывается временем сканирования на стороне НП
(`app/novaposhta/tracking.dispatch_scan_time`, поле `DateScan`). Публичной
документации, подтверждающей и имя поля, и его формат, найти не удалось, а
ошибиться здесь дорого в обе стороны: возьмём не то поле — либо признаем
успевшими все накладные подряд (если подставится `DateCreated`, то есть СТАРТ
отсчёта), либо начнём засчитывать промахи по дате вручения.

Ровно этот класс ошибки уже стрелял на `_STATUS_CODES["2"]`: код 2 у НП —
«Видалено», а не «створено», и накладная, удалённая в кабинете, навсегда
оставалась «підтверджена». Тогда правду сняли живым запросом — здесь так же.

Только чтение: документы не создаются и не меняются, бюджет ТТН не расходуется.
Ключ ФОП берётся из БД или `NP_PROBE_API_KEY` и **никуда не печатается**.

Запуск (номера из боевой БД):
    .venv/bin/python -m scripts.e2e.tracking_probe --limit 10

Запуск по конкретным номерам:
    .venv/bin/python -m scripts.e2e.tracking_probe --number 59000999 --number 20451500870149
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

from app.config import get_settings
from app.novaposhta import methods
from app.novaposhta.client import NovaPoshtaClient
from app.novaposhta.tracking import dispatch_scan_time
from scripts.e2e.price_probe import _resolve_profile

#: Что ищем в ответе. `DateScan` — то, на чём стоит вердикт SLA; остальные нужны,
#: чтобы увидеть их формат и убедиться, что мы не перепутали одно с другим.
DATE_KEYS = (
    "DateScan",
    "DateCreated",
    "RecipientDateTime",
    "ActualDeliveryDate",
    "ScheduledDeliveryDate",
    "LastTransactionDateTimeGM",
    "DatePayedKeeping",
)


async def _numbers_from_db(limit: int) -> list[str]:
    from app.db.base import get_sessionmaker
    from app.db.models import Shipment
    from sqlalchemy import select

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = (
            select(Shipment.ttn_number)
            .where(Shipment.ttn_number.is_not(None))
            .order_by(Shipment.created_at.desc())
            .limit(limit)
        )
        return [row for row in (await session.execute(stmt)).scalars().all() if row]


def _report(row: dict[str, Any]) -> None:
    number = row.get("Number", "—")
    print(f"\n  ТТН {number}: {row.get('Status', '—')} (StatusCode={row.get('StatusCode', '—')})")
    present = False
    for key in DATE_KEYS:
        value = row.get(key)
        if value not in (None, "", "0000-00-00 00:00:00"):
            print(f"      {key:<26} = {value!r}")
            present = True
    if not present:
        print("      (ни одного непустого поля с датой)")

    # Главное: разбирается ли то, на чём стоит вердикт SLA.
    from app.novaposhta.schemas import TrackingStatus

    parsed = dispatch_scan_time(
        TrackingStatus(
            number=str(number),
            status=str(row.get("Status", "")),
            status_code=str(row.get("StatusCode", "")),
            raw=row,
        )
    )
    verdict = (
        f"{parsed.isoformat()} (UTC)" if parsed else "НЕ РАЗОБРАНО → вердикт SLA будет «не знаем»"
    )
    print(f"      dispatch_scan_time()       → {verdict}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=None, help="подстрока названия ФОП для ключа НП")
    parser.add_argument("--number", action="append", default=[], help="номер ТТН (можно повторять)")
    parser.add_argument("--limit", type=int, default=10, help="сколько номеров взять из БД")
    args = parser.parse_args()

    source, api_key = await _resolve_profile(args.profile)
    numbers = args.number or await _numbers_from_db(args.limit)
    if not numbers:
        raise SystemExit("нет номеров ТТН — укажите --number или наполните БД")

    print(f"Ключ НП: {source} (…{api_key[-4:]})")
    print(f"Спрашиваем статусы по {len(numbers)} номерам, только чтение.")

    client = NovaPoshtaClient(settings=get_settings())
    try:
        rows = await methods.get_status_documents(client, api_key=api_key, numbers=numbers)
    finally:
        await client.aclose()

    if not rows:
        raise SystemExit("НП не вернула ни одной строки — проверьте ключ и номера")

    for row in rows:
        _report(row.raw)

    scanned = sum(
        1 for row in rows if row.raw.get("DateScan") not in (None, "", "0000-00-00 00:00:00")
    )
    print(f"\nИтог: {scanned} из {len(rows)} строк несут непустой DateScan.")
    print("Если у отправленных ТТН он пуст — поле выбрано неверно, вердикт SLA")
    print("будет вырождаться в «не знаем», и `_SCAN_TIME_FIELD` надо менять.")


if __name__ == "__main__":
    asyncio.run(main())
