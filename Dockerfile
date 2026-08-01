FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Зависимости отдельным слоем (кэш). `--require-hashes` — не украшение: без него
# `>=`-границы резолвились на момент сборки, и два образа одного git sha могли
# приехать с разными версиями aiogram/pydantic/cryptography. requirements.txt —
# скомпилированный лок (источник — requirements.in), см. CONTRIBUTING.md.
COPY requirements.txt .
RUN pip install --upgrade pip && pip install --require-hashes -r requirements.txt

# Версия сборки: CI прокидывает git sha (--build-arg GIT_SHA=...), локально — «dev».
# Пишется в лог при старте (bot.start/worker.start) → трассируемость «что в проде».
ARG GIT_SHA=dev
ENV APP_VERSION=$GIT_SHA

# Код приложения и миграции
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini

# По умолчанию — бот; worker/migrate переопределяют command в compose
CMD ["python", "-m", "app.main"]
