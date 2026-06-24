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

## 2026-06-24 · feat/andrey-warehouse-phone-resilience · 349da9a
- **Сделано:** три точечные правки UX/устойчивости, найденные на E2E-прогоне бота:
  - **НП-довідник, serve-stale-on-error** (`app/novaposhta/cache.py`): при
    транзиентном `NovaPoshtaUnavailable` на поиске відділення кэш отдаёт ранее
    закэшированный полный список города и фильтрует локально — пользователь не
    застревает в середине создания ТТН. Нет полного списка в кэше → ошибка
    пробрасывается как прежде.
  - **📦 Склад (manager/owner)** был мёртвой кнопкой (нет хендлера на текст) —
    добавлен `open_warehouse` в `handlers/manager_shipments`: ссылки на книги
    «Склад»/«Приймання» + зведення залишків по клиентам. Новый сервис
    `inventory.stock_totals` / `stock_summary` (чтение Sheets через `asyncio.to_thread`,
    сбой листа одного клиента не валит сводку).
  - **Валидация телефона** в ⚙️ Налаштування: `normalize_phone` вынесен в
    `app/utils/phone` (общий с шагом получателя ТТН), `client_cabinet` отбивает
    мусор (`Тест ФОП → ❌ Невірний номер…`), значение не сохраняется.
  - Тесты: `tests/test_phone`, `tests/test_inventory_summary`, +кейсы stale-fallback/
    reraise в `tests/test_novaposhta_cache`. `ruff check` + `ruff format --check` чистые;
    локальный прогон — на изолированной БД (см. ниже).
- **Дальше:** push ветки, PR в `main`, дождаться зелёного CI, смержить.
- **Открытые вопросы:** локальный `pytest` без override `DATABASE_URL` стирает dev-БД
  (conftest `drop_all` по `.env`); прогонять на отдельной `novapostbot_test`. Два
  пред-существующих падения reports/notifications — окружение (.env owner-IDs +
  date-window), не код.

## 2026-06-23 · feat/step-phase7-stock-source · phase7-stock-source
- **Сделано:** полностью закрыта Фаза 7 — seam под будущий CRM/WMS для склада:
  - введён контракт `app/sheets/source.py`: `StockSource`, `StockRow`, `StockDelta`;
    текущая рабочая реализация собрана как `GoogleSheetsStockSource`, добавлен явный
    stub `CrmStockSource`.
  - в `app/sheets/__init__.py` добавлена factory `build_stock_source()` и
    переключатель через новый конфиг `INVENTORY_SOURCE` (`sheets`/`crm`).
  - default-path чтения/списания переведён на фабрику источника без изменения
    handler/service API: `services/inventory`, `shipment`, `stats`, `tracking`,
    `returns`, `jobs`, `worker`.
  - сохранена обратная совместимость импортов через alias-классы
    `InventorySheetReader` / `InventorySheetMutator`.
  - досинканы `.env.example`, `README.md`, `CLAUDE.md`, `docs/02-architecture.md`,
    `docs/04-warehouse-sheets.md`, `docs/ROADMAP.md`.
  - валидация: `./.venv/bin/ruff check app tests`, `./.venv/bin/ruff format --check app tests`,
    полный `./.venv/bin/pytest` — всё зелёное (**330 passed**).
- **Дальше:** push ветки, PR в `main`, дождаться зелёного GitHub Actions CI и смержить.
- **Открытые вопросы:** реальный CRM/WMS REST adapter остаётся отдельной следующей задачей
  поверх готового seam; в этой фазе он сознательно оставлен stub-ом.

## 2026-06-23 · fix/phase6-support-auth · review-fix
- **Сделано:** после code review Фазы 6 закрыты два боевых риска и досинкан статус:
  - support hardening: серверный гейт на доступ к тредам в `handlers/support` — менеджер
    может открыть/ответить/закрыть только свой тред или очередь `waiting`; подмена callback
    с чужим `thread_id` больше не даёт доступ к чужой переписке.
  - `can_handle_support` теперь реально применяется: менеджер без этого флага не может
    открыть inbox поддержки и не может встать на дежурство (`🟢 Я на зв'язку`).
  - Добавлены регрессионные bot-tests на чужой тред и revoked support-permission.
  - `README.md` синхронизирован со статусом: Фаза 6 уже в `main`, следующая — Фаза 7.
  - Валидация: `ruff` зелёный, полный `pytest` зелёный (**326 passed**; в отдельном worktree
    тестам нужны явные `DATABASE_URL` + `FERNET_KEY`, потому что `.env` туда не копируется).
- **Дальше:** открыть PR в `main`, дождаться зелёного CI и смержить.
- **Открытые вопросы:** скрывать ли reply-кнопки/пункты меню по manager permissions до входа
  в экран (сейчас гейтинг жёстко на server-side; UI может показать кнопку, но действие
  корректно запрещается).

## 2026-06-21 · chore/docs-status-phase6 · синк статус-доков под закрытую Фазу 6
- **Сделано:** Фаза 6 полностью в `main` (PR #37–#41: фундамент → дежурство → поддержка
  → персонал → отчёты/аналитика). Досинканы статус-доки: `CLAUDE.md` «Текущий статус»
  «Фазы 0–5» → «Фазы 0–6» (+ описание Фазы 6), следующая — **Фаза 7** (задел CRM/WMS,
  step). `docs/ROADMAP.md`: верхний статус «завершена Фаза 5» → «Фаза 6», таблица
  владельцев (6 → ✅ в `main`, 7 → следующая), секция Фазы 6 с пометкой реализации
  PR 6a–6e и отложенными TODO. Кода/тестов не трогали.
- **Дальше:** **Фаза 7** (задел CRM/WMS за абстракцией `app/sheets/`), владелец — step.
- **Открытые вопросы:** нет.

## 2026-06-21 · feat/alex-phase6-reports · PR 6e — отчёты/аналитика (📊 Звіти / 📈 Аналітика)
- **Сделано:** последний модуль Фазы 6:
  - `app/db/repositories/reports.py` (`ReportsRepository`): кросс-клиентские агрегаты —
    ТТН по окнам `status_changed_at` (сводки) и `dispatched_at` (fee/опоздавшие),
    счётчики тредов поддержки по менеджерам (open/closed).
  - `app/services/reports.py`: `period_report` (відправлено/повернення/втрати + чисті
    продажі + разбивка по клиентам; гейт `can_view_reports`), `financial_report` (сумма
    `fee_amount` где не `fee_free` + список опоздавших ТТН по `sla_met=False`; owner),
    `manager_support_stats` (open/closed треды по менеджерам; owner), `fee_for_units`
    (preview-формула `20 + (n−1)`). Периоды сьогодні/тиждень/місяць (переиспользует
    статус-сеты из `services/stats`).
  - `app/bot/handlers/reports.py` («📊 Звіти», manager+owner с правом) и
    `handlers/analytics.py` («📈 Аналітика», owner) с inline-переключателем периода;
    `keyboards/reports`, `texts/reports`. Роутеры в `dispatcher`.
  - Тесты: `test_reports` (агрегация по клиентам / право `can_view_reports` / fee+опоздавшие
    / owner-гейт / поддержка по менеджерам), `tests/bot/test_reports_handlers`. Локально
    ruff чисто, гейт слоёв чист, чистые юниты зелёные; DB-тесты — CI.
  - **Отложено (TODO):** аттрибуция ТТН по менеджерам (нет `manager_id` у `shipments` —
    per-manager пока = метрики поддержки), сводка склада в отчётах (зависит от Sheets),
    произвольный диапазон дат и графики.
- **Дальше:** синк статус-доков под закрытую Фазу 6 (CLAUDE.md/ROADMAP).
- **Открытые вопросы:** нет.

## 2026-06-21 · feat/alex-phase6-staff · PR 6d — управление персоналом (👔, owner-only)
- **Сделано:** владелец управляет менеджерами:
  - `app/services/staff.py`: `list_staff`/`get_staff_card` (менеджеры + индикатор on-duty +
    per-flag права), `add_manager` (по Telegram-ID/телефону: промоут не-активного юзера
    или создание нового; активного клиента/владельца/уже-менеджера отклоняем; флаги on
    по умолчанию), `set_permission` (тогл флага из реестра), `block_manager`/
    `unblock_manager` (при блоке — сброс дежурства + возврат открытых тредов в очередь),
    `demote_manager` (роль→client, дежурство/треды сняты). Всё через `_require_owner` +
    `can_manage` + `audit_logs`.
  - `repositories/support.unassign_open_for_manager`; новые исключения (`StaffNotFound`,
    `StaffAlreadyManager`, `StaffPromotionForbidden`, `InvalidPermissionFlag`);
    `notifications.manager_added_text`.
  - `app/bot/handlers/staff.py` на «👔 Персонал» (owner/dev): список (поиск/пагинация/
    «➕ Додати»), карточка с тоглами прав, блок/розблок/зняти роль (с подтверждением),
    найм по вводу. `keyboards/staff`, `texts/staff`, states `StaffState`; роутер в
    `dispatcher`.
  - Тесты: `test_staff` (owner-гейт/найм/права/блок+треды/снятие роли + audit),
    `tests/bot/test_staff_handlers` (список/найм+пуш/тогл права). Локально ruff чисто,
    гейт слоёв чист, чистые юниты зелёные; DB-тесты — CI.
