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
`DEV_TELEGRAM_IDS`: `/as <role>`, impersonation. Права менеджера — per-flag в
`users.permissions`. Авторизация — только телефон (`request_contact`).

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

**Фазы 0–7 — в `main`.** Фаза 0 (инфраструктура/каркас), Фаза 1 (данные+RBAC,
каркас бота, auth, dev god-mode), Фаза 2 (регистрация/подтверждение + управление
клиентами), Фаза 3 (кабинет клиента + остатки), Фаза 4 (интеграция НП + создание
ТТН, NP-first; + hardening follow-up: гейт полноты данных отправителя, стойкость
Redis-кэша, обязательный `sender_phone` при сохранении профиля, backstop
`DecryptionError` при ротации `FERNET_KEY`), Фаза 5 (уведомления/трекинг/SLA/
возвраты в воркере: APScheduler-поллинг статусов НП, списание в «Склад» при
«відправлено», SLA-таймер 30 раб. минут, возвраты returned/lost/damaged, очередь
отправлений менеджера, клиентские настройки уведомлений + low-stock anti-spam),
Фаза 6 (поддержка/дежурство + персонал/аналитика: дежурство «🟢 Я на зв'язку» +
авто-снятие воркером по расписанию, релей-чат клиент↔дежурный + очередь без
дежурного → владельцу + лог owner/dev, 👔 Персонал с per-flag правами/наймом/
блокировкой/снятием роли, 📊 Звіти/📈 Аналітика — fee-итоги + список опоздавших ТТН +
поддержка по менеджерам), Фаза 7 (seam склада: `StockSource` +
`GoogleSheetsStockSource` + заглушка `CrmStockSource`, переключение через
`INVENTORY_SOURCE`, без изменения handler/service слоя). Опц. отложено —
PR 9e «Останні отримувачі»; per-manager аттрибуция ТТН, сводка склада в отчётах,
кастомный диапазон/графики; отдельной будущей задачей остаётся реальный
CRM/WMS REST adapter поверх seam.

**Модель работы — sequential-by-phase:** один человек полностью закрывает фазу
(домен + bot/UI), второй ждёт мержа в `main`. Phase 2/4/6 → alex, Phase 3/5/7 → step.
Детали и распределение — в [docs/ROADMAP.md](docs/ROADMAP.md) и
[CONTRIBUTING.md](CONTRIBUTING.md). Репозиторий **публичный** (приватный ломает CI
на free-тарифе).
