"""Каркас Telegram-бота для Phase 1."""

from __future__ import annotations

from app.bot import permissions


def build_dispatcher(settings):
    from app.bot.dispatcher import build_dispatcher as _build_dispatcher

    return _build_dispatcher(settings)


__all__ = ["build_dispatcher", "permissions"]
