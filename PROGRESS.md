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

## 2026-07-05 · feat/alex-stock-link-text · книга-зеркало «как Склад»: таблица + pivot (QA #3, Часть C)
- **Зачем:** владелец хочет, чтобы клиентская read-only книга-зеркало выглядела как
  основной «Склад» — оформленная таблица + свод «Зведення» за категорією/за товаром.
- **Ограничение:** read-only зритель не может менять селекторы → интерактивная боковая
  панель «Склада» мертва. Read-only-эквивалент: «Товари» = оформленная таблица (за
  товаром) + лист «📊 Зведення» = KPI + живой pivot по категориям (за категорією).
- **Сделано:**
  - **C1:** порядок колонок `_VIEW_HEADERS` выровнен под «Склад» (D=Кількість, E=Ціна)
    → переиспользование форматирования/формул/pivot провижна без дублей. Бот зеркало
    не читает, основной «Склад» не тронут — безопасно.
  - **C2:** `_sync_view_book` пишет **только данные** (A2:F, `batch_clear`+`update`),
    не затирая оформление/бэндинг/CF/формулу «Доступно»(G)/pivot (`values:clear` их не
    трогает). Вкладку берёт по имени `_VIEW_TAB`. Убрана преамбула Клієнт/ФОП и мёртвая
    цепочка `client_label`/`sender_name`/`default_profile`. Цена — числом (RAW). Новый
    тестируемый хелпер `_view_data_row`.
  - **C3 (провижн):** `format_view_book(gc, book, source_tab)` — оформляет книгу-зеркало
    ТОЧНО как основной «Склад» (по просьбе владельца «полностью как в основной таблице,
    без лишних листов»): один лист «Товари» с той же `style_stock_worksheet` +
    `write_available_formula` + **боковой панелью `write_side_summary`** (I–J: Всього /
    За категорією / За товаром), БЕЗ отдельного листа-сводки. Чтобы данные-зависимое
    оформление (чипы/бэндинг/панель) сразу совпало — при привязке подтягиваем остатки
    клиента из основного «Складу» (`_read_stock_rows`). Рантайм-синк далее держит данные
    свежими (A2:F, панель I–J не трогает). Лишний лист «📊 Зведення» из ранней итерации
    удаляется. Read-only: селекторы панели зритель не меняет (как и решили) — «Всього»/
    формулы живые. (Ранняя итерация с отдельным pivot-листом отброшена.)
  - Тесты: `_view_data_row` (порядок/типы), `_VIEW_HEADERS` порядок. Полный pytest + ruff зелёные.
- **Проверено вживую** на тест-книге: одна вкладка «Товари», 15 строк подтянуты из
  «Складу», Доступно считается, боковая панель «Зведення» (Позицій 15 / Одиниць 20 /
  Вартість 4576 ₴), бэндинг по данным, 5 CF (низкий остаток + чипы категорій), фильтр.
- **Дальше:** E2E клиентом → /code-review + /simplify.

---

## 2026-07-05 · feat/alex-stock-link-text · ссылка на склад в текст + синк-на-входе (QA #3 доводка)
- **Зачем:** по уточнению владельца — ссылку на персональную таблицу склада показывать
  **в тексте** сообщения (под заголовком «📦 Товари»), а не кнопкой; и зеркало должно
  быть свежим, когда клиент смотрит.
- **Сделано:**
  - `products_text(page, *, sheet_url=None)` — HTML-ссылка «Моя таблиця складу (перегляд)»
    в тексте под заголовком, когда книга-зеркало заведена. Кнопка-ссылка из
    `build_inventory_kb` убрана (и неиспользуемый параметр `sheet_url`).
  - Web-preview у трёх рендеров «Товари» отключён (`disable_web_page_preview=True`),
    чтобы ссылка не давала карточку docs.google.com.
  - **Синк-на-входе:** `_show_inventory` после отрисовки дёргает `best_effort_sync`
    (`log_key="inventory_open_sheet_sync_failed"`) — при каждом входе в «Товари»
    Google-зеркало пересинкивается под текущий «Склад». Пагинацию/поиск не синкаем.
  - Тесты: ссылка проверяется в тексте (`products_text`), а не в клавиатуре; фейк
    `FakeMessage.answer/edit_text` принимает доп. kwargs. Полный набор зелёный.
- **Блокер провижна (Часть B плана):** `scripts/provision_sheets.py --client-books`
  падает `[403] Drive storage quota exceeded` — у сервис-аккаунта нет своей квоты
  Drive, `SA.create()` невозможен. Основная «Склад» работает, т.к. её создал человек
  и расшарил на SA. Нужно: провижн от OAuth-пользователя (личный Google, 15 ГБ) или
  Shared Drive (Workspace), либо ручное создание книги + шаринг на SA. Ссылка в боте
  не показывается, пока `stock_view_book_id` не задан — штатно.
- **Решение по провижну (≤15 клиентов):** книги-зеркала владеет личный Google-аккаунт
  владельца (нативные Sheets не тратят квоту), создаются вручную по одной при
  онбординге. Добавлен хелпер `provision_sheets.py --attach-book <url|id> --for <ref>`:
  проверяет доступ SA (open_by_key + ensure «Товари»), раздаёт link-viewer, пишет
  `stock_view_book_id`. `_extract_book_id`/`_sa_email`/`_resolve_client`. SA-email для
  шаринга: `np-sheets@numeric-datum-500315-t2.iam.gserviceaccount.com`.
- **Дальше:** владелец создаёт тест-книгу + шарит на SA → `--attach-book` → E2E #3.
- **Открытые вопросы:** OAuth-as-user/Shared Drive — только если клиентов станет много.

---

## 2026-07-05 · fix/alex-reset-button · кнопка «🧹 Скинути» (QA #7)
- **Зачем:** по QA-проходу владельца кнопка «Скинути» в кабинете клиента казалась
  сломанной: рисовалась всегда, без активного фильтра сброс = no-op-редактирование
  → Telegram «message is not modified» → ошибка глотается, экран не меняется.
