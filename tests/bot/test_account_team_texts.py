"""Тексты экрана «Команда»: экранирование и единые метки статуса."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from app.bot.keyboards import account_team as kb
from app.bot.texts import account_team as texts
from app.db.models.enums import MembershipStatus


def _member(full_name=None, phone=None, status=MembershipStatus.active):
    return SimpleNamespace(
        user_id=uuid4(), full_name=full_name, phone=phone, status=status, role=None
    )


def test_member_card_escapes_telegram_name():
    # `full_name` — имя из Telegram, управляется пользователем. Сырым в HTML нельзя:
    # Telegram отвергнет разметку и карточка не отрисуется вовсе.
    item = _member(full_name="<b>Злий</b> & Ко")
    card = texts.member_card_text(item)
    assert "&lt;b&gt;Злий&lt;/b&gt; &amp; Ко" in card
    assert "<b>Злий</b>" not in card


def test_member_card_escapes_phone():
    item = _member(full_name="Іван", phone="<script>")
    assert "&lt;script&gt;" in texts.member_card_text(item)


def test_status_label_is_ukrainian_everywhere():
    # Регрессия: список печатал «активний», а карточка того же работника — сырое
    # `status.value` («active»). Метки жили в клавиатуре, карточка их не знала.
    for status, expected in (
        (MembershipStatus.invited, "очікує"),
        (MembershipStatus.active, "активний"),
        (MembershipStatus.blocked, "заблокований"),
    ):
        assert texts.status_label(status) == expected
        assert expected in texts.member_card_text(_member(full_name="Іван", status=status))


def test_keyboard_and_card_agree_on_status():
    item = _member(full_name="Іван", status=MembershipStatus.invited)
    markup = kb.build_team_kb([item], offset=0, total=1, limit=8)
    button_text = markup.inline_keyboard[0][0].text
    assert texts.status_label(item.status) in button_text
    assert texts.status_label(item.status) in texts.member_card_text(item)
