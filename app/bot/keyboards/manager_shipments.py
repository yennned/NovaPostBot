"""Inline-клавиатуры manager shipment queue."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.services.manager_shipments import ManagerShipmentCard, ManagerShipmentPage

PAGE_SIZE = 6


def build_queue_kb(page: ManagerShipmentPage) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=_tab_label("created", "Створені", page),
                callback_data="mq:list:created:0",
            ),
            InlineKeyboardButton(
                text=_tab_label("confirmed", "Підтверджені", page),
                callback_data="mq:list:confirmed:0",
            ),
            InlineKeyboardButton(
                text=_tab_label("returns", "Повернення", page),
                callback_data="mq:list:returns:0",
            ),
        ],
        [
            InlineKeyboardButton(text="🔎 Пошук", callback_data=f"mq:search:{page.bucket}"),
            InlineKeyboardButton(text="🧹 Скинути", callback_data=f"mq:clear:{page.bucket}"),
        ],
    ]
    for item in page.items:
        label = item.ttn_number or item.recipient_name
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"mq:card:{page.bucket}:{page.offset}:{item.id}",
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if page.offset > 0:
        nav.append(
            InlineKeyboardButton(
                text="◀️",
                callback_data=f"mq:list:{page.bucket}:{max(page.offset - page.limit, 0)}",
            )
        )
    if page.offset + page.limit < page.total:
        nav.append(
            InlineKeyboardButton(
                text="▶️",
                callback_data=f"mq:list:{page.bucket}:{page.offset + page.limit}",
            )
        )
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_card_kb(bucket: str, offset: int, card: ManagerShipmentCard) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if card.can_confirm:
        rows.append(
            [
                InlineKeyboardButton(
                    text="✅ Підтвердити",
                    callback_data=f"mq:confirm:{bucket}:{offset}:{card.shipment.id}",
                )
            ]
        )
    if card.can_cancel:
        rows.append(
            [
                InlineKeyboardButton(
                    text="❌ Скасувати",
                    callback_data=f"mq:cancel:{bucket}:{offset}:{card.shipment.id}",
                )
            ]
        )
    if card.can_receive_return:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔄 Оглянути повернення",
                    callback_data=f"mq:return:{bucket}:{offset}:{card.shipment.id}",
                )
            ]
        )
    if card.can_mark_lost or card.can_mark_damaged:
        issue_row: list[InlineKeyboardButton] = []
        if card.can_mark_lost:
            issue_row.append(
                InlineKeyboardButton(
                    text="⚠️ Втрата",
                    callback_data=f"mq:lost:{bucket}:{offset}:{card.shipment.id}",
                )
            )
        if card.can_mark_damaged:
            issue_row.append(
                InlineKeyboardButton(
                    text="⚠️ Пошкодження",
                    callback_data=f"mq:damaged:{bucket}:{offset}:{card.shipment.id}",
                )
            )
        rows.append(issue_row)
    rows.append(
        [
            InlineKeyboardButton(
                text="◀️ До списку",
                callback_data=f"mq:list:{bucket}:{offset}",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_return_inspection_kb(
    bucket: str,
    offset: int,
    card: ManagerShipmentCard,
    decisions: dict[str, bool],
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for index, item in enumerate(card.shipment.items):
        accepted = decisions.get(item.sku, True)
        marker = "✅" if accepted else "⚠️"
        target = "На склад" if accepted else "Брак"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{marker} {item.sku} ×{item.quantity} · {target}",
                    callback_data=f"mq:rit:{index}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="✅ Прийняти повернення", callback_data="mq:ria"),
            InlineKeyboardButton(text="◀️ Назад", callback_data="mq:rib"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="📬 До черги",
                callback_data=f"mq:list:{bucket}:{offset}",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _tab_label(token: str, label: str, page: ManagerShipmentPage) -> str:
    marker = "• " if token == page.bucket else ""
    return f"{marker}{label} ({page.counts.get(token, 0)})"
