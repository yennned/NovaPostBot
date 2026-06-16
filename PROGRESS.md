# PROGRESS — журнал разработки NovaPostBot

Журнал ведётся **после каждого локального коммита**: дата, ветка, хеш, что
сделано, что дальше, открытые вопросы. Это не BRD — декомпозиция задач в
[`docs/ROADMAP.md`](docs/ROADMAP.md) и в плане.

Формат записи:

```
## YYYY-MM-DD · <ветка> · <короткий хеш>
- Сделано: …
- Дальше: …
- Открытые вопросы: …
```

---

## Фаза 1 — распределение задач

Разрез по границе «данные/правила» ↔ «бот/диалог», чтобы минимизировать конфликты
файлов. **`alex` ведёт data + RBAC в двух ветках, `step` — каркас бота + auth.**
Порядок: **db мержится первым** (фундамент), затем rbac и bot/auth ребейзятся на
свежий `main` (импортируют enum ролей и модель `User`). **Один коммиттер на
ветку.** Подробности — в [CONTRIBUTING.md](CONTRIBUTING.md).

### Трек A1 — `alex`: слой данных · `feat/alex-phase1-db`
- [ ] `app/db/models/` — `enums` (роли `client<manager<owner`, статусы), `user`
      (role, status, phone, permissions JSONB), `sender_profile` (ФОП,
      `np_api_key` Fernet), `audit`.
- [ ] `app/db/repositories/` — `user`, `sender_profile`, `audit`.
- [ ] Alembic — начальная миграция схемы (`migrations/versions/`).
- [ ] `app/sheets/client.py` — read-only скелет клиента Sheets (каркас).
- [ ] `tests/` — репозитории.

### Трек A2 — `alex`: RBAC-ядро · `feat/alex-phase1-rbac`
- [ ] `app/bot/permissions.py` — иерархия ролей, `can_manage(actor, target)`,
      per-flag `has_permission(user, flag)`, dev-allowlist проверяется первой.
- [ ] bootstrap владельцев из `OWNER_TELEGRAM_IDS`.
- [ ] `tests/` — permissions (иерархия, флаги, dev-allowlist).
- [ ] зависит от `enums`/`user` из A1 → старт по согласованному контракту,
      финальная привязка после мержа A1.

### Трек B — `step`: каркас бота + auth + меню + dev god-mode · `feat/step-phase1-bot-auth`
- [ ] `app/bot/dispatcher.py`, `middlewares.py` (inject session/user +
      «эффективная роль/пользователь» из dev-контекста), `states.py`, `filters.py`.
- [ ] `app/bot/handlers/start.py` — `/start` → `request_contact` →
      создание/поиск user, гейтинг `pending`/`active`/`blocked`.
- [ ] `app/bot/handlers/dev.py` — `/as client|manager|owner`, impersonation,
      kill-switch (two-man rule, окна 1ч/3ч), audit (`dev_*`).
- [ ] `app/bot/keyboards/` + `texts/` — рольовые меню (uk) для client/manager/owner.
- [ ] `app/main.py` — сборка и запуск (long polling).
- [ ] `tests/` — middleware/эффективная роль, логика `/start`, two-man rule.

---

## 2026-06-14 · feat/alex-phase0-infra · 776f15b
- **Сделано:** старт **Фазы 0**. `git init` (main). Каркас инфраструктуры:
  `.gitignore`, `.dockerignore`, `.env.example`, `CONTRIBUTING.md`, этот журнал;
  тулинг (`pyproject.toml` — ruff+pytest, `.pre-commit-config.yaml`,
  `requirements*.txt`); CI (`.github/workflows/ci.yml` — ruff+pytest); Docker
  (`Dockerfile`, `docker-compose.yml` — redis/migrate/bot/worker); каркас
  приложения (`app/config.py` — pydantic-settings, `app/logging_config.py` —
  structlog, `app/db/base.py` — async engine с `statement_cache_size=0`);
  alembic-скелет (`alembic.ini`, `migrations/env.py`); юнит-тест конфигурации.
- **Дальше:** деплой на GitHub (приватный репозиторий, защита `main`, PR),
  затем **Фаза 1** (модели БД, RBAC, `/start`, рольовые меню, dev god-mode).
- **Открытые вопросы:** имя/владелец GitHub-репозитория; для Фазы 4 — сверить
  COD-тариф НП и точный набор обязательных полей `InternetDocument.save`.
