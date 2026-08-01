"""Клавиатуры для контакта и role-based home-экрана."""

from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from app.db.models.enums import UserRole


def build_contact_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Надіслати номер телефону", request_contact=True)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


CLIENT_TEAM_BUTTON = "👥 Команда"

_CLIENT_MENU_ROWS = [
    ["📦 Товари", "🚚 Створити ТТН"],
    ["📬 Відправлення", "📊 Статистика"],
    ["💬 Звернення до менеджера", "⚙️ Налаштування"],
]

_MANAGER_MENU_ROWS = [
    ["🟢 Я на зв'язку", "📬 Відправлення"],
    ["📦 Склад", "👥 Клієнти"],
    ["💬 Підтримка", "📊 Звіти"],
]

_OWNER_MENU_ROWS = [
    ["📬 Відправлення", "📦 Склад"],
    ["👥 Клієнти", "👔 Персонал"],
    ["📈 Аналітика"],
]

CLIENT_MENU_TEXTS = frozenset(
    [*(text for row in _CLIENT_MENU_ROWS for text in row), CLIENT_TEAM_BUTTON]
)

# Тексты ВСЕХ кнопок нижних панелей. Нужны хендлерам, которые ловят произвольный
# текст в своём FSM-состоянии (поиск, ввод телефона/даты, релей поддержки): без
# явного `~F.text.in_(MENU_TEXTS)` такой хендлер съедает тап кнопки меню как
# «ввод», и кнопка выглядит сломанной. Источник один: добавили кнопку в строки
# выше — она автоматически здесь. См. `app/bot/handlers/menu_escape.py`.
MENU_TEXTS = frozenset(
    CLIENT_MENU_TEXTS
    | {text for row in _MANAGER_MENU_ROWS for text in row}
    | {text for row in _OWNER_MENU_ROWS for text in row}
)


def build_role_menu(role: UserRole, *, account_owner: bool) -> ReplyKeyboardMarkup:
    """Нижняя панель роли.

    `account_owner` — обязательный: молчаливый `False` по умолчанию уже один раз
    съел кнопку «👥 Команда» на выходе из чата поддержки, поэтому каждый вызов
    обязан решить явно (см. `permissions.is_account_owner`).
    """
    if role is UserRole.client:
        # Копия внешнего списка: `append` ниже иначе мутировал бы константу модуля.
        rows = list(_CLIENT_MENU_ROWS)
        if account_owner:
            rows.append([CLIENT_TEAM_BUTTON])
    elif role is UserRole.manager:
        rows = _MANAGER_MENU_ROWS
    else:
        rows = _OWNER_MENU_ROWS

    keyboard = [[KeyboardButton(text=text) for text in row] for row in rows]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)
