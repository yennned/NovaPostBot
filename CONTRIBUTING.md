# CONTRIBUTING — правила разработки NovaPostBot

Для нас двоих (я + Степан). Цель — чистый процесс: защищённый `main`, ветка на
задачу, точечные коммиты, зелёный CI.

## Рабочая папка

Работаем **только** в `/Users/yenin/Desktop/NovaPostBot`. Другие папки не трогаем.

## Ветки и PR

- В `main` — **только через Pull Request**. Защита `main` включена и **форсится
  сервером** (GitHub branch protection): прямой push запрещён **всем, включая
  владельца** (`enforce_admins`), мерж — только при **зелёном CI** (`lint-test`),
  ветка должна быть актуальна (strict). Ревью пока **не обязательно**
  (`required_approving_review_count = 0`) — поднимем до обязательного, когда в
  команде стабильно двое. Force-push и удаление `main` запрещены, история линейна.
- Ветка на задачу от свежего `main`:
  - `feat/<owner>-<short>` — новая функциональность,
  - `fix/<owner>-<short>` — багфикс,
  - `chore/<owner>-<short>` — инфраструктура/процесс.
  - `<owner>` — хэндл: **alex** или **step**. Пример: `feat/alex-ttn-create`.
- Параллельные задачи у агента — отдельный git **worktree** на задачу. У каждого
  разработчика — свой **клон** репозитория.
- В `main` мержим **squash-merge** (merge-commit и rebase отключены в репо) после
  зелёного CI; ветка после мержа **удаляется автоматически**.

## Зоны ответственности (граница по слою, не по фазе)

Делим работу **по слою кода**, а не «фаза на человека» — так файлы не
пересекаются by design и git не может выдать конфликт:

- **alex — доменный слой:** `app/services/`, `app/db/` (модели, репозитории,
  Alembic-миграции), `app/novaposhta/`, `app/sheets/`, `app/worker.py`,
  `app/utils/`. Чистая логика без aiogram (переиспользуема для WebApp).
- **step — бот-слой:** всё под `app/bot/*` (dispatcher, middlewares, states,
  filters, permissions-wiring, handlers, keyboards, texts) + `app/main.py`.

**step не редактирует `app/services|db|...`, alex не редактирует `app/bot/*`.**
Связь между слоями — через явный контракт (сигнатуры сервисов + структуры +
исключения), который alex фиксирует первым. Поэтому фаза «закрывается», когда
оба слоя смержены и связаны (wiring). Полное распределение задач по фазам — в
[docs/ROADMAP.md](docs/ROADMAP.md). Чек-лист текущей фазы — в [PROGRESS.md](PROGRESS.md).

### Параллельная работа без конфликтов
- **Контракт-первый:** alex мержит минимальный контракт (методы/структуры/
  исключения, покрытые тестами) в `main` **раньше**; step ветвится от свежего
  `main` и пишет UI под контракт. Не `cherry-pick` (дублирует коммиты) — только
  `git fetch` + `rebase`.
- **Фундамент мержится первым** (Трек A → B; контракт → bot-layer).
- **Одна Alembic-миграция в полёте.** Только одна ветка за раз трогает модели/
  миграции; вторую ребейзят на первую перед PR — иначе две `head`-ревизии и
  ручной `alembic merge`. Модели/миграции ведёт alex.
- **`pre-commit install` обязателен у обоих** — иначе разные IDE авто-форматят
  по-разному и CI падает на `ruff format --check`. Формат — только через `ruff`,
  не через форматтер редактора. `.editorconfig`/`.gitattributes` фиксируют LF и
  базовый стиль.
- **Ребейз на свежий `main` перед PR** — `main` двигается, не держим ветку долго.

### Зоны ответственности — Фаза 1 (история)

Трек `alex` был **одной неделимой задачей** (permissions зависит от моделей) —
одна ветка и один PR.

