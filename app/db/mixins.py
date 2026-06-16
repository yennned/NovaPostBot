"""Переиспользуемые миксины для ORM-моделей.

`UUIDPrimaryKeyMixin` — UUID-первичный ключ (генерация на стороне Python, без
зависимости от расширений Postgres). `TimestampMixin` — таймстемпы создания и
обновления на стороне БД (Europe/Kyiv обеспечивается на уровне приложения).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import Mapped, mapped_column


class UUIDPrimaryKeyMixin:
    """UUID-первичный ключ (`default=uuid.uuid4`, доступен до flush)."""

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    """`created_at` / `updated_at` со server-side значениями."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
