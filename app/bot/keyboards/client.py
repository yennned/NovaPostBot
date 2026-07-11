"""Inline-клавиатуры кабинета клиента (Фаза 3)."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.keyboards.common import category_chips
from app.services.client_settings import (
    NOTIFY_APPROVED,
    NOTIFY_LOW_STOCK,
    NOTIFY_SHIPMENT_STATUS,
    ClientSettingsView,
)
from app.services.inventory import InventoryPage
from app.services.sender_profile import SenderProfileView
from app.services.shipments import ShipmentPage

PRODUCTS_PAGE_SIZE = 6
SHIPMENTS_PAGE_SIZE = 6

NOTIFICATION_CALLBACK_TOKENS = {
    NOTIFY_APPROVED: "apr",
    NOTIFY_SHIPMENT_STATUS: "shp",
    NOTIFY_LOW_STOCK: "stk",
}
SENDER_PROFILE_FIELD_TOKENS = {
    "name": "nm",
    "sender_full_name": "fio",
    "sender_phone": "ph",
    "edrpou": "edr",
}


def _nav_row(
    prefix: str, *, offset: int, total: int, limit: int, extra: str = ""
) -> list[InlineKeyboardButton]:
    row: list[InlineKeyboardButton] = []
    if offset > 0:
        row.append(
            InlineKeyboardButton(
                text="◀",
                callback_data=f"{prefix}:{max(offset - limit, 0)}{extra}",
            )
        )
    if offset + limit < total:
        row.append(
            InlineKeyboardButton(
                text="▶",
                callback_data=f"{prefix}:{offset + limit}{extra}",
            )
        )
    return row


def build_inventory_kb(
    page: InventoryPage,
    *,
    active_category: str | None = None,
    query: str | None = None,
) -> InlineKeyboardMarkup:
    # «🧹 Скинути» показываем только при активном фильтре (поиск или категория) —
    # иначе сброс был бы no-op-редактированием (Telegram «message is not modified»)
    # и кнопка казалась бы сломанной.
    search_row = [InlineKeyboardButton(text="🔎 Пошук", callback_data="cab:psearch")]
    if query or active_category:
        search_row.append(InlineKeyboardButton(text="🧹 Скинути", callback_data="cab:pclear"))
    rows: list[list[InlineKeyboardButton]] = [search_row]
    rows.extend(category_chips(page.categories, prefix="cab:pcat", active=active_category))
    for item in page.items:
        price = f"{item.price:.2f} ₴" if item.price is not None else "—"
        name = item.name[:18]
        category = f"{item.category[:10]} · " if item.category else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{category}{name} · {price} · {item.available} шт",
                    callback_data=f"cab:products:{page.offset}",
                )
            ]
        )
    nav = _nav_row("cab:products", offset=page.offset, total=page.total, limit=page.limit)
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⌂ Головна", callback_data="home:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_shipments_kb(
    page: ShipmentPage, bucket: str, *, query: str | None = None
) -> InlineKeyboardMarkup:
    # «🧹 Скинути» показываем только при активном поиске — без него сброс был бы
    # no-op-редактированием (Telegram «message is not modified») → кнопка казалась
    # бы сломанной. Статус-фильтр — это вкладки-бакеты, «Скинути» их не трогает.
    search_row = [InlineKeyboardButton(text="🔎 Пошук", callback_data=f"cab:ssearch:{bucket}")]
    if query:
        search_row.append(
            InlineKeyboardButton(text="🧹 Скинути", callback_data=f"cab:sclear:{bucket}")
        )
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="Створені", callback_data="cab:shipments:created:0"),
            InlineKeyboardButton(text="Підтверджені", callback_data="cab:shipments:confirmed:0"),
            InlineKeyboardButton(text="Повернення", callback_data="cab:shipments:returns:0"),
        ],
        search_row,
    ]
    for item in page.items:
        label = item.ttn_number or item.recipient_name
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"cab:shipment:{bucket}:{page.offset}:{item.id}",
                )
            ]
        )
    nav = _nav_row(
        f"cab:shipments:{bucket}",
        offset=page.offset,
        total=page.total,
        limit=page.limit,
    )
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⌂ Головна", callback_data="home:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_shipment_card_kb(
    bucket: str,
    offset: int,
    shipment_id: uuid.UUID,
    *,
    can_cancel: bool,
) -> InlineKeyboardMarkup:
    rows = []
    if can_cancel:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🗑 Видалити ТТН",
                    callback_data=f"cab:cancel:{bucket}:{offset}:{shipment_id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="◀ До списку",
                callback_data=f"cab:shipments:{bucket}:{offset}",
            )
        ]
    )
    rows.append([InlineKeyboardButton(text="⌂ Головна", callback_data="home:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_stats_kb(selected: str) -> InlineKeyboardMarkup:
    periods = [("today", "Сьогодні"), ("week", "Тиждень"), ("month", "Місяць")]
    row = []
    for token, label in periods:
        marker = "• " if token == selected else ""
        row.append(
            InlineKeyboardButton(
                text=f"{marker}{label}",
                callback_data=f"cab:stats:{token}",
            )
        )
    today = date.today()
    days_row = [
        InlineKeyboardButton(
            text=(today - timedelta(days=shift)).strftime("%d.%m"),
            callback_data=f"cab:statsday:{(today - timedelta(days=shift)).isoformat()}",
        )
        for shift in range(3)
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            row,
            days_row,
            [InlineKeyboardButton(text="📅 Обрати дату", callback_data="cab:statspick")],
            [InlineKeyboardButton(text="⌂ Головна", callback_data="home:open")],
        ]
    )


def build_settings_kb(
    view: ClientSettingsView, *, account_owner: bool = True
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for item in view.notifications:
        marker = "🟢" if item.enabled else "⚪"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{marker} {item.label}",
                    callback_data=(f"cab:set:toggle:{NOTIFICATION_CALLBACK_TOKENS[item.key]}"),
                )
            ]
        )
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text="✏️ Змінити ПІБ",
                    callback_data="cab:set:edit:full_name",
                ),
                InlineKeyboardButton(
                    text="📱 Змінити телефон",
                    callback_data="cab:set:edit:phone",
                ),
            ],
            [InlineKeyboardButton(text="⌂ Головна", callback_data="home:open")],
        ]
    )
    if account_owner:
        rows.insert(
            -1,
            [InlineKeyboardButton(text="🏢 Мої ФОП", callback_data="cab:set:profiles")],
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_sender_profiles_kb(
    profiles: list[SenderProfileView], *, can_manage: bool = True
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=("⭐ " if profile.is_default else "") + profile.name,
                callback_data=f"cab:set:profile:{profile.id}",
            )
        ]
        for profile in profiles
    ]
    if can_manage:
        rows.append([InlineKeyboardButton(text="➕ Додати ФОП", callback_data="cab:set:padd")])
    rows.append([InlineKeyboardButton(text="◀ До налаштувань", callback_data="cab:set:back")])
    rows.append([InlineKeyboardButton(text="⌂ Головна", callback_data="home:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_sender_pick_kb(profiles: list[SenderProfileView]) -> InlineKeyboardMarkup:
    """Выбор ФОП-отправителя на входе в создание ТТН (когда профилей > 1)."""
    rows = [
        [
            InlineKeyboardButton(
                text=("⭐ " if profile.is_default else "") + profile.name,
                callback_data=f"ttn:sender:{profile.id}",
            )
        ]
        for profile in profiles
    ]
    rows.append([InlineKeyboardButton(text="⌂ Головна", callback_data="home:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_sender_profile_kb(
    profile: SenderProfileView, *, can_manage: bool = True
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text="✏️ Назва",
                callback_data=(f"cab:set:pedit:{SENDER_PROFILE_FIELD_TOKENS['name']}:{profile.id}"),
            ),
            InlineKeyboardButton(
                text="👤 Контакт",
                callback_data=(
                    f"cab:set:pedit:{SENDER_PROFILE_FIELD_TOKENS['sender_full_name']}:{profile.id}"
                ),
            ),
        ],
        [
            InlineKeyboardButton(
                text="📱 Телефон",
                callback_data=(
                    f"cab:set:pedit:{SENDER_PROFILE_FIELD_TOKENS['sender_phone']}:{profile.id}"
                ),
            ),
            InlineKeyboardButton(
                text="🧾 ЄДРПОУ",
                callback_data=(
                    f"cab:set:pedit:{SENDER_PROFILE_FIELD_TOKENS['edrpou']}:{profile.id}"
                ),
            ),
        ],
    ]
    if not can_manage:
        rows = []
    if can_manage and not profile.is_default:
        rows.append(
            [
                InlineKeyboardButton(
                    text="⭐ Зробити основним",
                    callback_data=f"cab:set:pdefault:{profile.id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="◀ До списку ФОП", callback_data="cab:set:profiles")])
    rows.append([InlineKeyboardButton(text="⌂ Головна", callback_data="home:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
