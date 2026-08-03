"""Каскад ТТН: несколько работников создают отправления встык и одновременно.

Бюджет реальных документов НП — жёсткий и **общий на все процессы**: три
персоны, работая параллельно, суммарно не должны превысить лимит владельца.
Считаем через файл с блокировкой (`fcntl.flock`), а не через счётчик в памяти —
процессы разные.

Сценарий одной ТТН намеренно неровный: человек ошибается в весе, промахивается
городом, возвращается на шаг назад и меняет товар. Ровный «счастливый путь»
пропустил бы ровно те баги, ради которых прогон и делается.
"""

from __future__ import annotations

import fcntl
import json
import os
import random
import re
import time
from pathlib import Path
from typing import Any

from scripts.e2e.human import GARBAGE_PHONE, GARBAGE_WEIGHT, Human
from scripts.e2e.lib import ARTIFACTS, Persona

#: Города, куда отправляем. Реальные, разные — чтобы задеть кэш справочников НП
#: и по попаданиям/промахам увидеть его эффект под нагрузкой.
CITIES = [
    ("Кременчук", "Кремечук"),
    ("Полтава", "Палтава"),
    ("Дніпро", "Днипро"),
    ("Харків", "Харкив"),
    ("Львів", "Львив"),
    ("Одеса", "Одеcа"),
    ("Вінниця", "Винница"),
    ("Черкаси", "Черкази"),
    ("Суми", "Сумі"),
    ("Житомир", "Житомер"),
]

RECIPIENTS = [
    "Тестовий Отримувач Перший",
    "Тестовий Отримувач Другий",
    "Тестовий Отримувач Третій",
    "Тестовий Отримувач Четвертий",
    "Тестовий Отримувач Пʼятий",
]

PHONES = [
    "380501112233",
    "380671112244",
    "380931112255",
    "380631112266",
    "380961112277",
]


