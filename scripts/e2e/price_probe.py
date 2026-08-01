"""Живой пробник `InternetDocument.getDocumentPrice` — сверка объёмного веса.

Зачем: тариф НП считается по максимуму из фактического и объёмного веса, а
`getDocumentPrice` мы зовём без габаритов и воспроизводим объёмный вес локально
(`mapping.billable_weight_kg`, коэффициент `объём м³ × 250`). Сам коэффициент
опубликован НП (novaposhta.ua/shipping-cost) — сомнений в нём нет; проверять надо
**проводку**: что габариты пресета доезжают до запроса, что берётся именно
максимум и что кэш не отдаёт старую оценку. Скрипт спрашивает у НП цену за
фактический вес и за расчётный тарифный и показывает разницу.

Только чтение: документы не создаются, бюджет ТТН не расходуется. Ключ ФОП
берётся из БД (расшифровывается ORM прозрачно) и **никуда не печатается** — в
выводе только последние 4 символа для идентификации профиля.

Запуск (ключ из БД по названию ФОП):
    .venv/bin/python -m scripts.e2e.price_probe --profile "Вероніка" --city Львів

Запуск без БД (ключ не попадает ни в git, ни в вывод) — переменной окружения
либо строкой `NP_PROBE_API_KEY=<ключ>` в локальном `.env`:
    NP_PROBE_API_KEY=<ключ> .venv/bin/python -m scripts.e2e.price_probe
"""

from __future__ import annotations

import argparse
import asyncio
import os
from decimal import Decimal
from pathlib import Path

from app.config import get_settings
from app.novaposhta import methods
from app.novaposhta.client import NovaPoshtaClient
from app.novaposhta.mapping import VOLUMETRIC_KG_PER_CUBIC_METER, billable_weight_kg
from sqlalchemy import select

# Пресеты коробок бота — дублируем сюда, чтобы пробник не тянул слой aiogram
# (`app/bot/keyboards` импортирует aiogram, а скрипт должен работать без него).
SIZE_PRESETS: dict[str, tuple[str, str, str]] = {
    "Мала": ("20", "20", "10"),
    "Середня": ("30", "30", "20"),
    "Велика": ("40", "40", "30"),
}
# Вес, который клиент выставляет вручную: заведомо меньше объёмного у «Великої».
MANUAL_WEIGHT = Decimal("2")
INSURED = Decimal("500")


def _decrypt_if_token(value: str) -> str:
    """Принять и «сырой» ключ НП, и Fernet-токен из колонки `np_api_key`.

    Значение из БД скопировать проще, чем сам ключ, поэтому поддерживаем обе формы.
    Токен расшифровывается тем же `FERNET_KEY`, что и в приложении: если он был
    зашифрован прод-ключом, локально он не откроется — об этом и сообщаем, а не
    падаем невнятным `InvalidToken`.
    """
    if not value.startswith("gAAAAA"):
        return value
    from app.utils.crypto import DecryptionError, decrypt

    try:
        return decrypt(value)
    except DecryptionError as exc:
        raise SystemExit(
            "NP_PROBE_API_KEY похож на Fernet-токен, но локальный FERNET_KEY его не "
            "открывает — значит он зашифрован ПРОД-ключом. Нужен либо прод FERNET_KEY, "
            "либо сам ключ НП открытым текстом."
        ) from exc


def _key_from_dotenv() -> str:
    """`NP_PROBE_API_KEY` из локального `.env` (gitignored).

    Читаем файл напрямую, а не через `Settings`: ключ пробника — не настройка
    приложения, и заводить под него поле конфига незачем. Так владелец кладёт
    ключ в `.env`, а тому, кто запускает скрипт, видеть его не нужно.
    """
    dotenv = Path(".env")
    if not dotenv.is_file():
        return ""
    for line in dotenv.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == "NP_PROBE_API_KEY":
            return value.strip().strip("'\"")
    return ""


async def _resolve_profile(profile_name: str | None) -> tuple[str, str]:
    """Ключ НП: из env `NP_PROBE_API_KEY`, иначе из БД по названию ФОП.

    Env-путь нужен, когда БД под рукой нет (ключи живут в проде, локальная база
    пустая): ключ не приходится ни коммитить, ни вставлять в командную строку —
    он подставляется окружением и в вывод не попадает.
    """
    env_key = os.environ.get("NP_PROBE_API_KEY", "").strip() or _key_from_dotenv()
    if env_key:
        return "з NP_PROBE_API_KEY", _decrypt_if_token(env_key)

    from app.db.base import get_sessionmaker
    from app.db.models import SenderProfile

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = select(SenderProfile)
        if profile_name:
            stmt = stmt.where(SenderProfile.name.ilike(f"%{profile_name}%"))
        profile = (await session.execute(stmt)).scalars().first()
        if profile is None:
            total = len((await session.execute(select(SenderProfile))).scalars().all())
            raise SystemExit(
                f"ФОП не найден (фильтр: {profile_name or '—'}; всего профилей в БД: {total}).\n"
                "Ключи ФОП живут в прод-БД. Варианты: указать прод `DATABASE_URL`, "
                "либо задать ключ напрямую: NP_PROBE_API_KEY=<ключ> "
                ".venv/bin/python -m scripts.e2e.price_probe"
            )
        return profile.name, profile.np_api_key


