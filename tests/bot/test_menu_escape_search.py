"""Регрессия: тап кнопки нижнего меню не должен «съедаться» состоянием поиска/правки клиентов.

Раздел «Клієнти» (`clients_router`) подключён раньше support/staff/reports/analytics/
warehouse, поэтому его message-хендлеры в состоянии `waiting_for_search`/`waiting_for_edit`
матчатся ПЕРЕД целевым хендлером кнопки. `menu_escape` снимает FSM-стейт, но `raw_state`
резолвится один раз на апдейт, так что без явного `~F.text.in_(MENU_TEXTS)` в самом
хендлере тап любой другой кнопки уходит в него как «поисковый запрос»/«новое значение».

Проверяем на уровне фильтров хендлера (без БД): текст кнопки меню отвергается, обычный
ввод — проходит. На старом коде (фильтр `~F.text.in_(MENU_TEXTS)` отсутствовал) тест падает.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.bot.handlers.clients_manage import router as clients_router
from app.bot.keyboards.menus import MENU_TEXTS
from magic_filter import MagicFilter


def _handler(name: str):
    return next(h for h in clients_router.message.handlers if h.callback.__name__ == name)


def _magic_filters(handler) -> list:
    """Bound `MagicFilter.resolve` каждого F-фильтра хендлера (State-фильтр пропускаем)."""
    out = []
    for f in handler.filters:
        if isinstance(getattr(f.callback, "__self__", None), MagicFilter):
            out.append(f.callback)
    return out


def _rejects_menu_text(name: str) -> None:
    filters = _magic_filters(_handler(name))
    assert filters, f"{name}: нет magic-фильтров — некорректная регистрация"
    menu_msg = SimpleNamespace(text=next(iter(MENU_TEXTS)))  # текст кнопки нижнего меню
    query_msg = SimpleNamespace(text="Іван Пошук")  # обычный ввод
    # хотя бы один фильтр хендлера отвергает текст кнопки меню (тап уходит дальше по роутерам)
    assert any(not bool(f(menu_msg)) for f in filters), (
        f"{name}: текст кнопки меню не отсеивается — тап будет съеден"
    )
    # нормальный ввод все magic-фильтры пропускают
    assert all(bool(f(query_msg)) for f in filters)


def test_client_search_state_ignores_menu_buttons():
    _rejects_menu_text("receive_search")


def test_client_edit_state_ignores_menu_buttons():
    _rejects_menu_text("receive_edit")