- **Сделано:** `build_shipments_kb` и `build_inventory_kb`
  ([app/bot/keyboards/client.py](app/bot/keyboards/client.py)) показывают «🧹 Скинути`
  только при активном фильтре (поиск; для товаров — поиск ИЛИ категория) — как уже
  сделано в менеджерской очереди. Проброшен `query` из хендлеров
  ([client_cabinet.py](app/bot/handlers/client_cabinet.py), 6 call-site). Тесты
  видимости кнопки — [tests/bot/test_client_keyboards.py](tests/bot/test_client_keyboards.py).
- **Дальше:** WS-2 (#1) — добавление менеджера по телефону/@нику.

---

## 2026-07-05 · fix/alex-support-close · закрытие чата поддержки (QA #5)
- **Зачем:** по QA «Завершити чат» не работал: клиентская кнопка лишь чистила
  стейт, тред оставался открытым и «воскресал»; «залипшая» reply-клавиатура после
  рестарта не матчилась и молчала. Плюс путаница «звернення йде власнику».
- **Сделано:**
  - Клиентское «⬅️ Завершити чат» реально **закрывает** тред (`closed`, `closed_at`)
    и уведомляет назначенного дежурного (`support_thread_closed_by_client_text`).
  - Fallback-хендлер на ту же кнопку **вне** состояния `client_chatting` — снимает
    «залипшую» клавиатуру и возвращает на головну (раньше игнорировалось).
    `StateFilter(None)`: срабатывает только при потерянном стейте, чтобы не
    перехватывать текст в состояниях менеджера/dev (иначе fallback глотал бы их).
  - Тред закрывается и коммитится **до** `state.clear()`: при сбое БД стейт не
    теряется, клиент может повторить (раньше clear шёл первым → неконсистентность).
  - Поправлен вводящий в заблуждение docstring `current_duty_managers` («менеджеры/
    владельцы» → только менеджеры; владелец дежурным быть не может) и
    `docs/10-support-duty.md` (очередь пингует менеджеров, не владельца).
- **Про «уходит владельцу»:** уже пофикшено в `main` (пинг менеджерам). Остаточное
  «нет живого релея» = никто не на дежурстве (нет менеджера / выходные). Дежурство
  вне графика для локального теста — через временное расширение `WORK_SCHEDULE` в
  `.env` (документировано); фрагильный dev-обход `OfficeClosed` не делаем — воркер
  всё равно снял бы смену на закрытом офисе.
- **Дальше:** WS-4 (#4) — полный мульти-ФОП.

---

## 2026-07-05 · feat/alex-multi-fop · мульти-ФОП: добавление + выбор в ТТН (QA #4)
- **Зачем:** по QA — при создании ТТН нет выбора ФОП (брался дефолт молча), а
  добавить свой ФОП через бота вообще было нельзя (ключ НП заводился только сидом).
- **Сделано:**
  - **Мастер «➕ Додати ФОП»** (self-service клиента): FSM
    `SenderProfileCreateState` (назва → ключ НП → контакт → телефон). Ключ
    валидируется в НП внутри `create_profile`; сообщение с ключом удаляется из чата;
    телефон нормализуется; первый ФОП — основной. Кнопка в «🏢 Мої ФОП».
  - **Выбор ФОП при создании ТТН:** если у клиента >1 профиля — шаг
    `CreateTtnState.picking_sender` (`build_sender_pick_kb`, `cb_pick_sender`);
    выбранный ФОП фиксируется на весь флоу и уходит в НП-сабмит. С одним ФОП —
    прежнее авто-поведение. Сервис: `shipment.resolve_sender_id(profile_id=…)`
    (публичный гейт по явному профилю; владение проверяется `profile.client_id ==
    client.id`, чужой UUID → отказ).
  - Тексты `no_profile_text`/`sender_profiles_text` обновлены (self-service вместо
    «зверніться до менеджера»).
  - Мастер устойчив к недоступности НП: транзиентный `NovaPoshtaError` при проверке
    ключа не роняет хендлер — черновик цел, шаг телефона держим, просим повторити
    (раньше — необработанное исключение, клиент застрявал без ответа).
  - Убраны мёртвая обёртка `resolve_default_sender_id` (без вызовов) и неиспользуемый
    текст `new_profile_cancelled_text`.
- **Отложено (не в этом PR):** смена ФОП на карточке-зведенні по кнопке (сейчас —
  рестарт через вход); орг-тип/ЄДРПОУ в мастере (дефолт ФОП); показ имени ФОП в
  summary.
- **Дальше:** WS-5 (#6) — инлайн-календарь + диапазон в статистике.

---

## 2026-07-05 · feat/alex-stats-calendar · календарь + диапазон в статистике (QA #6)
- **Зачем:** по QA — в статистике клиента дата вводилась только текстом и только
  один день; хотелось выбор мышью и диапазон «с — по».
- **Сделано:**
  - Переиспользуемый **инлайн-календарь** `app/bot/keyboards/calendar.py` (месячная
    сетка 7×N, навигация ‹мес›, без внешних зависимостей; callback `cal:*`).
  - Кнопка «📅 Обрати дату» теперь открывает календарь; **выбор одного дня** (клик +
    «Застосувати») и **диапазона** (клик начала → клик конца). Состояние начала —
    в FSM-data `stats_cal_from`.
  - Сервис: `stats._bounds`/`get_client_stats` принимают `date_from`/`date_to`
    (включительно, автосортировка дат); snapshot period=`range`.
  - **«Період» показывает включённые дни** как дата-диапазон (последний день =
    `end − 1c`): раньше печаталась эксклюзивная граница `04.07 00:00` для 01–03.07 —
    будто захвачен лишний день. После применения диапазона ни один пресет в
    клавиатуре не подсвечивается (`build_stats_kb("range")`).
  - **Убран мёртвый текстовый ввод даты** (`receive_stats_date` + стейт
    `waiting_for_stats_date` + хелпер `_edit_stats_screen`): после перехода на
    календарь в это состояние никто не переводил — путь был недостижим. Календарь
    полностью его заменяет. *(Если понадобится ручной ввод — вернём отдельной
    кнопкой на экране календаря.)*
- **Отложено:** тем же календарём заменить ввод дат в отчётах менеджера/владельца
  (ReportsState/AnalyticsState) — опционально, отдельной задачей.
- **Дальше:** WS-6 (#3) — ссылка на Google-таблицу склада клиенту.
- **Открытые вопросы:** нет.

---

## 2026-07-05 · fix/alex-add-manager · найм менеджера по телефону (QA #1)
- **Зачем:** по QA владельца менеджер не добавлялся ни по телефону, ни по @нику.
  Причина по телефону: без «+» трактовался как Telegram-ID; с «+» не совпадал с
  хранимым `380…` (без нормализации). Найм по @нику решили **не делать**: username
  в Telegram переназначаемы → нашли бы устаревшего носителя ника (confused-deputy,
  всплыло на code/security-review). Вместо этого — упор на телефон.
- **Сделано:**
  - **Телефон нормализуется в формат НП `380…` с обеих сторон:** `register_contact`
    пишет нормализованный; миграция `a1b2c3d4e5f6` чинит старые строки
    (collision-safe: при дубле не роняет upgrade); handler `staff_add_input`
    нормализует ввод (0…/380…/+380…, с пробелами/дефисами) → phone, голые цифры →
    Telegram-ID, остальное → ошибка.
  - **Найм по телефону работает даже для того, кто ещё не запускал бота:**
    `add_manager` заводит предзаготовку менеджера (`telegram_id` пуст, роль/статус
    проставлены). `users.telegram_id` → nullable (та же миграция; unique сохранён,
    NULL не конфликтуют). При первом входе по контакту `register_contact`
    подхватывает запись по номеру и проставляет `telegram_id` — менеджер активен
    сразу. Пуш-приветствие откладывается до этого момента.
  - Тексты подсказки/ошибки обновлены; удалён весь username-путь (колонка,
    `get_by_username`, last-seen в middleware/`/start`).
- **Дальше:** WS-3 (#5) — закрытие чата поддержки + дежурство для теста.
- **Открытые вопросы:** приветствие предзаготовленному менеджеру при первом входе
  можно слать из `register_contact` — отдельной мелкой задачей.

---


---


---


---

## 2026-07-05 · feat/alex-client-stock-link · ссылка на склад клиенту (QA #3)
- **Зачем:** по QA — у клиента не было ссылки на свою таблицу склада для просмотра.
- **Сделано:**
  - **Кнопка-ссылка** «📊 Відкрити таблицю складу» в экране «📦 Товари» — показывается
    только когда у клиента заведена книга-зеркало (`users.stock_view_book_id`).
    URL-хелпер `inventory.stock_view_book_url`; проброс `sheet_url` в
    `build_inventory_kb` (все три рендера).
  - **Провижн** персональных read-only книг: `scripts/provision_sheets.py
    --client-books` — создаёт по книге-зеркалу на активного клиента без
    `stock_view_book_id`, раздаёт read-only, пишет `stock_view_book_id` в БД.
    Вкладка/заголовки берутся из `client_sheet_sync` (`_VIEW_TAB`/`_VIEW_HEADERS`,
    единый источник). Наполнение строк «Товари» — рантайм-синк `_sync_view_book`.
  - **Шаринг link-only:** `share(anyone, reader, with_link=True)` —
    `allowFileDiscovery=false`, книга не индексируется поиском (доступ только по
    ссылке, которую отдаёт лишь бот). Персональная книга — чужой склад не откроется.
  - **Без книг-сирот:** `stock_view_book_id` пишется в БД сразу после
    создания+шаринга каждой книги (свой короткий сеанс); сбой на одном клиенте не
    оставляет уже созданные публичные книги без записи (повторный прогон не плодит
    дубли). Клиент, удалённый из БД к моменту записи, логируется как сирота.
- **Важно:** сам провижн (Google Drive create/share) **локально не проверялся** —
  нужен реальный запуск с Drive-write service-account (у SA скрипта scope `drive`).
  Bot-часть (кнопка) покрыта тестами и работает независимо.
- **Дальше:** все 6 задач QA закрыты локально — тест на @test_np_np_bot → push/PR.
- **Открытые вопросы:** способ шаринга (email vs link-viewer) — по умолчанию
  link-viewer, т.к. Google-почты клиента у нас нет; `book_id` фактически bearer-
  токен (утёк линк → чужой видит склад). Когда появится email клиента — шарить на
  него точечно (`--share`), а не «anyone». Уточнить при первом провижне.

---

## 2026-07-05 · feat/alex-coderabbit-cli · доки CodeRabbit CLI + autofix
- **Зачем:** закрепить локальный уровень ревью «до пуша». Кроме CodeRabbit App на
  PR добавлен **CodeRabbit CLI** (`coderabbit`/`cr`, авторизован) — ревью diff в
  терминале до PR; и skill **`autofix`** — применение предложений CodeRabbit из
  тредов PR по одному с подтверждением.
- **Доки:** в CONTRIBUTING («Среды и процесс») блок «CodeRabbit CLI» + обновлён
  итоговый поток: `checkout → тест-бот → coderabbit review → PR → App+lint-test →
  autofix → squash-merge`.
- **Дальше:** позже — Codex-ревью рядом/вместо (нужен платный ChatGPT).
- **Открытые вопросы:** нет.

---

## 2026-07-05 · feat/alex-devflow · тест/review-среда, PR-ворота, откат
- **Зачем:** выстроить профессиональный поток «обкатать ветку на отдельном
  тест-боте → PR → merge в main → прод; при проблеме — быстрый откат», не трогая
  боевого бота. Этап A (сейчас): тест-бот **локально** (ничего не платить);
  этап B (позже, когда клиенты) — always-on staging на том же VPS. План —
  `~/.claude/plans/streamed-shimmying-panda.md`.
- **Флаг среды:** `ENVIRONMENT` (local/staging/production) в `app/config.py`;
  печатается в логах старта (`bot.start`/`worker.start`) и в `/version` — чтобы
  не спутать тест с продом. На поведение кода не влияет.
- **Откат прода:** `.github/workflows/rollback.yml` (`workflow_dispatch` + тег
  образа) — по SSH подменяет `APP_IMAGE` на VPS и перекатывает. Тот же
  `concurrency: deploy-main`, что у `deploy`. Правило: откат меняет код, не схему
  БД → миграции держим backward-compatible (expand/contract).
- **Авто-ревью PR:** CodeRabbit (GitHub App, free для публичных репо), конфиг
  `.coderabbit.yaml` (ревью на русском). Сигнальный, не гейт. Активация разово на
  coderabbit.ai (установка App на репо) — секретов/workflow не требует.
- **Доки:** раздел «Среды и процесс» в CONTRIBUTING (таблица сред, зачем PR,
  expand/contract, откат, этап B), тест-бот в README/.env.example.
- **Дальше:** установить CodeRabbit App на репо (coderabbit.ai); VPS+деплой —
  когда появится сервер (rollback/deploy пока «спят», SSH-шаг скипается).
- **Открытые вопросы:** нет (оставили 2 бота: тест локально + боевой в резерве).

---
## 2026-07-04 · feat/step-simplify-services · чистка services + follow-up
- **Зачем:** `/simplify` по `app/services/` + разбор отложенных пунктов. Только
  качество (reuse/simplify/efficiency/altitude), без изменения задуманного поведения.
- **Дедупликация:** `_require_active_client` (канон в `shipments`), `compute_shipment_fee`
  (убрал дубль `reports.fee_for_units`), `_bounds` (канон в `stats`, `reports` импортит),
  `now_local` (вынес в `utils/timefmt`, из `duty`/`support`), наборы `RETURN/LOSS_STATUSES`
  (канон в `shipments`, самый нижний слой), `_staff_recipient_ids` переиспользует
  `_manager_recipient_ids`.
- **Эффективность:** блокирующие вызовы Sheets (чтение `inventory`, записи `tracking`/
  `returns`) уведены в `asyncio.to_thread`; N+1 в `list_client_returns` убран opt-in
  `joinedload(stock_movements)`; `list_queue` — 3 `COUNT` → один `GROUP BY`
  (`count_by_status_groups`); `poll_shipments` — чтения НП конкурентно через `TaskGroup`
  + семафор(8), записи последовательно.
- **Altitude:** единый `best_effort_sync` (пробрасывает `SQLAlchemyError`, глотает
  остальное) вместо 8 расходившихся try/except; `record_for_items` — единая точка
  конвенции движений склада (4 write-пути); RBAC-гейты `require_staff`/`require_can_manage`
  переехали в `bot/permissions.py`; синк Sheets — выделенный single-worker executor
  (амортизация OAuth без нагрузки на общий пул).
- **Баг-фикс `_bounds`:** окно `today/week/month` теперь `[start, конец периода)`, а не
  `[start, now]`. Прежний верхний край `now` (часы приложения) против `status_changed_at`
  (штамп Postgres `now()`) при рассинхроне часов в пару мс уводил свежую строку «в
  будущее» → она выпадала из отчёта «за сьогодні» (флейк `test_period_report`). Показ
  «Період» обрезается до `now` отдельно (`min(end, now_local)`).
- **Тесты:** новые — `_bounds` (today>now, week-понедельник, month Dec→Jan),
  `count_by_status_groups`, `record_for_items`; фикс монкипатч-таргета в `test_clients`;
  reset-фикстура общего `SheetsClient`. Полный прогон зелёный (стабильно, без гонки).
- **Ревью (CodeRabbit App на #66, 2026-07-05):** rebase на свежий `main`; 🟠 Major —
  записи склада `apply_deltas` в `returns`/`tracking` уведены с общего `asyncio.to_thread`
  на выделенный single-worker `_sheets_executor` (новый хелпер `run_on_sheets_executor`)
  — устранена гонка read-modify-write по листу клиента; 🟡 Minor — сообщения
  `PermissionDenied` в `bot/permissions.py` переведены на украинский (были видны юзеру
  через `str(exc)`).
- **Дальше:** merge в `main`.

---

## 2026-07-04 · fix/alex-cicd-buildx · фикс deploy-джоба
- **Проблема:** первый `deploy` на `main` (#53) упал: `Build and push image` — «Cache
  export is not supported for the docker driver» (я задал `cache-to: type=gha`, но
  дефолтный docker-драйвер GHA-кэш не поддерживает). SSH-деплой скипнулся корректно.
- **Фикс:** шаг `docker/setup-buildx-action@v3` перед build — поднимает buildx с
  драйвером `docker-container`, где GHA-кэш работает.

## 2026-07-04 · chore/alex-cicd · CI/CD + командная гигиена (Tier 1 + Tier 2)
- **Зачем:** закрыть два разрыва «командного» проекта — не было автодеплоя (прод
  обновлялся вручную по SSH `up -d --build`) и трассируемости версии (по образу нельзя
  сказать, какой коммит в проде). Ветка off `main`, в `main`/прод пока НЕ мержим.
- **Tier 1 — CD + версия:**
  - **Версия в образе:** `Dockerfile` `ARG GIT_SHA=dev` → `ENV APP_VERSION`;
    `config.app_version` (alias `APP_VERSION`); логи `bot.start`/`worker.start`
    получили `version=…`; dev-команда `/version` (handlers/dev.py).
  - **Образ в compose:** YAML-anchor `x-app` + `image: ${APP_IMAGE:-novapostbot:local}`
    — локально `build` собирает `:local`, на VPS `APP_IMAGE=ghcr…:latest` → `pull`.
  - **Автодеплой:** job `deploy` в `ci.yml` (push в `main`, `needs: lint-test`):
    build+push образа в **GHCR** (`:latest` + `:sha-<short>`, GHA-кэш) → деплой по SSH
    (`appleboy/ssh-action`: `compose pull && up -d --no-build`). Guard: без секрета
    `SSH_HOST` шаг деплоя скипается (первый merge не падает), образ всё равно пушится.
  - **Релизы:** `release.yml` по тегу `v*` → GitHub Release (авто-заметки) + образ с
    тегом версии; `CHANGELOG.md` (Keep a Changelog).
- **Tier 2 — GitHub-гигиена:** `.github/CODEOWNERS` (layer-split; логин step —
  плейсхолдер), PR-шаблон, issue-шаблоны (bug/feature) + config, `dependabot.yml`
  (pip + actions, weekly), `LICENSE` (Proprietary/All Rights Reserved). README —
  бейдж CI + раздел «Хостинг и деплой»; CONTRIBUTING — раздел «CI/CD и деплой» +
  чек-лист активации (секреты, GHCR-логин, CODEOWNERS, обязательное ревью).
- **Проверено:** `docker compose config` (anchor раскрывается; `APP_IMAGE`-override
  работает), `docker build --build-arg GIT_SHA=…` → `APP_VERSION` зашит в образ,
  workflow-YAML валиден, layer-check/ruff/compileall чисты, `pytest` — всё зелёное
  кроме пред-существующей флакуши `test_period_report_aggregates_by_client` (часы
  colima↔host; изолированно проходит, к CI/CD отношения не имеет).
- **Дальше (предпосылки пользователя, не автоматизируется):** задать secrets
  `SSH_HOST`/`SSH_USER`/`SSH_PRIVATE_KEY` (+опц. `DEPLOY_PATH`); на VPS `APP_IMAGE` в
  `.env` + `docker login ghcr.io` (или публичный GHCR-пакет); вписать логин step в
  CODEOWNERS; при желании — включить обязательное 1 ревью. Порядок: сначала
  секреты/логины → потом merge, иначе deploy-джоб скипнется.
- **Открытые вопросы:** GHCR-пакет приватный по умолчанию — решить, `docker login` на
  VPS или сделать пакет публичным.

## 2026-07-04 · feat/alex-sklad-summary · доведение склад-WIP + /simplify-чистка
- **Ветка:** блок B склада (панель «Зведення» + синк Резерв/Доступно) вынесен из
  `feat/alex-all-wip` в отдельную ветку off `main` (cherry-pick WIP-коммита) —
  независимо от блока A (support/menu/dates). В `main` пока НЕ мержим: сначала живая
  проверка.
- **Сделано (/simplify-чистка):**
  1) **Единый источник заголовков склада.** `provision_sheets` импортирует
     `app.sheets.client._STOCK_EXPECTED_HEADERS`; `STOCK_HEADERS = [*read, "Резерв",
     "Доступно"]`. Устранён дубль канонических колонок.
  2) **`LOW_STOCK` из настроек.** Порог для CF-правил берётся из
     `settings.low_stock_threshold` (лениво в `style_stock_worksheet`), не хардкод `3`.
  3) **Арифметика колонок → `gspread.utils.rowcol_to_a1`.** Удалён самописный
     `_col_letter` (inventory) и `chr(ord("A")+…)` (provision); новый хелпер `_col_a1`
     (0-based → буква). Заодно ушёл латентный баг за колонкой Z.
  4) **Стале-комментарии панели** (говорили F/G/H — реально H/I/J) приведены к факту;
     докстринг `write_side_summary` (J7/J13, колонки I–J).
  5) **Тест-фейк:** убраны мёртвые `update_cell`/`append_row`; добавлен `batch_update`.
  6) **Altitude:** `write_reserved` убран из протокола `StockSource` и заглушки
     `CrmStockSource` — это вьюшка поверх Sheets, вызывается напрямую из
     `client_sheet_sync`, не через seam; остаётся методом `GoogleSheetsStockSource`.
  7) **Efficiency:** `apply_deltas` батчит обновления количества в один
     `batch_update` (1 запрос вместо N `update_cell` на много-позиционной ТТН).
- **Сделано (доведение):** локаль книги «Склад» закреплена явно (`ensure_locale`,
  `updateSpreadsheetProperties locale=uk_UA` после `open_or_create`) — снимает открытый
  вопрос про `;`-разделитель (comma-decimal локаль). Пропущено сознательно: дедуп
  раскладки секций панели (снижал читаемость).
- **Тесты:** `test_sheets_read_rows` дополнен write-side — `write_available_formula`
  (G2/ARRAYFORMULA/`;`), `apply_deltas` батч (один `batch_update` на N дельт),
  `_write_stock_reserved` best-effort (зеркалит резерв; глотает `StockSheetNotFound`
  и ошибки API). Полный `pytest` на `novapostbot_test` — **392 passed** (флакуша
  `test_period_report_aggregates_by_client` в этот раз зелёная, часы синхронны);
  `ruff check`/`format` чисты. Provision `--dry-run` — OK, колонки H/I/J.
- **Проверено вживую:** провижининг прогнан на реальной книге «Склад» (те же book ID,
  новых книг не создано) — `locale=uk_UA`, `Доступно G2 = ARRAYFORMULA(IF(A2:A="";"";
  D2:D-F2:F))`, панель «📊 Зведення» на I, селектор J7=«Всі»; ботовое `read_stock` —
  15 позиций без ошибок на панельных колонках. Docker `bot`+`worker` пересобраны и
  подняты (миграции прогнаны, bot polling / worker scheduler стартовали чисто).
- **Дальше:** решение про PR блока B в `main` (пока НЕ мержим — по договорённости).
- **Открытые вопросы:** `ensure_locale` ставит `uk_UA`; если у книги нужна иная
  comma-decimal локаль — параметр функции.

## 2026-06-25 · feat/alex-support-manager-dates · pending
- **Сделано:** три UX/логических правки по обращению владельца.
  1) **Поддержка — функция менеджера, не владельца.** `support._can_handle_support`
     больше не пускает owner; `_is_staff` = только manager/dev; полный лог+поиск
     (`_is_dev`) — только dev god-mode. Owner-меню (`keyboards/menus`) без «💬 Підтримка».
     Очередь без дежурного теперь пингует **менеджеров** с правом `can_handle_support`
     (`notifications.notify_support_queued_to_managers`), а не владельца;
     `ThreadOpenResult.notify_owner` → `notify_managers`. Это и чинило «кнопку
     🟢 Я на зв'язку»: обращения шли владельцу, менеджер их не видел.
  2) **Кнопка «🧹 Скинути» в очереди отправлений.** Рендерим только при активном
     поиске (`build_queue_kb` смотрит `page.query`) — иначе сброс был no-op-
     редактированием (глушился «message is not modified», кнопка «не работала»);
     `cb_clear` даёт тост «Пошук скинуто.» и не падает при пустом поиске.
  3) **Своя дата в отчётах/аналитике (все роли).** `reports._bounds`/`period_report`/
     `financial_report`/`manager_support_stats` приняли `day`; `build_period_kb`
     получил ряд последних дней + «📅 Обрати дату»; хендлеры `reports`/`analytics`
     — `rep:day`/`an:day`/`*:pick` + ручной ввод через новый `utils/dates.parse_user_date`
     (вынесен из клиентской статистики, она тоже переведена). Заголовок отчёта при
     выборе дня показывает конкретную дату.
- **Тесты:** `test_dates`, `test_notifications` (очередь → менеджеры, не owner),
  `test_support`/`test_support_handlers` (owner без поддержки, рутинг к менеджеру),
  `test_reports` (`day`-границы), `test_reports_handlers` (`rep:day`/`an:day`),
  `test_manager_shipments_ui` (Скинути только с поиском). Локально `pytest`: 394
  passed; единственный «красный» — `test_period_report_aggregates_by_client` —
  **пред­существующий флаки** из-за рассинхрона часов colima-VM (Postgres `now()`) и
  хоста (Python `now()`): `status_changed_at` уезжает на ~10–30 мс за `end`. На чистом
  дереве (без моих правок) падает идентично; CI зелёный (там Postgres и Python на одних
  часах). `ruff check`/`ruff format --check` чисты по моим файлам.
- **Дальше:** ревью/PR в `main`; визуальная проверка в боте (см. план).
- **Открытые вопросы:** очередь без дежурного пингует всех менеджеров с правом
  поддержки — если захотят сузить до on-duty, поменять получателей.

## 2026-06-25 · feat/alex-ttn-ux-fixes · База (Склад: інтерактивне «Зведення» + синк Резерв/Доступно)
- **Сделано:** на листе **каждого** клиента в книге «Склад» — интерактивная панель
  «📊 Зведення» формулами **справа** (I/J) + синхронизация Резерв/Доступно.
  **(1) Панель** (`scripts/provision_sheets.side_summary_cells`/`write_side_summary`,
  позиции колонок параметризованы `PANEL_*`): секции *Всього* / *За категорією* / *За
  товаром*; дропдауны (Data Validation) категории (`Всі`+категории) и артикула
  (`ONE_OF_RANGE A2:A`); живые `IF/COUNTIF/SUMIF/SUMPRODUCT/VLOOKUP` (локаль с запятой
  → `;`); открытые диапазоны → авто-захват новых строк; справа → не «сползает» от
  `appendRow`, Apps Script не трогали. **(2) Резерв/Доступно синкаются в «Склад»**
  (выбор владельца — не убирать): **Доступно (G)** = `ARRAYFORMULA(=Кількість−Резерв)`
  (`write_available_formula`); **Резерв (F)** пишет бот из Postgres —
  `GoogleSheetsStockSource.write_reserved` (зеркалит `reserved_by_sku` по колонке F),
  вызывается из уже существующего `client_sheet_sync._sync_client_sheets_sync`
  (best-effort, на всех событиях ТТН). **(3) Критфикс:** панель добавляет справа
  пустые заголовки → `get_all_records` падал; добавлен `expected_headers` в
  `read_rows`, `apply_deltas` (иначе списание/возврат бота падали на листе с панелью)
  и внутренние чтения провижининга. Оформление листа растянуто на 7 колонок (A–G).
  **Данные:** добавлены 6 картриджей Xros (категория «Картриджі», 130 ₴, по 1 шт).
- **Тесты:** `tests/test_sheets_read_rows.py` — `apply_deltas`/`write_reserved` на
  фейк-листе с панельными колонками, `read_rows`/`read_stock` устойчивость, структура
  `side_summary_cells`. Полный `pytest` на `novapostbot_test` зелёный (кроме
  пред-существующей флакуши `test_period_report_aggregates_by_client`, изолированно
  проходит); ruff чист. **Проверено вживую** на книге «Склад»: F/G + ARRAYFORMULA,
  панель/фильтры, синк резерва (CHS-COLA резерв 1 → Доступно 2; сброс → 3),
  чтение бота 15 поз/20 од без ошибок.
- **Дальше:** пересобрать docker bot+worker. Опц.: засев Резерв из PG в провижининге.
- **Открытые вопросы:** разделитель `;` верен для локали книги с запятой; при
  dot-локали — `,` либо `=SUM(ARRAYFORMULA(D2:D*E2:E))`.

## 2026-06-25 · feat/alex-ttn-ux-fixes · pending
- **Сделано:** пакет UX-правок бота по фидбэку (живой тест + E2E). **(1) Навигация:**
  общий хелпер `keyboards/common.py` (`nav_footer` `[◀ Назад][⌂ Головна]`, `category_chips`,
  `home_button`/`back_button`); единый футер на всех inline-экранах всех ролей
  (clients/manager_shipments/staff/reports+analytics/support + 3 экрана-промпта ТТН,
  где были тупики); унифицированы стрелки (везде `◀`/`▶`, без `◀️`/`⬅️`). Основное меню
  ролей **возвращено на постоянную нижнюю reply-панель** (`build_role_menu` на `/start`,
  авторизации, выходе из чата поддержки) — single-window для под-экранов сохранён;
  мёртвый inline `build_home_keyboard` удалён. Работает и в dev (`/as`, через
  `effective_role`). **(2) ТТН-флоу:** в пикере товаров — чипы категорий как в «Товари»
  (все категории, не 3) + артикул убран из подписей обоих списков; экран «Параметри
  посилки» — 3 коробки (Мала≤2 / Середня≤10 / Велика≤30 кг), выбор коробки авто-ставит
  вес → «Далі» доступна с порога, «Вказати вагу» остаётся override; защита от
  отрицательного/oob индекса категории. **(3) COD:** для ФОП накладений платіж — это
  услуга «Контроль оплати» (NovaPay), а не классическая Післяплата → `to_save_props`
  переведён с `BackwardDeliveryData{Money}` на скалярный `AfterpaymentOnGoodsCost`
  (боем подтверждено create+delete у ФОП Максименко; «Послуга Післяплата недоступна»
  больше не возникает); гард «COD без суммы» (нулевая цена корзины → не ставим cod),
  понятное сообщение об отказе, удалён мёртвый `codeq`/`build_cod_amount_kb`; комиссия
  COD на карточке помечена орієнтовною (оценка через `RedeliveryCalculate`). **(4) Время:**
  SLA-дедлайн и карточка клиента показывали UTC → единый форматер `utils/timefmt.py`
  (`to_local`/`fmt_dt`, UTC→Europe/Kyiv); на него сведены manager_shipments/client_cabinet/
  duty/reports/support/clients (убран дрейф разных конвертеров).
- **Дальше:** PR в `main` (зелёный CI). Опц.: реальный pricing «Контроль оплати» вместо
  `RedeliveryCalculate`-оценки; рассмотреть консолидацию осиротевших `home:*` callback-хендлеров.
- **Открытые вопросы:** пред-существующая флакуша `test_period_report_aggregates_by_client`
  (падает в полном прогоне и на чистом `main`, изолированно проходит) — изоляция теста, не
  прод-баг; чинить отдельной задачей.

## 2026-06-25 · fix/pr49-followup · pending
- **Сделано:** follow-up по код-ревью PR #49 — закрыт **BLOCKER + 4 HIGH**.
  **(BLOCKER)** CI снова гоняет весь DB-слой: вернул `services: postgres` +
  `DATABASE_URL`/`DATABASE_URL_DIRECT`/`FERNET_KEY` и `pytest -q` вместо белого
  списка из ~23 файлов; сохранены новые шаги (`workflow_dispatch`, timeout,
  Compileall). **(HIGH-1)** Выход из чата поддержки больше не оставляет залипшую
  reply-клавиатуру: на всех 4 путях (`client_chat_exit`/`staff_reply_exit` +
  оба `thread_unavailable`) шлём `ReplyKeyboardRemove()`, затем отдельным
  сообщением inline-home (хелпер `_exit_chat_to_home` в `support.py`).
  **(HIGH-2)** `stock_sheet_key` продвигается только при подтверждённом
  переименовании вкладки: `_rename_main_worksheets` → `bool`,
  `_sync_client_sheets_sync` → `(rename_ok, book_id)`, ключ не двигаем при сбое.
  **(HIGH-3)** view-book отложен: рантайм-синк больше не зовёт `gc.create()`
  (скоуп `drive.readonly`), `_sync_view_book` возвращает `None` при пустом
  `stock_view_book_id`; создание книги вернём через provisioning. **(HIGH-4)**
  три незащищённых вызова `sync_client_sheets`
  (`sender_profile`/`clients`/`client_settings`) обёрнуты в best-effort
  try/except + `logger.warning` — сбой Sheets не валит переименование клиента.
  Новый `tests/test_client_sheet_sync.py` (3 кейса: rename-ok двигает ключ,
  rename-fail не двигает, `gc.create` не зовётся). Заодно починены 4 устаревших
  теста кабинета (`test_client_cabinet_handlers.py`), которые #49 не обновил под
  новый параметр `state` (их раньше скрывал белый список CI).
- **Консилиум (8 ревьюеров → adversarial verify → судья Opus 4.8):** вынес
  вердикт NO-GO с 1 реальным HIGH — добил его. **HIGH:** `except Exception`
  вокруг `sync_client_sheets` глотал и `SQLAlchemyError`; т.к. sync делает
  SELECT/flush на сессии вызывающего, сбой БД оставлял сессию в
  rollback-required, а следующий запрос (`_card`/`get_client_settings`) →
  `PendingRollbackError` → middleware откатывал всю транзакцию, тихо теряя уже
  сфлашенное переименование. Добавил `except SQLAlchemyError: raise` перед
  широким `except` во всех 3 коллерах + 2 регрессионных теста. **MEDIUM
  (тот же класс, что HIGH-1):** ещё 2 выхода из `staff_reply_message`
  (`_can_handle_support`/`_can_access_thread`) оставляли залипшую reply-клаву —
  провёл через `_exit_chat_to_home`; добавил тесты на `ReplyKeyboardRemove`.
  2 cleanup-сабагента: app-код чист, в тестах убран мёртвый `ShipmentCard`-стаб.
- **Дальше:** push ветки + PR в `main`. Отложенные хвосты #49 (вне этого PR):
  MEDIUM — нет «⌂ Головна» в staff-разделах (тупики навигации), COD можно
  прицепить с нулевой корзиной; LOW — мёртвый
  `build_role_menu`/`build_cod_amount_kb`/`cod_invalid()`, бэкфилл миграции vs
  рантайм на whitespace-only именах (`btrim`), чип «today» на кастомной дате,
  ротация CI `FERNET_KEY` в `secrets`, обёртка `_sync_view_book` (дремлет до
  provisioning).
- **Открытые вопросы:** локально 1 тест падает —
  `test_reports.py::test_period_report_aggregates_by_client` — из-за **+48 мс
  скоса часов** colima-VM (Postgres) против хоста (Python): `status_changed_at`
  (PG `now()`) попадает на ~33–48 мс позже строгой границы `< end` (Python
  `now()`). Доказано: при общих часах (как в GitHub CI) строка попадает в окно
  3/3 → тест зелёный. Остальные **361 passed**.

## 2026-06-25 · bot-improvements · pending
- **Сделано:** старт перевода бота в `single-window` UX: добавлен inline home-dashboard вместо рабочего упора в reply-клавиатуру; для owner убраны отдельные `Звіти` и `Я на зв'язку`, duty оставлен только manager, owner-вход остаётся через `Аналітику`; клиентский кабинет переведён на callback-entrypoints из home (`Товари`/`Відправлення`/`Статистика`/`Налаштування`), статистика получила явный `Обрати дату`, карточка клиента теперь даёт `Видалити ТТН`, а после удаления ТТН скрывается из рабочего списка; поток `Створити ТТН` начал переход на одно окно: поиск товара без листания, цены на кнопках, home/back-кнопки, COD привязан к сумме корзины, `Оголошена вартість` отвязана как отдельная страховая сумма; support переведён на inline-entrypoints из home и получил возврат в `Головна`; owner-staff UX упрощён до одной операции `Видалити менеджера` (снять роль + заблокировать + вернуть треды в очередь) вместо раздельных кнопок блокировки/снятия роли. Добавлены `users.stock_sheet_key` / `users.stock_view_book_id`, Alembic migration, best-effort sync-сервис клиентских Sheets (`app/services/client_sheet_sync.py`), хук синхронизации на смену имени клиента/ФОПа и на ключевые складские события (`create/cancel/dispatched/return`).
- **Дальше:** дотянуть single-window на оставшиеся manager/owner-разделы, дочистить ТТН-flow после всех текстовых шагов, расширить tests под новый home-dashboard и новую staff/delete-логику, отдельно прогнать DB/bot набор на тестовой БД, досинкать docs по Google Sheets/view-файлам.
- **Открытые вопросы:** полноценный e2e-прогон `pytest` в этом сеансе завис на инициализации окружения/плагинов до выполнения тест-кейсов; синтаксис (`py_compile`) и targeted `ruff` по изменённым файлам — зелёные.

