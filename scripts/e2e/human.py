"""«Человеческий шум» — не украшение, а профиль реально находившихся дефектов.

Каждый примитив здесь соответствует классу багов, который уже ловили в проде:

* `double_tap`            → дубли ТТН, двойное списание (single-flight в `ttn.py`);
* `stale_tap`             → устаревший `callback_data` с прошлого экрана (`ce77398`);
* `menu_escape_mid_flow`  → тап нижнего меню посреди FSM (`802022a`, `19ce0c8`, `12f2b17`);
* `typo_then_fix`         → пустой/ошибочный поиск города, затем правильный;
* `garbage_input`         → мусор в вес/телефон/ЕДРПОУ/кількість;
* `panic_spam`            → нервная серия тапов по одной кнопке.

Шум детерминирован: `Human(seed=...)` — чтобы падение воспроизводилось.
"""

from __future__ import annotations

import random
from typing import Any

from scripts.e2e.lib import Persona

#: Мусор, который живой человек реально вводит в поля.
GARBAGE_WEIGHT = ["дохера", "-5", "0", "99999", "1,5,7", " "]
GARBAGE_PHONE = ["12", "телефон", "+3809", "00000000000"]
GARBAGE_EDRPOU = ["абвгд", "1", "-"]
GARBAGE_QTY = ["-3", "0", "мільйон", "999999"]


class Human:
    """Обёртка над персоной, добавляющая человеческую неаккуратность."""

    def __init__(self, persona: Persona, *, seed: int = 0, intensity: float = 0.35) -> None:
        self.p = persona
        self.rng = random.Random(seed)
        self.intensity = intensity

    def maybe(self) -> bool:
        return self.rng.random() < self.intensity

    async def pause(self) -> None:
        """Живой человек думает, а не строчит апдейты в цикле."""
        await self.p.idle(self.rng.uniform(0.2, 1.8))

    async def double_tap(self, pattern: str) -> list[dict[str, Any]]:
        """Тот же тап дважды подряд без паузы."""
        first = await self.p.tap(pattern)
        second = await self.p.tap(pattern, data=self._last_data(first))
        return [first, second]

    async def panic_spam(self, pattern: str, times: int = 4) -> list[dict[str, Any]]:
        entries = [await self.p.tap(pattern)]
        data = self._last_data(entries[0])
        for _ in range(times - 1):
            entries.append(await self.p.tap(pattern, data=data))
        return entries

    async def stale_tap(self, data: str) -> dict[str, Any]:
        """Тап по кнопке, которой на текущем экране уже нет."""
        return await self.p.tap("<stale>", data=data)

    async def typo_then_fix(self, wrong: str, right: str) -> list[dict[str, Any]]:
        entries = [await self.p.send(wrong)]
        await self.pause()
        entries.append(await self.p.send(right))
        return entries

    async def garbage_then(
        self, pool: list[str], good: str, *, count: int = 2
    ) -> list[dict[str, Any]]:
        """Ввести `count` мусорных значений, затем правильное."""
        entries = []
        for value in self.rng.sample(pool, min(count, len(pool))):
            entries.append(await self.p.send(value))
        entries.append(await self.p.send(good))
        return entries

    async def menu_escape_mid_flow(self, menu_button: str) -> dict[str, Any]:
        """Уйти в нижнее меню посреди сценария и вернуться."""
        return await self.p.send(menu_button)

    async def back_and_change(self, back_pattern: str = "Назад|◀") -> dict[str, Any]:
        return await self.p.tap(back_pattern)

    @staticmethod
    def _last_data(entry: dict[str, Any]) -> str | None:
        target = entry.get("target", "")
        if "[" in target and target.endswith("]"):
            return target[target.rindex("[") + 1 : -1]
        return None
