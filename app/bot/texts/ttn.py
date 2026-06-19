"""Тексты потока создания ТТН (Фаза 4, PR 9). Все строки — украинский."""

from __future__ import annotations

from decimal import Decimal

from app.bot.keyboards.ttn import SIZE_PRESETS
from app.services.inventory import InventoryItem, InventoryPage


def _money(value: Decimal | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def no_profile_text() -> str:
    return (
        "🚫 <b>ФОП ще не налаштований</b>\n\n"
        "Щоб створювати ТТН, потрібен ваш ФОП із ключем Нової Пошти. "
        "Зверніться до менеджера — він додасть профіль."
    )


def not_validated_text() -> str:
    return (
        "🚫 <b>Ключ ФОП не підтверджено в НП</b>\n\n"
        "Ключ Нової Пошти вашого ФОП ще не пройшов перевірку. "
        "Зверніться до менеджера, щоб він підтвердив ключ."
    )


def cart_picker_text(page: InventoryPage, *, cart_count: int) -> str:
    parts = ["🚚 <b>Створення ТТН</b> — крок 1: оберіть товари"]
    if not page.items:
        parts.append("\nНа залишку поки немає позицій.")
    else:
        parts.append(f"\nДоступно позицій: {page.total}. Натисніть товар, щоб додати в кошик.")
    if cart_count:
        parts.append(f"🧺 У кошику: {cart_count} поз.")
    return "\n".join(parts)


def stepper_text(item: InventoryItem, qty: int) -> str:
    return (
        f"📦 <b>{item.name}</b> ({item.sku})\n"
        f"На залишку: <b>{item.available}</b> шт · ціна: {_money(item.price)}\n\n"
        f"Кількість у кошик: <b>{qty}</b> шт"
    )


def qty_prompt_text(item: InventoryItem) -> str:
    return f"Введіть кількість для «{item.name}» (1–{item.available}):"


def cart_review_text(lines: list[tuple[str, int, Decimal | None]]) -> str:
    """lines: (name, qty, unit_price)."""
    if not lines:
        return "🧺 <b>Кошик порожній</b>\n\nДодайте хоча б одну позицію."
    parts = ["🧺 <b>Кошик</b>"]
    total = Decimal("0")
    for idx, (name, qty, price) in enumerate(lines, start=1):
        line_sum = (price or Decimal("0")) * qty
        total += line_sum
        parts.append(f"#{idx} {name} · {qty} шт · {_money(price)}")
    parts.append(f"\n💰 Орієнтовна сума товарів: <b>{_money(total)}</b>")
    return "\n".join(parts)


def parcel_text(*, weight: str | None, size_token: str) -> str:
    weight_line = f"{weight} кг" if weight else "ще не вказано"
    return (
        "📦 <b>Параметри посилки</b> — крок 2\n\n"
        f"⚖️ Вага: <b>{weight_line}</b>\n"
        f"📐 Габарити: <b>{SIZE_PRESETS[size_token]}</b>\n\n"
        "Вкажіть вагу та оберіть габарити, потім — «Далі»."
    )


def weight_prompt_text() -> str:
    return "Введіть вагу посилки в кілограмах (напр. 0.8 або 2,5):"


def weight_invalid_text() -> str:
    return "❌ Невірна вага. Введіть число більше 0 (напр. 0.8 або 2,5)."


def recipient_kind_text() -> str:
    return "👤 <b>Отримувач</b> — крок 3\n\nКому відправляємо?"