## 2026-06-25 · fix/stock-edit-resilience · pending
- **Сделано:** разбор Docker-логов bot/worker → две устойчивости. **(1)** Лист
  склада клиента, названный по Telegram-имени, мог отсутствовать → сырой
  `gspread.WorksheetNotFound` валил хендлер «створити ТТН», кабінет товарів и
  воркерный `low_stock_job` трейсбеком. Заведено доменное `StockSheetNotFound`
  (`app/sheets/source.py`), трансляция `gspread → домен` на единственной границе
  Sheets (`SheetsClient.get_stock_worksheet` — покрывает и `read_rows`, и
  `apply_deltas`), а общий choke point `get_inventory_snapshot` теперь мягко
  деградирует к пустому остатку + `logger.warning("inventory.sheet_missing")`
  (manager-сводка `stock_totals` уже глотала это отдельно → None, без изменений).
  **(2)** Дабл-тап inline-кнопки спамил `TelegramBadRequest: message is not
  modified` (32×) трейсбеками. Погашено в едином `errors_router`
  (`app/bot/handlers/errors.py`): `@router.errors(ExceptionTypeFilter(
  TelegramBadRequest))` → `None` для «not modified» (обработано, без лога),
  `UNHANDLED` для прочих (пробрасываются) — один choke point вместо правок 30+
  call-site. Тесты: нет листа → пустой остаток; гасим/пробрасываем
  `TelegramBadRequest`. Прогон таргетных — зелёный, ruff чист. Ревью дифа
  (3 независимых finder-агента): корректность и aiogram/gspread-pitfalls чисто;
  поправлен устаревший комментарий регистрации `errors_router`.
