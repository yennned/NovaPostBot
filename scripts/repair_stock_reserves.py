#!/usr/bin/env python
"""Починка журнала: снять бронь под ТТН, которые закрылись без её возврата.

    PYTHONPATH=. .venv/bin/python scripts/repair_stock_reserves.py          # только показать
    PYTHONPATH=. .venv/bin/python scripts/repair_stock_reserves.py --apply  # записать

**Что это чинит.** До PR #158 трекинг переводил ТТН в `cancelled` по коду НП «2»
(документ удалили в кабинете НП), не записывая парного `ttn_cancel`. Правка
закрыла путь на будущее, но строки, накопившиеся до неё, так и остались: бронь
`ttn_reserve` без пары навсегда.

**Почему это не видно ни в остатке, ни в старой сверке.** Доступный остаток
считается по СТАТУСУ ТТН, а не по движениям, — он верен. Инвариант
`ledger_matches_balance` смотрит только на физические типы, а бронь к ним не
относится, — он тоже молчит. Расходится ровно журнал: тот, по которому разбирают
«куда делся товар». Постоянную проверку добавили в сверку
(`StockBalanceRepository.unreleased_reserves`), этот скрипт — разовая уборка
накопленного.

Правка идемпотентна дважды: сам `release_reserve_in_ledger` проверяет, что парного
движения нет, а выборка после записи перестаёт возвращать починенную ТТН.
Количество на складе скрипт **не двигает** — `ttn_cancel` не физический тип.
"""

from __future__ import annotations

import argparse
import asyncio
import uuid

from app.db.base import get_engine, get_sessionmaker
from app.db.models.client_account import ClientAccount
from app.db.models.shipment import Shipment
from app.db.repositories import StockBalanceRepository
from app.services.tracking import release_reserve_in_ledger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Снять зависшую бронь в журнале движений")
    parser.add_argument("--apply", action="store_true", help="записать (по умолчанию — показать)")
    return parser.parse_args()


async def find_broken(
    session: AsyncSession,
) -> list[tuple[ClientAccount, uuid.UUID, str | None, int]]:
    """`(аккаунт, shipment_id, номер ТТН, бронь)` по всем аккаунтам."""
    accounts = (await session.scalars(select(ClientAccount).order_by(ClientAccount.name))).all()
    repo = StockBalanceRepository(session)
    found = []
    for account in accounts:
        for shipment_id, number, reserve in await repo.unreleased_reserves(account.id):
            found.append((account, shipment_id, number, reserve))
    return found


async def repair(session: AsyncSession, shipment_ids: list[uuid.UUID]) -> None:
    """Записать снятие брони по каждой ТТН. Коммит — на вызывающем."""
    for shipment_id in shipment_ids:
        # joinedload обязателен: `release_reserve_in_ledger` читает `shipment.items`,
        # а ленивая загрузка в asyncio падает `MissingGreenlet` — починка молча не
        # сработала бы, и бронь осталась бы висеть.
        shipment = (
            (
                await session.execute(
                    select(Shipment)
                    .where(Shipment.id == shipment_id)
                    .options(joinedload(Shipment.items))
                )
            )
            .unique()
            .scalar_one()
        )
        await release_reserve_in_ledger(session, shipment=shipment)


async def _run(args: argparse.Namespace) -> int:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        broken = await find_broken(session)
        if not broken:
            print("✅ Зависшей брони нет — журнал сходится.")
            return 0

        print(f"Найдено ТТН с невозвращённой бронью: {len(broken)}")
        for account, shipment_id, number, reserve in broken:
            label = account.name or str(account.id)
            print(f"  {label}: ТТН {number or '—'} · бронь {reserve} · {shipment_id}")

        if not args.apply:
            print("\nНичего не записано. Для записи: --apply")
            return 0

        await repair(session, [shipment_id for _, shipment_id, _, _ in broken])
        await session.commit()

        left = await find_broken(session)
        print(f"\n✅ Записано. Осталось незакрытых: {len(left)}")
        return 0 if not left else 1


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