async def _price_with_seat(
    client: NovaPoshtaClient,
    *,
    api_key: str,
    city_sender_ref: str,
    city_recipient_ref: str,
    dims: tuple[str, str, str],
) -> Decimal | None:
    """Цена у НП за фактический вес, но с габаритами в `OptionsSeat`.

    Зовём `client.call` напрямую, а не через `to_price_props`: боевой маппинг
    габариты в оценку намеренно не шлёт (объёмный вес считаем сами), и ради
    разовой сверки его контракт менять незачем. `None` — если НП не приняла поле.
    """
    length, width, height = dims
    volume = Decimal(length) * Decimal(width) * Decimal(height) / Decimal("1000000")
    props = {
        "CitySender": city_sender_ref,
        "CityRecipient": city_recipient_ref,
        "Weight": f"{MANUAL_WEIGHT:f}",
        "ServiceType": "WarehouseWarehouse",
        "Cost": f"{INSURED:f}",
        "CargoType": "Cargo",
        "SeatsAmount": "1",
        "OptionsSeat": [
            {
                "volumetricVolume": f"{volume:f}",
                "volumetricWidth": width,
                "volumetricLength": length,
                "volumetricHeight": height,
                "weight": f"{MANUAL_WEIGHT:f}",
            }
        ],
    }
    try:
        rows = await client.call(
            api_key=api_key,
            model="InternetDocument",
            method="getDocumentPrice",
            props=props,
        )
    except Exception:
        return None
    raw = (rows[0] if rows else {}).get("Cost")
    return None if raw is None else Decimal(str(raw))


async def _city_ref(client: NovaPoshtaClient, api_key: str, query: str) -> tuple[str, str]:
    cities = await methods.get_cities(client, api_key=api_key, query=query)
    if not cities:
        raise SystemExit(f"Місто «{query}» не знайдено в довіднику НП")
    return cities[0].ref, cities[0].name


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", help="подстрока названия ФОП (например «Вероніка»)")
    parser.add_argument("--city", default="Київ", help="місто-отримувач (по умолчанию Київ)")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.np_sender_city_ref:
        raise SystemExit("NP_SENDER_CITY_REF не задан в .env — расчёт цены невозможен")

    profile_name, api_key = await _resolve_profile(args.profile)
    print(f"ФОП: {profile_name} (ключ …{api_key[-4:]})")

    client = NovaPoshtaClient(settings=settings)
    try:
        city_ref, city_name = await _city_ref(client, api_key, args.city)
        print(f"Маршрут: наш склад → {city_name}\n")

        for preset, dims in SIZE_PRESETS.items():
            volumetric = billable_weight_kg(
                Decimal("0"), length_cm=dims[0], width_cm=dims[1], height_cm=dims[2]
            )
            billable = billable_weight_kg(
                MANUAL_WEIGHT, length_cm=dims[0], width_cm=dims[1], height_cm=dims[2]
            )

            # Что НП скажет за «голый» фактический вес — то, что бот показывал ДО фикса.
            plain = await methods.get_price(
                client,
                api_key=api_key,
                city_sender_ref=settings.np_sender_city_ref,
                city_recipient_ref=city_ref,
                weight_kg=MANUAL_WEIGHT,
                cost=INSURED,
            )
            # Что бот показывает ПОСЛЕ фикса (Weight = max(факт, объёмный)).
            fixed = await methods.get_price(
                client,
                api_key=api_key,
                city_sender_ref=settings.np_sender_city_ref,
                city_recipient_ref=city_ref,
                weight_kg=MANUAL_WEIGHT,
                cost=INSURED,
                length_cm=dims[0],
                width_cm=dims[1],
                height_cm=dims[2],
            )
            # Эталон: спрашиваем НП цену за тот же фактический вес, но с реальными
            # габаритами в OptionsSeat — пусть считает объёмный вес сама. Совпадение
            # с нашим расчётом означает, что габариты доехали и максимум взят верно.
            seat_cost = await _price_with_seat(
                client,
                api_key=api_key,
                city_sender_ref=settings.np_sender_city_ref,
                city_recipient_ref=city_ref,
                dims=dims,
            )
            verdict = "✅" if seat_cost == fixed.cost else "⚠️"
            size = "×".join(dims)
            print(
                f"{preset:9} {size:>10} см | об'ємна {volumetric:>6} кг | "
                f"тарифна {billable:>6} кг | було {plain.cost:>7} ₴ → стало {fixed.cost:>7} ₴ "
                f"| НП з OptionsSeat {seat_cost if seat_cost is not None else '—':>7} {verdict}"
            )

        print(
            f"\nКоефіцієнт у коді: 1 м³ = {VOLUMETRIC_KG_PER_CUBIC_METER} кг "
            "(опубліковано: novaposhta.ua/shipping-cost).\n"
            "✅ у колонці = наш локальний розрахунок збігся з ціною, яку НП дала за\n"
            "   тими самими габаритами в OptionsSeat, тобто габарити доїхали до\n"
            "   запиту і максимум узято правильно. ⚠️ означає розбіжність — дивитись\n"
            "   проводку габаритів, а якщо НП змінила тариф — константу\n"
            "   `VOLUMETRIC_KG_PER_CUBIC_METER` в app/novaposhta/mapping.py."
        )
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
