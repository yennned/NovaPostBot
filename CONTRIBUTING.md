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

## Зоны ответственности (Фаза 1)

Scope Фазы 1 разрезан на два почти независимых трека по границе
«данные/правила» ↔ «бот/диалог» — так минимизируем конфликты файлов. Полный
чек-лист задач — в [PROGRESS.md](PROGRESS.md).

| Разработчик | Ветка | Область кода |
|-------------|-------|--------------|
| **alex** | `feat/alex-phase1-db` | `app/db/` (модели + репозитории), Alembic-миграция, `app/sheets/` (скелет), тесты на репозитории |
| **alex** | `feat/alex-phase1-rbac` | `app/bot/permissions.py` (RBAC + dev-allowlist), bootstrap владельцев, тесты на permissions |
| **step** | `feat/step-phase1-bot-auth` | `app/bot/` (dispatcher, middlewares, states, filters, keyboards, texts), `/start`+auth, dev god-mode (`/as`, impersonation, kill-switch), `app/main.py`, тесты на middleware/`/start`/two-man rule |

**Правила координации:**
- **`feat/alex-phase1-db` — фундамент, мержится первым.** Ветки `*-rbac` и
  `*-bot-auth` импортируют enum ролей и модель `User`: стартуют параллельно по
  согласованному контракту, БД-зависимые места привязывают после мержа db —
  `rebase` на свежий `main`.
- **alex** держит `db` и `rbac` в двух своих worktree (`NovaPostBot-db`,
  `NovaPostBot-rbac`); ветку `feat/step-*` ведёт **step** в своём клоне.
- **Один коммиттер на ветку.** В одну ветку коммитит один человек; параллельный
  worktree той же ветки используется для работы/превью, синхронизация через
  push/pull — без двойных коммитов в одну ветку.

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

CI (GitHub Actions, `.github/workflows/ci.yml`) гоняет `ruff` + `pytest` и
является гейтом для merge в `main`.

## Документация

Дизайн и флоу — в [`docs/`](docs/) (оглавление — [`README.md`](README.md)).
Поэтапный план — [`docs/ROADMAP.md`](docs/ROADMAP.md). Контекст для ассистентов —
[`CLAUDE.md`](CLAUDE.md). Любая правка логики синхронизируется с docs.