class TtnBudget:
    """Общий на все процессы счётчик созданных документов НП."""

    def __init__(self, path: Path, limit: int) -> None:
        self.path = path
        self.limit = limit
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text(json.dumps({"used": 0, "limit": limit, "entries": []}))

    def _write(self, fh, state: dict[str, Any]) -> None:
        """Записать состояние и дотолкать его на диск ДО снятия блокировки.

        Без `flush` (+`fsync`) содержимое остаётся в буфере файлового объекта и
        уходит только при `close()` — уже после `LOCK_UN`. Соседний процесс в
        этот момент видит файл усечённым (`truncate` виден, данные — нет) и
        падает на `json.load`. Поймано тестом
        `test_ttn_budget_survives_concurrent_claims`.
        """
        fh.seek(0)
        fh.truncate()
        json.dump(state, fh, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())

    def claim(self, persona: str) -> int | None:
        """Занять слот. Возвращает номер слота или None, если бюджет исчерпан."""
        with self.path.open("r+", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                state = json.load(fh)
                if state["used"] >= self.limit:
                    return None
                state["used"] += 1
                slot = state["used"]
                state["entries"].append({"slot": slot, "persona": persona, "ts": time.time()})
                self._write(fh, state)
                return slot
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def release(self, slot: int) -> None:
        """Вернуть слот, если ТТН так и не была создана (сценарий прервался)."""
        with self.path.open("r+", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                state = json.load(fh)
                state["used"] = max(0, state["used"] - 1)
                state["entries"] = [e for e in state["entries"] if e["slot"] != slot]
                self._write(fh, state)
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


async def _open_ttn(p: Persona, h: Human) -> bool:
    """Дойти от главного экрана до пикера товаров."""
    await p.send("/start")
    button = p.screen.find_reply("Створити ТТН")
    if button is None:
        p.defects.append({"kind": "no_ttn_button", "target": "🚚 Створити ТТН"})
        return False
    await p.send(button.text)

    # Если у аккаунта больше одного ФОП — появляется экран выбора отправителя.
    if p.screen.find_data("ttn:sender:") or p.screen.find_data("cab:ttn:sender:"):
        prefix = "ttn:sender:" if p.screen.find_data("ttn:sender:") else "cab:ttn:sender:"
        await p.tap_data(prefix)
    return bool(p.screen.find_data("cab:ttn:pick:") or p.screen.find_data("cab:ttn:page:"))


async def _spread_over_catalogue(p: Persona, h: Human) -> None:
    """Разойтись по каталогу перед набором корзины.

    Пикер всегда открывается на первой странице, а в листе крупного аккаунта 1636
    позиций по 5–15 штук в каждой. Без разбега все ТТН прогона тянут одни и те же
    шесть верхних SKU: их доступный остаток обнуляют брони самого прогона, и он
    останавливается о гейт oversell на втором десятке ТТН — то есть упирается не в
    то, ради чего затевался. Живой прогон 2026-08-03 встал ровно так.

    Человек с таким каталогом первые шесть строк тоже не покупает: он сначала
    выбирает категорию, потом листает. Отсюда и порядок здесь.

    Тапаем ▶ по тексту и только когда кнопка на экране есть: `tap_data` на
    отсутствующей кнопке пишет `missing_button` в дефекты, и разбег сам стал бы
    источником ложных находок. По префиксу `cab:ttn:page:` тапать тоже нельзя —
    он общий у ◀ и ▶, и со второй страницы первой совпадёт ◀, то есть «разбег»
    ходил бы туда-сюда между двумя страницами.
    """
    chips = [b for b in p.screen.inline if b.data and b.data.startswith("cab:ttn:pcat:")]
    if chips:
        chip = chips[h.rng.randrange(len(chips))]
        await p.tap(chip.text, data=chip.data)
    for _ in range(h.rng.randrange(0, 7)):
        forward = next((b for b in p.screen.inline if b.text == "▶" and b.data), None)
        if forward is None:
            return
        await p.tap(forward.text, data=forward.data)


async def _fill_cart(p: Persona, h: Human, *, items: int) -> int:
    """Набрать корзину. Человек листает, фильтрует, ошибается количеством."""
    added = 0
    await _spread_over_catalogue(p, h)
    for n in range(items):
        if h.maybe():  # полистать страницы
            await p.tap_data("cab:ttn:page:")
        if h.maybe():  # сузить категорией
            await p.tap_data("cab:ttn:pcat:")

        # Позиция с нулевым доступным остатком степпер не открывает — бот
        # отвечает алертом и оставляет пикер. Человек в этом месте просто тычет
        # в следующий товар, поэтому перебираем, пока степпер не появится.
        for attempt in range(6):
            if not await p.tap_data("cab:ttn:pick:", nth=attempt):
                break
            if p.screen.find_data("cab:ttn:qok"):
                break
        if not p.screen.find_data("cab:ttn:qok"):
            break

        # Степпер: нервный плюс-минус, иногда ручной ввод с мусором.
        await p.tap_data("cab:ttn:qd:1")
        if h.maybe():
            await h.double_tap("\\+5|cab:ttn:qd:5")
        if h.maybe():
            await p.tap_data("cab:ttn:qnum")
            await h.garbage_then(GARBAGE_QTY_POOL, "2", count=1)
        if not await p.tap_data("cab:ttn:qok"):
            break
        added += 1

        if n < items - 1 and not p.screen.find_data("cab:ttn:pick:"):
            await p.tap_data("cab:ttn:page:")  # «➕ Додати ще товар»
        await h.pause()
    if added:
        # Безусловно: это регрессионный контроль, а не «человеческая» вариативность.
        # За `h.maybe()` он бы срабатывал через раз — ровно то, из-за чего дефект
        # правки и не был замечен.
        await _check_cart_edit(p)
    return added


_CART_TOTAL_RX = re.compile(r"сума товарів:\s*([\d.]+)")


def _cart_total(text: str) -> str | None:
    """Ориентировочная сумма товаров с экрана корзины — «денежная» подпись правки.

    None — в корзине есть позиции без цены (сумма рисуется как «—»), сравнивать нечего.
    """
    match = _CART_TOTAL_RX.search(re.sub(r"<[^>]+>", "", text))
    return match.group(1) if match else None


async def _check_cart_edit(p: Persona) -> None:
    """Зайти в правку позиции и подтвердить её, ничего не меняя.

    Правка обязана ЗАМЕНЯТЬ количество. Пока этого не было, «✏️» работала как
    «добавить»: подтверждение без единого изменения удваивало позицию, а вместе с
    ней объявленную стоимость и наложенный платёж. Ни один пробник сюда не заходил —
    поэтому дефект и дожил до прода.
    """
    if not await p.tap_data("cab:ttn:cart"):
        return
    before = _cart_total(p.screen.text)
    if not await p.tap_data("cab:ttn:cedit:"):
        return
    if not await p.tap_data("cab:ttn:qok"):
        return
    if not p.screen.find_data("cab:ttn:cedit:"):
        await p.tap_data("cab:ttn:cart")
    after = _cart_total(p.screen.text)
    if before is not None and after is not None and before != after:
        p.defects.append(
            {
                "kind": "cart_edit_changed_total",
                "target": "cab:ttn:cedit → cab:ttn:qok",
                "detail": f"сумма товаров {before} → {after} без изменения количества",
            }
        )


GARBAGE_QTY_POOL = ["-3", "0", "мільйон"]


def _extract_ttn(entry: dict[str, Any]) -> str | None:
    """Номер ТТН из экрана успеха; None — значит бот её не создал."""
    haystack = str(entry.get("screen_text") or "")
    for call in entry.get("outgoing", []):
        haystack += " " + str(call.get("text") or "")
    if "ТТН створено" not in haystack:
        return None
    match = re.search(r"\b(\d{14})\b", haystack)
    return match.group(1) if match else None


def _reject_reason(entry: dict[str, Any]) -> str:
    """Чем бот объяснил отказ — для отчёта важнее факта «не создалось»."""
    texts = [str(call.get("text") or "") for call in entry.get("outgoing", [])]
    meaningful = [t for t in texts if t and "Створюємо" not in t]
    return (meaningful[-1] if meaningful else str(entry.get("screen_text") or ""))[:200]


async def _resolve_city(p: Persona) -> bool:
    """Выбрать город — или убедиться, что бот уже перешёл к отделениям.

    На точном названии («Кременчук») бот не показывает список городов, а сразу
    рисует відділення. Жёсткий `tap cab:ttn:city:` тут ронял бы сценарий на
    ровном месте — это особенность экрана, а не дефект.
    """
    if p.screen.find_data("cab:ttn:city:"):
        return bool(await p.tap_data("cab:ttn:city:"))
    return bool(p.screen.find_data("cab:ttn:wh:"))


async def _one_ttn(p: Persona, h: Human, *, index: int, submit: bool) -> dict[str, Any]:
    """Одна ТТН целиком. `submit=False` — дойти до карточки и не отправлять."""
    started = time.perf_counter()
    result: dict[str, Any] = {"index": index, "submitted": False}

    if not await _open_ttn(p, h):
        result["failed_at"] = "open"
        return result

    added = await _fill_cart(p, h, items=h.rng.choice([1, 1, 2]))
    result["items"] = added
    if not added:
        result["failed_at"] = "cart"
        return result

    # Шаг 2 — параметри посилки.
    if not await p.tap_data("cab:ttn:next"):
        await p.tap_data("cab:ttn:cart")
        await p.tap_data("cab:ttn:next")
    await p.tap_data(f"cab:ttn:sz:{h.rng.choice(['s', 'm', 'l'])}")
    if h.maybe():
        await p.tap_data("cab:ttn:wt")
        await h.garbage_then(GARBAGE_WEIGHT, "3", count=2)
    if not await p.tap_data("cab:ttn:torcpt"):
        result["failed_at"] = "parcel"
        return result

    # Шаг 3 — отримувач.
    await p.tap_data("cab:ttn:rk:p")
    await p.send(RECIPIENTS[index % len(RECIPIENTS)])
    if h.maybe():
        await h.garbage_then(GARBAGE_PHONE, PHONES[index % len(PHONES)], count=1)
    else:
        await p.send(PHONES[index % len(PHONES)])

    # Місто: сперва с опечаткой, потом верно.
    right, wrong = CITIES[index % len(CITIES)]
    await h.typo_then_fix(wrong, right)
    if not await _resolve_city(p):
        result["failed_at"] = "city"
        return result

    # Відділення: иногда полистать, иногда найти поиском.
    if h.maybe():
        await p.tap_data("cab:ttn:whpage:")
    if not await p.tap_data("cab:ttn:wh:"):
        result["failed_at"] = "warehouse"
        return result

    # Картка: человек почти всегда что-то правит перед отправкой.
    #
    # Каждая правка — только если её кнопка ЕСТЬ на экране. Прежде сценарий тапал
    # `cab:ttn:setpm:` и `cab:ttn:back:city` вслепую, а карточка предлагает
    # `cab:ttn:edit:pay` и `cab:ttn:edit:city`. Тап несуществующей кнопки сам по
    # себе безвреден, но следом шёл `p.send(right)` — текст города в диалог, где
    # его никто не ждёт. Бот на такое молчит совершенно правильно, а `validate`
    # видел «бот не ответил ничем» и выносил 🔴 «признак падения в хендлере».
    # Пять критических находок live2 — ровно это. Красный вердикт от ошибки
    # харнесса хуже отсутствующего: настоящий сигнал в нём тонет.
    # Правка оплаты — двухэкранная: «✏️ Оплата» уводит на выбор способа, и на том
    # экране кнопки «Відправити» нет. Тап без выбора бросал ТТН прямо здесь —
    # `failed_at: card` на трети сценариев прогона live4, и выглядело это как
    # дефект бота, а не харнесса. Живой человек, открыв выбор, выбирает.
    if h.maybe() and p.screen.find_data("cab:ttn:edit:pay"):
        await p.tap_data("cab:ttn:edit:pay")
        if h.maybe() and p.screen.find_data("cab:ttn:setpm:cod"):
            await p.tap_data("cab:ttn:setpm:cod")
            # Наложенный платёж спрашивает сумму ещё одним экраном — и на нём
            # «Відправити» тоже нет. Берём сумму корзины: это единственный
            # вариант без свободного ввода.
            if p.screen.find_data("cab:ttn:cod:cart"):
                await p.tap_data("cab:ttn:cod:cart")
            elif p.screen.find_data("cab:ttn:card"):
                await p.tap_data("cab:ttn:card")
        elif p.screen.find_data("cab:ttn:setpm:prepay"):
            await p.tap_data("cab:ttn:setpm:prepay")
    if h.maybe() and p.screen.find_data("cab:ttn:recompute"):
        await p.tap_data("cab:ttn:recompute")
    if h.maybe() and p.screen.find_data("cab:ttn:edit:city"):
        await p.tap_data("cab:ttn:edit:city")
        # Текст шлём, только если бот действительно ждёт название города.
        if "місто" in p.screen.text.lower():
            await p.send(right)
            await _resolve_city(p)
            if p.screen.find_data("cab:ttn:wh:"):
                await p.tap_data("cab:ttn:wh:")

    # Страховка на будущие двухэкранные правки: любая из них может оставить нас не
    # на карточке, и тогда сценарий бросает ТТН с `failed_at: card` — то есть врёт
    # про бота. Человек в этом месте жмёт «◀ До картки», а не уходит из формы.
    if not p.screen.find_data("cab:ttn:send") and p.screen.find_data("cab:ttn:card"):
        await p.tap_data("cab:ttn:card")

    result["card_reached"] = bool(p.screen.find_data("cab:ttn:send"))
    if not result["card_reached"]:
        result["failed_at"] = "card"
        return result

    if submit:
        entry = await p.tap_data("cab:ttn:send")
        result["submit_ms"] = entry.get("total_ms")
        result["screen"] = (p.screen.text or "")[:300]
        # Успех определяем по ОТВЕТУ бота, а не по факту тапа. Иначе отказ
        # («⚠️ Склад тимчасово недоступний» при 429 от Sheets) засчитывался бы
        # как созданная ТТН — и слот общего бюджета не возвращался бы в котёл.
        result["ttn_number"] = _extract_ttn(entry)
        result["submitted"] = result["ttn_number"] is not None
        if not result["submitted"]:
            result["reject_reason"] = _reject_reason(entry)
        # Двойной тап отправки: дубля быть не должно.
        if result["submitted"] and h.maybe():
            dup = await p.tap("Відправити", data="cab:ttn:send")
            result["double_submit_ttn"] = _extract_ttn(dup)
            result["double_submit_screen"] = (dup.get("screen_text") or "")[:200]
    else:
        await p.tap_data("cab:ttn:cancel")

    result["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return result


async def run_cascade(
    persona: Persona,
    *,
    human: Human,
    budget: int,
    run_id: str,
    global_limit: int = 10,
    pace_seconds: float = 0.0,
) -> dict[str, Any]:
    """Создавать ТТН, пока не кончится личный или общий бюджет.

    `pace_seconds` — минимальный интервал между началами ТТН у ЭТОЙ персоны;
    0 (дефолт) сохраняет прежнее поведение «встык».

    Ритм держится от старта прогона, а не «поспать после ТТН»: иначе фактическая
    интенсивность падала бы вместе с латентностью НП, и заявленные «2,5 ТТН/мин»
    превращались бы в «сколько выйдет». Ровно та же ошибка была в первой версии
    нагрузочного `submit.py` и там же оплачена переделкой sweep'а.

    Зачем это на живом НП: темп прогона — согласованная величина, а не побочный
    эффект latency чужого API. Без ритма одна персона выдаёт 6–8 ТТН/мин, то есть
    втрое выше того, о чём договаривались.
    """
    shared = TtnBudget(ARTIFACTS / run_id / "ttn_budget.json", global_limit)
    created: list[dict[str, Any]] = []
    dry_runs: list[dict[str, Any]] = []
    started_at = time.monotonic()
    sent = 0

    for index in range(budget):
        if pace_seconds > 0:
            due = started_at + sent * pace_seconds
            delay = due - time.monotonic()
            if delay > 0:
                await persona.idle(delay)
        sent += 1
        slot = shared.claim(persona.name)
        if slot is None:
            # Бюджет исчерпан другими персонами — доходим до карточки без отправки.
            dry_runs.append(await _one_ttn(persona, human, index=index, submit=False))
            continue
        outcome = await _one_ttn(persona, human, index=index, submit=True)
        outcome["slot"] = slot
        if not outcome.get("submitted"):
            shared.release(slot)
        created.append(outcome)
        await human.pause()

    return {
        "created": created,
        "dry_runs": dry_runs,
        "submitted": sum(1 for c in created if c.get("submitted")),
    }


# Детерминизм отбора получателей/городов не должен зависеть от глобального random.
random.seed(0)
