"""Inline-клавіатури команди клієнтського акаунта."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.texts import account_team as texts
from app.db.models.enums import MembershipStatus
from app.services.account_team import AccountMemberView


def build_team_kb(
    items: list[AccountMemberView], *, offset: int, total: int, limit: int
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for item in items:
        label = texts.member_label(item)
        state = texts.status_label(item.status)
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{label} · {state}", callback_data=f"team:view:{item.user_id}"
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(
            InlineKeyboardButton(text="◀", callback_data=f"team:list:{max(0, offset - limit)}")
        )
    if offset + limit < total:
        nav.append(InlineKeyboardButton(text="▶", callback_data=f"team:list:{offset + limit}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="➕ Запросити працівника", callback_data="team:invite")])
    rows.append([InlineKeyboardButton(text="⌂ Головна", callback_data="home:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_member_kb(item: AccountMemberView) -> InlineKeyboardMarkup:
    action = (
        InlineKeyboardButton(text="✅ Відновити", callback_data=f"team:restore:{item.user_id}")
        if item.status is MembershipStatus.blocked
        else InlineKeyboardButton(text="⛔ Заблокувати", callback_data=f"team:block:{item.user_id}")
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [action],
            [InlineKeyboardButton(text="◀ До команди", callback_data="team:list:0")],
        ]
    )