- **Дальше:** PR 6e — отчёты/аналитика (📊 Звіти / 📈 Аналітика), последний в Фазе 6.
- **Открытые вопросы:** нет.

## 2026-06-21 · feat/alex-phase6-support · PR 6c — поддержка (релей клиент↔дежурный + лог)
- **Сделано:** релей-чат поддержки через бота (без прямых TG-контактов):
  - `app/services/support.py`: `get_duty_contact`, `open_or_get_thread` (маршрутизация —
    рабочее время+дежурный → `open` назначен дежурному; рабочее время без дежурного →
    `waiting` + сигнал владельцу; вне часов → `waiting`), `post_message` (bump инбокса),
    `claim_if_waiting` (ответ на очередь назначает тред менеджеру), `close_thread`.
  - `repositories/support.list_for_manager_inbox` — свои `open` + вся очередь `waiting`
    (заступивший дежурный разгребает накопленное).
  - `services/notifications`: релей-тексты (HTML-escape пользовательского текста),
    `notify_support_queued_to_owner` (строка-метка, чтобы пуш после commit не упёрся в
    expired-атрибуты), `_owner_recipient_ids`.
  - `app/bot/handlers/support.py`: клиент «💬 Звернення до менеджера» (карточка дежурного
    → «Почати чат» → FSM-чат, релей менеджеру или очередь), менеджер «💬 Підтримка»
    (инбокс «мої + черга», открыть/відповісти/закрити), owner/dev — полный лог + поиск.
    `keyboards/support`, `texts/support`, states `SupportState`; роутер в `dispatcher`.
  - Тесты: `test_support` (маршрутизация/очередь/повтор/escape), `test_support_repository`
    (+inbox), `tests/bot/test_support_handlers` (релей менеджеру/клиенту, очередь, claim,
    инбокс). Локально ruff (lint+format) чисто, гейт слоёв чист, чистые юниты зелёные;
    DB-тесты исполняются на Postgres в CI.
- **Дальше:** PR 6d — управление персоналом (👔 Персонал, owner-only).
- **Открытые вопросы:** нет.

## 2026-06-21 · feat/alex-phase6-duty · PR 6b — дежурство менеджера (смена + авто-снятие)
- **Сделано:** на фундаменте 6a реализовано дежурство:
  - `app/services/duty.py`: `go_on_duty` (открыть смену — `on_duty`/`duty_date`/`duty_since`
    + audit `duty_started`; вне рабочих часов — `OfficeClosed` с `next_open`),
    `current_duty_managers` (дежурные сейчас, вставший последним — первый; фильтр
    `duty_date=сегодня` отсекает зависшие смены), `clear_expired_duty` (снять смену при
    закрытии отделения / новом дне + audit `duty_ended`).
  - Хендлер `app/bot/handlers/duty.py` на кнопку «🟢 Я на зв'язку» (manager/owner/dev) +
    uk-тексты `texts/duty`; роутер зарегистрирован в `dispatcher` (перед `errors`). Кнопка
    уже была в меню — теперь на неё есть обработчик.
  - Воркер: job `clear_expired_duty_job` (interval `DUTY_CHECK_SECONDS`=300с, `max_instances=1`,
    опц. пуш «зміну завершено» снятым) + `notifications.duty_shift_ended_text`.
  - `UserRepository.set_duty` расширен `duty_since`; новый конфиг `DUTY_CHECK_SECONDS`
    (+ `.env.example`); доменное исключение `OfficeClosed`.
  - Тесты `test_duty` (открытие смены / закрыто после часов / выходной / роль / порядок
    дежурных / авто-снятие / зависшая смена прошлого дня). Локально ruff (lint+format)
    чисто, гейт слоёв чист, чистые юниты зелёные; DB-тесты исполняются на Postgres в CI.
- **Дальше:** PR 6c — релей-чат поддержки (клиент↔дежурный) поверх `current_duty_managers`.
- **Открытые вопросы:** нет.

## 2026-06-21 · feat/alex-phase6-foundation · PR 6a — фундамент Фазы 6 (данные + расписание + реестр прав)
- **Сделано:** старт Фазы 6 (поддержка/дежурство + персонал/аналитика, владелец alex по
  sequential-by-phase). PR 6a — фундамент под следующие PR, без UI:
  - Модели `app/db/models/support.py`: `SupportThread` (client/assigned_manager/shipment,
    `status`, `closed_at`) + `SupportMessage` (thread/`sender_role`/text); enum
    `SupportThreadStatus(open/waiting/closed)`; зарегистрированы в `models/__init__`.
  - Колонка `users.duty_since` (момент открытия смены — выбор «вставшего последним»
    дежурного при нескольких на смене + отображение статуса).
  - Миграция `c9e2f7a1b3d4_phase6_support_threads_and_duty` от head `b7d2f4a1c9e0`
    (таблицы `support_threads`/`support_messages` + enum + `duty_since`; downgrade обратим).
  - `app/db/repositories/support.py` (`SupportRepository`): create_thread / add_message /
    get_active_thread_for_client / get_with_messages / list_for_manager / list_all (поиск
    клиент/менеджер/дата + фильтр статуса) / assign_manager / close_thread.
  - `app/utils/work_schedule.py`: оконная логика расписания вынесена из `utils/sla.py`
    (единый источник правды) + новые `is_open` / `current_window_end` — нужны дежурству
    (маршрутизация поддержки и авто-снятие смены при закрытии отделения). `sla.py`
    импортирует общие хелперы.
  - Реестр прав в `app/bot/permissions.py`: `PERMISSION_FLAGS` + канонические ключи
    (`can_manage_clients`/`can_edit_clients` + новые `can_handle_support`/`can_view_reports`);
    `services/clients` ссылается на канонические константы (без дублей строк).
  - Тесты: `test_work_schedule` (окна/`is_open`/границы/выходные), реестр прав в
    `test_permissions`, `test_support_repository` (треды/сообщения/инбокс/лог/закрытие).
    Локально ruff (lint+format) чисто, чистые юниты зелёные, весь сьют собирается;
    DB-тесты исполняются на Postgres в CI.
- **Дальше:** PR 6b — дежурство (`🟢 Я на зв'язку`, `services/duty`, авто-снятие воркером)
  на этом фундаменте.
- **Открытые вопросы:** нет.

