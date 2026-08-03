"""Водораздел ингеста приёмки: докуда лист «Історія» уже перенесён в Postgres.

Источник дельты приёмки — лист «Історія» книги «Склад», куда Apps Script пишет
строку на каждую применённую приёмку (`applyToStock_`). Он append-only и
произведён скриптом, которого мы не трогаем, — то есть готовый поток событий.

Колонка «Оброблено» книги «Приймання» на эту роль не годится: `clearRows_`
(`scripts/intake_apps_script.gs`) удаляет перенесённые строки сразу после записи,
и опрос гарантированно терял бы данные. Диф «Склад.Кількість против PG» тоже не
годится — он неотличим от ручной правки человеком.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class StockIntakeCursor(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "stock_intake_cursor"
    __table_args__ = (UniqueConstraint("book_id", "tab", name="uq_stock_intake_cursor_book_tab"),)

    book_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tab: Mapped[str] = mapped_column(String(255), nullable=False)

    #: Номер последней перенесённой строки. Двигается в ТОЙ ЖЕ транзакции, что и
    #: применение дельт: сбой на середине пачки откатывает и то и другое, а повтор
    #: переигрывает пачку целиком, ничего не задваивая.
    last_row: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    #: Хэш строки-водораздела. Если человек удалил или вставил строки в «Історію»,
    #: номер `last_row` начинает указывать не туда, и продолжать по нему — значит
    #: тихо потерять или задвоить приёмку. Несовпадение = ингест не выполняется
    #: вовсе (fail closed) + сигнал владельцу. Угадывать здесь нечего.
    last_row_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: Почему ингест остановлен, или `NULL` — он здоров. Хранится в БД, а не в памяти
    #: процесса, потому что читатель здесь другой: **зеркало**. Пока ингест стоит,
    #: приёмка меняет ячейку «Кількість», а зеркало видит расхождение с
    #: `mirrored_quantity` и не может отличить её от правки человека — применяет
    #: движением `manual` вместо `intake`, а превысившую `STOCK_MANUAL_DELTA_LIMIT`
    #: отклоняет и возвращает в ячейку значение из PG, то есть стирает приёмку из
    #: листа. Признак останова снимает эту слепоту.
    halted_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)

    last_ingested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