- **Дальше:** PR в `main` (зелёный CI), мерж. Опц. follow-up: убрать локальный
  `_edit_or_ignore` в `clients_manage.py` (частично дублирует глобальный хендлер).
- **Открытые вопросы:** `fernet_decrypt_failed` в логах (2×) — это правка данных,
  не код: клиенту с нечитаемым ключом НП нужно перевводить ключ (ротация
  `FERNET_KEY`/сид не тем ключом), backstop уже ловит без краша.

## 2026-06-24 · fix/alex-test-env-isolation · pending
- **Сделано:** изоляция тестового окружения от локального `.env`. Autouse-фикстура
  `_clear_settings_cache` (tests/conftest.py) теперь обнуляет
  `OWNER_TELEGRAM_IDS`/`DEV_TELEGRAM_IDS` вокруг каждого теста (тем же
  save/restore, что и `FERNET_KEY`). Без этого `get_settings()` (читает `.env`)
  подмешивал реальные ID разработчика в получателей уведомлений и в проверки прав,
  и тесты с точной сверкой адресатов (`test_notify_new_client_*`,
  `test_notify_low_stock_*`) краснели только локально, при зелёном CI. Тесты,
  которым нужны конкретные id, ставят их через `monkeypatch.setenv` и перекрывают
  пустышку. Также `.gitignore` игнорирует локальный `docker-compose.override.yml`
  (персистентный dev-стек, не для прода). Полный прогон — 354 passed, ruff чист.
