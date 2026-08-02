"""Async-движок и базовый класс моделей (SQLAlchemy 2.0).

Приложение подключается к Neon через пулер (PgBouncer): для asyncpg отключаем кэш
prepared statements (`statement_cache_size=0`). Alembic ходит прямым коннектом.
"""

from __future__ import annotations

import re

from sqlalchemy import MetaData, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings
from app.logging_config import get_logger

_log = get_logger("db")

#: Мажор Postgres, на котором мы разрабатываем и гоняем CI (`docker-compose.yml`,
#: `ci.yml`). Единственная точка правды для этого числа — здесь; в YAML рядом с
#: образом `postgres:18-alpine` стоит ссылка на неё.
#:
#: Прод — managed Neon, и его версию выбираем не мы: она может уехать сама, после
#: чего CI перестанет проверять то, на чём работает прод. Ровно этот рассинхрон
#: уже стрелял (CI на 16, прод на 18.4), и обнаружился он боевым сбоем, потому что
#: версия прод-БД нигде не проверялась — только строчкой в PROGRESS.md.
EXPECTED_PG_MAJOR = 18

# Конвенция имён constraint'ов — чтобы Alembic autogenerate давал
# детерминированные имена индексов/ключей (иначе БД сама придумывает разные).
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Базовый класс для всех ORM-моделей."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def make_engine(url: str | None = None) -> AsyncEngine:
    """Движок приложения. Размеры пула заданы явно, а не дефолтами SQLAlchemy.

    Дефолт (`pool_size=5, max_overflow=10, pool_timeout=30`) рассчитан на короткие
    запросы, а у нас коннект удерживается через внешнее I/O: сессия открывается на
    апдейт, а внутри апдейта лежат вызовы НП и Sheets. При десятке одновременных
    отправок 15 коннектов кончаются, и следующий апдейт ждёт **полминуты** прежде
    чем упасть — то есть пользователь полминуты смотрит в тишину, а потом получает
    ошибку. Короткий `pool_timeout` превращает это в быстрый и понятный отказ.

    `pool_recycle` — не столько про Postgres, сколько про PgBouncer Neon: коннект,
    который пулер уже закрыл со своей стороны, `pool_pre_ping` обнаружит лишним
    round-trip'ом на каждом взятии. Пересоздание по возрасту дешевле.
    """
    settings = get_settings()
    return create_async_engine(
        url or settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=settings.db_pool_recycle,
        pool_pre_ping=True,
        connect_args={"statement_cache_size": 0},
    )


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine


def get_sessionmaker() -> async_sessionmaker:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


def server_major(version: str) -> int | None:
    """Мажор из строки `SHOW server_version` («18.4», «18.4 (Debian …)», «16beta1»)."""
    match = re.match(r"\s*(\d+)", version)
    return int(match.group(1)) if match else None


async def check_server_version(session: AsyncSession) -> str:
    """Записать фактическую версию Postgres в лог; расхождение мажора — ERROR.

    Сознательно НЕ падаем: Neon вправе обновиться сам, и прод не должен
    отказываться стартовать из-за этого. Задача — сделать расхождение видимым
    (в логе старта теперь всегда есть, на какой версии БД мы фактически
    работаем), а не остановить бота.

    Зовётся только из `app/main.py`: воркер намеренно не открывает сессию на
    старте, чтобы ночью Neon (scale-to-zero) засыпал — проверка разбудила бы его.
    """
    version = (await session.execute(text("SHOW server_version"))).scalar_one()
    major = server_major(version)
    _log.info("db.version", version=version, expected_major=EXPECTED_PG_MAJOR)
    if major != EXPECTED_PG_MAJOR:
        _log.error(
            "db.version_mismatch",
            version=version,
            actual_major=major,
            expected_major=EXPECTED_PG_MAJOR,
            hint="разработка и CI идут на другом мажоре — сверьте EXPECTED_PG_MAJOR",
        )
    return version
