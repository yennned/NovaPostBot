"""Inline-клавиатуры потока создания ТТН (Фаза 4, PR 9). Namespace `cab:ttn:*`.

Длинные значения (sku/ref) в callback_data НЕ кладём (лимит 64 байта) — держим
списки в FSM-data, в callback идёт короткий индекс/токен. Экраны обновляются
`edit_text`, как в кабинете Фазы 3.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.keyboards.common import category_chips, home_button
from app.services.inventory import InventoryPage

TTN_PAGE_SIZE = 6

# Пресеты «коробок»: токен → подпись (с тиром веса). Габариты «Власних розмірів»
# не собираем — только пресет-метка на ТТН. Выбор коробки подставляет вес
# (верхняя граница тира, см. SIZE_DEFAULT_WEIGHT) — клиенту достаточно выбрать
# коробку, чтобы появилась «Далі»; «⚖️ Вказати вагу» остаётся точным override.
SIZE_PRESETS: dict[str, str] = {
    "s": "Мала (до 2 кг)",
    "m": "Середня (до 10 кг)",
    "l": "Велика (до 30 кг)",
}
SIZE_DEFAULT_WEIGHT: dict[str, str] = {"s": "2", "m": "10", "l": "30"}
DEFAULT_SIZE_TOKEN = "s"  # noqa: S105 — это пресет коробки, не секрет


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


def build_cart_picker_kb(
    page: InventoryPage, *, cart_count: int, active_category: str | None = None
) -> InlineKeyboardMarkup:
    """Список товаров для набора корзины (по индексу страницы; sku — в FSM-data).

    Браузинг как в «Товари»: сначала чипы категорий (`cab:ttn:pcat:*`), потом
    товары. Артикул в подписи не показываем — выбор идёт по индексу.
    """
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="🔎 Пошук", callback_data="cab:ttn:search"),
            InlineKeyboardButton(text="🧹 Скинути", callback_data="cab:ttn:searchclear"),
        ]
    ]
    rows.extend(category_chips(page.categories, prefix="cab:ttn:pcat", active=active_category))
    for idx, item in enumerate(page.items):
        prefix = "🚫 " if item.available <= 0 else ""
        category = f"{item.category[:10]} · " if item.category else ""
        price = f"{item.price:.2f} ₴" if item.price is not None else "—"
        rows.append(
            [
                InlineKeyboardButton(
                    text=(f"{prefix}{category}{item.name[:16]} · {price} · {item.available} шт"),
                    callback_data=f"cab:ttn:pick:{idx}",
                )
            ]
        )
    nav = _nav_row(page.offset, page.total, page.limit)
    if nav:
        rows.append(nav)
    cart_label = f"🧺 Кошик ({cart_count})" if cart_count else "🧺 Кошик порожній"
    rows.append([InlineKeyboardButton(text=cart_label, callback_data="cab:ttn:cart")])
    rows.append(
        [
            InlineKeyboardButton(text="⌂ Головна", callback_data="home:open"),
            InlineKeyboardButton(text="✖ Скасувати", callback_data="cab:ttn:cancel"),
        ]
    )
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
    rows.append(
        [
            InlineKeyboardButton(text="⌂ Головна", callback_data="home:open"),
            InlineKeyboardButton(text="✖ Скасувати", callback_data="cab:ttn:cancel"),
        ]
    )
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
            InlineKeyboardButton(text="⌂ Головна", callback_data="home:open"),
            InlineKeyboardButton(text="✖ Скасувати", callback_data="cab:ttn:cancel"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_recipient_kind_kb() -> InlineKeyboardMarkup:
    """Розвилка типу отримувача (керує наявністю кроку ЄДРПОУ).

    TODO (PR 9e, опц. — отложено): сюда добавить блок «Останні отримувачі» —
    `shipments.last_recipients(client)` → кнопки `cab:ttn:rcpt:<idx>`. Тап
    подставляет ТОЛЬКО name/phone/kind/edrpou (НЕ місто/відділення — в БД нет
    ref-колонок) и пропускает шаги ПІБ/ЄДРПОУ/телефон, ведя сразу к выбору міста.
    """
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


# Сколько результатов показывать: города — без пагинации (юзер уточняет запрос),
# відділення — окнами (их в большом городе много).
CITY_RESULTS = 9
WAREHOUSE_PAGE_SIZE = 8


def build_card_kb(*, is_org: bool) -> InlineKeyboardMarkup:
    """Карточка-зведення: ✏️-правка каждого поля + перерасчёт + отмена. Кнопка
    «✅ Відправити» добавится в PR 9d."""
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="✅ Відправити ТТН", callback_data="cab:ttn:send")],
        [
            InlineKeyboardButton(text="✏️ Отримувач", callback_data="cab:ttn:edit:name"),
            InlineKeyboardButton(text="✏️ Телефон", callback_data="cab:ttn:edit:phone"),
        ],
    ]
    if is_org:
        rows.append([InlineKeyboardButton(text="✏️ ЄДРПОУ", callback_data="cab:ttn:edit:edrpou")])
    rows.extend(
        [
            [InlineKeyboardButton(text="✏️ Місто/відділення", callback_data="cab:ttn:edit:city")],
            [
                InlineKeyboardButton(text="✏️ Вага", callback_data="cab:ttn:edit:weight"),
                InlineKeyboardButton(text="✏️ Габарити", callback_data="cab:ttn:edit:size"),
            ],
            [
                InlineKeyboardButton(text="✏️ Опис", callback_data="cab:ttn:edit:descr"),
                InlineKeyboardButton(text="✏️ Вартість", callback_data="cab:ttn:edit:insured"),
            ],
            [
                InlineKeyboardButton(text="✏️ Оплата", callback_data="cab:ttn:edit:pay"),
                InlineKeyboardButton(text="✏️ Платник", callback_data="cab:ttn:edit:payer"),
            ],
            [InlineKeyboardButton(text="🔄 Перерахувати ціну", callback_data="cab:ttn:recompute")],
            [
                InlineKeyboardButton(text="⌂ Головна", callback_data="home:open"),
                InlineKeyboardButton(text="✖ Скасувати", callback_data="cab:ttn:cancel"),
            ],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_back_to_card_kb() -> InlineKeyboardMarkup:
    """Под prompt правки поля — вернуться на карточку без изменений."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀ До картки", callback_data="cab:ttn:card")]]
    )