- **Дальше:** —
- **Открытые вопросы:** нет.

## 2026-06-24 · review-killswitch-hardening · pending
- **Сделано:** review/hardening-пакет после обновления `main`:
  - полностью удалён `kill-switch` из bot-layer, меню, текстов, типов и docs;
  - `DevService.is_dev()` переведён на реальный injected allowlist;
  - UX-фолбэк имени пользователя: в приветственных/pending-текстах больше не
    печатается `None`;
  - sender profiles: self-service закрыт для неактивных клиентов, staff-path —
    только для active staff; добавлен DB-level partial unique index «один default
    ФОП на клиента» + Alembic migration;
  - returns hardening: валидация unknown SKU / отрицательных qty / переприхода;
  - low-stock job больше не обрезает остатки UI-пагинацией;
  - reports/stats исправлены на двойную атрибуцию одной ТТН в периоде
    (`dispatched_at` отдельно от возвратов/потерь по `status_changed_at`);
  - tests hardening: безопасный guard на reset тестовой БД, понятная ошибка при
    пустом `DATABASE_URL`, тестовый fallback `FERNET_KEY`, новые регрессионные
    тесты на returns/profile access/reports/stats/jobs/dev-service.
- **Дальше:** push review-ветки, открыть PR в `main`, дождаться зелёного CI.
- **Открытые вопросы:** локально полный `pytest` упирается в отсутствие
  `DATABASE_URL`; в GitHub Actions это покрыто service-container Postgres и env.

