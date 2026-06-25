"""Клавиатуры для контакта и role-based home-экрана."""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.db.models.enums import UserRole


def build_contact_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Надіслати номер телефону", request_contact=True)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def build_role_menu(role: UserRole) -> ReplyKeyboardMarkup:
    if role is UserRole.client:
        rows = [
            ["📦 Товари", "🚚 Створити ТТН"],
            ["📬 Відправлення", "📊 Статистика"],
            ["💬 Звернення до менеджера", "⚙️ Налаштування"],
        ]
    elif role is UserRole.manager:
        rows = [
            ["🟢 Я на зв'язку", "📬 Відправлення"],
            ["📦 Склад", "👥 Клієнти"],
            ["💬 Підтримка", "📊 Звіти"],
        ]
    else:
        rows = [
            ["📬 Відправлення", "📦 Склад"],
            ["👥 Клієнти", "💬 Підтримка"],
            ["👔 Персонал", "📈 Аналітика"],
        ]

    keyboard = [[KeyboardButton(text=text) for text in row] for row in rows]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def build_home_keyboard(role: UserRole) -> InlineKeyboardMarkup:
    if role is UserRole.client:
        rows = [
            [
                InlineKeyboardButton(text="📦 Товари", callback_data="home:products"),
                InlineKeyboardButton(text="🚚 Створити ТТН", callback_data="home:ttn"),
            ],
            [
                InlineKeyboardButton(text="📬 Відправлення", callback_data="home:shipments"),
                InlineKeyboardButton(text="📊 Статистика", callback_data="home:stats"),
            ],
            [
                InlineKeyboardButton(text="💬 Підтримка", callback_data="home:support_client"),
                InlineKeyboardButton(text="⚙️ Налаштування", callback_data="home:settings"),
            ],
        ]
    elif role is UserRole.manager:
        rows = [
            [
                InlineKeyboardButton(text="🟢 Я на зв'язку", callback_data="home:duty"),
                InlineKeyboardButton(
                    text="📬 Відправлення",
                    callback_data="home:manager_shipments",
                ),
            ],
            [
                InlineKeyboardButton(text="📦 Склад", callback_data="home:warehouse"),
                InlineKeyboardButton(text="👥 Клієнти", callback_data="home:clients"),
            ],
            [
                InlineKeyboardButton(text="💬 Підтримка", callback_data="home:support_staff"),
                InlineKeyboardButton(text="📊 Звіти", callback_data="home:reports"),
            ],
        ]
    else:
        rows = [
            [
                InlineKeyboardButton(
                    text="📬 Відправлення",
                    callback_data="home:manager_shipments",
                ),
                InlineKeyboardButton(text="📦 Склад", callback_data="home:warehouse"),
            ],
            [
                InlineKeyboardButton(text="👥 Клієнти", callback_data="home:clients"),
                InlineKeyboardButton(text="💬 Підтримка", callback_data="home:support_staff"),
            ],
            [
                InlineKeyboardButton(text="👔 Персонал", callback_data="home:staff"),
                InlineKeyboardButton(text="📈 Аналітика", callback_data="home:analytics"),
            ],
        ]
    return InlineKeyboardMarkup(inline_keyboard=rows)
