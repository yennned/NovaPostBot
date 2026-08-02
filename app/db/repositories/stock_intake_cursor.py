"""Водораздел ингеста приёмки: докуда лист «Історія» уже перенесён в Postgres."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from app.db.models.stock_intake_cursor import StockIntakeCursor
from app.db.repositories.base import BaseRepository


class StockIntakeCursorRepository(BaseRepository):
    async def get(self, *, book_id: str, tab: str) -> StockIntakeCursor | None:
        stmt = select(StockIntakeCursor).where(
            StockIntakeCursor.book_id == book_id, StockIntakeCursor.tab == tab
        )
        return await self.session.scalar(stmt)

    async def create_at(
        self, *, book_id: str, tab: str, row: int, fingerprint: str | None
    ) -> StockIntakeCursor:
        """Завести водораздел на указанной строке.

        Вызывающий обязан ставить его на ТЕКУЩИЙ конец журнала, а не на ноль: вся
        прошлая приёмка уже учтена в количествах листа, и переигрывание её задвоило
        бы остаток.
        """
        cursor = StockIntakeCursor(
            book_id=book_id, tab=tab, last_row=row, last_row_fingerprint=fingerprint
        )
        await self._add(cursor)
        return cursor

    async def advance(
        self, cursor: StockIntakeCursor, *, row: int, fingerprint: str | None
    ) -> None:
        """Сдвинуть водораздел. Коммитит вызывающий — вместе с применёнными дельтами.

        Отдельного коммита здесь нет намеренно: водораздел и дельты обязаны
        двигаться одной транзакцией. Иначе сбой между ними либо теряет приёмку
        (водораздел уехал, дельты откатились), либо задваивает её (наоборот).
        """
        cursor.last_row = row
        cursor.last_row_fingerprint = fingerprint
        cursor.last_ingested_at = datetime.now(UTC)
        await self.session.flush()
