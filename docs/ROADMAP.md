# Роадмап — процесс разработки и поэтапный план

## Git-гигиена

Сейчас `NovaPostBot` — НЕ git-репозиторий. Первый шаг — инициализация.

1. **Инициализация git + GitHub**
   - `git init`, ветка по умолчанию `main`.
   - Приватный репозиторий на GitHub (gh CLI), `origin`.
   - Защита `main`: попадание только через Pull Request (branch protection +
     обязательный review + зелёный CI).

2. **.gitignore (до первого коммита)** — не коммитим: `.env`, `*.env` (кроме
   `.env.example`), `.venv/`/`venv/`, `__pycache__/`, `*.py[cod]`,
   `.pytest_cache/`/`.ruff_cache/`/`.mypy_cache/`, `*.db`/`*.sqlite3`/`*.dump`,
   `backups/`, скриншоты (`*.png`/`*.jpg`/`*.jpeg`/`*.webp`), **service-account
   `*.json`/credentials**, `.DS_Store`, `.idea/`/`.vscode/`,
   `.claude/settings.local.json`.

3. **Секреты и шифрование**
   - Секреты только в `.env` (в git — `.env.example` с пустыми значениями).
   - **Google service-account JSON** — в `.env`/секрет, НЕ в git.
   - Ключ НП каждого ФОП — Fernet, хранится **зашифрованным в Postgres**
     (`sender_profiles.np_api_key`). `FERNET_KEY` — из env.
   - Перед первым push: `git ls-files` не содержит `.env`, ключи, скриншоты,
     venv, дампы БД.
   - PII (телефон/ФІО) персонал видит **открыто** (≤20 клиентов); чувствительные
     действия — в `audit_logs`.

4. **Изоляция двух разработчиков (я + Степан)**
   - Каждый — свой **отдельный клон** репозитория.
   - Для параллельных задач у агента — git **worktree** на задачу.
   - Ветка на задачу: `feat/<owner>-<short>`, `fix/<owner>-<short>` от свежего
     `main`.

5. **Коммиты** — точечно (`git add <файлы>`, без `git add .`); маленькие
   осмысленные коммиты; conventional-стиль; в `main` — только squash-merge PR
   после ревью и зелёного CI.

6. **PROGRESS.md** — после **каждого локального коммита**: дата, ветка, хеш, что
   сделано, что дальше, открытые вопросы. Журнал, не BRD.

7. **CONTRIBUTING.md** — правила веток/PR/коммитов, gitignore-политика, секреты,
   как обновлять PROGRESS.md, как запускать линт/тесты.

8. **Качество** — `ruff` (lint+format) + `pre-commit`, `pytest` на чистой логике,
   GitHub Actions CI (pytest + ruff) как гейт для merge в `main`.

## Поэтапный план (Фазы 0–7)

Каждая фаза — одна/несколько веток-задач, мердж через PR.

- **Фаза 0 — Инфраструктура и процесс.** `git init` + GitHub + branch protection,
  `.gitignore`, `.env.example`, `PROGRESS.md`, `CONTRIBUTING.md`, ruff/pre-commit/
  CI, Dockerfile + docker-compose (bot/worker/redis), `config.py`,
  `logging_config.py`, подключение **managed Postgres Neon** (`DATABASE_URL`
  pooled + `DATABASE_URL_DIRECT` для Alembic; asyncpg `statement_cache_size=0` за
  пулером — см. [02-architecture.md](02-architecture.md)) + Alembic, заготовка
  Google service-account. Очистка устаревших доков.

- **Фаза 1 — Слой данных (Postgres) + каркас бота, авторизация, роли, dev
  god-mode.** `app/db/` (модели + репозитории), `app/sheets/` (только склад),
  RBAC (3 роли + per-flag), middleware, `/start` (телефон), bootstrap
  владельцев, рольовые меню (uk), dev-allowlist + `/as` + impersonation +
  kill-switch, unit-тесты.

- **Фаза 2 — Регистрация/подтверждение + управление клиентами.** Надёжное
  уведомление владельцу/менеджеру; рабочее подтверждение (статус меняется, кнопки
  исчезают); блокировка/удаление; список и карточка клиента (PII открыто).

- **Фаза 3 — Кабинет клиента (чтение) + остатки из Sheets.** Книга «Склад», чтение
  остатков; просмотр товаров (поиск/пагинация, без ручного добавления); статистика
  today/week/month + выбор дня + остаток. Приёмка (Google + Apps Script) вне бота.

- **Фаза 4 — Интеграция НП и создание ТТН.** `novaposhta/*`, ФОП/SenderProfile
  (ключ Fernet в Postgres), FSM создания ТТН (пресеты размеров, фіз/юр, платник/
  оплата/COD/страховка → поля НП, ценообразование онлайн) — NP-first → Shipment +
  резерв, отмена → возврат; «Відправлення» клиента и менеджера.

- **Фаза 5 — Уведомления, трекинг, возвраты/проблемы (воркер).** Матрица пушей
  по ролям, APScheduler-трекинг → статусы, списание в «Склад» при «відправлено»;
  возвраты/lost/damaged, «Повернення замовлення», low-stock; SLA-таймер
  (30 рабочих минут, триггер `dispatched`) + бесплатная обработка при промахе.

- **Фаза 6 — Поддержка и дежурство + персонал/аналитика.** Чат клиент↔дежурный,
  «я на зв'язку» (авто-снятие), очередь без дежурного → владельцу, лог переписок;
  сессии менеджера = смена; 👔 Персонал (per-flag права), 📊 Звіти/Аналітика —
  финотчёт (fee-формула) + список опоздавших ТТН.

- **Фаза 7 — Задел на CRM/WMS для склада.** За абстракцией `app/sheets/` —
  альтернативный источник (CRM/WMS REST) без изменения хендлеров/сервисов;
  переключение через конфиг. (Postgres — уже с Фазы 0.)

- **Задел: Mini App (WebApp)** — для тяжёлых экранов (форма ТТН, каталог товаров,
  дашборд аналитики); подключается при необходимости. Бэкенд уже API-first
  (сервисы/репозитории отделены от хендлеров — [02-architecture.md](02-architecture.md)),
  Mini App переиспользует ту же логику.

## Проверка (end-to-end)

- **Локально (Docker):** `docker compose up -d --build` → `bot`, `worker`, `redis`;
  Postgres — Neon по `DATABASE_URL`; Sheets — service-account из `.env`; миграции
  `alembic upgrade head`. Логи: `docker compose logs -f bot`.
- **Данные:** Postgres (миграции создают схему) + тестовые книги Google Sheets
  «Склад» и «Приймання» (лист на клиента) с Apps Script.
- **Сценарии в Telegram:** новый клиент `/start` → уведомление → подтверждение
  меняет статус и убирает кнопки → блокировка закрывает доступ; dev `/as manager`;
  kill-switch требует второго dev; статистика today/week/month + день; создание
  ТТН → резерв; отмена возвращает; «відправлено» по трекингу → списание в «Склад»;
  возврат → «Повернення замовлення».
- **Тесты:** `pytest -q` (permissions, validators, stats-окна, inventory ledger,
  поля ТТН → НП). CI — гейт для merge.
- **Git-гигиена:** `git ls-files | grep -iE '\.env$|\.png|\.jpe?g|venv|\.db'` →
  только `.env.example`; `main` недоступен для прямого push.