def build_success_kb() -> InlineKeyboardMarkup:
    """Экран успеха: создать ещё одну ТТН."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚚 Створити ще одну", callback_data="cab:ttn:again")],
            [InlineKeyboardButton(text="⌂ Головна", callback_data="home:open")],
        ]
    )


def build_size_edit_kb(current: str) -> InlineKeyboardMarkup:
    row = [
        InlineKeyboardButton(
            text=("• " if token == current else "") + label,
            callback_data=f"cab:ttn:setsz:{token}",
        )
        for token, label in SIZE_PRESETS.items()
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            row,
            [InlineKeyboardButton(text="◀ До картки", callback_data="cab:ttn:card")],
        ]
    )


def build_payer_edit_kb(current: str) -> InlineKeyboardMarkup:
    def _mark(value: str, label: str) -> str:
        return ("• " if current == value else "") + label

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_mark("Recipient", "Отримувач"), callback_data="cab:ttn:setpr:r"
                )
            ],
            [
                InlineKeyboardButton(
                    text=_mark("Sender", "Відправник"), callback_data="cab:ttn:setpr:s"
                )
            ],
            [InlineKeyboardButton(text="◀ До картки", callback_data="cab:ttn:card")],
        ]
    )


def build_payment_edit_kb(current: str) -> InlineKeyboardMarkup:
    def _mark(value: str, label: str) -> str:
        return ("• " if current == value else "") + label

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_mark("prepay", "Передоплата"), callback_data="cab:ttn:setpm:prepay"
                )
            ],
            [
                InlineKeyboardButton(
                    text=_mark("cod", "Накладений платіж"), callback_data="cab:ttn:setpm:cod"
                )
            ],
            [InlineKeyboardButton(text="◀ До картки", callback_data="cab:ttn:card")],
        ]
    )


def build_cancel_kb() -> InlineKeyboardMarkup:
    """Клавиатура под prompt текстового ввода: вихід у меню + «Скасувати»."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                home_button(),
                InlineKeyboardButton(text="✖ Скасувати", callback_data="cab:ttn:cancel"),
            ]
        ]
    )


def build_city_results_kb(cities: list[dict]) -> InlineKeyboardMarkup:
    """Результаты поиска города (по индексу; список City — в FSM-data)."""
    rows: list[list[InlineKeyboardButton]] = []
    for idx, city in enumerate(cities[:CITY_RESULTS]):
        area = f" ({city['area']})" if city.get("area") else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{city['name']}{area}"[:60],
                    callback_data=f"cab:ttn:city:{idx}",
                )
            ]
        )
    rows.append(
        [home_button(), InlineKeyboardButton(text="✖ Скасувати", callback_data="cab:ttn:cancel")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_warehouse_results_kb(warehouses: list[dict], *, offset: int) -> InlineKeyboardMarkup:
    """Окно відділень (абсолютный индекс в callback; список — в FSM-data) + пошук за №."""
    rows: list[list[InlineKeyboardButton]] = []
    window = warehouses[offset : offset + WAREHOUSE_PAGE_SIZE]
    for i, wh in enumerate(window):
        abs_idx = offset + i
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"№{wh['number']}: {wh['description']}"[:60],
                    callback_data=f"cab:ttn:wh:{abs_idx}",
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(
            InlineKeyboardButton(
                text="◀", callback_data=f"cab:ttn:whpage:{max(offset - WAREHOUSE_PAGE_SIZE, 0)}"
            )
        )
    if offset + WAREHOUSE_PAGE_SIZE < len(warehouses):
        nav.append(
            InlineKeyboardButton(
                text="▶", callback_data=f"cab:ttn:whpage:{offset + WAREHOUSE_PAGE_SIZE}"
            )
        )
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔎 Знайти за №", callback_data="cab:ttn:whfind")])
    rows.append(
        [home_button(), InlineKeyboardButton(text="✖ Скасувати", callback_data="cab:ttn:cancel")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
