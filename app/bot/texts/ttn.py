"""Тексты потока создания ТТН (Фаза 4, PR 9). Все строки — украинский."""

from __future__ import annotations

import html
from decimal import Decimal

from app.bot.keyboards.ttn import SIZE_PRESETS
from app.bot.texts.common import invalid_phone_text
from app.services.inventory import InventoryItem, InventoryPage


def _money(value: Decimal | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def no_profile_text() -> str:
    return (
        "🚫 <b>ФОП ще не налаштований</b>\n\n"
        "Щоб створювати ТТН, потрібен ваш ФОП із ключем Нової Пошти. "
        "Відкрийте «⚙️ Налаштування» → «🏢 Мої ФОП» → «➕ Додати ФОП»."
    )


def pick_sender_text() -> str:
    return (
        "🚚 <b>Створення ТТН</b> — оберіть ФОП-відправника.\n"
        "Від нього піде накладна (ключ НП і дані відправника — цього профілю)."
    )


def not_validated_text() -> str:
    return (
        "🚫 <b>Ключ ФОП не підтверджено в НП</b>\n\n"
        "Ключ Нової Пошти вашого ФОП ще не пройшов перевірку. "
        "Зверніться до менеджера, щоб він підтвердив ключ."
    )


def sender_incomplete_text() -> str:
    return (
        "🚫 <b>ФОП налаштований не до кінця</b>\n\n"
        "Бракує даних відправника (телефон або контакт у НП), без них ТТН створити "
        "не вийде. Зверніться до менеджера, щоб він доповнив профіль."
    )


def sender_dispatch_not_configured_text() -> str:
    return (
        "🚫 <b>Склад відправника не налаштований</b>\n\n"
        "Адреса нашого складу-відправника ще не задана в системі. "
        "Зверніться до підтримки — це налаштування на нашому боці."
    )


def cart_picker_text(page: InventoryPage, *, cart_count: int) -> str:
    parts = ["🚚 <b>Створення ТТН</b> — крок 1: оберіть товари"]
    if not page.items:
        parts.append("\nНа залишку поки немає позицій.")
    else:
        parts.append(
            f"\nДоступно позицій: {page.total}. "
            "Шукайте за SKU/назвою/категорією або натисніть товар, "
            "щоб додати в кошик."
        )
    if cart_count:
        parts.append(f"🧺 У кошику: {cart_count} поз.")
    return "\n".join(parts)


def stepper_text(item: InventoryItem, qty: int) -> str:
    # Назва/sku — из Sheets (могут содержать < & ") → экранируем для parse_mode=HTML.
    return (
        f"📦 <b>{html.escape(item.name)}</b> ({html.escape(item.sku)})\n"
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
        parts.append(f"#{idx} {html.escape(name)} · {qty} шт · {_money(price)}")
    parts.append(f"\n💰 Орієнтовна сума товарів: <b>{_money(total)}</b>")
    return "\n".join(parts)


def parcel_text(*, weight: str | None, size_token: str) -> str:
    weight_line = f"{weight} кг" if weight else "ще не вказано"
    return (
        "📦 <b>Параметри посилки</b> — крок 2\n\n"
        f"📐 Коробка: <b>{SIZE_PRESETS[size_token]}</b>\n"
        f"⚖️ Вага: <b>{weight_line}</b>\n\n"
        "Оберіть коробку (вага підставиться автоматично) — і тисніть «Далі».\n"
        "Потрібна точна вага? Натисніть «Вказати вагу»."
    )


def weight_prompt_text() -> str:
    return "Введіть вагу посилки в кілограмах (напр. 0.8 або 2,5):"


def weight_invalid_text() -> str:
    return "❌ Невірна вага. Введіть число більше 0 (напр. 0.8 або 2,5)."


def recipient_kind_text() -> str:
    return "👤 <b>Отримувач</b> — крок 3\n\nКому відправляємо?"


def recipient_name_prompt(kind: str) -> str:
    if kind == "organization":
        return "Введіть повну назву організації (напр. ТОВ «Ромашка»):"
    return "Введіть ПІБ отримувача (напр. Іваненко Іван Іванович):"


def recipient_name_invalid() -> str:
    return "❌ Порожнє значення. Введіть ПІБ або назву організації."


def edrpou_prompt() -> str:
    return "Введіть код ЄДРПОУ організації або ІПН ФОП (8 або 10 цифр):"


def edrpou_invalid() -> str:
    return "❌ Невірний код. ЄДРПОУ — 8 цифр, ІПН ФОП — 10 цифр."


def phone_prompt() -> str:
    return "Введіть телефон отримувача (напр. 0671234567):"


def phone_invalid() -> str:
    return invalid_phone_text()


def city_prompt() -> str:
    return "📍 <b>Місто отримувача</b> — почніть вводити назву (напр. Київ):"


def city_not_found(query: str) -> str:
    return f"Нічого не знайшли за «{html.escape(query)}». Спробуйте іншу назву міста."


def city_results_text(query: str) -> str:
    return f"Знайдено за «{html.escape(query)}». Оберіть місто:"


def warehouse_results_text(city_name: str, *, total: int) -> str:
    return (
        f"🏤 <b>Відділення у місті {html.escape(city_name)}</b>\n"
        f"Знайдено: {total}. Оберіть відділення або знайдіть за номером."
    )


def warehouse_none_text(city_name: str) -> str:
    return f"У місті {html.escape(city_name)} відділень не знайдено. Спробуйте інше місто."


def warehouse_find_prompt() -> str:
    return "Введіть номер або частину адреси відділення:"


def search_unavailable_text() -> str:
    return "⚠️ Довідник НП тимчасово недоступний. Спробуйте за хвилину."


def insured_prompt() -> str:
    return "Введіть оголошену вартість (страхову суму) у гривнях, напр. 1200:"


def insured_invalid() -> str:
    return "❌ Невірна сума. Введіть число 0 або більше (напр. 1200)."


def description_prompt() -> str:
    return "Введіть опис вкладення (напр. Одяг):"


def description_invalid() -> str:
    return "❌ Порожній опис. Введіть текст."


def cod_invalid() -> str:
    return "❌ Сума накладеного платежу має бути більшою за 0."


def size_edit_text() -> str:
    return "📐 Оберіть габарити посилки:"


def payer_edit_text() -> str:
    return "🧾 Хто платить за доставку?"


def payment_edit_text() -> str:
    return "💳 Спосіб оплати:"


def cod_amount_choice_text() -> str:
    return "💵 Оберіть суму накладеного платежу:"


def cod_amount_prompt() -> str:
    return "Введіть суму накладеного платежу в гривнях, наприклад 1200:"


def success_text(ttn_number: str | None) -> str:
    num = f"<b>{html.escape(ttn_number)}</b>" if ttn_number else "—"
    return (
        f"✅ <b>ТТН створено!</b>\n\n"
        f"Номер: {num}\n"
        "Резерв активний — позиції зменшено у 📦 Товари.\n"
        "Передайте посилку на наш склад для відправлення."
    )


def card_text(data: dict, price: dict) -> str:
    """Карточка-зведення перед відправкою. `data` — FSM-data, `price` — кэш цены."""
    cart = data.get("cart", {})
    items = "; ".join(f"{html.escape(e['name'])} ×{e['qty']}" for e in cart.values())
    kind = "організація" if data.get("recipient_kind") == "organization" else "особа"
    payment = "Накладений платіж" if data.get("payment_method") == "cod" else "Передоплата"
    payer = "Відправник" if data.get("payer_type") == "Sender" else "Отримувач"
    size_label = SIZE_PRESETS.get(data.get("size_token", "s"), "—")

    lines = [
        "📋 <b>Перевірте ТТН перед відправкою</b>",
        "",
        f"📦 Товари: {items}",
        f"👤 Отримувач: {html.escape(data.get('recipient_name', ''))} ({kind})",
    ]
    if data.get("recipient_edrpou"):
        lines.append(f"🧾 ЄДРПОУ: {data['recipient_edrpou']}")
    lines.extend(
        [
            f"📱 Телефон: {data.get('recipient_phone', '')}",
            f"📍 {html.escape(data.get('recipient_city_name', ''))}, "
            f"{html.escape(data.get('recipient_warehouse_name', ''))}",
            f"⚖️ Вага: {data.get('weight', '')} кг",
            f"📐 Габарити: {size_label}",
            f"📝 Опис: {html.escape(data.get('description', ''))}",
            f"🛡 Оголошена вартість: {data.get('insured_amount', '0')} ₴",
            f"💳 Оплата: {payment}",
        ]
    )
    if data.get("cod_amount"):
        source = "сума з кошика" if data.get("cod_amount_source") == "cart" else "власна сума"
        lines.append(f"   Накладений платіж: {data['cod_amount']} ₴ ({source})")
    lines.append(f"🧾 Платник доставки: {payer}")
    lines.append("─────────────")
    if price.get("unavailable"):
        lines.append("💵 Розрахунок недоступний — вартість підтвердить менеджер")
    else:
        lines.append(f"💵 Вартість доставки (НП): <b>{price.get('cost', '—')}</b> ₴")
        if price.get("redelivery"):
            # Орієнтовно: оцінка йде через RedeliveryCalculate, фактичну комісію
            # «Контроль оплати» (NovaPay) підтверджує менеджер при відправленні.
            lines.append(f"   Комісія за переказ COD: ≈{price['redelivery']} ₴ (орієнтовно)")
        if price.get("eta"):
            lines.append(f"📅 Орієнтовна доставка: {html.escape(str(price['eta']))}")
    return "\n".join(lines)