## 2026-06-24 · feat/andrey-warehouse-phone-resilience · 0206efb
- **Сделано:** правки по multi-agent code-review (xhigh) поверх 349da9a:
  - **HTML-инъекция/крэш** в новом экране 📦 Склад: `client.full_name` (из Telegram/
    ввода клиента) шёл в сообщение `parse_mode=HTML` без экранирования — `<`/`&`
    в имени роняли весь экран ошибкой 400. → `html.escape` для имени и book_id;
    футер «Разом» не печатает 0/0, когда все листы недоступны.
  - **cache stale-fallback**: возвращаем только совпадения (было `filtered or items`
    → при отсутствии совпадений показывал ВСЕ відділення города как «найденные»).
  - **novaposhta client**: 408/429 → `NovaPoshtaUnavailable` (ретраятся + идут в
    stale-fallback); раньше падали как постоянная ошибка без ретраев.
  - **inventory.stock_summary**: параметр `reader` (тестируемость) + коммент про
    последовательное чтение (gspread-сессия не потокобезопасна, клиентов немного).
  - тесты: stock_summary через reader, no-match fallback → пусто, 429 → retried.
  - E2E-кролер: 0 мёртвых/ошибочных кнопок по всем ролям (все кнопки кликабельны).
- **Дальше:** дождаться зелёного CI на PR #45, смержить.
- **Открытые вопросы (из ревью, отложено):** (1) канонизация телефона при регистрации
  (`register_contact` хранит сырой Telegram-формат `+380…`, само-правка — `380…`;
  риск обхода `unique` при разных написаниях одного номера) — отдельной задачей,
  ломает `test_start_service`; (2) кэш «полного списка» города ограничен `limit=50`
  в `get_warehouses` → для крупных городов stale-fallback видит лишь первые 50
  (best-effort, теперь честно отдаёт пусто вне набора).

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
  role-based меню и focused-тесты bot-слоя. Перед merge ветка
  была перебазирована на актуальный `main`; отдельно закрыт баг с enum-статусами
  и расширен гейтинг контакта только на auth-state.