## 2026-06-20 · chore/docs-status-phase5 · синк статус-доков под Фазу 5 (#35)
- **Сделано:** Фаза 5 смержена в `main` (PR #35, `a3bf4cd`) — воркер `worker.py` +
  `jobs.py` (APScheduler-поллинг статусов НП и low-stock), `utils/sla` (30 раб. минут,
  Europe/Kyiv), `services/{tracking,returns,notifications}`, `services/manager_shipments`
  + `handlers/manager_shipments` (очередь отправлений, SLA-карточки, ручные lost/damaged,
  приёмка возврата с per-item inspection), клиентские настройки уведомлений + low-stock
  anti-spam (persisted state), миграции `phase5_foundation` / `phase5_low_stock_alert_state`,
  новые модели/репо (`stock_movement`/`notification_setting`/`low_stock_alert`). Тесты: 238 → 264.
  Этим коммитом досинкан статус, который #35 не трогал: `CLAUDE.md` «Текущий статус» «Фазы 0–4»
  → «Фазы 0–5», следующая — **Фаза 6** (alex); распределение `Phase 2/4/6 → alex, Phase 3/5/7 → step`.
  (`docs/ROADMAP.md`/`CONTRIBUTING.md`/`README.md` уже обновлены в #35.) Кода/тестов не трогали.
- **Дальше:** **Фаза 6** (поддержка/дежурство + персонал/аналитика), владелец по
  sequential-by-phase — alex.
- **Открытые вопросы:** нет. (Опц. отложено — PR 9e «Останні отримувачі».)

## 2026-06-19 · chore/docs-status-phase4-followups · синк статус-доков (без кода)
- **Сделано:** актуализированы статус-секции, отставшие от факта. `CLAUDE.md` «Текущий статус» (был
  «Фаза 2 в main, следующая Фаза 3») → «Фазы 0–4 в `main` (+ hardening follow-up), следующая Фаза 5,
  ведёт step»; распределение `Phase 2/4 → alex, Phase 3/5 → step`. `docs/ROADMAP.md` Фаза 4: добавлен
  блок про follow-up #32/#33 (гейт отправителя, обязательный `sender_phone`, стойкость Redis,
  backstop `DecryptionError`), счётчик тестов 225 → 238. Кода/тестов не трогали.
- **Дальше:** **Фаза 5** (трекинг НП/SLA/low-stock в воркере), владелец — step.
- **Открытые вопросы:** нет.

## 2026-06-19 · feat/phase4-decrypt-error-backstop · backstop непрочитаного ключа ФОП (Фаза 4, follow-up)
- **Сделано:** закрыт последний не-блокер из журнала. `np_api_key` расшифровывается на чтении строки
  ORM (`EncryptedString`); при ротации/потере `FERNET_KEY` любая загрузка `SenderProfile` бросала
  `DecryptionError`, а профили читаются во многих местах (создание/цена/адреса/отмена/кабинет/клиенты)
  — пользователь получал опаковую ошибку. Так как ключ глобальный (ломается «всё разом»), ловим не
  точечно в 8 сервисах, а **одним dispatcher-level backstop'ом**: новый `app/bot/handlers/errors.py`
  (`errors_router` с `ExceptionTypeFilter(DecryptionError)`) — громкий лог `fernet_decrypt_failed` для
  ops + понятный uk-текст пользователю (в ответ на message/callback). Зарегистрирован в `dispatcher.py`;
  транзакция к этому моменту уже откатана `ServicesMiddleware`. Докстринг `crypto.decrypt` уточнён.
  Тесты (+3): ответ в message / через callback / без цели (не падаем). Полный сьют **238** зелёный,
  ruff (lint+format) + гейт слоёв чисты.
- **Дальше:** **Фаза 5** (трекинг НП/SLA/low-stock в воркере), владелец по sequential-by-phase — step.
- **Открытые вопросы:** нет.

## 2026-06-19 · feat/phase4-sender-redis-hardening · гейт даних відправника + стійкість Redis (Фаза 4, follow-up)
- **Сделано:** закрыты два реальных бага боевого пути, которые маскировали моки.
  **Issue A (неполный отправитель):** гейт создания ТТН проверял только `np_sender_ref`, а
  `contact_ref`/`phone`/`city_ref`/`warehouse_ref` молча уходили в НП пустыми → реальный `save_ttn`
  упал бы. Введён единый предикат `shipment.ensure_sender_dispatchable(profile, settings)` (вызов в
  `_resolve_sender`) + новые исключения `SenderProfileIncomplete` (нет телефона/контакта) и
  `SenderDispatchNotConfigured` (не задан склад-отправитель `NP_SENDER_*`). Вход в FSM
  (`start_create_ttn`) переключён с `is_np_validated`-проверки на `shipment.resolve_default_sender_id`
  (то же предусловие — UI и сабмит ведут себя одинаково), добавлены uk-тексты гейта и ветки
  `_submit_error_text`. **Issue B (Redis):** `NPReferenceCache` дёргал Redis без `try/except`, докстринг
  обещал устойчивость — падение Redis ломало поиск адресов целиком. Чтения/запись обёрнуты в
  `RedisError` → мягкий fallback к `loader` (с `log.warning`), докстринг `_store` исправлен.
  Тесты (+8): 3 в `test_shipment_create` (нет телефона/контакта/склада → исключение, НП не вызывается),
  3 в `test_novaposhta_cache` (Redis down/write-fail → fallback), 2 entry в `test_ttn_cart`; фикстуры
  «happy» дополнены телефоном/контактом и заданным складом (они и были той дырой).
  **Первопричина (по решению владельца):** `sender_phone` теперь **обязателен при сохранении профиля**
  — `create_profile` отбивает пустой телефон (`SenderProfileIncomplete`), `update_profile` запрещает его
  очистку, в кабинете телефон убран из «очищаемых» полей. Так состояние «ключ валиден, но телефона нет»
  не возникает в принципе (гейт на создании ТТН остаётся как defense-in-depth). +2 теста профиля,
  поправлены вызовы `create_profile` в тестах. Полный сьют **235** зелёный, ruff (lint+format) + гейт
  слоёв чисты.
- **Дальше:** **Фаза 5** (трекинг НП/SLA/low-stock в воркере), владелец по sequential-by-phase — step.
  Опц. хардненинг: ловить `DecryptionError` из ORM-чтения на уровне сервисов (срабатывает только при
  ротации `FERNET_KEY`).
- **Открытые вопросы:** нет.

## 2026-06-19 · chore/alex-phase4-env-9e-stub · плейсхолдеры env + стаб 9e (Фаза 4)
- **Сделано:** без изменения логики. В `.env.example` добавлен блок «Нова Пошта» с пустыми
  `NP_SENDER_CITY_REF`/`NP_SENDER_WAREHOUSE_REF` (Ref нашего склада — заполнить перед боевым
  запуском; транспорт/TTL имеют дефолты в `config.py`). PR 9e (история получателей) **отложен** —
  точка подключения помечена `TODO (PR 9e)` в `keyboards/ttn.build_recipient_kind_kb` (подставляет
  только name/phone/kind/edrpou — в БД нет ref міста/відділення). Заметки в `docs/ROADMAP.md`
  (Фаза 4: реализовано / отложено 9e / что задать перед запуском). Сьют **225** зелёный, ruff чист.
- **Дальше:** по Фазе 4 — точка (опц. 9e позже). Следующая — **Фаза 5** (трекинг НП/SLA/low-stock
  в воркере), владелец по sequential-by-phase — step.
- **Открытые вопросы:** нет.

## 2026-06-19 · feat/alex-phase4-ttn-cancel · NP-aware «Скасувати» (Фаза 4, follow-up 9d)
- **Сделано:** закрыт orphan-шов на **отмене**. Новый write-`shipment.cancel_shipment(np_client)`:
  **NP-first** — сначала `InternetDocument.delete` в НП, и только при успехе снимаем статус в БД
  (резерв выводится из статуса → освобождается). Иначе (снять резерв до удаления в НП) при сбое
  НП была бы «живая» ТТН в НП с освобождённым резервом → риск oversell. «Уже видалено»
  (`NovaPoshtaNotFound`) — идемпотентный успех; прочие ошибки НП → `TtnCancelFailed` (статус не
  трогаем, отмену можно повторить). БД-часть (гарды/статус-чек/аудит/карточка) переиспользована из
  read-side `shipments.cancel_shipment`. Бот-хендлер `cab:cancel` (`client_cabinet`) переключён на
  NP-aware вариант (инжект `np_client`), добавлен uk-текст на `TtnCancelFailed`. Тесты (3):
  delete+release, NP-error→резерв держится, already-deleted→идемпотентно; бот-тест отмены обновлён
  под новую сигнатуру. Полный сьют **225** зелёный, ruff + гейт слоёв чисты.
- **Дальше:** опц. PR 9e — история получателей. E2E к реальному НП — при наличии ключа.
- **Открытые вопросы:** точная идемпотентность `InternetDocument.delete` (классификация «уже
  видалено») — уточнить на боевом НП; сейчас ловим `NovaPoshtaNotFound`.

## 2026-06-19 · feat/alex-phase4-ttn-send · FSM ТТН: відправлення + 🚚 (Фаза 4, PR 9d)
- **Сделано:** поток создания ТТН **ожил для клиента**. Кнопка «✅ Відправити ТТН» на карточке →
  `cb_submit` → `create_shipment` (NP-first + резерв) → экран успеха з № ТТН + «🚚 Створити ще одну».
  Reply-кнопка меню **🚚 Створити ТТН** привязана к входу (`open_create_ttn` → `start_create_ttn`).
  **Single-flight**: модульный set `_SUBMITTING` по `telegram_id` (атомарная проверка `in`+`add` без
  await между ними) — двойной тап не создаёт две ТТН. Доменные ошибки → uk: `InsufficientStock` →
  «на залишку лише N …» (имя из кошика), `SenderProfileNotValidated/NotConfigured`, `TtnCreationFailed`
  → конкретный текст; при ошибке карточка остаётся (NP-first → повтор безопасен). Пуш менеджеру
  «Створені» — через `BotNotifier` (best-effort внутри сервиса). **Focused-review (критичный money-path)**:
  найден реальный баг шва — если показ успеха (`edit_text`) падал, исключение откатывало транзакцию
  middleware и осиротил бы NP-ТТН; пофикшено `_show_success` (глотает `TelegramAPIError`, успех — не
  блокирует коммит). Тесты (7): успех, single-flight, InsufficientStock-uk, missing-fields, привязка
  🚚/again, success-render-failure. Полный сьют **222** зелёный, ruff + гейт слоёв чисты.
- **Дальше:** NP-aware «Скасувати» (отдельный мелкий PR): write-`cancel_shipment` с `np_client`
  (`InternetDocument.delete`, NP-first) + переключить бот-хендлер `cab:cancel`. Опц. PR 9e — история
  получателей. Профильный E2E к НП — при наличии реального ключа.
- **Открытые вопросы:** `NP_SENDER_CITY_REF` нужен в окружении для расчёта/отправки; габарити «Власні»
  (ДхШхВ) домен пока не несёт (метка-пресет).

## 2026-06-19 · feat/alex-phase4-ttn-card-edit · FSM ТТН: правка картки + COD (Фаза 4, PR 9c-2)
- **Сделано:** карточка стала редактируемой. ✏️ на каждом поле: текст (ПІБ/тел/ЄДРПОУ/вага/
  опис/вартість) → state `editing_field` + `edit_field`-токен → `receive_edit` (валидация per
  поле, нормализация телефона, вес/сумма через `_parse_weight`/`_parse_money`); выбор —
  инлайн-пикеры (габарити S/M/L, платник Отримувач/Відправник, оплата). **COD**: пикер оплати →
  «Накладений платіж» → ввод суммы (`payment_method=cod` ставится ТОЛЬКО после валидной суммы >0,
  иначе остаётся prepay — нет COD без суммы); кнопка «= вартість товарів». Prepay сбрасывает
  `cod_amount=None`. Правка міста/відділення = повторный заход в пошук, `cb_wh` в конце снова
  рендерит карточку (return «даром», без флага). «◀ До картки» (`cb_card`) везде; `_back_to_card`
  отвечает на callback и при потере клиента (нет зависшего спиннера). Габарит-токены `setsz/setpr/
  setpm` разведены без коллизий с `sz/city/cart/card`. `receive_weight` отрефакторен на `_parse_weight`.
  **Focused-review**: 5 находок — все false-positive (ревьюер не учёл self-answer в `_back_to_card`
  и что `insured_amount` всегда числовой); кода не меняли. Тесты (15): пикеры, правки, COD-машина
  (set-after-amount, prepay-reset, codeq), back-to-card. Полный сьют **215** зелёный, ruff + гейт чисты.
- **Дальше:** PR 9d — кнопка «✅ Відправити» → `create_shipment` (single-flight) + uk-ошибки +
  привязка 🚚-кнопки меню (поток оживает) + NP-aware «Скасувати» в карточке відправлення.
- **Открытые вопросы:** нет.

## 2026-06-19 · feat/alex-phase4-ttn-card · FSM ТТН: картка-зведення + ціна НП (Фаза 4, PR 9c-1)
- **Сделано:** PR 9c разбит на 9c-1 (карточка + цена) и 9c-2 (правка полей + COD) ради
  ревьюабельности. **9c-1**: новый сервис `services/pricing.py` (`quote_ttn` —
  `getDocumentPrice`, склад-отправитель из `NP_SENDER_CITY_REF`, ключ ФОП по id из FSM).
  В боте — карточка-зведення: после выбора відділення `cb_wh` рендерит её через `_show_card`.
  Молчаливые дефолты (`_ensure_card_defaults`): `insured_amount` = Σ(price×qty) из кошика,
  `description` = имена товаров, `payment_method=prepay`, `payer_type=Recipient`. Цена НП —
  с кэшем в FSM-data по `_price_key` (місто|вага|вартість|COD — ровно поля, влияющие на
  `to_price_props`) + кнопка «🔄 Перерахувати» (force). Graceful-degradation: `NovaPoshtaError`/
  `ClientServiceError` → «Розрахунок недоступний — підтвердить менеджер», отправку не блокируем.
  Все имена/місто/опис/eta — HTML-escape. **Focused-review** применён: guard неполного FSM в
  `_card_price` (устаревшая `recompute` не падает `KeyError`, НП не дёргаем); escape `eta`.
  Тесты: `tests/test_pricing.py` (3, Postgres+NP-mock) + карточка в `test_ttn_cart.py` (дефолты,
  graceful, кэш, recompute, stale). Полный сьют **200** зелёный, ruff + гейт слоёв чисты.
- **Дальше:** PR 9c-2 — точечная правка ✏️ полей карточки (mini-states) + под-экран COD
  (prepay/cod + сума, «= вартість товарів») + правка міста/відділення (`return_to_summary`).
- **Открытые вопросы:** товар без цены в кошику → `insured` неполный (правится на картці в 9c-2);
  `NP_SENDER_CITY_REF` должен быть задан для живого расчёта (иначе graceful «недоступно»).

## 2026-06-19 · feat/alex-phase4-ttn-recipient · FSM ТТН: отримувач + адреса (Фаза 4, PR 9b)
- **Сделано:** PR 9b — данные получателя и адрес НП. `cb_recipient_kind` теперь ведёт по цепочке:
  **ПІБ/назва → ЄДРПОУ** (только для організації, валидация 8/10 цифр) **→ телефон**
  (нормализация `0XX`/`380XX`/`+380XX` → `380XXXXXXXXX`) **→ місто** (пошук `address.search_cities`
  через инжектированные `np_client`/`np_cache`, результаты кнопками по индексу из FSM-data) **→
  відділення** (`search_warehouses`, окна по 8 с пагинацией + «🔎 Знайти за №»). Выбор відділення →
  state `summary` (карточка — PR 9c). Ошибки НП (`NovaPoshtaError`) → «довідник недоступний»;
  `ClientServiceError` → текст. Города/відділення держим списками в FSM-data, в callback — индекс.
  Тексты с именами/запросами/містом — HTML-escape. Новые состояния в `CreateTtnState`.
  **Focused-review** применён: жёсткий guard негативного индекса (`idx < 0`) во всех
  index/offset-хендлерах (Python negative-indexing мог бы выбрать «последний» при crafted
  callback). Тесты (14): телефон/ЄДРПОУ, цепочка person/org, пошук міста/відділення (мок address),
  пагинация, негатив-индекс. Полный сьют **192** зелёный, ruff + гейт слоёв чисты.
- **Дальше:** PR 9c — карточка-зведення + точечная правка ✏️ + `getDocumentPrice` (кэш +
  graceful-degradation) + под-экран COD.
- **Открытые вопросы:** нет.

## 2026-06-19 · feat/alex-phase4-ttn-cart · FSM ТТН: каркас + кошик (Фаза 4, PR 9a)
- **Сделано:** PR 9a Фазы 4 — старт UX создания ТТН (**Express-картка**, выбрана советом
  4 философий → 3 судьи → синтез). Новый `app/bot/handlers/ttn.py` (router `create_ttn`,
  namespace `cab:ttn:*`), `keyboards/ttn.py`, `texts/ttn.py`, `CreateTtnState` в `states.py`.
  Покрыто: **вход** с ранним резолвом ФОП (нет → «ФОП ще не налаштований», без `np_sender_ref`
  → «ключ не підтверджено» — разные uk-тексты, чтобы не заполнять форму ради отказа на финале);
  **кошик** поверх `list_inventory` (степпер −1/+1/+5/+10/Макс + «✏️ Ввести число», клампинг
  по остатку, гард `available=0`, агрегация дублей sku с капом на остаток, перегляд/правка/
  видалення позиций); экран **«Параметри посилки»** (вага — текстовый ввод с валидацией и
  нормализацией `,`→`.`; габариты — пресет S/M/L); розвилка **типу отримувача** (особа/орг).
  Длинные sku в callback не кладём — резолвим по индексу страницы; корзина/вес/тип — в FSM-data.
  🚚-кнопку меню к входу привяжет PR 9d (поток пока не самодостаточен для юзера). Тесты
  `tests/bot/test_ttn_cart.py` (21): ФОП-гейт (Postgres), степпер/клампинг/агрегация, вес
  валидный/невалидный, переходы. **Code-review (10 углов)** применён: HTML-экранирование
  имён товаров (Sheets) в parse_mode=HTML экранах (иначе `<`/`&` ломали бы рендер); сброс
  FSM-стейта в `picking_items` в `cb_pick`/`cb_qty_ok`/`cb_cart_edit` (защита от мис-роутинга
  текста после «Ввести число»); дедуп рендера параметрів (`_show_parcel(edit=...)`); снят
  мёртвый `QTY_DELTAS`. Полный сьют **176** зелёный, ruff + гейт границы слоёв чисты.
- **Дальше:** PR 9b — отримувач + адреса: ПІБ/ЄДРПОУ(8/10)/телефон, `search_cities`/
  `search_warehouses` через `waiting_*`-state с `City[]`/`Warehouse[]` в FSM-data.
- **Открытые вопросы:** габариты «Власні» (ДхШхВ) — домен `create_shipment` их пока не несёт,
  в Фазе 4 это только метка-пресет (учтено в плане).

## 2026-06-19 · feat/alex-phase4-composition · composition root (Фаза 4, PR 8)
- **Сделано:** PR 8 Фазы 4 — composition root. В `app/main.py` собираем на весь процесс
  один `NovaPoshtaClient`, один `redis.asyncio`-клиент (`from_url(settings.redis_url)`) и
  `NPReferenceCache`; пробрасываем `np_client`/`np_cache` через `build_dispatcher` →
  `ServicesMiddleware` в `data` хендлера (рядом с `db_session`/`services`) — так FSM
  создания ТТН (PR 9) получит их через DI. На завершении polling — `aclose()` клиента НП и
  Redis (`try/finally`). FSM-хранилище — **оставлено `MemoryStorage`** (решение владельца):
  redis-клиент служит только кэшу справочников НП, бот не зависит от Redis для FSM/`/start`.
  `build_dispatcher`/`ServicesMiddleware` приняли `np_client`/`np_cache` опционально
  (back-compat для существующих вызовов/тестов). Тесты `tests/bot/test_composition.py`:
  middleware кладёт `np_client`/`np_cache` в `data`; без проброса — `None`. Полный сьют
  (153) зелёный, ruff + гейт границы слоёв чисты. `worker.py` без изменений (NP/Redis в Фазе 5).
- **Дальше:** PR 9a — каркас FSM `CreateTtnState` + кошик (`cab:ttn:*`, ранний резолв ФОП,
  степпер кількості, экран «Параметри посилки» вага+габарити) поверх `inventory`.
- **Открытые вопросы:** нет.

## 2026-06-19 · feat/alex-phase4-address-search · address-search сервис (Фаза 4, PR 7)
- **Сделано:** PR 7 Фазы 4 — `services/address.py` (`search_cities`/`search_warehouses`)
  для FSM создания ТТН. Резолвит ключ ФОП клиента (явный/дефолтный; нет →
  `SenderProfileNotConfigured`) и ходит в справочники НП через `NPReferenceCache`
  (cache-aside; loader → `methods.get_cities`/`get_warehouses`). `Address.*` требует
  валидный ключ, но **не** провалидированный ФОП (Ref отправителя тут не нужен) —
  берём ключ профиля как есть. Тесты: Postgres + `fakeredis` + фейковый NP
  (`MockTransport`): города возвращаются и кэшируются (loader 1 раз), відділення,
  отсутствие ФОП → ошибка. Полный сьют (151) зелёный, ruff + гейт границы чисты.
- **Дальше:** PR 8 — composition root (`app/main.py`): один `NovaPoshtaClient` + один
  `redis.asyncio` + `NPReferenceCache`, проброс в deps/handlers.
- **Открытые вопросы:** нет (тонкая обёртка).

## 2026-06-19 · feat/alex-phase4-create-shipment · write-сервис создания ТТН (Фаза 4, PR 6)
- **Сделано:** PR 6 Фазы 4 — **ядро домена**. `services/shipment.py` `create_shipment`
  (NP-first): гард активного клиента → резолв ФОП (нет → `SenderProfileNotConfigured`,
  не валидирован → `SenderProfileNotValidated`) → пред-проверка остатков
  (`qty ≤ available`, агрегируя дубли строк по sku → `InsufficientStock`) → НП
  `ensure_recipient` (контрагент-получатель) + `save_ttn` → при успехе
  `repo.create(status=created, ttn_number, np_ref, items, size_preset, weight)` —
  **резерв включается сам** (выводимый из статуса) → аудит → best-effort пуш
  персоналу «Створені». Любая ошибка НП → `TtnCreationFailed`, в БД ничего (резерва
  нет). `methods.ensure_recipient` (`Counterparty.save` фіз/юр) + `mapping`
  (`split_full_name`, `to_recipient_counterparty_props`) — стандарт НП v2.0,
  изолировано, под табличными тестами (контракт получателя в доках отсутствует —
  **требует боевой сверки**). `notifications.notify_shipment_created` +
  общий `_staff_recipient_ids`. Новые исключения: NotConfigured/NotValidated/
  InsufficientStock/TtnCreationFailed/TtnCancelFailed. **Правки по `/code-review`:**
  агрегация дублей sku (анти-oversell); пуш в try/except (сбой пуша не откатывает
  записанную ТТН); COD без суммы → доменная ошибка (анти-«тихий сброс COD»);
  `ensure_recipient`/`save_ttn`/`validate_key` достают `Ref` через гард (не KeyError
  мимо обработчика); юр-получатель — `CompanyName`. Тесты на Postgres (happy+резерв/
  NP-fail/over-reserve/дубли-sku/COD-без-суммы/нет-ФОП/не-валидирован/сбой-получателя)
  + methods. Полный сьют (148) зелёный, ruff + гейт границы слоёв чисты.
- **Дальше:** PR 7 — address-search сервис (города/відділення поверх `NPReferenceCache`).
- **Открытые вопросы:** контракт `Counterparty.save` получателя (имена полей,
  разбиение ПІБ, юрособа) — сверить с боевым НП; шов NP-save↔commit (известный
  остаточный риск — НП не переиспользует номера, single-flight в FSM PR 9);
  NP-aware `cancel` (NP delete) — в PR 9d при проводке кнопки «Скасувати».

## 2026-06-19 · feat/alex-phase4-shipment-cols · миграция size_preset + weight (Фаза 4, PR 5)
- **Сделано:** PR 5 Фазы 4 — единственный schema-PR. В `shipments` добавлены
  `size_preset VARCHAR(32)` и `weight NUMERIC(8,3)` (оба nullable): пресет НП и
  фактический вес (полезен для карточки и синка перевзвешивания НП в Фазе 5).
  Габариты «Власних розмірів» — транзитом в НП, не персистим. Модель `Shipment`
  расширена, `ShipmentRepository.create` принял `size_preset`/`weight`. Миграция
  `3f9a2b7c1d8e` (head = phase3 `2c1d4e8f1a6b`). Проверено: `alembic upgrade head →
  downgrade -1 → upgrade head` (колонки появляются/исчезают), `alembic check` — «No
  new upgrade operations detected» (модель совпадает с миграциями). Round-trip тест
  репозитория. Полный сьют (133) зелёный, ruff чист.
- **Дальше:** PR 6 — write-сервис `services/shipment.py` (`create_shipment` NP-first
  + резерв, `cancel_shipment` с NP delete). Ядро домена.
- **Открытые вопросы:** нет (миграция изолирована).

## 2026-06-19 · feat/alex-phase4-sender-validation · валидация ключа ФОП (Фаза 4, PR 4)
- **Сделано:** PR 4 Фазы 4 — первый сервисный PR. `services/sender_profile`:
  `create_profile`/`update_profile` приняли `np_client`; при заданном клиенте ключ
  ФОП валидируется в НП (`methods.validate_key_and_get_sender`) **до** записи —
  плохой ключ/нет контрагента → `SenderProfileKeyInvalid` (новое исключение),
  профиль не сохраняется; успех подтягивает `np_sender_ref`/`np_contact_ref` в
  профиль, склад-отправитель — из конфига (`NP_SENDER_WAREHOUSE_REF`). Транзитный
  `NovaPoshtaUnavailable` не клеймит ключ — пробрасывается. `SenderProfileView`
  получил `is_np_validated`. `repo.create` принял ref-поля (миграции не нужно —
  колонки есть с Фазы 1). Конфиг: `NP_SENDER_CITY_REF`/`NP_SENDER_WAREHOUSE_REF`.
  Без `np_client` (часть тестов) валидация пропускается — профиль
  «непровалидирован», создание ТТН его не пропустит (PR 6). **Правки по
  `/code-review`:** склад в refs кладём только если задан в конфиге (правка одного
  ключа не обнуляет ранее сохранённый склад); пустой `np_api_key` при update →
  `SenderProfileKeyInvalid` (ФОП без ключа бесполезен). Тесты: фейковый NP-клиент
  на `MockTransport` (валидный ключ/плохой ключ/без клиента/ротация/сохранение
  склада/пустой ключ). Полный сьют (132) зелёный, ruff + гейт границы слоёв чисты
  (`services` импортирует `novaposhta` — это абстракция, не aiogram).
- **Дальше:** PR 5 — миграция `shipments.size_preset` + `weight` (единственный
  schema-PR) + расширение модели/`repo.create`.
- **Открытые вопросы:** точный набор полей отправителя в `save` (CitySender/
  SenderAddress vs counterparty-address) — сверить с боевым НП; пока склад/город из
  конфига.

## 2026-06-19 · feat/alex-phase4-np-cache · Redis cache справочников НП (Фаза 4, PR 3)
- **Сделано:** PR 3 Фазы 4 — `app/novaposhta/cache.py` `NPReferenceCache`:
  cache-aside поверх `redis.asyncio` для `Address.*` (города/відділення). На miss
  зовётся инъектируемый `loader` (обращение к НП), результат кладётся в Redis с TTL;
  на hit — из кэша. Ключи общие (справочники от ключа ФОП не зависят), нормализация
  query (регистр/пробелы). Первое использование Redis в проекте — клиент живёт в
  `novaposhta/` (как gspread в `sheets/`), сервисы видят только `NPReferenceCache`.
  Конфиг: `np_cities_ttl_seconds` (24ч), `np_warehouses_ttl_seconds` (6ч).
  `fakeredis` в `requirements-dev.txt`. **Правки по `/code-review`:** пустой
  результат **не** кэшируем (НП мог отдать `[]` на блипе — иначе «не знайдено»
  залипло бы на TTL); `ttl ≤ 0` (выключенный кэш) — пропускаем `set`, иначе Redis
  бросил бы `invalid expire time` после успешного loader. Тесты (9) на `fakeredis`:
  miss→hit (loader once), нормализация ключа, раздельные записи, TTL из конфига,
  ошибка loader не кэшируется, пусто не кэшируется, `ttl=0` не падает. Полный сьют
  (126) зелёный, ruff + гейт границы слоёв чисты.
- **Дальше:** PR 4 — валидация ключа ФОП в `services/sender_profile` (первый PR,
  трогающий существующий сервис + его тесты).
- **Открытые вопросы:** CI Redis-сервиса нет — тесты кэша на `fakeredis` (реальный
  Redis не нужен); боевую сверку справочников — при наличии ключа НП.

## 2026-06-19 · feat/alex-phase4-np-methods · NP methods + mapping (Фаза 4, PR 2)
- **Сделано:** PR 2 Фазы 4 — обёртки методов НП и **чистый маппинг полей**.
  `mapping.py` (без I/O): `to_save_props` (черновик ТТН → `InternetDocument.save`:
  `PayerType` выбор клиента, `PaymentMethod=Cash` константой, `Cost`=страховая,
  COD→`BackwardDeliveryData`, передоплата→поле не шлём, `VolumeGeneral`/`SeatsAmount`
  опц.), `to_price_props` (`getDocumentPrice`, COD→`RedeliveryCalculate`),
  `money`/`weight` (Decimal→строка). `methods.py` (поверх `client.call`):
  `get_cities`/`get_warehouses` (справочники), `get_price`, `get_status_documents`
  (батч), `save_ttn`/`delete_ttn`, `validate_key_and_get_sender` (Counterparty
  Sender Ref + контакт). `schemas.py` — доменные frozen-структуры
  (`City`/`Warehouse`/`SenderIdentity`/`SenderValidation`/`RecipientSpec`/
  `ParcelSpec`/`TTNDraft`/`TTNResult`/`PriceQuote`/`TrackingStatus`). Контракт НП
  v2.0: `ServiceType=WarehouseWarehouse`, стороны по Ref'ам контрагентов; вся
  рисковая логика изолирована в `mapping.py` (открытый вопрос Фазы 0 — правка одним
  файлом). **Правки по `/code-review`:** `money`/`weight` через `Decimal(str(...))`
  (не тащим float-шум вроде `199.99…0094`); `get_price` при отсутствии `Cost` бросает
  `NovaPoshtaValidationError` (не выдаём «0 грн» за доставку). Тесты: табличные на
  `mapping` + `methods` через `MockTransport` (без сети/ключей). Полный сьют (117)
  зелёный, ruff + гейт границы слоёв чисты.
- **Дальше:** PR 3 — `cache.py` (Redis cache-aside для `Address.*`) + `fakeredis` в
  dev-deps; затем PR 4 — валидация ключа ФОП в `sender_profile`.
- **Открытые вопросы:** точный набор обязательных полей `save`/форма COD — пинятся
  табличными тестами `mapping`; сверить с боевым НП при наличии ключа.

## 2026-06-19 · feat/alex-phase4-np-core · NP transport core (Фаза 4, PR 1)
- **Сделано:** старт **Фазы 4** (интеграция НП + создание ТТН). PR 1 — транспортное
  ядро `app/novaposhta/`: `client.NovaPoshtaClient` (async поверх `httpx`, единый
  POST-эндпоинт НП, `apiKey` передаётся **на вызов** — он per-ФОП; инъектируемый
  `transport` для тестов; tenacity-ретраи только для временных сбоев
  `NovaPoshtaUnavailable` (сеть/таймаут/5xx), бизнес-ошибки НП не ретраятся),
  `schemas.NPEnvelope` (разбор конверта `{success,data,errors,errorCodes}`),
  `exceptions` (`NovaPoshtaError` → `Unavailable`/`AuthError`/`ValidationError`/
  `NotFound`). Конфиг: `np_api_url`/`np_timeout_seconds`/`np_max_retries`/
  `np_retry_backoff`. **Правки по `/code-review`:** классификация сначала по
  `errorCodes` (auth-код `20000200068` → `AuthError`, важно для валидации ключа в
  PR 4), затем текстовый фолбэк (убран слишком широкий хинт «ключ»); `_as_str_list`
  терпим к dict-форме поля НП; ретраи в тестах без реальных пауз
  (`np_retry_backoff=0`); тест-настройки герметичны (`_env_file=None`). Тесты (12)
  на `httpx.MockTransport` — без сети и ключей: декод конверта, payload,
  классификация (текст + код), ретраи 5xx/сеть, recover-after-transient,
  бизнес-ошибка без ретрая, 404/не-JSON/не-dict тело. Полный сьют (97) зелёный
  локально (Postgres-контейнер), ruff + гейт границы слоёв чисты (`app/novaposhta/`
  вне гейта — httpx/redis живут там, сервисы видят абстракции).
- **Дальше:** PR 2 — `methods.py` (save/delete/price/tracking/cities/warehouses/
  validate_key) + `mapping.py` (чистый `to_save_props`: PayerType/COD/фіз-юр/Cost).
- **Открытые вопросы:** точный набор обязательных полей `InternetDocument.save` и
  форма `BackwardDeliveryData` для COD — изолированы в `mapping.py` (PR 2), правка
  одним файлом с табличными тестами. Полагаемся на стандартный контракт НП v2.0.

## 2026-06-18 · feat/step-phase3 · 18aa2cd
- **Сделано:** **Фаза 3 закрыта полностью.** Реализован кабинет клиента end-to-end:
  `app/db/models/shipment.py`, `app/db/repositories/shipment.py`,
  `app/services/{inventory,shipments,stats,client_settings}.py`,
  `app/sheets/inventory.py`, миграция
  `2c1d4e8f1a6b_phase3_shipments_and_items`, bot/UI
  `handlers/client_cabinet.py`, `keyboards/client.py`, `texts/client_cabinet.py`,
  `states.ClientCabinetState`. Работают товары (поиск/категории/пагинация),
  відправлення (группы/поиск/карточка/скасування), статистика
  today/week/month + выбор дня, настройки клиента, self-edit профиля и просмотр/
  правка ФОП-профилей. Дополнительно устранён циклический импорт в `app/bot`,
  укорочены `callback_data` под лимит Telegram. Тесты расширены
  (`test_inventory`, `test_shipments`, `test_client_settings`, bot-тесты и
  клавиатуры). Локально: **85 passed**, `ruff` зелёный. Создан PR
  [#16](https://github.com/yennned/NovaPostBot/pull/16), CI зелёный.
- **Дальше:** после мержа PR #16 alex стартует **Фазу 4** от свежего `main`
  (интеграция НП + создание ТТН).
- **Открытые вопросы:** неблокирующий GitHub annotation про Node.js 20 в
  `actions/checkout@v4` и `actions/setup-python@v5`; сам CI проходит успешно.

## 2026-06-18 · feat/alex-phase2-profile · правка профиля клиента (Фаза 2 закрыта)
- **Сделано:** UI правки профиля клиента — последний кусок Фазы 2. `cb_edit`
  (выбор поля Ім'я/Телефон), `cb_edit_field` (FSM `ClientManageState.waiting_for_edit`),
  `receive_edit` (вызов `clients.update_client_profile`, ловит `ClientServiceError`/
  `PhoneAlreadyTaken` → uk-текст, перерисовывает карточку). Кнопка «✏️ Редагувати» +
  `build_edit_fields_kb` в клавиатурах, ярлыки полей в текстах. Гарды как в
  остальных хендлерах. Тесты bot-слоя (правка имени, коллизия телефона, set-state).
  Полный сьют (67) зелёный, ruff + гейт границы чисты. **Фаза 2 закрыта полностью.**
- **Дальше:** Степан стартует **Фазу 3** (кабинет клиента + остатки) от свежего
  `main` по `docs/phase3-stepan-brief.md`.
- **Открытые вопросы:** нет.

## 2026-06-18 · docs/alex-distribution-sync · синхронизация доков распределения
- **Сделано:** привёл все forward-looking доки к модели **sequential-by-phase**
  (чтобы не путать со старым layer-split). `docs/ROADMAP.md` — раздел распределения
  переписан: sequential как основная модель + таблица владельцев фаз + «scope
  каждой фазы» (оба слоя на владельца), layer-split явно помечен фолбэком.
  `docs/phase2-stepan-brief.md` → **`docs/phase3-stepan-brief.md`** (Степан ведёт
  всю Фазу 3 целиком от свежего main; убрано контракт-потребление/cherry-pick).
  `CLAUDE.md` — «Текущий статус» обновлён (Фазы 0–2 в main, модель sequential).
  `CONTRIBUTING.md` уже консистентен (sequential основной, layer-split фолбэк).
  Грепом подтверждено: WT1/WT2/«делится на 2 worktree» в forward-looking доках нет.
- **Дальше:** доделать UI правки профиля клиента — закрыть Фазу 2 полностью.
- **Открытые вопросы:** нет.

## 2026-06-18 · feat/alex-phase2-fixes · фиксы code-review (10 находок)
- **Сделано:** правки по итогам /code-review Фазы 2.
  (1) `_require_staff`/`_require_can_manage` проверяют **статус актёра** — блок/архив
  менеджер больше не управляет клиентами по «залипшим» reply-кнопкам.
  (2) Пуши шлём **после commit** (`start.receive_contact`, `cb_action` approve) —
  сбой коммита не оставит ложное уведомление; conftest переведён на
  `join_transaction_mode="create_savepoint"` (commit в тестах не ломает изоляцию).
  (3) callback-хендлеры ловят битый `callback.data` (split/uuid/int) → «кнопка
  застаріла». (4) Гард `callback.message is None` (старое сообщение).
  (5) `update_client_profile` проверяет занятость телефона → доменное
  `PhoneAlreadyTaken` вместо сырого IntegrityError. (6) Поиск исключает команды
  (`~startswith("/")`) и не ищет по кнопке «Клієнти». (7) `restore` → `pending`
  (повторное подтверждение, блок не теряется). (8) `created_at` в карточке →
  Europe/Kyiv. (9) Уведомления персоналу шлём `asyncio.gather` (параллельно).
  (10) `_card` без лишнего `get_default_for_client` (дефолт из уже загруженного
  списка). Новые тесты (блок-актёр, коллизия телефона) + правка restore-теста.
  **64 теста зелёные**, ruff + гейт границы чисты.
- **Дальше:** правка профиля клиента (UI) — остаток Фазы 2; затем Степан → Фаза 3.
- **Открытые вопросы:** нет.

## 2026-06-18 · chore/alex-ci-boundary · усиления council
- **Сделано:** CI-гейт «`app/services` и `app/db` не импортируют aiogram»
  (grep-шаг в `ci.yml`) — держит сервисный слой API-first/переиспользуемым даже
  при ослабленной (sequential) границе. `.gitignore`: паттерн `* [0-9].*` против
  дубликатов файл-синка (iCloud/Dropbox «dispatcher 2.py»). Локально гейт зелёный.
- **Дальше:** правка профиля клиента (остаток Фазы 2) отдельным PR.
- **Открытые вопросы:** нет.

## 2026-06-18 · feat/alex-phase2 · bot/UI управления клиентами
- **Сделано:** UI раздела «Клієнти» (Фаза 2) поверх контракта. `handlers/
  clients_manage.py` — вход по кнопке меню, список со статус-вкладками
  (pending/active/blocked/archived/всі) + пагинация + поиск (FSM
  `ClientManageState.waiting_for_search`), карточка клиента, действия над статусом
  (підтвердити/блок/розблок/архів/відновити) через `services.clients`. `keyboards/
  clients.py` (inline), `texts/clients.py` (uk + маппинг `ClientServiceError` →
  сообщения). `notify.BotNotifier` (Notifier поверх aiogram `Bot`, HTML, глотает
  сбои доставки). Wiring: пуш владельцам/дежурным при регистрации
  (`start.receive_contact`, `result.created`) и клиенту при подтверждении. Роутер
  включён в dispatcher. Тесты bot-слоя (открытие/доступ/карточка/approve+пуш/
  запрещённый переход) + обновлены start-тесты. Полный сьют (62) зелёный, ruff чист.
- **Дальше:** правка профиля клиента (ПІБ/телефон) отдельным мелким PR; затем
  фаза собирается end-to-end и мержится. После полного мержа Фазы 2 — Степан
  стартует Фазу 3.
- **Открытые вопросы:** нет.

## 2026-06-18 · feat/alex-phase2 · sender_profile (backend-ready)
- **Сделано:** `services/sender_profile.py` — create/list/get/update/set_default
  поверх готового репозитория; `SenderProfileView` (ключ НП наружу не отдаётся,
  только `has_api_key`); первый профиль клиента авто-дефолтный; права (свой клиент
  / manager+/dev); аудит (ключ в аудите маскируется `***`). **NP-валидация НЕ
  делается — Фаза 4.** `exceptions.SenderProfileNotFound`. Тесты на Postgres
  (6) — зелёные, ruff чист.
- **Дальше:** bot/UI Фазы 2 (handlers/clients_manage, клавиатуры, тексты,
  ClientManageState, wiring, BotNotifier, триггеры пушей) — доводим фазу до
  end-to-end и мержим.
- **Открытые вопросы:** нет.

## 2026-06-18 · feat/alex-clients · смена модели работы
- **Решение:** перешли на **sequential-by-phase** (последовательно по фазам, не
  параллельно по слоям). Один владелец на фазу (backend+UI), второй ждёт мержа.
  **Phase 2 → alex целиком, Phase 3 → step целиком.** Причина: активный писатель
  сейчас один (Степан разгоняется) → throughput-издержка ≈ 0, плюсы (всегда
  рабочий `main`, нет дрейфа контракта, WIP=1) перевешивают. Принято после council
  (3/4 за гибрид, но выбран sequential осознанно). Триггер возврата к layer-split —
  второй одновременный писатель / дедлайн на 2×. Зафиксировано в
  [CONTRIBUTING.md](CONTRIBUTING.md) и [docs/ROADMAP.md](docs/ROADMAP.md). Контракт
  Фазы 2 (ниже) = backend-половина, alex доводит фазу до UI и мержит.
- **Открытые вопросы:** нет.

## 2026-06-18 · feat/alex-clients · 60d8956
- **Сделано:** **контракт Фазы 2** (слой alex, контракт-первый). `services/clients.py`
  — доменный API управления клиентами (list/card/approve/block/unblock/archive/
  restore/update_profile), frozen-структуры `ClientListItem`/`ClientPage`/
  `ClientCard`, карта переходов статусов, проверки `can_manage` + per-flag
  (`can_manage_clients`/`can_edit_clients`), аудит. `services/exceptions.py`
  (`ClientServiceError` → NotFound/PermissionDenied/TransitionForbidden/
  AlreadyInStatus). `services/notifications.py` — `Notifier`-протокол +
  `notify_new_client_registered` (владельцам+дежурным) / `notify_client_approved`,
  uk-тексты backend-owned. `repositories/user.py`: `list_by_status`
  (фильтр/поиск/пагинация) + `count_by_status`. Бриф Степану —
  `docs/phase2-stepan-brief.md`. Тесты на Postgres + mock Notifier — полный сьют
  зелёный, ruff чист.
- **Дальше:** контракт-PR в `main` первым; Степан ветвится от `main` и пишет
  bot-layer Фазы 2 по брифу. Параллельно `feat/alex-senders` — sender_profile
  backend-ready.
- **Открытые вопросы:** мусорные дубликаты « 2.py» в worktree (артефакт
  файл-синка) — почистить, в git не коммитим.

## 2026-06-17 · main · c3e3fb0
- **Сделано:** смержен **Track B / step / Phase 1 bot-auth** через PR
  [#5](https://github.com/yennned/NovaPostBot/pull/5). В `main` вошли bot-layer
  (`app/bot/`), wiring в `app/main.py`, `/start` с auth-гейтингом по
  `pending/active/blocked/archived`, dev-команды `/as`, `/as_user`,
  `/kill_switch`, role-based меню и focused-тесты bot-слоя. Перед merge ветка
  была перебазирована на актуальный `main`; отдельно закрыт баг с enum-статусами
  и расширен гейтинг контакта только на auth-state.
- **Дальше:** идти в следующий продуктовый кусок поверх Phase 1: owner/manager
  approval-flow для новых `pending`-клиентов, push-уведомления и переход dev-state
  из in-memory в постоянное хранилище.
- **Открытые вопросы:** runtime-хранилище dev-контекста и kill-switch state
  (FSM/Redis/БД) ещё не выбрано.

## 2026-06-17 · feat/step-phase1-bot-auth · e9a8e2c
- **Сделано:** старт трека **step / Phase 1 bot-auth**. Добавлен каркас
  `app/bot/` (dispatcher, middlewares, filters, states, handlers, keyboards,
  texts), реализованы `/start` + запрос контакта + создание `pending`-клиента,
  dev-команды `/as`, `/as_user`, `/kill_switch`, role-based меню и wiring в
  `app/main.py`. После мержа трека A bot-layer переведён на реальные
  `User`/`UserRole`/репозитории из data-layer; на in-memory пока оставлено только
  dev-state для impersonation/kill-switch. Покрыто focused-тестами на
  start/dev/effective context.
- **Дальше:** добрать DB-зависимые участки flow и заменить in-memory dev-state на
  постоянное хранилище (Redis/БД), когда будет согласован final runtime-контур.
- **Открытые вопросы:** где хранить dev-контекст и kill-switch state до появления
  постоянного Redis/FSM-контура.

## Фаза 1 — распределение задач

Два трека по границе «данные/правила» ↔ «бот/диалог»: **`alex` — данные + RBAC-ядро
(одна неделимая задача, одна ветка), `step` — каркас бота + auth.** Внутри трека A
порядок последовательный (permissions импортирует `enums`/`User`), поэтому это
**один worktree и один PR**, без дробления. **Трек A мержится первым** (фундамент);
трек B импортирует `enums`/`User`, поэтому ребейзится на свежий `main` после мержа
A. **Один коммиттер на ветку.** Подробности — в [CONTRIBUTING.md](CONTRIBUTING.md).

### Трек A — `alex`: данные + RBAC-ядро · `feat/alex-phase1-db`
- [x] `app/db/models/` — `enums` (роли `client<manager<owner`, статусы), `user`
      (role, status, phone, permissions JSONB), `sender_profile` (ФОП,
      `np_api_key` Fernet), `audit`.
- [x] `app/db/repositories/` — `user`, `sender_profile`, `audit`.
- [x] Alembic — начальная миграция схемы (`migrations/versions/`).
- [x] `app/sheets/client.py` — read-only скелет клиента Sheets (каркас).
- [x] `app/bot/permissions.py` — иерархия ролей, `can_manage(actor, target)`,
      per-flag `has_permission(user, flag)`, dev-allowlist проверяется первой.
- [x] bootstrap владельцев из `OWNER_TELEGRAM_IDS` (`app/services/bootstrap.py`).
- [x] `tests/` — репозитории + crypto + permissions + bootstrap (на реальном Postgres).

### Трек B — `step`: каркас бота + auth + меню + dev god-mode · `feat/step-phase1-bot-auth`
- [x] `app/bot/dispatcher.py`, `middlewares.py` (inject session/user +
      «эффективная роль/пользователь» из dev-контекста), `states.py`, `filters.py`.
- [x] `app/bot/handlers/start.py` — `/start` → `request_contact` →
      создание/поиск user, гейтинг `pending`/`active`/`blocked`.
- [x] `app/bot/handlers/dev.py` — `/as client|manager|owner`, impersonation,
      kill-switch (two-man rule, окна 1ч/3ч), audit (`dev_*`).
- [x] `app/bot/keyboards/` + `texts/` — рольовые меню (uk) для client/manager/owner.
- [x] `app/main.py` — сборка и запуск (long polling).
- [x] `tests/` — middleware/эффективная роль, логика `/start`, two-man rule.

---

## 2026-06-18 · fix/alex-phase1-hardening · d938267
- **Сделано:** хардениг по итогам ревью кода Трека A (баги, не стиль).
  (1) `get_settings()` обёрнут в `@lru_cache` — раньше конструировался новый
  `Settings()` (чтение `.env` + парс ID) на каждый вызов, а он в горячем пути
  `is_dev`/`can_manage`/`has_permission` (на каждый апдейт Telegram). В тестах кеш
  сбрасывается autouse-фикстурой `_clear_settings_cache` (`get_settings.cache_clear()`).
  (2) bootstrap-аудит: `user_id=None` (системное действие — актора нет; раньше
  новый владелец писался актором собственного создания). (3) `crypto.decrypt()`
  оборачивает `InvalidToken` в доменное `DecryptionError` — чтобы битый/ротированный
  `FERNET_KEY` не ронял загрузку ORM в Фазе 2/4. Тесты: кеш `get_settings`,
  `user_id IS NULL` в bootstrap, `DecryptionError` на битом токене. **25 passed**,
  ruff чист.
- **Дальше:** распределение Фаз 2–7 зафиксировано в
  [docs/ROADMAP.md](docs/ROADMAP.md) («Распределение задач по фазам»). Старт
  параллельных треков: Степан — Трек B (каркас бота), я — два worktree Фазы 3
  (склад/остатки и отправления/статистика).
- **Открытые вопросы:** нет.

## 2026-06-17 · feat/alex-phase1-db · a8847c3 (RBAC-часть трека A)
- **Сделано:** **RBAC-ядро Фазы 1**. `app/bot/permissions.py` (чистая логика, без
  aiogram/БД — переиспользуемо для WebApp): `role_at_least`, `can_manage`
  (строго сверху вниз; менеджеры друг другом не управляют; собой нельзя),
  `has_permission` (per-flag, по умолчанию включено, owner/dev — всё),
  dev-allowlist (`DEV_TELEGRAM_IDS`) проверяется **первым**. Bootstrap владельцев
  `app/services/bootstrap.ensure_owners` (создаёт/повышает/активирует из
  `OWNER_TELEGRAM_IDS`, пишет в `audit_logs`, идемпотентно). Тесты permissions +
  bootstrap — всего 23 passed, ruff чист. **Трек A закрыт целиком (данные + RBAC).**
- **Дальше:** PR трека A в `main`; затем `step` ребейзится и стартует трек B.
- **Открытые вопросы:** нет.

## 2026-06-17 · feat/alex-phase1-db · 0e0e98f (DB-часть трека A)
- **Сделано:** **слой данных Фазы 1**. База: `Base.metadata` с naming_convention,
  `app/db/mixins.py` (UUID PK через `uuid4`, таймстемпы), `app/utils/crypto.py`
  (Fernet поверх `FERNET_KEY`), `app/db/types.EncryptedString` (прозрачный шифр
  ключа НП). Модели: `enums` (`UserRole`/`UserStatus`/`OrgType` — `StrEnum`,
  нативные PG-enum), `User`, `SenderProfile`, `AuditLog`. Репозитории:
  `user`/`sender_profile`/`audit` (тонкий слой над `AsyncSession`,
  эксклюзивный `set_default`). Начальная Alembic-миграция (проверены
  upgrade→downgrade→upgrade и `alembic check`; явный DROP TYPE для enum в
  downgrade). Read-only скелет `app/sheets/client.py`. Тесты на **реальном
  Postgres** (`conftest` с per-test rollback) + crypto — 11 passed, ruff чист.
  CI: postgres-service; docker-compose: профиль `dev` с локальным postgres.
- **Дальше:** RBAC-часть трека A — `app/bot/permissions.py` (иерархия,
  `can_manage`, per-flag права, dev-allowlist первым), bootstrap владельцев из
  `OWNER_TELEGRAM_IDS`, тесты permissions. Затем PR трека A в `main`.
- **Открытые вопросы:** нет.

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
