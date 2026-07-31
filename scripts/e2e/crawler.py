"""Обход **всех** кнопок роли: нижняя панель → BFS по inline-клавиатурам.

Ровно так был найден баг `802022a` (тап кнопки меню в состоянии поиска
«съедался»): 544 клика по всем ролям, из них выпали три экрана. Ручной сценарий
такое не ловит — он ходит там, где автор ожидал.

Безопасность боевого стенда. Обход по умолчанию **не трогает чужие данные**:
разрушающие callback'и (удаление клиента/работника/менеджера, блокировки,
пометки «втрачено»/«пошкоджено», закрытие чужих обращений) в денилисте. Их
проверяют отдельно и только на объектах, созданных самим прогоном.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from scripts.e2e.human import Human
from scripts.e2e.lib import Persona

#: Разрушающие действия над реальными данными — в обход не берём.
#:
#: Правки полей (`cab:set:edit:`, `cab:set:pedit:`, `cl:editf:`) попали сюда не из
#: осторожности, а по факту: на первом боевом прогоне обход вошёл в «✏️ Змінити ПІБ»
#: и в правку ФОП и записал туда мусор — переименовал пятерых реальных клиентов и
#: затёр `name`/`sender_full_name`/`edrpou` у двух ФОП. Данные восстановлены
#: (`scripts/e2e/restore_names.py`), путь закрыт здесь и в `SAFE_TEXT_INPUT`.
DESTRUCTIVE = (
    "cl:del",
    "cl:delok",
    "cl:act:block",
    "cl:act:delete",
    "cl:edit",
    "cl:editf",
    "stf:delete",
    "stf:deleteok",
    "stf:block",
    "stf:flag",
    "stf:add",
    "team:delete",
    "team:deleteok",
    "team:block",
    "team:invite",
    "mq:lost",
    "mq:damaged",
    "mq:return",
    "mq:confirm",
    "mq:cancel",
    "cab:cancel",
    "sup:close",
    "sup:start",
    "sup:reply",
    "cab:set:toggle",
    "cab:set:pdefault",
    "cab:set:edit",
    "cab:set:pedit",
    "cab:set:padd",
    "cab:ttn:send",
)

#: Единственные экраны, куда обход печатает свободный текст. Всё остальное, что
#: ждёт ввода, он покидает **не печатая**: на боевом стенде любой другой текст —
#: это запись в чужие данные, а не проверка экрана.
SAFE_TEXT_INPUT = (
    "cab:psearch",
    "cab:ssearch",
    "cab:ttn:search",
    "cab:ttn:whfind",
    "cl:search",
    "sup:search",
    "stf:search",
    "mq:search",
)


@dataclass
class CrawlStats:
    visited: set[str] = field(default_factory=set)
    taps: int = 0
    presses: int = 0
    skipped_destructive: list[str] = field(default_factory=list)
    silent: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _is_destructive(data: str) -> bool:
    return any(data.startswith(prefix) for prefix in DESTRUCTIVE)


def _norm(data: str) -> str:
    """Свести `cab:shipments:created:20` к `cab:shipments:created:*`, чтобы
    пагинация не крутила обход бесконечно, но каждая ветка была посещена."""
    return re.sub(r":\d+$", ":*", data)


class Crawler:
    def __init__(
        self,
        persona: Persona,
        *,
        human: Human,
        max_depth: int = 3,
        max_taps: int = 200,
        allow_destructive: bool = False,
        search_probe: str = "кава",
    ) -> None:
        self.p = persona
        self.human = human
        self.max_depth = max_depth
        self.max_taps = max_taps
        self.allow_destructive = allow_destructive
        # Осмысленный запрос, а не мусор: поиск с попаданием проверяет и фильтр,
        # и отрисовку результатов, а «зжзж» проверял бы только пустой ответ.
        self.search_probe = search_probe
        self.stats = CrawlStats()

    async def home(self) -> None:
        """Вернуться на главный экран — надёжно, даже если застряли в FSM."""
        if self.p.screen.find("Головна|⌂"):
            await self.p.tap("Головна|⌂")
        else:
            await self.p.send("/start")

    async def crawl_role(self) -> CrawlStats:
        await self.p.send("/start")
        menu = [b.text for b in self.p.screen.reply]
        self.p.record({"action": "menu", "target": "reply_keyboard", "buttons": menu})

        for label in menu:
            entry = await self.p.send(label)
            self.stats.presses += 1
            self._account(entry, label)
            await self.human.pause()

            # Человек, попав на экран, обычно тыкает пару кнопок и уходит.
            await self._descend(depth=1)

            # Иногда — уходит в меню прямо посреди сценария (класс багов 802022a).
            if self.human.maybe():
                await self.human.menu_escape_mid_flow(menu[0])
            await self.home()

        return self.stats

    async def _descend(self, *, depth: int) -> None:
        if depth > self.max_depth or self.stats.taps >= self.max_taps:
            return

        snapshot = [b for b in self.p.screen.inline if b.data]
        for button in snapshot:
            if self.stats.taps >= self.max_taps:
                return
            data = button.data or ""
            key = _norm(data)
            if key in self.stats.visited:
                continue
            if not self.allow_destructive and _is_destructive(data):
                self.stats.skipped_destructive.append(data)
                continue
            self.stats.visited.add(key)

            entry = await self.p.tap(button.text, data=data)
            self.stats.taps += 1
            self._account(entry, data)

            # Нервный дубль — проверка, что повторный тап не ломает экран.
            if self.human.maybe():
                dup = await self.p.tap(button.text, data=data)
                self.stats.taps += 1
                self._account(dup, f"{data} (double)")

            if data in SAFE_TEXT_INPUT:
                # Поиск — единственный текстовый ввод, который ничего не сохраняет.
                await self.p.send(self.search_probe)
                await self.home()
                continue

            await self._descend(depth=depth + 1)
            await self.human.pause()

    def _account(self, entry: dict[str, Any], target: str) -> None:
        if not entry:
            return
        if entry.get("silent"):
            self.stats.silent.append(target)
        if entry.get("exception"):
            self.stats.errors.append(f"{target}: {entry['exception']}")
        if entry.get("error_screen"):
            self.stats.errors.append(f"{target}: {entry['error_screen']}")
