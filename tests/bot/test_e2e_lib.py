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
from scripts.e2e.lib import ERROR_MARKERS, Button, Screen, _parse_markup


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
    """Маркеры должны совпадать с боевыми текстами отказа.

    Разъедутся — прогон перестанет отличать «экран отрисован» от «клиент получил
    отказ», и отчёт станет ложно-зелёным.

    Источников два, и это не небрежность. Первый — глобальный errors-router:
    хендлер упал, показана заглушка. Второй — отказ НП на самом сабмите: хендлер
    отработал штатно, но документа нет. Второй маркер добавлен после прогона
    2026-08-03, где такой отказ не попал в отчёт вовсе.
    """
    from app.bot.handlers import errors, ttn
    from app.services.exceptions import TtnCreationFailed

    live_texts = (
        errors._UNEXPECTED_TEXT,
        errors._KEY_UNREADABLE_TEXT,
        errors._STOCK_UNAVAILABLE_TEXT,
        ttn._submit_error_text(TtnCreationFailed("Description is not valid"), {}),
    )
    for marker in ERROR_MARKERS:
        assert any(marker in text for text in live_texts), (
            f"маркер «{marker}» не знайдено в боєвих текстах"
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


def test_verdict_counts_attempts_not_only_error_screens() -> None:
    """Вердикт обязан видеть знаменатель.

    Прежде вердикт складывался только из находок, а находки рождались из экранов
    ошибки. Отказ НП на сабмите (`Description is not valid`) экраном ошибки не
    считался, и прогон 2026-08-03 отчитался 🟢 ЧИСТО, потеряв 17 попыток из 60:
    клиент проходил всю форму и не получал документ.

    Мутация: вернуть `_check_throughput`, смотрящий только на `submitted`
    без разбора причин, — пропадёт критичность отказа НП.
    """
    from scripts.e2e.validate import _check_throughput

    summaries = [
        {
            "persona": "veronika",
            "cascade": {
                "created": [
                    {"submitted": True, "ttn_number": "1"},
                    {"submitted": False, "reject_reason": "❌ Не вдалося створити ТТН: bad"},
                    {"submitted": False, "failed_at": "cart"},
                ],
                "dry_runs": [],
            },
        }
    ]
    findings = _check_throughput(summaries)

    rejected = {f["detail"]: f["severity"] for f in findings}
    assert any("Не вдалося створити ТТН" in d and s == "high" for d, s in rejected.items())
    assert any("cart" in d and s == "medium" for d, s in rejected.items())
    assert all("1 з 3" in f["detail"] for f in findings), "у знахідки має бути знаменник"


def test_verdict_is_critical_when_nothing_was_created() -> None:
    """Ноль созданных ТТН — критично, остальные метрики отчёта тогда пусты.

    Прогон, где 100 % сабмитов упали, имеет отличный p95: отказ быстрее успеха.
    """
    from scripts.e2e.validate import _check_throughput

    findings = _check_throughput(
        [{"persona": "x", "cascade": {"created": [{"submitted": False}] * 5}}]
    )

    assert [f["severity"] for f in findings] == ["high"]
    assert findings[0]["rule"] == "ttn_created"


def test_throughput_silent_without_cascade() -> None:
    """Прогон без каскада (одни сценарии-пробники) не должен выдумывать находок."""
    from scripts.e2e.validate import _check_throughput

    assert _check_throughput([{"persona": "x", "cascade": {}}]) == []


def test_collect_findings_runs_the_throughput_check() -> None:
    """Проверка обязана быть подключена, а не просто существовать.

    Прогон 2026-08-03 отчитался 🟢 ЧИСТО не потому, что проверок не было, а
    потому что ни одна не смотрела на знаменатель. Отдельно проверяем факт
    вызова: выпавшую из цепочки проверку иначе не отличить от молчаливой.

    Мутация: убрать `_check_throughput` из `collect_findings` — тест покраснеет.
    """
    from scripts.e2e.validate import collect_findings

    findings = collect_findings(
        steps=[],
        before={"shipments": []},
        after={"shipments": [], "movements": [], "users": []},
        summaries=[{"persona": "x", "cascade": {"created": [{"submitted": False}]}}],
    )

    assert [f["rule"] for f in findings] == ["ttn_created"]


def test_submit_failure_is_an_error_marker() -> None:
    """Отказ НП на сабмите — маркер отказа, а не «просто экран».

    Мутация: убрать `SUBMIT_FAILED_MARKER` из `ERROR_MARKERS` — тест покраснеет.
    Без этого прогон считает такой шаг успешным: заглушки errors-router нет,
    исходящее сообщение есть, молчания нет.
    """
    from scripts.e2e.lib import SUBMIT_FAILED_MARKER

    assert SUBMIT_FAILED_MARKER in ERROR_MARKERS


def test_validate_import_does_not_load_prod_env() -> None:
    """Импорт валидатора не должен переводить процесс на боевое окружение.

    `load_stand_env` делает `load_dotenv(".env.prod", override=True)`: боевые
    `DATABASE_URL`, `BOT_TOKEN`, ключи. При загрузке на импорте любой тест, зовущий
    `get_settings.cache_clear()`, дальше работал бы с продом — а гейт безопасной
    тестовой БД в `conftest` отрабатывает один раз на старте сессии и этого уже
    не заметит.

    Поймано полным прогоном: после добавления тестов на вердикт два чужих теста
    начали видеть боевой `np_sender_warehouse_ref`.

    Мутация: вынести `load_stand_env(...)` из-под `if __name__ == "__main__"`.
    """
    import os
    import subprocess
    import sys

    from scripts.e2e.env import DEFAULT_ENV_FILE

    if not DEFAULT_ENV_FILE.exists():
        return  # на CI боевого файла нет — травить нечем

    probe = (
        "import os;"
        "before = os.environ.get('NP_SENDER_WAREHOUSE_REF');"
        "import scripts.e2e.validate;"
        "print(before == os.environ.get('NP_SENDER_WAREHOUSE_REF'))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=os.getcwd(),
        env={**os.environ, "PYTHONPATH": os.getcwd()},
    )
    assert out.stdout.strip().endswith("True"), out.stdout + out.stderr


class _StepperPersona:
    """Персона на фейковом пикере со степпером — с контрактом настоящей.

    Важны две верности, обе оплачены разбором. Первая: `tap` возвращает
    **непустой** результат на успехе и `{}` только когда кнопки нет
    (`Persona.tap`, lib.py) — иначе цикл перебора позиций замыкается на первой же
    удачной, и то, что тест якобы покрывает, не выполняется. Вторая: бот на
    **правильном** количестве перерисовывает степпер (`receive_qty` →
    `build_stepper_kb`), и только на неправильном отвечает текстом без кнопок.
    Фейк, стирающий клавиатуру на любом вводе, закрепил бы неверное поведение.
    """

    def __init__(self, *, available: int = 7, stepper_survives_valid: bool = True) -> None:
        self.available = available
        self.stepper_survives_valid = stepper_survives_valid
        # Третья позиция — чтобы ранний выход был наблюдаем: перебор обязан
        # остановиться на той, что открыла степпер, а не домотать страницу.
        self._picker = [
            Button(text="🚫 Кава стара · 0 шт", data="cab:ttn:pick:0"),
            Button(text=f"Кава · {available} шт", data="cab:ttn:pick:1"),
            Button(text="🚫 Чай · 0 шт", data="cab:ttn:pick:2"),
        ]
        self._stepper = [
            Button(text="−1", data="cab:ttn:qd:-1"),
            Button(text="+1", data="cab:ttn:qd:1"),
            Button(text="+5", data="cab:ttn:qd:5"),
            Button(text=f"Макс ({available})", data="cab:ttn:qmax"),
            Button(text="✏️ Ввести число", data="cab:ttn:qnum"),
            Button(text="✓ Додати", data="cab:ttn:qok"),
        ]
        self._cart = [
            Button(text="✏️", data="cab:ttn:cedit:0"),
            Button(text="🛒 Кошик", data="cab:ttn:cart"),
        ]
        self.screen = Screen(inline=list(self._picker))
        self.defects: list[dict] = []
        self.taps: list[str] = []
        self.typed: list[str] = []

    async def tap(self, pattern: str, *, data: str | None = None):
        if data is None:
            button = self.screen.find(pattern)
            if button is None or button.data is None:
                self.defects.append({"kind": "missing_button", "target": pattern})
                return {}
            data = button.data
        self.taps.append(data)
        if data == "cab:ttn:pick:0":  # позиция с нулевым остатком степпер не открывает
            return {"ok": True}
        if data.startswith("cab:ttn:pick:") or data.startswith("cab:ttn:cedit:"):
            self.screen = Screen(inline=list(self._stepper))
        elif data == "cab:ttn:qok":
            # Бот возвращает на пикер, где живёт кнопка кошика.
            self.screen = Screen(inline=[*self._picker, *self._cart[1:]])
        elif data == "cab:ttn:cart":
            # Правка обязана ЗАМЕНЯТЬ количество — сумма не меняется.
            self.screen = Screen(text="сума товарів: 150.00", inline=list(self._cart))
        return {"ok": True}

    async def tap_data(self, prefix: str, *, nth: int = 0):
        matches = [b for b in self.screen.inline if b.data and b.data.startswith(prefix)]
        if len(matches) <= nth:
            self.defects.append({"kind": "missing_button", "target": prefix})
            return {}
        return await self.tap(matches[nth].text, data=matches[nth].data)

    async def send(self, text: str):
        self.typed.append(text)
        try:
            value = int(text)
        except ValueError:
            value = -1
        if 1 <= value <= self.available and self.stepper_survives_valid:
            self.screen = Screen(inline=list(self._stepper))
        else:
            # «❌ Кількість має бути 1–N» — текст без клавиатуры.
            self.screen = Screen(inline=[])
        return {"ok": True}


class _StepperHuman:
    """Всё «может быть» — да; мусор и пауза детерминированы."""

    def __init__(self, persona) -> None:
        self.p = persona
        self.rng = type("R", (), {"randrange": staticmethod(lambda *a: 0)})()

    def maybe(self) -> bool:
        return True

    async def double_tap(self, pattern: str):
        await self.p.tap(pattern)
        await self.p.tap(pattern)

    async def pause(self) -> None:
        return None

    async def garbage_then(self, pool, good, *, count=2):
        for value in [*list(pool)[:count], good]:
            await self.p.send(value)


async def test_cart_fills_and_invents_no_defects_at_stock_of_one() -> None:
    """Позиция с остатком 1: товар добавлен, дефектов ноль.

    Каскад вводил в поле количества «правильное» 2. У позиции, где на остатке
    ровно одна штука, двойка тоже отвергается («Кількість має бути 1–1»), экран
    отказа кнопок не несёт, и следующий тап по `cab:ttn:qok` писал
    `missing_button` — каскад сам себе выдумывал дефект бота (живой прогон
    2026-08-03).

    Мутация: вернуть в `_valid_qty` константу «2» — появится дефект.
    """
    from scripts.e2e.cascade import _fill_cart

    persona = _StepperPersona(available=1)
    added = await _fill_cart(persona, _StepperHuman(persona), items=1)

    assert added == 1, "товар обязан попасть в кошик"
    # Перебор позиций работает: первая с нулевым остатком степпер не открыла.
    assert persona.taps[:2] == ["cab:ttn:pick:0", "cab:ttn:pick:1"]
    assert persona.typed[-1] == "1", f"введено непринимаемое количество: {persona.typed}"
    assert "cab:ttn:qok" in persona.taps, "подтверждение количества не нажато"
    assert persona.defects == [], f"каскад выдумал дефект: {persona.defects}"


async def test_manual_quantity_still_produces_multi_unit_lines() -> None:
    """Ручной ввод не должен схлопывать корзину в одну штуку.

    Простая замена «2» на «1» дефект бы убрала, но многоштучных позиций этот путь
    не давал бы вовсе — а на них держатся сверка кошика, гейт oversell и
    арифметика брони. Потолок читается с кнопки «Макс (N)».

    Мутация: вернуть константу «1» — тест покраснеет.
    """
    from scripts.e2e.cascade import _valid_qty
    from scripts.e2e.lib import Button, Screen

    class _P:
        screen = Screen(inline=[Button(text="Макс (9)", data="cab:ttn:qmax")])

    class _H:
        rng = type("R", (), {"randrange": staticmethod(lambda n: n - 1)})()

    # Потолок держим на 3: корзина прогона не должна выкупать склад.
    assert _valid_qty(_P(), _H()) == "3"


async def test_stepper_regression_is_still_reported() -> None:
    """Если степпер пропал после ПРАВИЛЬНОГО количества — это дефект бота.

    Самый опасный размен при починке ложной находки — заглушить настоящую.
    Проверка «кнопка на экране есть?» вместо тапа сделала бы именно это: реальная
    поломка экрана количества прошла бы молча, а отчёт остался бы зелёным.

    Мутация: заменить `if not await p.tap_data("cab:ttn:qok")` на проверку
    `p.screen.find_data(...)` — дефект пропадёт.
    """
    from scripts.e2e.cascade import _fill_cart

    persona = _StepperPersona(available=7, stepper_survives_valid=False)
    added = await _fill_cart(persona, _StepperHuman(persona), items=1)

    assert added == 0
    assert [d["target"] for d in persona.defects] == ["cab:ttn:qok"], (
        f"поломка экрана количества осталась незамеченной: {persona.defects}"
    )


async def test_open_stepper_skips_positions_without_stock() -> None:
    """Перебор доходит до позиции, которая степпер открывает, и не врёт про кнопки.

    Идиома была скопирована в трёх драйверах, и правка доставалась им по одному:
    разбор PR #165 нашёл её только в каскаде. Здесь она одна и покрыта.

    Мутация: убрать проверку `find_data("cab:ttn:qok")` внутри цикла — перебор
    домотает страницу до конца и тапнет позицию, которая уже не нужна.
    """
    from scripts.e2e.lib import open_stepper

    persona = _StepperPersona(available=4)
    assert await open_stepper(persona) is True
    # Ровно две: на второй степпер открылся, третью трогать незачем.
    assert persona.taps == ["cab:ttn:pick:0", "cab:ttn:pick:1"]
    assert persona.defects == []


async def test_open_stepper_reports_when_nothing_opens() -> None:
    """Ни одна позиция степпер не открыла — False, и это НЕ выдуманный дефект.

    Пикер кончился честно: `tap_data` на отсутствующей `nth` пишет
    `missing_button`, поэтому перебор обязан останавливаться на числе позиций,
    которое на экране, а не на фиксированной шестёрке.
    """
    from scripts.e2e.lib import Button, Screen, open_stepper

    class _P:
        def __init__(self) -> None:
            self.screen = Screen(inline=[Button(text="🚫 Кава · 0 шт", data="cab:ttn:pick:0")])
            self.defects: list[dict] = []
            self.taps: list[str] = []

        async def tap(self, pattern: str, *, data: str | None = None):
            self.taps.append(data or pattern)
            return {"ok": True}

        async def tap_data(self, prefix: str, *, nth: int = 0):
            matches = [b for b in self.screen.inline if b.data and b.data.startswith(prefix)]
            if len(matches) <= nth:
                self.defects.append({"kind": "missing_button", "target": prefix})
                return {}
            return await self.tap(matches[nth].text, data=matches[nth].data)

    persona = _P()
    assert await open_stepper(persona) is False
    assert persona.defects == [], "перебор не должен выдумывать отсутствующие позиции"
