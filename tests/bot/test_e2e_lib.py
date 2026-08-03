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


async def test_cascade_pace_holds_rhythm_from_start(monkeypatch, tmp_path) -> None:
    """Ритм считается от старта прогона, а не «поспать после ТТН».

    Иначе фактическая интенсивность падает вместе с латентностью НП, и заявленные
    «2,5 ТТН/хв» превращаются в «сколько выйдет» — на живом прогоне это значит,
    что темп задаёт чужой API, а не мы. Ровно эту ошибку уже оплатили переделкой
    sweep'а в `scripts/load/submit.py`.

    Мутация: считать паузу от конца предыдущей ТТН — суммарное ожидание станет
    полным `pace × N` вместо `pace × N − потраченное`, и тест покраснеет.
    """
    from scripts.e2e import cascade

    clock = {"now": 1000.0}
    slept: list[float] = []

    monkeypatch.setattr(cascade.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(cascade, "ARTIFACTS", tmp_path)

    class _Persona:
        name = "p"

        async def idle(self, seconds: float) -> None:
            slept.append(seconds)
            clock["now"] += seconds

    class _Human:
        async def pause(self) -> None:
            return None

    async def fake_one_ttn(persona, human, *, index, submit):
        clock["now"] += 8.0  # ТТН в живом НП стоит около восьми секунд
        return {"submitted": True}

    monkeypatch.setattr(cascade, "_one_ttn", fake_one_ttn)

    await cascade.run_cascade(
        _Persona(), human=_Human(), budget=3, run_id="pace", global_limit=10, pace_seconds=20.0
    )

    # Первая — сразу, дальше досыпаем ТОЛЬКО остаток интервала: 20 − 8 = 12.
    assert slept == [12.0, 12.0]


async def test_cascade_without_pace_keeps_old_behaviour(monkeypatch, tmp_path) -> None:
    """`pace_seconds=0` — прежний режим «встык»: ни одного лишнего ожидания."""
    from scripts.e2e import cascade

    slept: list[float] = []
    monkeypatch.setattr(cascade, "ARTIFACTS", tmp_path)

    class _Persona:
        name = "p"

        async def idle(self, seconds: float) -> None:
            slept.append(seconds)

    class _Human:
        async def pause(self) -> None:
            return None

    async def fake_one_ttn(persona, human, *, index, submit):
        return {"submitted": True}

    monkeypatch.setattr(cascade, "_one_ttn", fake_one_ttn)

    await cascade.run_cascade(
        _Persona(), human=_Human(), budget=3, run_id="nopace", global_limit=10
    )

    assert slept == []


async def test_spread_walks_forward_and_never_taps_a_missing_button() -> None:
    """Разбег по каталогу идёт ВПЕРЁД и не выдумывает кнопок.

    Две ловушки разом. Первая: `cab:ttn:page:` — общий префикс у ◀ и ▶, и со
    второй страницы первой совпадает ◀; «разбег» по префиксу ходил бы туда-сюда
    между двумя страницами, а все ТТН прогона тянули бы одни и те же шесть
    верхних SKU. Вторая: `tap_data` на отсутствующей кнопке пишет
    `missing_button` в дефекты — разбег стал бы источником ложных находок.

    Мутация: заменить поиск «▶» на `tap_data("cab:ttn:page:")` — офсеты пойдут
    вниз, и первый assert покраснеет.
    """

    from scripts.e2e.cascade import _spread_over_catalogue
    from scripts.e2e.lib import Button

    class _Screen:
        def __init__(self) -> None:
            self.offset = 0
            self.inline: list[Button] = []
            self._render()

        def _render(self) -> None:
            self.inline = [Button(text="🍏 Напої", data="cab:ttn:pcat:1")]
            if self.offset > 0:
                self.inline.append(Button(text="◀", data=f"cab:ttn:page:{self.offset - 6}"))
            if self.offset < 24:  # каталог на пять страниц
                self.inline.append(Button(text="▶", data=f"cab:ttn:page:{self.offset + 6}"))

    class _Persona:
        def __init__(self) -> None:
            self.screen = _Screen()
            self.defects: list[dict] = []
            self.taps: list[str] = []

        async def tap(self, pattern: str, *, data: str | None = None):
            self.taps.append(data or pattern)
            if data and data.startswith("cab:ttn:page:"):
                self.screen.offset = int(data.rsplit(":", 1)[1])
                self.screen._render()
            return {}

        async def tap_data(self, prefix: str, *, nth: int = 0):
            """Как настоящий: первая совпавшая кнопка, иначе дефект.

            Нужен, чтобы мутация «тапать по префиксу `cab:ttn:page:`» краснела
            по существу — уходом офсетов назад, — а не `AttributeError` фейка.
            """
            matches = [b for b in self.screen.inline if b.data and b.data.startswith(prefix)]
            if len(matches) <= nth:
                self.defects.append({"kind": "missing_button", "target": prefix})
                return {}
            return await self.tap(matches[nth].text, data=matches[nth].data)

    class _Rng:
        """Детерминированно: первая категория и ровно четыре шага вперёд.

        Со случайным seed длина разбега плавает, и «шаг назад» пряталcя бы за
        прогоном длиной в один шаг — тест был бы зелёным по удаче.
        """

        @staticmethod
        def randrange(*args: int) -> int:
            return 4 if len(args) == 2 else 0

    class _Human:
        rng = _Rng()

    persona = _Persona()
    await _spread_over_catalogue(persona, _Human())

    pages = [int(t.rsplit(":", 1)[1]) for t in persona.taps if t.startswith("cab:ttn:page:")]
    assert pages == [6, 12, 18, 24], "разбег обязан двигаться вперёд"
    assert persona.screen.offset == pages[-1]
    assert persona.defects == [], "разбег не должен порождать дефектов"


async def test_spread_stops_at_the_end_of_the_catalogue() -> None:
    """Каталог короче разбега — упираемся в конец и выходим, а не тапаем пустоту."""
    import random

    from scripts.e2e.cascade import _spread_over_catalogue
    from scripts.e2e.lib import Button

    class _Persona:
        def __init__(self) -> None:
            self.screen = type("S", (), {"inline": [Button(text="🍏", data="cab:ttn:pcat:1")]})()
            self.defects: list[dict] = []
            self.taps: list[str] = []

        async def tap(self, pattern: str, *, data: str | None = None):
            self.taps.append(data or pattern)
            return {}

        async def tap_data(self, prefix: str, *, nth: int = 0):
            matches = [b for b in self.screen.inline if b.data and b.data.startswith(prefix)]
            if len(matches) <= nth:
                self.defects.append({"kind": "missing_button", "target": prefix})
                return {}
            return await self.tap(matches[nth].text, data=matches[nth].data)

    class _Human:
        rng = random.Random(1)

    persona = _Persona()
    await _spread_over_catalogue(persona, _Human())

    assert persona.taps == ["cab:ttn:pcat:1"]
    assert persona.defects == []


async def test_category_pick_never_taps_reset_chip() -> None:
    """«Сузить категорией» обязано сужать, а не снимать фильтр.

    `cab:ttn:pcat:all` — первый чип в клавиатуре всегда (`category_chips`
    ставит «Всі» перед категориями), поэтому тап по префиксу `cab:ttn:pcat:`
    попадал именно в него: пикер возвращался на первую страницу полного
    каталога, разбег отменялся, и корзина набиралась из тех же шести верхних
    SKU, чей остаток уже выкуплен бронями прогона. Живой прогон 2026-08-03:
    13 ТТН из 60 умерли с пустой корзиной, и в отчёте это выглядело как дефект
    бота.

    Мутация: вернуть `p.tap_data("cab:ttn:pcat:")` — тап уйдёт в `all`.
    """
    from scripts.e2e.cascade import _pick_category
    from scripts.e2e.lib import Button

    class _Persona:
        def __init__(self) -> None:
            self.screen = type(
                "S",
                (),
                {
                    "inline": [
                        Button(text="• Всі", data="cab:ttn:pcat:all"),
                        Button(text="Кава", data="cab:ttn:pcat:0"),
                        Button(text="Чай", data="cab:ttn:pcat:1"),
                    ]
                },
            )()
            self.taps: list[str] = []
            self.defects: list[dict] = []

        async def tap(self, pattern: str, *, data: str | None = None):
            self.taps.append(data or pattern)
            return {}

        async def tap_data(self, prefix: str, *, nth: int = 0):
            matches = [b for b in self.screen.inline if b.data and b.data.startswith(prefix)]
            if len(matches) <= nth:
                self.defects.append({"kind": "missing_button", "target": prefix})
                return {}
            return await self.tap(matches[nth].text, data=matches[nth].data)

    class _Human:
        rng = type("R", (), {"randrange": staticmethod(lambda *a: 0)})()

    persona = _Persona()
    await _pick_category(persona, _Human())

    assert persona.taps == ["cab:ttn:pcat:0"]
    assert "cab:ttn:pcat:all" not in persona.taps


async def test_category_pick_is_silent_without_real_categories() -> None:
    """Только «Всі» на экране — не тапаем ничего и не пишем дефект.

    Иначе у аккаунта без категорий каскад сам бы себе выдумывал `missing_button`.
    """
    from scripts.e2e.cascade import _pick_category
    from scripts.e2e.lib import Button

    class _Persona:
        def __init__(self) -> None:
            self.screen = type(
                "S", (), {"inline": [Button(text="• Всі", data="cab:ttn:pcat:all")]}
            )()
            self.taps: list[str] = []
            self.defects: list[dict] = []

        async def tap(self, pattern: str, *, data: str | None = None):
            self.taps.append(data or pattern)
            return {}

    class _Human:
        rng = type("R", (), {"randrange": staticmethod(lambda *a: 0)})()

    persona = _Persona()
    await _pick_category(persona, _Human())

    assert persona.taps == []
    assert persona.defects == []
