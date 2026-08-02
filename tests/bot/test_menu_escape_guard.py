"""Кнопка нижней панели не должна попадать в хендлер свободного текста.

**Как это выглядит у человека.** Клиент вводит місто отримувача, НП отвечает
«довідник тимчасово недоступний», человек тычет «🚚 Створити ТТН», чтобы начать
заново — и получает «Нічого не знайшли за «🚚 Створити ТТН». Спробуйте іншу назву
міста». Кнопка выглядит сломанной. Поймано живым прогоном по проду 2026-08-03.

**Почему `menu_escape` сам по себе не спасает.** Он снимает стейт и отдаёт событие
дальше через `SkipHandler`, но `raw_state` резолвится мидлварью **один раз на
апдейт**: `StateFilter` у хендлеров ниже всё ещё видит старое состояние и съедает
тап. Поэтому каждый хендлер, ловящий свободный текст в состоянии, обязан нести
`~F.text.in_(MENU_TEXTS)` — это второй, обязательный пункт защиты, описанный в
`app/bot/handlers/menu_escape.py`.

**Почему тест структурный.** Дефект возвращается не правкой существующего
хендлера, а добавлением нового: их уже под тридцать, и забыть гард на очередном
шаге формы слишком легко — что и произошло с четырнадцатью сразу. Проверка
поштучно, по одному тесту на состояние, эту дыру не закрывает: новый хендлер
приходит без своего теста.
"""

from __future__ import annotations

import ast
import pathlib

HANDLERS_DIR = pathlib.Path(__file__).resolve().parents[2] / "app" / "bot" / "handlers"

#: Хендлеры, которые ловят текст в состоянии, но гард им не нужен: они матчат
#: КОНКРЕТНУЮ строку (`F.text == …`), а не свободный ввод, поэтому чужой текст
#: кнопки в них не попадёт по построению.
_EXACT_TEXT_MATCH = "F.text =="


def _free_text_state_handlers() -> list[tuple[str, int, str, str]]:
    """(файл, строка, имя, исходник декоратора) для хендлеров свободного текста в состоянии."""
    found: list[tuple[str, int, str, str]] = []
    for path in sorted(HANDLERS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                func = decorator.func
                if not (isinstance(func, ast.Attribute) and func.attr == "message"):
                    continue
                source = ast.unparse(decorator)
                if "State." not in source:
                    continue
                if _EXACT_TEXT_MATCH in source:
                    continue
                # Свободный ввод — это `F.text` без сравнения с конкретной строкой.
                if not any(
                    isinstance(arg, ast.Attribute) and arg.attr == "text" for arg in decorator.args
                ):
                    continue
                found.append((path.name, node.lineno, node.name, source))
    return found


def test_every_free_text_state_handler_excludes_menu_buttons() -> None:
    """Ни один хендлер свободного текста в состоянии не ест кнопки меню.

    Мутация: снять `~F.text.in_(MENU_TEXTS)` с любого из них — тест назовёт файл,
    строку и имя функции.
    """
    handlers = _free_text_state_handlers()
    assert handlers, "сканер ничего не нашёл — он сломан, а не код чист"

    unguarded = [
        f"{name}:{lineno} {func}" for name, lineno, func, src in handlers if "MENU_TEXTS" not in src
    ]

    assert not unguarded, (
        "хендлеры свободного текста без ~F.text.in_(MENU_TEXTS) — тап кнопки нижней\n"
        "панели уйдёт в них как ввод, и кнопка будет выглядеть сломанной:\n  "
        + "\n  ".join(unguarded)
    )


def test_scanner_notices_a_missing_guard() -> None:
    """Сканер обязан уметь найти дыру, а не только подтверждать чистоту.

    Без этого тест выше зелёный и при сломанном сканере — то есть проверяет
    собственную опечатку, а не код.
    """
    source = (
        "@router.message(CreateTtnState.entering_city_query, F.text, ~F.text.startswith('/'))\n"
        "async def receive_city_query(message):\n    ...\n"
    )
    tree = ast.parse(source)
    decorator = tree.body[0].decorator_list[0]
    rendered = ast.unparse(decorator)

    assert "State." in rendered
    assert _EXACT_TEXT_MATCH not in rendered
    assert "MENU_TEXTS" not in rendered  # именно такую строку сканер обязан считать дырой