- **Дальше:** идти в следующий продуктовый кусок поверх Phase 1: owner/manager
  approval-flow для новых `pending`-клиентов, push-уведомления и переход dev-state
  из in-memory в постоянное хранилище.
- **Открытые вопросы:** runtime-хранилище dev-контекста (FSM/Redis/БД) ещё не
  выбрано.

## 2026-06-17 · feat/step-phase1-bot-auth · e9a8e2c
- **Сделано:** старт трека **step / Phase 1 bot-auth**. Добавлен каркас
  `app/bot/` (dispatcher, middlewares, filters, states, handlers, keyboards,
  texts), реализованы `/start` + запрос контакта + создание `pending`-клиента,
  dev-команды `/as`, `/as_user`, role-based меню и wiring в
  `app/main.py`. После мержа трека A bot-layer переведён на реальные
  `User`/`UserRole`/репозитории из data-layer; на in-memory пока оставлено только
  dev-state для impersonation. Покрыто focused-тестами на
  start/dev/effective context.
- **Дальше:** добрать DB-зависимые участки flow и заменить in-memory dev-state на
  постоянное хранилище (Redis/БД), когда будет согласован final runtime-контур.
- **Открытые вопросы:** где хранить dev-контекст до появления постоянного
  Redis/FSM-контура.

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
      audit (`dev_*`).
- [x] `app/bot/keyboards/` + `texts/` — рольовые меню (uk) для client/manager/owner.
- [x] `app/main.py` — сборка и запуск (long polling).
- [x] `tests/` — middleware/эффективная роль, логика `/start`.

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
