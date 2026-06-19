"""Каркас Telegram-бота для Phase 1."""

from __future__ import annotations

from app.bot import permissions


def build_dispatcher(settings, *, np_client=None, np_cache=None):
    from app.bot.dispatcher import build_dispatcher as _build_dispatcher

    return _build_dispatcher(settings, np_client=np_client, np_cache=np_cache)


__all__ = ["build_dispatcher", "permissions"]
