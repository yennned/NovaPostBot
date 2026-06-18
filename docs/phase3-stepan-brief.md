# Бриф Степану — Фаза 3 (кабинет клиента + остатки)

Документ для шаринга. Модель работы — **sequential-by-phase**: Фазу 3 ты ведёшь
**целиком** (и доменный слой, и bot/UI), от свежего `main`, после того как Фаза 2
(alex) полностью смержена. Внизу есть блок **«В память IDE»** — скопируй его в
память своей IDE.

## 0. Когда и как стартовать
1. Дождись, что **Фаза 2 полностью в `main`** (регистрация/подтверждение +
   управление клиентами + правка профиля). Без этого кабинет клиента строить не на
   чем (он читает клиентов/ФОП/отправления из Фазы 2).
2. `git fetch origin && git checkout main && git rebase origin/main`.
3. `git checkout -b feat/step-phase3` от свежего `main`. Один коммиттер на ветку.
4. Во время работы: `git push -u origin feat/step-phase3`; PR в защищённый `main`,
   merge только при зелёном CI.

## 1. Scope Фазы 3 (твой, целиком)
Кабинет клиента (чтение) + остатки. Делаешь оба слоя.

**Доменный слой (`app/services`, `app/db`, `app/sheets` — без aiogram):**
- `app/sheets/inventory.py` — read-only чтение книги «Склад» (лист на клиента),
  поверх скелета `app/sheets/client.py`; кэш справочников в Redis.
- `app/services/inventory.py` — `available = stock(Sheets) − reserved(PG)`.
- `app/db/repositories/shipment.py` — `get_by_client_and_status`, `get_by_ttn_number`,
  агрегаты по статусам; `reserved_by_sku(client_id)` (для inventory).
- `app/services/stats.py` — окна today/week/month в **Europe/Kyiv**, выбор дня;
  net = відправлено − повернення − втрати; топ-SKU, остаток.

**Bot/UI (`app/bot/*`):**
- `app/bot/handlers/client_cabinet.py` — меню клиента: 📦 Товари (поиск/пагинация),
  📬 Відправлення (группы/поиск/карточка/відмова), 📊 Статистика (периоды + день),
  ⚙️ Налаштування.
- `app/bot/keyboards/client.py`, тексты (uk), `states.ClientCabinetState`, wiring.

**Итог:** кабинет работает end-to-end, `pytest -q` зелёный, PR в `main`.

## 2. Как переиспользовать готовое (Фаза 1–2 в `main`)
- Сервисы зовёшь так же, как в Фазе 2: хендлер берёт `db_session` +
  `effective_context` (инъекция middleware), зовёт `app/services/*`, ловит доменные
  исключения (`app/services/exceptions.py`) → uk-текст.
- Гейтинг доступа клиента — статус `active` (см. `/start` и `_require_*` в
  `services/clients.py` как образец проверок).
- Пуши (если нужны) — через `Notifier`/`app/bot/notify.BotNotifier`, шли **после
  commit** (см. как сделано в `handlers/clients_manage`/`start`).
- Подключение роутера — в `app/bot/dispatcher.py` (как `clients_router`).

## 3. Правила (см. CONTRIBUTING.md)
- Модель **sequential**: не начинаешь Фазу 3 до мержа Фазы 2; alex не начинает
  Фазу 4 до мержа Фазы 3.
- Один коммиттер на ветку; **`pre-commit install` обязателен**; формат — только
  `ruff format` (не форматтер IDE); ребейз на свежий `main` перед PR.
- **Одна Alembic-миграция в полёте** (если Фаза 3 добавляет таблицы/поля — это
  твоя единственная миграция на ветку).
- CI-гейт: `app/services` и `app/db` **не импортируют aiogram** (держим API-first).

---

## В память IDE (скопировать)
```
Проект NovaPostBot. Модель: sequential-by-phase — один владелец на фазу (домен +
bot/UI), второй ждёт мержа. Моя фаза — Phase 3 (кабинет клиента + остатки), веду
ЦЕЛИКОМ от свежего main после мержа Phase 2.
Scope: sheets/inventory, services/inventory (available = stock − reserved),
repositories/shipment, services/stats (окна Europe/Kyiv) + handlers/client_cabinet,
keyboards/client, тексты (uk), states.ClientCabinetState, wiring.
Правила: хендлеры зовут app/services/* через db_session+effective_context, ловят
app/services/exceptions → uk-текст; пуши после commit; app/services|db БЕЗ aiogram
(CI-гейт); pre-commit + ruff format обязательны; одна Alembic-миграция в полёте;
ребейз на main перед PR; не cherry-pick. Язык бота — украинский, комментарии —
русский, таймзона Europe/Kyiv.
```