| Разработчик | Ветка | Область кода |
|-------------|-------|--------------|
| **alex** | `feat/alex-phase1-db` | `app/db/` (модели + репозитории), Alembic-миграция, `app/sheets/` (скелет), `app/bot/permissions.py` (RBAC + dev-allowlist), bootstrap владельцев, тесты на permissions/репозитории |
| **step** | `feat/step-phase1-bot-auth` | `app/bot/` (dispatcher, middlewares, states, filters, keyboards, texts), `/start`+auth, dev god-mode (`/as`, impersonation, kill-switch), `app/main.py`, тесты на middleware/`/start`/two-man rule |

**Правила координации:**
- **Трек A (`feat/alex-phase1-db`) — фундамент, мержится первым.** Трек B
  импортирует enum ролей и модель `User`: стартует с частей без БД, БД-зависимые
  места привязывает после мержа A — `rebase` ветки `feat/step-*` на свежий `main`.
- **alex** ведёт свою задачу в одном worktree (`NovaPostBot-db`); ветку
  `feat/step-*` ведёт **step** в своём клоне.
- **Один коммиттер на ветку.** В одну ветку коммитит один человек; синхронизация
  через push/pull — без двойных коммитов в одну ветку.

## Коммиты

- **Точечно:** `git add <конкретные файлы>` — **никаких `git add .`**.
- Маленькие осмысленные коммиты, **conventional**-стиль:
  `feat: …`, `fix: …`, `chore: …`, `docs: …`, `test: …`, `refactor: …`.
- В конце сообщения — `Co-Authored-By: Claude ...` (когда коммитит ассистент).

## PROGRESS.md

После **каждого локального коммита** — запись в [`PROGRESS.md`](PROGRESS.md):
дата, ветка, хеш, что сделано, что дальше, открытые вопросы.

## Секреты

> ⚠️ **Репозиторий ПУБЛИЧНЫЙ** (free-тариф; приватность сменим на платном плане).
> Весь код виден всем — попавший в git секрет утекает мгновенно и навсегда
> (история остаётся даже после удаления файла). Дисциплина секретов — критична.

- Секреты только в `.env` (в git — `.env.example` с пустыми значениями).
- **Google service-account JSON** — в `./secrets/` (gitignored) или секрет
  хостинга, НЕ в git.
- Ключ НП каждого ФОП — Fernet, шифруется в Postgres (`sender_profiles.np_api_key`);
  `FERNET_KEY` — из env.
- Перед первым push: `git ls-files` не должен содержать `.env`, ключи, скриншоты,
  venv, дампы БД.

## Линт и тесты

```bash
pip install -r requirements.txt -r requirements-dev.txt
pre-commit install            # хуки ruff на коммит
ruff check . && ruff format --check .
pytest -q
```

**Тесты требуют Postgres** (часть тестов гоняет репозитории/миграции на живой БД,
а не на моках). Локально:

```bash
docker compose --profile dev up -d postgres   # postgres:16 на localhost:5432
cp .env.example .env                           # выставить DATABASE_URL(_DIRECT)
                                               # на localhost и FERNET_KEY
pytest -q
```

`DATABASE_URL`/`DATABASE_URL_DIRECT` для локальной БД:
`postgresql+asyncpg://novapost:novapost@localhost:5432/novapostbot`. `FERNET_KEY`
сгенерировать: `python -c "from app.utils.crypto import generate_key; print(generate_key())"`.
В CI Postgres поднимается автоматически (service-container в `ci.yml`).

CI (GitHub Actions, `.github/workflows/ci.yml`) гоняет `ruff` + `pytest`
(с Postgres-сервисом) и является гейтом для merge в `main`.

## Документация

Дизайн и флоу — в [`docs/`](docs/) (оглавление — [`README.md`](README.md)).
Поэтапный план — [`docs/ROADMAP.md`](docs/ROADMAP.md). Контекст для ассистентов —
[`CLAUDE.md`](CLAUDE.md). Любая правка логики синхронизируется с docs.
