"""Базовый репозиторий: держит сессию и общие низкоуровневые операции."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession


class BaseRepository:
    """Общий предок репозиториев — хранит `AsyncSession`."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _add(self, obj: object) -> None:
        """Добавить объект и сделать flush (получить id/server_default'ы)."""
        self.session.add(obj)
        await self.session.flush()
