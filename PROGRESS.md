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
