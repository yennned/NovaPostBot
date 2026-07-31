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
    """Заглушка httpx: отдаёт getMe/getWebhookInfo и заданный ответ на getUpdates.

    Запоминает посещённые методы — по ним проверяем не только вердикт, но и то,
    что пробник не полез в `getUpdates` там, где не должен.
    """

    def __init__(
        self,
        updates_response: httpx.Response | Exception,
        *,
        webhook_url: str = "",
        me_response: httpx.Response | Exception | None = None,
    ) -> None:
        self._updates = updates_response
        self._webhook_url = webhook_url
        self._me = me_response
        self.calls: list[str] = []

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get(self, url: str, params: dict | None = None) -> httpx.Response:
        method = url.rsplit("/", 1)[-1]
        self.calls.append(method)
        request = httpx.Request("GET", url)
        if method == "getMe":
            if isinstance(self._me, Exception):
                raise self._me
            if self._me is not None:
                return self._me
            return httpx.Response(
                200, json={"ok": True, "result": {"username": "bot"}}, request=request
            )
        if method == "getWebhookInfo":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {"pending_update_count": 0, "url": self._webhook_url},
                },
                request=request,
            )
        if isinstance(self._updates, Exception):
            raise self._updates
        return self._updates


@pytest.fixture
def _patch_client(monkeypatch: pytest.MonkeyPatch):
    """Ставит заглушку и возвращает её — чтобы тест мог заглянуть в `calls`."""
    installed: list[_FakeAsyncClient] = []

    def _install(
        updates_response: httpx.Response | Exception,
        *,
        webhook_url: str = "",
        me_response: httpx.Response | Exception | None = None,
    ) -> list[_FakeAsyncClient]:
        def _factory(**_kw: object) -> _FakeAsyncClient:
            client = _FakeAsyncClient(
                updates_response, webhook_url=webhook_url, me_response=me_response
            )
            installed.append(client)
            return client

        monkeypatch.setattr(prod_health.httpx, "AsyncClient", _factory)
        return installed

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


async def test_webhook_mode_is_unknown_not_alive(_patch_client) -> None:
    """С включённым вебхуком `409` перестаёт что-либо доказывать.

    Telegram отвечает `409` и на «getUpdates при активном вебхуке» — такой ответ
    придёт даже от погашенного контейнера. Признак живости неприменим, поэтому
    вердикт «не знаю», а сам длинный поллинг не запускается вовсе.
    """
    installed = _patch_client(
        httpx.Response(409, json={"ok": False, "description": "Conflict: webhook is active"}),
        webhook_url="https://example.test/hook",
    )

    assert await prod_health._probe("t") == _UNKNOWN
    assert "getUpdates" not in installed[0].calls


async def test_probe_own_network_failure_is_unknown(_patch_client) -> None:
    """Отвал сети у пробника не должен читаться как «прод лежит».

    До правки исключение улетало из `_probe` наружу, интерпретатор завершался
    кодом 1 — ровно тем, которым обозначено «бот не работает».
    """
    _patch_client(
        httpx.Response(200, json={"ok": True, "result": []}),
        me_response=httpx.ConnectError("сеть недоступна"),
    )

    assert await prod_health._probe("t") == _UNKNOWN


async def test_timeout_on_the_long_poll_is_unknown(_patch_client) -> None:
    """Таймаут длинного поллинга — тоже отказ пробника, а не диагноз."""
    _patch_client(httpx.ReadTimeout("истекло ожидание"))

    assert await prod_health._probe("t") == _UNKNOWN


async def test_malformed_payload_is_unknown(_patch_client) -> None:
    """Ответ без `result` — повод сказать «не знаю», а не упасть трейсбеком."""
    _patch_client(
        httpx.Response(200, json={"ok": True, "result": []}),
        # `request` обязателен: без него `raise_for_status` бросает RuntimeError
        # самого httpx, и тест проверял бы поведение заглушки, а не пробника.
        me_response=httpx.Response(
            200, json={"ok": False}, request=httpx.Request("GET", "https://t/getMe")
        ),
    )

    assert await prod_health._probe("t") == _UNKNOWN


def test_poll_waits_longer_than_a_production_polling_cycle() -> None:
    """Ждать нужно дольше цикла aiogram (`timeout=30`), иначе живой бот просто не
    успеет прийти за апдейтами и пробник объявит его мёртвым."""
    assert prod_health._POLL_TIMEOUT_SECONDS > 30
    assert prod_health._HTTP_TIMEOUT_SECONDS > prod_health._POLL_TIMEOUT_SECONDS
