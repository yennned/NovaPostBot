"""Тест composition root (Фаза 4, PR 8).

`ServicesMiddleware` должен прокидывать единые `np_client`/`np_cache` в `data`
хендлера — рядом с `db_session`/`services`, чтобы FSM создания ТТН (PR 9) получил
их через DI. Бот не стартуем; сессия — фейковая (DB не нужна).
"""

from __future__ import annotations

from app.bot.middlewares import ServicesMiddleware
from app.bot.services import InMemoryDevState


class _FakeSession:
    """Минимальный async-context, достаточный для прохода ServicesMiddleware."""

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


def _sessionmaker() -> object:
    return lambda: _FakeSession()


async def test_services_middleware_injects_np_client_and_cache() -> None:
    np_client = object()
    np_cache = object()
    captured: dict[str, object] = {}

    async def handler(event: object, data: dict[str, object]) -> str:
        captured.update(data)
        return "ok"

    middleware = ServicesMiddleware(
        _sessionmaker(),
        dev_ids=frozenset(),
        dev_state=InMemoryDevState(),
        np_client=np_client,
        np_cache=np_cache,
    )

    result = await middleware(handler, object(), {})

    assert result == "ok"
    assert captured["np_client"] is np_client
    assert captured["np_cache"] is np_cache


async def test_services_middleware_np_defaults_to_none() -> None:
    """Без проброса (старые вызовы/тесты) ключи присутствуют и равны None."""
    captured: dict[str, object] = {}

    async def handler(event: object, data: dict[str, object]) -> None:
        captured.update(data)

    middleware = ServicesMiddleware(
        _sessionmaker(),
        dev_ids=frozenset(),
        dev_state=InMemoryDevState(),
    )

    await middleware(handler, object(), {})

    assert captured["np_client"] is None
    assert captured["np_cache"] is None
