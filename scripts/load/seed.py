"""Сид ЛОКАЛЬНОЙ базы под нагрузочный прогон.

Форма данных взята с боевого прода, а не выдумана — иначе прогон измерит стенд, а
не систему:

- **20 бизнес-аккаунтов**, у каждого владелец и до 5 работников (~130 клиентских
  пользователей), плюс 5 менеджеров и владелец сервиса — целевой профиль;
- **самый крупный аккаунт — 1636 позиций**, 1631 с ценами
  (`PROGRESS.md:221-222`). Средний размер склада мельче на порядок, и именно
  разброс важен: пагинация и сводка ведут себя по-разному на 20 позициях и на
  полутора тысячах;
- **у части аккаунтов склада нет вовсе** — в проде это штатный случай
  (`inventory.sheet_missing`, `PROGRESS.md:652-654`), и в нагрузке он обязан
  встречаться, иначе путь «пустой склад» не проверяется никогда;
- **у части владельцев несколько ФОП** — так в проде.

Пользователи и членства заводятся **настоящими сервисными путями**
(`create_for_owner`, `invite_employee`, `activate_employee_contact`), а не
INSERT-ами: сид, собранный руками, разъедется с продом молча.

Гарантия против выстрела в ногу: скрипт отказывается работать с базой, чьё имя не
оканчивается на `_load`. Обычный `pytest` уже однажды стирал dev-базу
(см. `local-test-db-isolation`), и повторять это через сид незачем.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.load.guards import require_load_database, require_offline_stand

#: Целевой профиль из плана.
ACCOUNTS = 20
EMPLOYEES_PER_ACCOUNT = 5
MANAGERS = 5

#: Разброс размеров склада. Первый — боевой максимум (1636 позиций у Вероніки),
#: остальные типичные. Сумма ~10 000 SKU — целевой объём из модели нагрузки:
#: на 3 тысячах пагинация и сводка ведут себя иначе, чем на десяти.
STOCK_SIZES = [
    1636,
    1200,
    1000,
    900,
    800,
    700,
    600,
    500,
    450,
    400,
    350,
    300,
    250,
    200,
    150,
    100,
    80,
    60,
]
#: Аккаунты без склада вовсе: `ACCOUNTS - len(STOCK_SIZES)` штук. В проде это
#: штатный случай (`inventory.sheet_missing`), и путь «пустой склад» обязан
#: встречаться в нагрузке, иначе он не проверяется никогда.

_BASE_TELEGRAM_ID = 900_000_000


async def _reset(engine) -> None:
    import app.db.models  # noqa: F401 — регистрирует таблицы в Base.metadata
    from app.db.base import Base

    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)


async def _seed_account(session, index: int, *, positions: int, rng: random.Random) -> None:
    from app.bot.types import ClientAccountContext
    from app.db.models.enums import StockMovementType, UserRole, UserStatus
    from app.db.repositories import (
        ClientAccountRepository,
        SenderProfileRepository,
        StockBalanceRepository,
        UserRepository,
    )
    from app.services import account_team

    users = UserRepository(session)
    accounts = ClientAccountRepository(session)
    owner_tid = _BASE_TELEGRAM_ID + index * 100

    owner = await users.create(
        telegram_id=owner_tid,
        phone=f"+38050{owner_tid % 10_000_000:07d}",
        full_name=f"Власник {index:02d}",
        role=UserRole.client,
        status=UserStatus.active,
    )
    # `UserRepository.create` при `role=client` уже заводит аккаунт и членство —
    # ветки «если членства нет, позвать `create_for_owner`» здесь быть не может, и
    # она была мёртвой. Имя аккаунта наследуется от владельца, и это верно: в проде
    # оно ровно такое же, а `stock_sheet_key` строится из него.
    membership = await accounts.get_membership(user_id=owner.id)
    if membership is None:  # pragma: no cover — недостижимо, но молчать нельзя
        raise SystemExit(f"аккаунт для владельца {owner_tid} не создан — сид не воспроизводит прод")
    account = membership.account

    # ФОП: у части владельцев их несколько — так в проде.
    profiles = 2 if index % 4 == 0 else 1
    for p in range(profiles):
        await SenderProfileRepository(session).create(
            client_id=owner.id,
            account_id=account.id,
            name=f"ФОП {index:02d}-{p}",
            np_api_key=f"np-key-{index:02d}-{p}",
            np_sender_ref="sender-cp",
            np_contact_ref="sender-ct",
            sender_phone="+380501112233",
            is_default=(p == 0),
        )

    context = ClientAccountContext(user=owner, account=account, membership=membership)
    for e in range(EMPLOYEES_PER_ACCOUNT):
        tid = owner_tid + e + 1
        invited = await account_team.invite_employee(
            session, context=context, phone=f"+38067{tid % 10_000_000:07d}"
        )
        employee = await users.get_by_id(invited.user_id)
        await account_team.activate_employee_contact(
            session, user=employee, telegram_id=tid, full_name=f"Працівник {index:02d}-{e}"
        )

    balances = StockBalanceRepository(session)
    for i in range(positions):
        sku = f"SKU-{i:04d}"
        await balances.upsert_meta(
            account_id=account.id,
            sku=sku,
            name=f"Товар {i:04d}",
            category=("Кава", "Чай", "Какао")[i % 3],
            # Пять позиций без цены — как в проде (1631 из 1636).
            price=None if i < 5 else Decimal(rng.randrange(50, 900)),
        )
        # Стартовый остаток — движением `intake`, а НЕ присваиванием `quantity`.
        # Присваивание в обход `apply_movement` нарушает инвариант «сумма
        # физических дельт == quantity» с первой же секунды: сверка пометила бы
        # все позиции как расхождение, а критерий приёмки «инвариант держится»
        # стал бы непроходимым по построению — и это выглядело бы как баг системы,
        # а не как баг стенда.
        await balances.apply_movement(
            account_id=account.id,
            sku=sku,
            delta=rng.randrange(50, 900),
            movement_type=StockMovementType.intake,
            comment="стартовий залишок стенда",
        )
    await session.flush()


async def _seed_staff(session) -> None:
    from app.db.models.enums import UserRole, UserStatus
    from app.db.repositories import UserRepository

    users = UserRepository(session)
    await users.create(
        telegram_id=_BASE_TELEGRAM_ID - 1,
        phone="+380500000001",
        full_name="Власник сервісу",
        role=UserRole.owner,
        status=UserStatus.active,
    )
    for m in range(MANAGERS):
        await users.create(
            telegram_id=_BASE_TELEGRAM_ID - 2 - m,
            phone=f"+38050000001{m}",
            full_name=f"Менеджер {m}",
            role=UserRole.manager,
            status=UserStatus.active,
        )


async def seed(
    *,
    accounts: int = ACCOUNTS,
    seed_value: int = 20260802,
    allow_live_google: bool = False,
) -> dict[str, int]:
    from app.config import get_settings
    from app.db.base import make_engine
    from sqlalchemy.ext.asyncio import async_sessionmaker

    settings = get_settings()
    require_load_database(settings.database_url)
    require_offline_stand(allow_live_google=allow_live_google)

    engine = make_engine()
    await _reset(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    # Псевдослучайность с фиксированным зерном — тут это требование, а не
    # небрежность: прогон обязан воспроизводиться. Криптостойкость не нужна.
    rng = random.Random(seed_value)  # noqa: S311
    positions_total = 0
    async with factory() as session:
        await _seed_staff(session)
        for index in range(accounts):
            positions = STOCK_SIZES[index] if index < len(STOCK_SIZES) else 0
            positions_total += positions
            await _seed_account(session, index, positions=positions, rng=rng)
        await session.commit()
    await engine.dispose()

    return {
        "accounts": accounts,
        "users": accounts * (1 + EMPLOYEES_PER_ACCOUNT) + MANAGERS + 1,
        "positions": positions_total,
        "accounts_without_stock": max(0, accounts - len(STOCK_SIZES)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accounts", type=int, default=ACCOUNTS)
    parser.add_argument("--seed", type=int, default=20260802)
    parser.add_argument(
        "--allow-live-google",
        action="store_true",
        help="снять гард на GOOGLE_SA_JSON/SHEETS_*_BOOK_ID/BOT_TOKEN (осознанно!)",
    )
    args = parser.parse_args()

    stats = asyncio.run(
        seed(
            accounts=args.accounts,
            seed_value=args.seed,
            allow_live_google=args.allow_live_google,
        )
    )
    print("Засіяно:")
    for key, value in stats.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
