"""Каркас Telegram-бота для Phase 1."""

from __future__ import annotations

from app.bot import permissions


def build_dispatcher(settings, *, np_client=None, np_cache=None, redis=None):
    """Фасад пакета: ленивый импорт, чтобы `app.bot` не тянул роутеры при импорте.

    Сигнатуру держим в точности как у настоящей: молчаливо потерянный `redis`
    означал бы FSM в памяти при внешне включённом Redis — то есть ровно тот дефект,
    который переезд и закрывает, только теперь ещё и невидимый.
    """
    from app.bot.dispatcher import build_dispatcher as _build_dispatcher

    return _build_dispatcher(settings, np_client=np_client, np_cache=np_cache, redis=redis)


__all__ = ["build_dispatcher", "permissions"]
