"""Тесты самого E2E-харнесса (`scripts/e2e/`).

Харнесс — инструмент, которым проверяют бот; если сгниёт он, прогон будет
«зелёным» просто потому, что перестал что-либо замечать. Поэтому проверяем
ровно те три свойства, на которых держится его ценность:

1. экран разбирается из настоящей aiogram-разметки (иначе тап «по видимому
   тексту» вырождается в тап по пустоте);
2. молчание бота фиксируется как дефект (главный детектор — см. `18b1995`);
3. бюджет реальных ТТН общий на процессы и не течёт при гонке.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from scripts.e2e.cascade import TtnBudget
from scripts.e2e.lib import ERROR_MARKERS, Screen, _parse_markup


def test_parse_inline_markup_keeps_text_and_data() -> None:
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔎 Пошук", callback_data="cab:ttn:search")],
            [InlineKeyboardButton(text="Кава Lavazza 7 шт", callback_data="cab:ttn:pick:3")],
        ]
    )
    inline, reply = _parse_markup(markup)

    assert reply == []
    screen = Screen(inline=inline)
    assert screen.buttons == ["🔎 Пошук", "Кава Lavazza 7 шт"]
    # Тап «по видимому тексту» — то, ради чего харнесс и хранит разметку.
    assert screen.find("Lavazza").data == "cab:ttn:pick:3"
    assert screen.find_data("cab:ttn:pick:").text == "Кава Lavazza 7 шт"
    assert screen.find("нема такої кнопки") is None


def test_parse_reply_markup_is_bottom_panel() -> None:
    markup = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📦 Товари"), KeyboardButton(text="🚚 Створити ТТН")]]
    )
    inline, reply = _parse_markup(markup)

    assert inline == []
    screen = Screen(reply=reply)
    assert screen.find_reply("Створити ТТН").text == "🚚 Створити ТТН"
    # Кнопка нижней панели не имеет callback_data — её «тап» это обычное сообщение.
    assert screen.find_reply("Товари").data is None


def test_error_markers_match_real_error_texts() -> None:
    """Маркеры должны совпадать с текстами боевого errors-router.

    Разъедутся — прогон перестанет отличать «экран отрисован» от «хендлер упал и
    пользователю показали заглушку», и отчёт станет ложно-зелёным.
    """
    from app.bot.handlers import errors

    live_texts = (
        errors._UNEXPECTED_TEXT,
        errors._KEY_UNREADABLE_TEXT,
        errors._STOCK_UNAVAILABLE_TEXT,
    )
    for marker in ERROR_MARKERS:
        assert any(marker in text for text in live_texts), (
            f"маркер «{marker}» не знайдено в errors.py"
        )


def test_ttn_budget_is_shared_and_capped(tmp_path) -> None:
    budget = TtnBudget(tmp_path / "budget.json", limit=3)

    slots = [budget.claim("persona") for _ in range(5)]

    assert [s for s in slots if s is not None] == [1, 2, 3]
    assert slots[3] is None and slots[4] is None


def test_ttn_budget_survives_concurrent_claims(tmp_path) -> None:
    """Три процесса тянут бюджет одновременно — суммарно не больше лимита.

    Счётчик в памяти здесь не годится: персоны живут в РАЗНЫХ процессах
    (роутеры-синглтоны не дают двух диспетчеров в одном), поэтому блокировка
    файловая и её надо проверять именно на гонке.
    """
    budget = TtnBudget(tmp_path / "budget.json", limit=10)

    with ThreadPoolExecutor(max_workers=8) as pool:
        claimed = list(pool.map(lambda i: budget.claim(f"p{i}"), range(40)))

    granted = [slot for slot in claimed if slot is not None]
    assert len(granted) == 10
    assert sorted(granted) == list(range(1, 11))

    state = json.loads((tmp_path / "budget.json").read_text())
    assert state["used"] == 10
    assert len(state["entries"]) == 10


def test_ttn_budget_release_returns_slot(tmp_path) -> None:
    """Сценарий сорвался до создания ТТН — слот возвращается в общий котёл."""
    budget = TtnBudget(tmp_path / "budget.json", limit=1)

    first = budget.claim("a")
    assert budget.claim("b") is None

    budget.release(first)

    assert budget.claim("b") == 1
