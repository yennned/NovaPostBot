"""Проверка версии Postgres на старте бота.

До этого версия прод-БД (managed Neon) жила единственной строчкой в PROGRESS.md.
Рассинхрон «CI на 16, прод на 18.4» обнаружился боевым сбоем — машинной сверки
не было нигде. Здесь её и закрываем: живой прогон против CI-шного Postgres
подтверждает, что `EXPECTED_PG_MAJOR` совпадает с тем, на чём идут тесты.
"""

from __future__ import annotations

import structlog
from app.db.base import EXPECTED_PG_MAJOR, check_server_version, server_major
from sqlalchemy.ext.asyncio import AsyncSession


class _FakeResult:
    def __init__(self, value: str) -> None:
        self._value = value

    def scalar_one(self) -> str:
        return self._value


class _FakeSession:
    """Сессия, отвечающая заданной версией: подделать её у живой БД нельзя."""

    def __init__(self, version: str) -> None:
        self.version = version

    async def execute(self, *_args, **_kwargs) -> _FakeResult:
        return _FakeResult(self.version)


def test_server_major_parses_real_formats():
    # Neon отдаёт «18.4», локальный контейнер — с суффиксом сборки.
    assert server_major("18.4") == 18
    assert server_major("18.4 (Debian 18.4-1.pgdg13+1)") == 18
    assert server_major("16beta1") == 16
    assert server_major("непонятно") is None


async def test_ci_postgres_matches_expected_major(db_session: AsyncSession):
    """Тесты и константа обязаны идти на одном мажоре — иначе сверка бессмысленна."""
    version = await check_server_version(db_session)
    assert server_major(version) == EXPECTED_PG_MAJOR


async def test_version_mismatch_is_logged_not_raised():
    """Расхождение видно в логе, но прод не отказывается стартовать.

    Neon вправе обновиться сам; падение бота на этом было бы лекарством хуже
    болезни. Задача проверки — сделать расхождение заметным.
    """
    with structlog.testing.capture_logs() as logs:
        version = await check_server_version(_FakeSession("16.9"))

    assert version == "16.9"
    mismatch = [entry for entry in logs if entry["event"] == "db.version_mismatch"]
    assert mismatch, logs
    assert mismatch[0]["log_level"] == "error"
    assert mismatch[0]["actual_major"] == 16
    assert mismatch[0]["expected_major"] == EXPECTED_PG_MAJOR


async def test_matching_version_logs_no_error():
    with structlog.testing.capture_logs() as logs:
        await check_server_version(_FakeSession(f"{EXPECTED_PG_MAJOR}.4"))

    assert not [entry for entry in logs if entry["event"] == "db.version_mismatch"]
    assert [entry for entry in logs if entry["event"] == "db.version"]
