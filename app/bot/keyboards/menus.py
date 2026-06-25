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
