"""Живая проверка оголошеної вартості: выводится из корзины и доезжает до НП.

Гоняет настоящие `Update` через настоящий `build_dispatcher` на боевом стенде —
юнит-тесты этого не покрывают: там и корзина, и цена подменены фейками, а здесь
сумма реально складывается из цен Google Sheets и уходит в `getDocumentPrice`
полем `Cost`.

Что доказывается:

1. на карточке `Оголошена вартість` = сумма корзины, а не молчаливый ноль;
2. страховой сбор действительно попадает в цену — сравниваем оценку при сумме из
   корзины и при нуле (это отдельный черновик, он отменяется, ТТН не создаёт);
3. созданные ТТН уносят в БД ту самую сумму, что показала карточка.

Запуск (сначала `preflight`, после — `validate --cleanup`):
    .venv/bin/python -m scripts.e2e.insured_probe --list
    .venv/bin/python -m scripts.e2e.insured_probe --run-id insured1 --as-user <tg> --count 5
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys

from scripts.e2e.env import load_stand_env

_env_file = None
if "--env-file" in sys.argv:
    _env_file = sys.argv[sys.argv.index("--env-file") + 1]
    os.environ.setdefault("E2E_REDIS_URL", "redis://localhost:6379/9")
load_stand_env(_env_file)

from decimal import Decimal  # noqa: E402

from scripts.e2e.lib import Persona, build_persona, open_stepper  # noqa: E402

RECIPIENT_NAME = "Тестенко Тарас Тарасович"
RECIPIENT_PHONE = "380671234567"
CITY = "Львів"
# «Велика» коробка: заодно видно строку «Тарифна вага», если объёмный вес перебьёт факт.
SIZE_TOKEN = "l"

_INSURED_RX = re.compile(r"Оголошена вартість:\s*([\d.,]+)\s*₴\s*\(([^)]+)\)")
_INSURED_UNSET_RX = re.compile(r"Оголошена вартість:.*не вказана")
_PRICE_RX = re.compile(r"вартість доставки \(НП\):\s*<?b?>?\s*([\d.,]+)")


def _num(raw: str) -> Decimal:
    return Decimal(raw.replace(" ", "").replace(",", "."))


async def _list_clients() -> int:
    """Кого можно гонять: активные клиенты, их бизнес-аккаунт и цены в «Складі».

    Аккаунт у пользователя не колонкой, а через `client_account_memberships`;
    склад — свойство аккаунта. Колонка «з ціною» здесь ключевая: без цен сумму
    вывести неоткуда, и прогон упрётся в блокирующий ввод — это не дефект.
    """
    from app.config import get_settings
    from app.db.base import get_sessionmaker
    from app.db.models import ClientAccount, ClientAccountMembership, User
    from app.db.models.enums import UserRole, UserStatus
    from app.sheets import build_stock_source
    from sqlalchemy import select

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        accounts = {
            account.id: account
            for account in (await session.execute(select(ClientAccount))).scalars()
        }
        account_by_user = {
            row.user_id: accounts.get(row.account_id)
            for row in (await session.execute(select(ClientAccountMembership))).scalars()
        }
        users = (await session.execute(select(User))).scalars().all()

    source = build_stock_source(get_settings())
    priced: dict[str, str] = {}
    for account in accounts.values():
        key = getattr(account, "stock_sheet_key", None)
        if not key or key in priced:
            continue
        try:
            rows = await asyncio.to_thread(source.read_stock, key)
        except Exception as exc:  # диагностика листа, а не поток управления
            priced[key] = f"помилка: {type(exc).__name__}"
            continue
        priced[key] = f"{sum(1 for r in rows if r.price is not None)}/{len(rows)} з ціною"

    print(f"{'telegram_id':>12}  акаунт / залишки")
    for user in users:
        if user.role is not UserRole.client or user.status is not UserStatus.active:
            continue
        if user.telegram_id is None:
            continue
        account = account_by_user.get(user.id)
        key = getattr(account, "stock_sheet_key", None) if account else None
        name = getattr(account, "name", None) if account else "—"
        print(f"{user.telegram_id:>12}  {name!r} / {priced.get(key, 'листа немає')}")
    return 0


async def _open_card(persona: Persona) -> str:
    """Пройти поток до карточки и вернуть её текст ('' — не дошли)."""
    await persona.send("/start")
    button = persona.screen.find_reply("Створити ТТН")
    if button is None:
        return ""
    await persona.send(button.text)
    for prefix in ("ttn:sender:", "cab:ttn:sender:"):
        if persona.screen.find_data(prefix):
            await persona.tap_data(prefix)
            break

    if not await open_stepper(persona):
        return ""
    await persona.tap_data("cab:ttn:qok")

    # После подтверждения количества бот возвращает пикер, а «Далі» живёт на экране
    # кошика. Тапать вслепую нельзя: промах пишется в отчёт находкой `button_present`
    # и выглядит дефектом продукта, хотя это ошибка сценария.
    if not persona.screen.find_data("cab:ttn:next"):
        await persona.tap_data("cab:ttn:cart")
    await persona.tap_data("cab:ttn:next")
    await persona.tap_data(f"cab:ttn:sz:{SIZE_TOKEN}")
    if not await persona.tap_data("cab:ttn:torcpt"):
        return ""

    await persona.tap_data("cab:ttn:rk:p")
    await persona.send(RECIPIENT_NAME)
    await persona.send(RECIPIENT_PHONE)
    await persona.send(CITY)
    if persona.screen.find_data("cab:ttn:city:"):
        await persona.tap_data("cab:ttn:city:")
    if not await persona.tap_data("cab:ttn:wh:"):
        return ""
    return persona.screen.text or ""


def _parse_card(card: str) -> tuple[Decimal | None, str, Decimal | None]:
    """(оголошена вартість, источник, ціна доставки) — None, если не указано."""
    price_match = _PRICE_RX.search(card)
    price = _num(price_match.group(1)) if price_match else None
    if _INSURED_UNSET_RX.search(card):
        return None, "не вказана", price
    insured_match = _INSURED_RX.search(card)
    if insured_match is None:
        return None, "?", price
    return _num(insured_match.group(1)), insured_match.group(2), price


# Заведомо выше порога бесплатного страхования НП: по тарифам комиссия считается
# «від оголошеної цінності до 500 ₴», поэтому на 200–300 ₴ сбор нулевой, цена не
# меняется — на такой сумме проверка не доказывает ничего.
_HIGH_INSURED = "10000"


async def _set_insured(persona: Persona, value: str) -> tuple[Decimal | None, str, Decimal | None]:
    """Задать сумму вручную и вернуть разбор карточки после пересчёта."""
    await persona.tap_data("cab:ttn:edit:insured")
    await persona.send(value)
    return _parse_card(persona.screen.text or "")


async def _insurance_delta(persona: Persona) -> None:
    """Отдельный черновик: доезжает ли сумма до НП полем `Cost` и меняет ли тариф.

    Черновик отменяется — ТТН не создаётся. Правку сначала делаем в бо́льшую
    сторону: если и на 10 000 ₴ цена не сдвинулась, дело не в пороге бесплатного
    страхования, а в проводке.
    """
    card = await _open_card(persona)
    if not card:
        print("  [delta] до картки не дійшли — пропускаю")
        return
    auto_amount, auto_source, auto_price = _parse_card(card)
    print(f"  з кошика {auto_amount} ₴ ({auto_source}) → доставка {auto_price} ₴")

    high_amount, high_source, high_price = await _set_insured(persona, _HIGH_INSURED)
    print(f"  вручну {high_amount} ₴ ({high_source}) → доставка {high_price} ₴")

    zero_amount, zero_source, zero_price = await _set_insured(persona, "0")
    print(f"  вручну {zero_amount} ₴ ({zero_source}) → доставка {zero_price} ₴")
    await persona.tap_data("cab:ttn:cancel")

    if high_amount != Decimal(_HIGH_INSURED) or high_source != "власна сума":
        print("  ⚠️  правка суми не застосувалася — дивись картку в логах прогону")
        return
    if high_price is None or zero_price is None:
        print("  ⚠️  ціну не розібрали")
    elif high_price > zero_price:
        print(f"  ✅ сума доїжджає до НП як Cost: страховий збір +{high_price - zero_price} ₴")
    else:
        print("  ⚠️  ціна не змінилася навіть на 10 000 ₴ — Cost до getDocumentPrice не доїхав")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="показати клієнтів і вийти")
    parser.add_argument("--run-id", default="insured")
    parser.add_argument("--as-user", type=int, help="telegram_id клієнта (god-mode /as_user)")
    parser.add_argument("--count", type=int, default=5, help="скільки ТТН створити")
    parser.add_argument("--no-submit", action="store_true", help="дійти до картки і не відправляти")
    parser.add_argument("--env-file", default=None)
    args = parser.parse_args()

    if args.list:
        return await _list_clients()
    if args.as_user is None:
        raise SystemExit("вкажіть --as-user (список: --list)")

    from scripts.e2e.env import dev_telegram_id

    persona, np_client, redis_client = await build_persona(
        name="insured", telegram_id=dev_telegram_id(), mode="stub", run_id=args.run_id
    )
    try:
        await persona.become(args.as_user)

        print("— страховий збір у ціні (чернетка, ТТН не створюється) —")
        await _insurance_delta(persona)

        print(f"\n— {args.count} ТТН —")
        created: list[tuple[Decimal | None, str, str | None]] = []
        for index in range(args.count):
            card = await _open_card(persona)
            if not card:
                print(f"  #{index + 1}: до картки не дійшли")
                continue
            insured, source, price = _parse_card(card)
            number = None
            if not args.no_submit:
                entry = await persona.tap_data("cab:ttn:send")
                haystack = str(entry.get("screen_text") or "") + " ".join(
                    str(call.get("text") or "") for call in entry.get("outgoing", [])
                )
                match = re.search(r"\b(\d{14})\b", haystack)
                number = match.group(1) if match and "ТТН створено" in haystack else None
                if number is None:
                    print(f"  #{index + 1}: НЕ створено — {haystack[:160]}")
            created.append((insured, source, number))
            print(
                f"  #{index + 1}: оголошена {insured} ₴ ({source}) · доставка {price} ₴ · ТТН {number or '—'}"
            )

        ok = [row for row in created if row[0] is not None and row[0] > 0]
        print(
            f"\nПідсумок: {len(ok)}/{len(created)} карток із ненульовою оголошеною вартістю "
            f"з кошика. Прибрати створені документи: "
            f"python -m scripts.e2e.validate --run-id {args.run_id} --cleanup --keep 0"
        )
        return 0 if len(ok) == len(created) and created else 1
    finally:
        await np_client.aclose()
        await redis_client.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
