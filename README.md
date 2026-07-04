# NovaPostBot — Telegram-бот фулфілменту Нової Пошти

[![CI](https://github.com/yennned/NovaPostBot/actions/workflows/ci.yml/badge.svg)](https://github.com/yennned/NovaPostBot/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12-blue)
![License](https://img.shields.io/badge/license-proprietary-lightgrey)

Личный кабинет фулфілменту в Telegram: клиенты создают ТТН через API Нової Пошти
своим ключом (мульти-ФОП), видят остатки/статистику/отправления и общаются с
дежурным менеджером; менеджеры обрабатывают и отправляют ТТН, ведут склад,
клиентов, поддержку и возвраты; владелец управляет персоналом и аналитикой.

> Полный дизайн, флоу по ролям и поэтапный план — в [docs/](docs/) и в файле
> плана `~/.claude/plans/squishy-launching-key.md`. Журнал прогресса —
> `PROGRESS.md` (ведётся после каждого коммита). Контекст для ассистентов —
> [`CLAUDE.md`](CLAUDE.md).

## Документация ([docs/](docs/))

| # | Документ | О чём |
|---|----------|-------|
| — | [COMMERCIAL-PROPOSAL](docs/COMMERCIAL-PROPOSAL.md) | **Для заказчика**: коммерческое предложение / функциональное описание, sign-off |
| 01 | [overview](docs/01-overview.md) | Контекст, цель, ключевые решения, объёмы, хостинг |
| 02 | [architecture](docs/02-architecture.md) | Дерево `app/`, гибрид Postgres+Sheets, модель данных |
| 03 | [roles-permissions](docs/03-roles-permissions.md) | RBAC, per-flag права, dev god-mode |
| 04 | [warehouse-sheets](docs/04-warehouse-sheets.md) | Склад/приёмка: две книги, Apps Script, синк |
| 05 | [flows-client](docs/05-flows-client.md) | Меню и флоу клиента (вкл. создание ТТН) |
| 06 | [flows-manager](docs/06-flows-manager.md) | Меню и флоу менеджера |
| 07 | [flows-owner-dev](docs/07-flows-owner-dev.md) | Владелец (персонал/аналитика) + dev god-mode |
| 08 | [notifications-tracking-returns](docs/08-notifications-tracking-returns.md) | Уведомления, трекинг, возвраты |
| 09 | [novaposhta-api](docs/09-novaposhta-api.md) | Интеграция НП, поля ТТН, ценообразование |
| 10 | [support-duty](docs/10-support-duty.md) | Поддержка и дежурство менеджера |
| — | [ROADMAP](docs/ROADMAP.md) | Git-процесс, фазы 0–7, проверка |

## Архитектура (гибрид хранилища)

- **PostgreSQL — вся БД:** пользователи/клиенты, ФОП (`sender_profiles`,
  ключ НП зашифрован Fernet), ТТН (`shipments` + items, резерв, движения),
  поддержка, уведомления, аудит/логи. Managed Postgres (**Neon**) + Alembic.
- **Складской источник — за seam `app/sheets/`:** по умолчанию это Google Sheets
  (книга «Склад», лист на клиента, read-only) + книга «Приймання» (черновик;
  синк в «Склад» кнопкой «Внести», Apps Script). `available = Склад(source) −
  reserved(Postgres)`. Phase 7 добавляет переключатель `INVENTORY_SOURCE`
  (`sheets`/`crm`) без изменения handler/service слоя.
- **Redis** — FSM/кэш справочников НП. **Docker** — bot + worker.

## Роли

`client → manager → owner` (строго сверху вниз) + **dev god-mode** по allowlist
(`DEV_TELEGRAM_IDS`): `/as <role>`, impersonation. Гранулярные права менеджера —
per-flag в `users.permissions`.
Авторизация — только телефон (`request_contact`).

## Стек

Python 3.12 · aiogram 3 · PostgreSQL (SQLAlchemy async + Alembic) · Redis ·
Google Sheets API (service-account) · Nova Poshta API · Docker. Часовой пояс —
Europe/Kyiv. Язык бота — украинский (uk).

## Структура

```
app/
  config.py            pydantic-settings (BOT_TOKEN, DATABASE_URL, REDIS_URL,
                       INVENTORY_SOURCE, GOOGLE_SA_JSON, SHEETS_*,
                       FERNET_KEY, OWNER/DEV_TELEGRAM_IDS)
  logging_config.py    structlog
  main.py              запуск бота (long polling)
  worker.py            APScheduler-воркер (трекинг, low-stock)
  db/                  PostgreSQL — вся БД (models/, repositories/, base, enums)
  sheets/              StockSource seam: Google Sheets now, CRM/WMS adapter later
  bot/                 dispatcher, middlewares, permissions, states, filters,
                       keyboards, texts (uk), handlers (start, client_cabinet,
                       clients_manage, ttn, stats, support, notifications, dev)
  services/            inventory, shipment, notifications, support, audit, reports
  novaposhta/          client, methods, schemas, tracking, exceptions
  utils/               crypto (Fernet), validators
migrations/            Alembic
tests/                 unit-тесты (чистая логика)
```

## Быстрый старт (Docker)

```bash
cp .env.example .env   # BOT_TOKEN, DATABASE_URL (Neon pooled),
                       # DATABASE_URL_DIRECT (Alembic), REDIS_URL, FERNET_KEY,
                       # INVENTORY_SOURCE, GOOGLE_SA_JSON, SHEETS_*,
                       # OWNER_TELEGRAM_IDS, DEV_TELEGRAM_IDS
docker compose up -d --build     # migrate (alembic upgrade head) → bot + worker
docker compose logs -f bot
```

Генерация `FERNET_KEY`:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Хостинг и деплой

Hetzner CPX21 VPS (бот + воркер + Redis в Docker) + managed Postgres **Neon**
(план **Launch** с отключённым scale-to-zero; Free — только dev/staging из-за
холодных стартов). Ориентир ~€10–15/мес. Код провайдеро-независим
(`DATABASE_URL`/`REDIS_URL`). Детали — [docs/01-overview.md](docs/01-overview.md).

**CI/CD.** Push/PR в `main` → CI `lint-test` (layer-check, ruff, compileall,
pytest на Postgres-контейнере). После зелёного CI **push в `main`** триггерит
`deploy`: сборка образа → **GHCR** (`:latest` + `:sha-<short>`) → SSH-деплой на VPS
(`docker compose pull && up -d --no-build`). Ручной `up -d --build` больше не нужен.
Версия сборки (git sha) пишется в лог старта (`bot.start version=…`) и отдаётся
командой `/version` (dev). Вехи — теги `vX.Y.Z` (`release.yml` → GitHub Release +
образ с тегом версии). Настройка секретов деплоя — в [CONTRIBUTING.md](CONTRIBUTING.md).

## Разработка

GitHub, ветка на задачу, в `main` только через PR (защищённый main), точечные
коммиты, `PROGRESS.md` после каждого локального коммита. Правила —
`CONTRIBUTING.md`. Секреты (`.env`, service-account JSON, ключи) в git не
попадают (`.gitignore`).

## Статус

Планирование завершено. Фазы **0–7** уже собраны в `main`. Phase 7 закрыла seam
для склада: `StockSource`/`GoogleSheetsStockSource`/`CrmStockSource` и
переключатель `INVENTORY_SOURCE` без изменений в хендлерах и сервисах.
