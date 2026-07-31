"""Пробник живости прода: направление конфликта `getUpdates` зафиксировано тестом.

Здесь ровно одна нетривиальная мысль, и в ней легко ошибиться на 180°. Кажется,
что «409 Conflict в ответ на мой запрос = кто-то уже поллит = бот жив». На самом
деле при конкуренции за токен Telegram обрывает того, кто поллил **раньше**, и
обслуживает новый запрос — короткий пробник получил бы `200 OK` и при живом боте,
и при мёртвом, то есть не проверял бы ничего.

Поэтому пробник сам встаёт в длинный поллинг и ждёт, когда его вытеснит боевой
процесс: **409 — это успех**, а спокойно отработавший до конца запрос — признак
того, что поллить некому.

Ошибиться тут дорого: инвертированный пробник объявляет живой прод мёртвым и
провоцирует «чинить» то, что работает.
"""

from __future__ import annotations

import httpx
import pytest
from scripts.e2e import prod_health

_ALIVE, _DOWN, _UNKNOWN = 0, 1, 2


class _FakeAsyncClient:
    """Заглушка httpx: отдаёт getMe/getWebhookInfo и заданный ответ на getUpdates."""

    def __init__(self, updates_response: httpx.Response) -> None:
        self._updates = updates_response

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get(self, url: str, params: dict | None = None) -> httpx.Response:
        request = httpx.Request("GET", url)
        if url.endswith("/getMe"):
            return httpx.Response(
                200, json={"ok": True, "result": {"username": "bot"}}, request=request
            )
        if url.endswith("/getWebhookInfo"):
            return httpx.Response(
                200, json={"ok": True, "result": {"pending_update_count": 0}}, request=request
            )
        return self._updates


@pytest.fixture
def _patch_client(monkeypatch: pytest.MonkeyPatch):
    def _install(updates_response: httpx.Response) -> None:
        monkeypatch.setattr(
            prod_health.httpx,
            "AsyncClient",
            lambda **_kw: _FakeAsyncClient(updates_response),
        )

    return _install


async def test_conflict_means_bot_is_alive(_patch_client) -> None:
    """Нас вытеснили — значит на том конце кто-то поллит."""
    _patch_client(httpx.Response(409, json={"ok": False, "description": "Conflict"}))

    assert await prod_health._probe("t") == _ALIVE


async def test_quiet_long_poll_means_bot_is_down(_patch_client) -> None:
    """Длинный поллинг никто не прервал — конкурента за токен нет."""
    _patch_client(httpx.Response(200, json={"ok": True, "result": []}))

    assert await prod_health._probe("t") == _DOWN


async def test_unexpected_status_is_not_reported_as_verdict(_patch_client) -> None:
    """Отвал сети/лимит — это «не знаю», а не «прод лежит»: иначе пробник
    поднимет ложную тревогу на своей же ошибке."""
    _patch_client(httpx.Response(429, json={"ok": False, "description": "Too Many Requests"}))

    assert await prod_health._probe("t") == _UNKNOWN


def test_poll_waits_longer_than_a_production_polling_cycle() -> None:
    """Ждать нужно дольше цикла aiogram (`timeout=30`), иначе живой бот просто не
    успеет прийти за апдейтами и пробник объявит его мёртвым."""
    assert prod_health._POLL_TIMEOUT_SECONDS > 30
    assert prod_health._HTTP_TIMEOUT_SECONDS > prod_health._POLL_TIMEOUT_SECONDS
