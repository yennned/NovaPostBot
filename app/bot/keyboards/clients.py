"""Inline-клавиатуры раздела «Клієнти» и manager-side возвратов.

callback_data (token = `<status|all>`):
- `cl:list:<token>:<offset>` — показать список (фильтр+страница)
- `cl:card:<token>:<uuid>` — карточка клиента
- `cl:act:<action>:<uuid>` — действие (approve/block/unblock/archive/restore)
- `cl:edit:<token>:<uuid>` — выбор поля для правки профиля
- `cl:editf:<field>:<token>:<uuid>` — правка поля (full_name/phone)
- `cl:search:<token>` — запрос строки поиска
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.keyboards.common import nav_footer
from app.bot.texts.clients import STATUS_LABELS, client_list_button
from app.db.models.enums import UserStatus
from app.services.clients import ClientCard, ClientPage
from app.services.manager_returns import ManagerReturnCard, ManagerReturnPage

PAGE_SIZE = 5

# Порядок вкладок-фильтров.
_TABS: list[tuple[str, str]] = [
    ("pending", STATUS_LABELS[UserStatus.pending]),
    ("active", STATUS_LABELS[UserStatus.active]),
]


def status_token(status: UserStatus | None) -> str:
    return status.value if status is not None else "all"


def parse_status_token(tab: str) -> UserStatus | None:
    return None if tab == "all" else UserStatus(tab)


def build_clients_list_kb(page: ClientPage, token: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    # Вкладки-фильтры (активная помечена •).
    tab_row = [
        InlineKeyboardButton(
            text=(f"• {label}" if key == token else label),
            callback_data=f"cl:list:{key}:0",
        )
        for key, label in _TABS
    ]
    rows.append(tab_row)

    # Клиенты текущей страницы.
    for item in page.items:
        rows.append(
            [
                InlineKeyboardButton(
                    text=client_list_button(item), callback_data=f"cl:card:{token}:{item.id}"
                )
            ]
        )

    # Пагинация.
    nav: list[InlineKeyboardButton] = []
    if page.offset > 0:
        prev_offset = max(0, page.offset - page.limit)
        nav.append(InlineKeyboardButton(text="◀", callback_data=f"cl:list:{token}:{prev_offset}"))
    if page.offset + page.limit < page.total:
        next_offset = page.offset + page.limit
        nav.append(InlineKeyboardButton(text="▶", callback_data=f"cl:list:{token}:{next_offset}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="🔎 Пошук", callback_data=f"cl:search:{token}")])
    rows.extend(nav_footer())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_client_card_kb(
    card: ClientCard, token: str, *, can_edit: bool = False
) -> InlineKeyboardMarkup:
    # Единое «удалённое» состояние — `blocked` (скрытие + запрет доступа),
    # обратимо «Розблокувати» → active. Отдельной кнопки «Архів» больше нет;
    # `archived` — legacy: новых архиваций из карточки не создаём, но
    # существующие архивные клиенты остаются восстанавливаемыми.
    actions: list[tuple[str, str]] = []
    if card.status is UserStatus.pending:
        actions = [("approve", "✅ Підтвердити"), ("block", "🚫 Заблокувати")]
    elif card.status is UserStatus.active:
        actions = [("block", "🚫 Заблокувати")]
    elif card.status is UserStatus.blocked:
        actions = [("unblock", "✅ Розблокувати")]
    elif card.status is UserStatus.archived:
        actions = [("restore", "♻️ Відновити")]

    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"cl:act:{action}:{card.id}")]
        for action, label in actions
    ]
    # Правка профиля клиента — только владелец (per-flag убран). Кнопку прячем,
    # чтобы менеджер не видел её и не упирался в отказ на submit.
    if can_edit:
        rows.append(
            [InlineKeyboardButton(text="✏️ Редагувати", callback_data=f"cl:edit:{token}:{card.id}")]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="📦 Повернення",
                callback_data=f"cl:returns:{token}:{card.id}:0",
            )
        ]
    )
    rows.extend(nav_footer(back=f"cl:list:{token}:0", back_label="До списку"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_edit_fields_kb(token: str, client_id) -> InlineKeyboardMarkup:
    """Выбор редактируемого поля карточки клиента."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Ім'я", callback_data=f"cl:editf:full_name:{token}:{client_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Телефон", callback_data=f"cl:editf:phone:{token}:{client_id}"
                )
            ],
            *nav_footer(back=f"cl:card:{token}:{client_id}", back_label="Назад"),
        ]
    )


def build_client_returns_kb(page: ManagerReturnPage, token: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for item in page.items:
        label = item.ttn_number or item.recipient_name
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"cl:retcard:{token}:{page.offset}:{item.id}",
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if page.offset > 0:
        nav.append(
            InlineKeyboardButton(
                text="◀",
                callback_data=(
                    f"cl:returns:{token}:{page.client_id}:{max(page.offset - page.limit, 0)}"
                ),
            )
        )
    if page.offset + page.limit < page.total:
        nav.append(
            InlineKeyboardButton(
                text="▶",
                callback_data=f"cl:returns:{token}:{page.client_id}:{page.offset + page.limit}",
            )
        )
    if nav:
        rows.append(nav)
    rows.extend(nav_footer(back=f"cl:card:{token}:{page.client_id}", back_label="До клієнта"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_return_card_kb(card: ManagerReturnCard, token: str, offset: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if card.can_receive:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔄 Повернення замовлення",
                    callback_data=f"cl:retrecv:{token}:{offset}:{card.shipment.id}",
                )
            ]
        )
    rows.extend(
        nav_footer(
            back=f"cl:returns:{token}:{card.client_id}:{offset}",
            back_label="До повернень",
        )
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
