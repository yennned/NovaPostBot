# CLAUDE.md — контекст проекта NovaPostBot

Краткая карта проекта для ассистентов и разработчиков (я + Степан). Подробности —
в [`docs/`](docs/) и в плане `~/.claude/plans/squishy-launching-key.md`.

> **Рабочая папка.** Работаем **только** в `/Users/yenin/Desktop/NovaPostBot`.
> Другие папки не читаем и не трогаем.

## Что строим

Telegram-бот личного кабинета фулфилмента Новой Почты. Клиенты создают ТТН своим
ключом НП (мульти-ФОП), видят остатки/статистику/отправления, пишут дежурному
менеджеру; менеджеры обрабатывают и отправляют ТТН, ведут склад, клиентов,
поддержку и возвраты; владелец управляет персоналом и аналитикой.

## Архитектура (гибрид хранилища)

- **PostgreSQL (managed Neon) — вся БД:** users, sender_profiles (ФОП, ключ НП
  зашифрован Fernet), shipments + items, stock_movements, support, notifications,
  audit_logs. SQLAlchemy async + Alembic.
- **Google Sheets — только склад:** книга «Склад» (лист на клиента, read-only) +
  книга «Приёмка» (лист на клиента, черновик; синк в «Склад» кнопкой «Внести» с
  двойным подтверждением, Apps Script). `available = Склад(Sheets) − reserved(PG)`.
- **Redis** — FSM/кэш справочников НП. **Docker** — bot + worker.

## Роли и доступ

`client → manager → owner` (строго сверху вниз) + **dev god-mode** по allowlist
`DEV_TELEGRAM_IDS` (ровно 2 человека): `/as <role>`, impersonation, kill-switch
(two-man rule, окна 1ч/3ч). Права менеджера — per-flag в `users.permissions`.
Авторизация — только телефон (`request_contact`).

## Базовые правила

Списание остатка — только авто по трекингу НП. Язык бота — **украинский**;
документы/код-комментарии — русский. Часовой пояс — **Europe/Kyiv**.

## Стек

Python 3.12 · aiogram 3 · PostgreSQL (SQLAlchemy async + Alembic) · Redis ·
Google Sheets API (service-account) · Nova Poshta API · Docker.

## Структура

`app/` → `config.py`, `main.py`, `worker.py`, `db/` (Postgres),
`sheets/` (только склад), `bot/` (dispatcher/middlewares/permissions/states/
keyboards/texts/handlers), `services/`, `novaposhta/`, `utils/`; `migrations/`;
`tests/`; `docs/` (детальный план); `PROGRESS.md` (журнал).

## Git-процесс

GitHub, **ветка на задачу** (`feat/<owner>-<short>`), в `main` **только через PR**
(защищённый main, зелёный CI), **точечные коммиты** (без `git add .`),
**`PROGRESS.md` после каждого коммита**. Секреты (`.env`, service-account JSON,
ключи) в git не попадают (`.gitignore`). Сообщения коммитов — conventional;
в конце: `Co-Authored-By: Claude ...`.

## Текущий статус

Планирование завершено, документация в `docs/` готова. Следующая — **Фаза 0**
(git init, GitHub, Docker, Neon, Alembic, базовый каркас). Репозиторий ещё **не
инициализирован**.
