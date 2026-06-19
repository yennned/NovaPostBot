"""Inline-клавиатуры потока создания ТТН (Фаза 4, PR 9). Namespace `cab:ttn:*`.

Длинные значения (sku/ref) в callback_data НЕ кладём (лимит 64 байта) — держим
списки в FSM-data, в callback идёт короткий индекс/токен. Экраны обновляются
`edit_text`, как в кабинете Фазы 3.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.services.inventory import InventoryPage

TTN_PAGE_SIZE = 6

# Пресеты габаритов: токен → подпись. Габариты «Власних розмірів» в Фазе 4 не
# собираем (домен create_shipment их не несёт) — только пресет-метка на ТТН.
SIZE_PRESETS: dict[str, str] = {
    "s": "Стандарт (S)",
    "m": "Середня (M)",
    "l": "Велика (L)",
}
DEFAULT_SIZE_TOKEN = "s"  # noqa: S105 — это пресет габаритов, не секрет


def _nav_row(offset: int, total: int, limit: int) -> list[InlineKeyboardButton]:
    """Пагинация ◀/▶ по товарам (offset-based, как в кабинете)."""
    row: list[InlineKeyboardButton] = []
    if offset > 0:
        row.append(
            InlineKeyboardButton(text="◀", callback_data=f"cab:ttn:page:{max(offset - limit, 0)}")
        )
    if offset + limit < total:
        row.append(InlineKeyboardButton(text="▶", callback_data=f"cab:ttn:page:{offset + limit}"))
    return row


def build_cart_picker_kb(page: InventoryPage, *, cart_count: int) -> InlineKeyboardMarkup:
    """Список товаров для набора корзины (по индексу страницы; sku — в FSM-data)."""
    rows: list[list[InlineKeyboardButton]] = []
    for idx, item in enumerate(page.items):
        prefix = "🚫 " if item.available <= 0 else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{prefix}{item.sku} · {item.name[:22]} · {item.available} шт",
                    callback_data=f"cab:ttn:pick:{idx}",
                )
            ]
        )
    nav = _nav_row(page.offset, page.total, page.limit)
    if nav:
        rows.append(nav)
    cart_label = f"🧺 Кошик ({cart_count})" if cart_count else "🧺 Кошик порожній"
    rows.append([InlineKeyboardButton(text=cart_label, callback_data="cab:ttn:cart")])
    rows.append([InlineKeyboardButton(text="✖ Скасувати", callback_data="cab:ttn:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_stepper_kb(*, qty: int, available: int) -> InlineKeyboardMarkup:
    """Степпер количества для выбранной позиции (кнопки + запасной ввод числа)."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="−1", callback_data="cab:ttn:qd:-1"),
            InlineKeyboardButton(text=f"{qty} шт", callback_data="cab:ttn:qnoop"),
            InlineKeyboardButton(text="+1", callback_data="cab:ttn:qd:1"),
        ],
        [
            InlineKeyboardButton(text="+5", callback_data="cab:ttn:qd:5"),
            InlineKeyboardButton(text="+10", callback_data="cab:ttn:qd:10"),
            InlineKeyboardButton(text=f"Макс ({available})", callback_data="cab:ttn:qmax"),
        ],
        [InlineKeyboardButton(text="✏️ Ввести число", callback_data="cab:ttn:qnum")],
        [
            InlineKeyboardButton(text="✓ Додати", callback_data="cab:ttn:qok"),
            InlineKeyboardButton(text="◀ Назад", callback_data="cab:ttn:page:0"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_cart_review_kb(skus: list[str]) -> InlineKeyboardMarkup:
    """Перегляд кошика: правка/видалення позиции по индексу + переходы."""
    rows: list[list[InlineKeyboardButton]] = []
    for idx in range(len(skus)):
        rows.append(
            [
                InlineKeyboardButton(text=f"✏️ #{idx + 1}", callback_data=f"cab:ttn:cedit:{idx}"),
                InlineKeyboardButton(text=f"❌ #{idx + 1}", callback_data=f"cab:ttn:crm:{idx}"),
            ]
        )
    rows.append([InlineKeyboardButton(text="➕ Додати ще товар", callback_data="cab:ttn:page:0")])
    if skus:
        rows.append([InlineKeyboardButton(text="➡️ Далі: параметри", callback_data="cab:ttn:next")])
    rows.append([InlineKeyboardButton(text="✖ Скасувати", callback_data="cab:ttn:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_parcel_kb(*, size_token: str, weight_set: bool) -> InlineKeyboardMarkup:
    """Экран «Параметри посилки»: вага (текст) + пресет габаритов (кнопки)."""
    size_row = [
        InlineKeyboardButton(
            text=("• " if token == size_token else "") + label,
            callback_data=f"cab:ttn:sz:{token}",
        )
        for token, label in SIZE_PRESETS.items()
    ]
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="⚖️ Вказати вагу", callback_data="cab:ttn:wt")],
        size_row,
    ]
    if weight_set:
        rows.append(
            [InlineKeyboardButton(text="➡️ Далі: отримувач", callback_data="cab:ttn:torcpt")]
        )
    rows.append(
        [
            InlineKeyboardButton(text="◀ Назад", callback_data="cab:ttn:cart"),
            InlineKeyboardButton(text="✖ Скасувати", callback_data="cab:ttn:cancel"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_recipient_kind_kb() -> InlineKeyboardMarkup:
    """Розвилка типу отримувача (керує наявністю кроку ЄДРПОУ в PR 9b)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👤 Приватна особа", callback_data="cab:ttn:rk:p")],
            [InlineKeyboardButton(text="🏢 Організація (ТОВ/ФОП)", callback_data="cab:ttn:rk:o")],
            [
                InlineKeyboardButton(text="◀ Назад", callback_data="cab:ttn:parcel"),
                InlineKeyboardButton(text="✖ Скасувати", callback_data="cab:ttn:cancel"),
            ],
        ]
    )
