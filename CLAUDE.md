# CLAUDE.md — контекст проекта NovaPostBot

Краткая карта проекта для ассистентов и разработчиков (я + Степан). Подробности —
в [`docs/`](docs/); фактическое состояние работ — в [`PROGRESS.md`](PROGRESS.md).

> **Рабочая папка.** Работаем **только** в `/Users/yenin/Desktop/NovaPostBot`.
> Другие папки не читаем и не трогаем.

## Что строим

Telegram-бот личного кабинета фулфилмента Новой Почты. Клиенты создают ТТН своим
ключом НП (мульти-ФОП), видят остатки/статистику/отправления, пишут дежурному
менеджеру; менеджеры обрабатывают и отправляют ТТН, ведут склад, клиентов,
поддержку и возвраты; владелец управляет персоналом и аналитикой.

## Архитектура (гибрид хранилища)

- **PostgreSQL (managed Neon) — вся БД:** client_accounts +
  client_account_memberships (бизнес-аккаунт и его люди), users, sender_profiles
  (ФОП, ключ НП зашифрован Fernet), shipments + items, stock_movements, support,
  notification_settings, low_stock_alerts, audit_logs. SQLAlchemy async + Alembic.
  ФОП/ТТН/склад/поддержка скоупятся по `account_id`.
- **Google Sheets — только склад:** книга «Склад» (лист на бизнес-аккаунт, read-only) +
  книга «Приёмка» (лист на бизнес-аккаунт, черновик; синк в «Склад» кнопкой «Внести» с
  двойным подтверждением, Apps Script). `available = Склад(Sheets) − reserved(PG)`.
- **Redis** — FSM/кэш справочников НП. **Docker** — bot + worker.

## Роли и доступ

`client → manager → owner` (строго сверху вниз) + **dev god-mode** по allowlist
`DEV_TELEGRAM_IDS`: `/as <role>`, impersonation. Права менеджера — per-flag в
`users.permissions`. Авторизация — только телефон (`request_contact`).

## Базовые правила

Списание остатка — только авто по трекингу НП. Язык бота — **украинский**;
документы/код-комментарии — русский. Часовой пояс — **Europe/Kyiv**.

## Стек

Python 3.14 (рантайм; код совместим с 3.12+) · aiogram 3 · PostgreSQL (SQLAlchemy async + Alembic) · Redis ·
Google Sheets API (service-account) · Nova Poshta API · Docker.

## Структура

`app/` → `config.py`, `main.py`, `worker.py`, `db/` (Postgres),
`sheets/` (только склад), `bot/` (dispatcher/middlewares/permissions/states/
keyboards/texts/handlers), `services/`, `novaposhta/`, `utils/`; `migrations/`;
`tests/`; `docs/` (детальный план); `PROGRESS.md` (журнал).

## Git-процесс

GitHub, **ветка на задачу** (`<тип>/<owner>-<short>`, где тип — `feat`/`fix`/
`chore`/`test`; см. [CONTRIBUTING.md](CONTRIBUTING.md)), в `main` **только через PR**
(защищённый main, зелёный CI), **точечные коммиты** (без `git add .`),
**`PROGRESS.md` после каждого коммита**. Секреты (`.env`, service-account JSON,
ключи) в git не попадают (`.gitignore`). Сообщения коммитов — conventional;
в конце: `Co-Authored-By: Claude ...`.

## Текущий статус

**Бот работает в проде** (Hetzner VPS: bot + worker + Redis в Docker; БД — Neon).
Мерж в `main` = автодеплой: CI → образ в GHCR → SSH `docker compose pull && up -d`.
Есть `rollback.yml` и релизные теги `vX.Y.Z`.

**Фазы 0–7 закрыты:** инфраструктура/каркас (0), данные+RBAC+auth+dev god-mode (1),
регистрация/подтверждение + управление клиентами (2), кабинет клиента + остатки (3),
интеграция НП + создание ТТН NP-first (4), уведомления/трекинг/SLA/возвраты в
воркере (5), поддержка/дежурство + персонал/аналитика (6), seam склада
`StockSource` + `INVENTORY_SOURCE` (7).

**После фаз работа идёт задачами:** бизнес-аккаунты и команды (`client_accounts` +
memberships, `account_owner`/`employee`), физическое удаление клиентов/менеджеров,
политика одного бота, CI/CD с откатом, харнесс живых aiogram-`Update` и пробники
`scripts/e2e/`, хардening по боевым прогонам (кэш Sheets, видимость сбоев склада,
объёмный вес в оценке цены).

Что осознанно **не** реализовано — одноимённый раздел в
[docs/ROADMAP.md](docs/ROADMAP.md). Фактический журнал — [PROGRESS.md](PROGRESS.md).

**Модель работы — sequential-by-phase** (один человек закрывает фазу целиком,
второй ждёт мержа): правило действует, но фазы закрыты и активный писатель сейчас
один. Детали — [CONTRIBUTING.md](CONTRIBUTING.md). Репозиторий **публичный**
(приватный ломает CI на free-тарифе).
