#!/usr/bin/env python
"""Backfill остатка: книга «Склад» → `stock_balances`, и водораздел ингеста.

Разовая операция перед включением `STOCK_INGEST_ENABLED`. Переносит текущее
количество из листа каждого аккаунта в Postgres и ставит водораздел журнала
приёмки на его нынешний конец — то и другое **одной транзакцией**.

    PYTHONPATH=. .venv/bin/python scripts/stock_backfill.py [--dry-run] [--yes]

**Почему это нельзя делать «на живую» без проверки.** Между чтением количества из
листа и фиксацией водораздела кто-то может нажать «Внести»:

- приёмка попала и в снимок листа, и в журнал после водораздела → применится
  **дважды**, остаток завышен, гейт от oversell разрешит продать чужое;
- приёмка не попала никуда → потеряна, остаток занижен.

Поэтому конец журнала читается ДО и ПОСЛЕ снимка листов. Разошлись — скрипт
**отказывается** писать и просит повторить с замороженной кнопкой «Внести». Это
превращает «надеемся, никто не нажал» в «проверено, что не нажимали».

Повторный запуск безопасен: количество приводится к листу движением `manual` с
честной дельтой, нулевые дельты не пишутся, водораздел переставляется на текущий
конец журнала.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from dataclasses import dataclass

from app.config import get_settings
from app.db.base import get_engine, get_sessionmaker
from app.db.models.client_account import ClientAccount
from app.db.models.enums import StockMovementType
from app.db.repositories import StockBalanceRepository, StockIntakeCursorRepository
from app.services.inventory_backend import stock_sheet_key
from app.sheets import StockRow, StockSheetNotFound, build_stock_source
from app.sheets.history import HISTORY_TAB, IntakeHistoryReader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

_PREVIEW = 5


@dataclass(frozen=True, slots=True)
class AccountPlan:
    """Снимок листа одного аккаунта. Лист читается ОДИН раз — план и применение
    работают по одному и тому же снимку, иначе они разошлись бы между собой."""

    account_id: uuid.UUID
    label: str
    key: str
    rows: list[StockRow]
    changes: list[tuple[str, int, int]]  # sku, было, стало
    missing_sheet: bool


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill остатка из «Склад» в Postgres")
    parser.add_argument("--dry-run", action="store_true", help="показать план и выйти")
    parser.add_argument("--yes", action="store_true", help="не спрашивать подтверждения")
    return parser.parse_args()


async def build_plans(session: AsyncSession, source) -> list[AccountPlan]:
    accounts = (await session.scalars(select(ClientAccount).order_by(ClientAccount.name))).all()
    balances = StockBalanceRepository(session)
    plans: list[AccountPlan] = []
    for account in accounts:
        key = stock_sheet_key(account)
        label = account.name or str(account.id)
        try:
            rows = list(source.read_stock(key))
        except StockSheetNotFound:
            plans.append(AccountPlan(account.id, label, key, [], [], True))
            continue
        changes = []
        for row in rows:
            current = await balances.get(account_id=account.id, sku=row.sku)
            before = 0 if current is None else current.quantity
            if before != row.quantity:
                changes.append((row.sku, before, row.quantity))
        plans.append(AccountPlan(account.id, label, key, rows, changes, False))
    return plans


async def apply_plans(session: AsyncSession, plans: list[AccountPlan]) -> None:
    balances = StockBalanceRepository(session)
    for plan in plans:
        # Отдельной ветки под `missing_sheet` здесь нет намеренно: у такого плана
        # `rows` пуст, цикл не выполняется, и остаток аккаунта остаётся как был.
        # Ветка была бы мёртвой — а мёртвая защита создаёт ложное ощущение, что
        # обнуление где-то предотвращается проверкой.
        for row in plan.rows:
            # Описательные поля принадлежат листу — забираем их всегда, даже когда
            # количество не изменилось: имя, категорию и цену человек правит прямо
            # в «Складі», и это остаётся его способом коррекции.
            balance = await balances.upsert_meta(
                account_id=plan.account_id,
                sku=row.sku,
                name=row.name,
                category=row.category,
                price=row.price,
            )
            delta = row.quantity - balance.quantity
            if delta:
                await balances.apply_movement(
                    account_id=plan.account_id,
                    sku=row.sku,
                    delta=delta,
                    movement_type=StockMovementType.manual,
                    comment=f"backfill з листа «{plan.key}»",
                )
            # База, по которой распознаётся ручная правка ячейки: то, что в листе
            # сейчас. Без неё «человек поправил» неотличимо от «PG изменился».
            balance.mirrored_quantity = row.quantity
        await session.flush()


def _print_plans(plans: list[AccountPlan]) -> int:
    total = 0
    for plan in plans:
        if plan.missing_sheet:
            print(f"  {plan.label}: листа «{plan.key}» немає — пропущено")
            continue
        total += len(plan.changes)
        print(f"  {plan.label}: {len(plan.rows)} позицій, змін {len(plan.changes)}")
        for sku, was, now in plan.changes[:_PREVIEW]:
            print(f"      {sku}: {was} → {now}")
        if len(plan.changes) > _PREVIEW:
            print(f"      … ще {len(plan.changes) - _PREVIEW}")
    print(f"разом змін: {total}")
    return total


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    if not settings.sheets_stock_book_id:
        print("SHEETS_STOCK_BOOK_ID не настроен", file=sys.stderr)
        return 2

    source = build_stock_source(settings)
    history = IntakeHistoryReader()

    before_end = history.last_row()
    print(f"кінець журналу «{HISTORY_TAB}» до знімка: рядок {before_end}")

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        plans = await build_plans(session, source)
        _print_plans(plans)

        after_end = history.last_row()
        if after_end != before_end:
            print(
                f"\n❌ Журнал приймання змінився під час знімка "
                f"(рядок {before_end} → {after_end}).\n"
                "   Повторіть із замороженою кнопкою «Внести»: інакше приймання, "
                "що приїхала між читаннями, або задвоїться, або загубиться.",
                file=sys.stderr,
            )
            return 1

        if args.dry_run:
            print("\n--dry-run: нічого не записано")
            return 0
        if not args.yes:
            answer = await asyncio.to_thread(input, "\nЗастосувати? [y/N] ")
            if answer.strip().lower() not in {"y", "yes"}:
                print("скасовано")
                return 0

        await apply_plans(session, plans)

        cursors = StockIntakeCursorRepository(session)
        window = history.read_window(after_end, 0)
        cursor = await cursors.get(book_id=settings.sheets_stock_book_id, tab=HISTORY_TAB)
        if cursor is None:
            await cursors.create_at(
                book_id=settings.sheets_stock_book_id,
                tab=HISTORY_TAB,
                row=after_end,
                fingerprint=window.watermark_fingerprint,
            )
        else:
            await cursors.advance(cursor, row=after_end, fingerprint=window.watermark_fingerprint)

        # Один коммит на всё: остаток и водораздел обязаны стать видимыми вместе.
        await session.commit()

    print(f"\n✅ Готово. Водорозділ інгесту — рядок {after_end}.")
    print("   Тепер можна вмикати STOCK_INGEST_ENABLED=true.")
    return 0


def main() -> int:
    async def _wrap() -> int:
        try:
            return await _run(_parse_args())
        finally:
            # Движок кэшируется на уровне модуля, а asyncpg-соединения привязаны к
            # циклу событий: без dispose следующий `asyncio.run` получил бы
            # протухшее соединение из пула.
            await get_engine().dispose()

    return asyncio.run(_wrap())


if __name__ == "__main__":
    raise SystemExit(main())
