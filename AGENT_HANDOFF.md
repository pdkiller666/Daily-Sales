# AGENT HANDOFF — Daily Sales Telegram Bot
> Последнее обновление: 2026-06-18 (сессия 833)
> Файл находится в корне проекта: `AGENT_HANDOFF.md` — пушится на GitHub, не деплоится на Amvera, не попадает в .local.
> Документ для агента, принимающего разработку. Содержит всё необходимое для немедленного продолжения работы.

---

## 0. БЫСТРЫЙ СТАРТ

```
Бот: @BotCraftAi_Test_3_bot  (тестовый, Replit)
Продакшн (Amvera): https://dailysalesdeploy-pdkiller666.amvera.io/
GitHub: https://github.com/pdkiller666/Daily-Sales.git
Amvera git: https://git.msk0.amvera.ru/pdkiller666/dailysalesdeploy
Super-admin Telegram ID: 921098636
Workflow: "Start application" → python start.py (→ убивает порт 5000, запускает main.py)

Деплой (по умолчанию — GitHub + Amvera напрямую):
  bash deploy.sh "Сообщение коммита"

Только GitHub (без Amvera):
  bash deploy.sh "Сообщение" --no-amvera
```

**Секреты в Replit Secrets (никогда не хардкодить):**
- `BOT_TOKEN` — токен тестового бота
- `GITHUB_TOKEN` — токен для push на GitHub
- `ADMIN_CHAT_ID` — ID супер-администратора
- `YANDEX_EMAIL` — адрес Яндекс Почты для SMTP (email-auth)
- `YANDEX_SMTP_PASSWORD` — **пароль приложения** Яндекс (16 симв.), НЕ пароль аккаунта

**Последний деплой:** GitHub `942fcc2` · Amvera `d433bcc` (2026-06-18, сессия 833). Оба хэша верифицированы через `git ls-remote`.

**Новые секреты (Web Push VAPID):**
- `VAPID_PUBLIC_KEY` — публичный VAPID-ключ (base64url, генерируется один раз)
- `VAPID_PRIVATE_KEY` — приватный VAPID-ключ
- `VAPID_MAILTO` — контактный email для VAPID заявок (`mailto:admin@example.com`)

**Сессия 785–786 (2026-06-16) — AI-сессии: явный сброс, авто-сжатие, авто-архивация:**

- **is_session_break / is_ai_summary**: новые bool-колонки в `direct_messages` и `chat_messages` (org_*.db); миграция `ALTER TABLE … ADD COLUMN IF NOT EXISTS` в `create_tables()`
- **14 новых DB-методов**: `add_dm_session_break`, `add_chat_session_break`, `get_dm_session_messages`, `get_chat_session_messages`, `get_last_dm_ai_activity`, `get_last_chat_ai_activity`, плюс методы для summary/поиска/архивации
- **chat.py**: `_fetch_ai_dm_history()` / `_fetch_ai_chat_history()` — считывают контекст ТОЛЬКО с последнего разрыва; `_maybe_compress_dm_session()` / `_maybe_compress_chat_session()` — авто-сжатие (>12 msg → summary + 6 свежих); новые роуты `POST /chat/ai/reset` (DM) и `POST /chat/topic/ai/reset` (топик), оба с CSRF
- **UI (chat/index.html)**: кнопка «🔄 Новый диалог» (только AI-контексты); кнопка вложений скрыта в AI-тредах (`x-show`); AI thinking indicator «🤔 ИИ думает…»
- **APScheduler**: +2 задачи → `prune_ai_tool_stats` (03:15) и `auto_archive_ai_sessions` (03:20); последняя обходит все org_*.db и вставляет разрывы для неактивных >30 дней AI-сессий
- **Константы**: `_SESSION_COMPRESS_AT=12`, `_SESSION_KEEP_FRESH=6` в chat.py
- GitHub `ba92ad8` · Amvera `0e7058a`. Приложение запущено, все 12 APScheduler-задач зарегистрированы.

**Сессии 711–714 (2026-06-15) — штрихкоды, мобильные ценники, камера TWA:**

- **Сессия 711 (фикс камеры)**: `Permissions-Policy: camera=()` в `SecurityHeadersMiddleware` (`web/app.py` строки 122-124) молча блокировал `getUserMedia` во всех контекстах (regular Chrome, installed PWA, TWA) без промпта и без записи разрешений. Исправлено на `camera=(self)`. GitHub `2ac9f72` · Amvera `3fea13e`. Урок: всегда проверять `Permissions-Policy` ПЕРВЫМ когда камера не запрашивается.

- **Сессия 712 (Task #53 — мобильный конструктор ценников)**: `web/templates/products/label.html` — A4-лист (210mm/~794px) обёрнут в `<div class="sheet-scroll-wrap">` с `overflow-x:auto`; кнопка «↔ Вписать» (zoom-to-fit, Alpine `toggleZoom()`, scale factor = `(vw-32)/794`, только ≤640px); тулбар — `min-height:44px` на всех кнопках, кнопка «🖨 Печать» скрыта на мобильном; дизайн-панель — `width:100%` полях, крупные цветовые пикеры. GitHub `f612210` · Amvera `dd365bd`.

- **Сессия 713 (Tasks #54+#57 — auto-fit + zoom стабильность)**: auto-fit при открытии на телефоне (`init()` проверяет `window.innerWidth<=640` → `$nextTick(() => this.toggleZoom())`); zoom сохраняется при переключении размера ценника (`$watch('size', ...)` → `$nextTick(() => this._applyZoom())`); рефактор `_applyZoom()` как отдельный метод. GitHub `a63ec12` · Amvera `dec00e9`.

- **Сессия 714 (Tasks #56+#58+#59 — barcode/article + zoom on rotation + pinch-zoom fix)**:
  - **#56 — разделение article и barcode**: новая колонка `barcode TEXT` в `products` (org_*.db) с уникальным частичным индексом `idx_products_barcode`; `get_product_by_barcode()` + `add_product(barcode=)` + `update_product(barcode=)`; бот — шаг «Штрихкод» при создании товара (FSM `waiting_for_barcode`) с кнопками «📸 Сканировать» / «⏭ Пропустить», редактирование штрихкода отдельным пунктом; `process_barcode_photo` в `sales_handlers.py` ищет сначала по `get_product_by_barcode`, потом fallback на `get_product_by_article`; поиск товаров в боте и веб охватывает `barcode`(индекс 8) и `article`(индекс 7); веб-форма (`form.html`, `detail.html`, `index.html`) — поле barcode + кнопка-сканер.
  - **#58** — zoom-to-fit перезапускается при повороте экрана (debounced `resize` listener).
  - **#59** — `_applyZoom()` использует `document.documentElement.clientWidth` (layout viewport, не зависит от pinch-zoom); VisualViewport API подавляет refit во время активного пинч-зума, при повороте — всегда refits.
  - GitHub `c8b5e8b` · Amvera `7e7b8d3`.

**Сессия 676 (2026-06-14) — ФИНАЛЬНЫЙ ФИК: bubblewrap заменён на прямой Gradle build:**

**Проблема:** bubblewrap CLI стабильно падал в GitHub Actions с `cli ERROR The provided androidSdk isn't correct` — 36 сборок, 25+ различных попыток. Ни симлинк `cmdline-tools/latest`, ни `android-actions/setup-android@v3`, ни ручная конфигурация SDK не помогали. Bubblewrap имеет жёсткую внутреннюю валидацию пути, которая не проходит ни в одной из конфигураций CI.

**Решение:** Полный отказ от bubblewrap. Создан нативный Android Gradle проект `android/` с прямой сборкой APK:
- `android/settings.gradle`, `android/build.gradle` — корневой проект
- `android/gradle.properties` — `android.useAndroidX=true` + `android.enableJetifier=true` (ОБЯЗАТЕЛЬНО — без них `checkReleaseAarMetadata FAILED`)
- `android/app/build.gradle` — AGP 8.2.2, `com.google.androidbrowserhelper:androidbrowserhelper:2.5.0`, signing через env vars `KEYSTORE_PATH`/`KEYSTORE_PASSWORD`
- `android/app/src/main/AndroidManifest.xml` — `LauncherActivity` + `DelegationService` для TWA
- `.github/workflows/build-twa.yml` — `gradle/actions/setup-gradle@v3` (НЕ `gradle/setup-gradle` — не существует) + `gradle :app:assembleRelease --project-dir android`

**⚠️ КРИТИЧНО для будущих агентов**: НЕ возвращаться к bubblewrap. Использовать только прямой Gradle build.

**Дополнительно (ранние сессии 657/660/661 — создание инфраструктуры):**
- `GET /download/android` (`web/app.py`) — 302 redirect на `https://github.com/pdkiller666/Daily-Sales/releases/latest`
- `POST /webhook/apk-release` — получает событие GitHub Release, скачивает APK локально (`data/apk/`), рассылает уведомление всем `owner_id` орг
- `GET /admin/apk` — суперадмин страница: статистика скачиваний (Chart.js), история релизов, источники
- Точки входа: лендинг (3-я CTA), дашборд (баннер + localStorage dismiss), настройки (карточка), бот (callback `apk_info` → ссылка)
- `twa-manifest.json` — конфиг TWA: `packageId=com.dailysales.app`, fingerprint SHA-256 `25:E3:EB:BB:2B:72:C7:12:CB:15:59:AD:1C:E9:6B:20:8A:4E:EB:19:97:B9:93:8F:37:47:31:96:82:BB:A1:1D`
- `deploy.sh` — `AMVERA_ONLY_EXCLUDE_DIRS = {'.github'}`: `.github/` идёт только на GitHub, `android/` — на оба
- **GitHub Secrets**: `KEYSTORE_BASE64` (base64 PKCS12, alias `dailysales`), `KEYSTORE_PASSWORD`, `APK_WEBHOOK_SECRET`

**Фиксы:**
- `start.py` — `_free_port(5000)`: автоматически убивает процесс на порту 5000 через `fuser -k` перед стартом (устраняет `[Errno 98] address already in use`)
- `main.py` — `check_scheduled_notifications()`: 1) naive datetime → UTC-aware (`.replace(tzinfo=pytz.UTC)`); 2) `json.loads` при двойном-encode возвращал строку вместо dict → добавлен `isinstance(_parsed, dict)`
- `web/templates/reports/index.html` — фильтры периода/категории/продавца сохраняются при любом изменении: смена периода, магазина, группировки, кастомных дат (5 мест исправлено, ранее фильтры сбрасывались)

**Сессия 640 (2026-06-11) — Улучшения из глубокого код-ревью (волны A/B/C, тест+деплой):**
- **Волна A (деплой GitHub `2e3e3a4` / Amvera `f66337d`):**
  - **A1 money-баг + идемпотентность аддонов**: `create_subscription_addon` писал в несуществующую колонку (`amount_paid`) → теперь `price`. Привязка к `payment_request_id`: partial UNIQUE index (try/except-guarded) + перехват IntegrityError (гонка) → re-SELECT. Ветка `addon_*` в `confirm_payment_request` проверяет `result==0` и компенсирует заявку (approved→pending), не оставляя оплату без выдачи.
  - **A2 логирование**: 42 немых `except: pass` в `database.py` → `logger.debug` (без смены control flow).
- **Волна B (деплой GitHub `2409f49` / Amvera `3ef5bb7`):**
  - **B3 безопасный restore**: `backup_manager.restore_backup` через SQLite Online Backup API (`src.backup(dest)`) + ограниченный retry (5×, добавлен `import time`) вместо `shutil.copy2` поверх открытых соединений.
  - **B4 event-loop offload**: admin `/orgs/{id}/delete` и `/backups/create` `async`→`def` (threadpool); цикл записи продаж в `pos_checkout` вынесен в `_write_sales()` через `await anyio.to_thread.run_sync`. Соединения thread-safe (`check_same_thread=False` + threading.local pool).
- **Волна C — безопасное подмножество (деплой GitHub `53a6008` / Amvera `f840e53`):**
  - **#9 jose→PyJWT**: установлен `PyJWT==2.13.0`; `web/auth.py` `import jwt`, `except jwt.PyJWTError`; проверена обратная совместимость токенов (живые сессии не инвалидируются). Дедуп `requirements.txt` (был полностью дублирован), удалён `python-jose`.
  - **#6 rate-limit**: `check_rate_limit` (5/600s по tg_id) на `POST /settings/email-change-password` (verify старого пароля → защита от брутфорса).
  - **#10 тесты**: +3 функциональных теста в `test_imports.py` (PyJWT round-trip + reject-invalid, `create_web_app()` build = 254 роута, rate_store limit) — всего 9, все зелёные.
- **Пропущено по решению пользователя**: #5 (лимиты/пагинация экспортов — менял поведение выгрузок), #7 (Depends-авторизация, ~91 роут), #8 (Pydantic-валидация) — широкий рискованный рефактор живого биллинга с косметической пользой.
- **Проверки**: `test_imports.py` 9 OK; рестарт воркфлоу чистый; smoke `login`=200, `/dashboard` (no-auth)=302. Architect review: PASS по всем трём волнам.

**Сессия 629 (2026-06-10) — G1–G5: консистентность модульного биллинга в боте:**
- **G2**: `subscription_menu` — список модулей теперь динамический (`get_all_billing_modules()`); статус каждого через `has_module()`; fallback на legacy-флаги при ошибке
- **G1**: Самостоятельная покупка модулей/пакетов в боте — кнопка «🧩 Подключить модули» в меню подписки; `buy_modules` — список с ценами и статусами; `start_module_purchase` — СБП (скриншот) или ЮKassa. `plan_type = module_<key> / bundle_<key>`; выдача через уже существующую `confirm_payment_request → grant_billing_item(30d)`. Зарегистрировано в `subscription_router.py`
- **G4**: Квитанция в `payment_admin_handlers.py` теперь показывает красивое название модуля/пакета вместо сырого ключа
- **G3**: `payment_system_admin.py` — кнопка «🧩 Модули и пакеты» в платёжной системе → `billing_modules_admin` (обзор: активные подписки, кол-во клиентов, выручка); `grant_billing` / `process_grant_billing` (state `PaymentSystemStates.waiting_grant_billing`) — ручная выдача модуля/пакета/расширения через `grant_billing_item`
- **G5**: `payment_statistics_menu` — блок статистики по модулям из `get_billing_stats()`; `payment_charts` переписан с заглушки на текстовую разбивку выручки по модулям
- **Фиксы (architect review)**: `from utils import he` на уровне модуля в `subscription_handlers.py` (было NameError); `he()` на всех названиях модулей из БД; tenant→shop_bot fallback для ЮKassa-ветки + hard guard при `user_id is None`; 64-байтный guard для callback_data кнопок `buymod_`/`buybnd_`
- **Тесты**: test_imports 5 OK · test_callbacks 0 проблем · test_scenarios 708/708; бот стартует чисто

**Сессия 612 (2026-06-10) — Аудит + фикс супер-кабинета (`/admin/*`):**
- **🔴 Сломанный сброс подписки исправлен**: кнопка «Сбросить» на `/admin/subs` постила в `POST /admin/subs/{id}/cancel`, который был мёртвой заглушкой (`return RedirectResponse('/admin/billing/grants')` — ничего не делал). Теперь `admin_cancel_sub` реально сбрасывает legacy-подписку: `DELETE FROM subscriptions WHERE user_id=? AND datetime(end_date)>datetime('now')` в shop_bot.db (raw conn) + `_guard(super_admin)` + `verify_csrf_token`; flash `?msg=cancelled|error`. Удаление активной строки → `get_user_subscription`/limits отдают дефолт «Бесплатный»
- **Удалён мёртвый код**: `POST /admin/subs/grant` (заглушка, ни одним шаблоном не использовалась — выдача доступов переехала на `/admin/billing/grants`)
- **🎨 Единый дизайн всех подстраниц**: создан `web/templates/admin/_macros.html` → макрос `page_header(icon, title, subtitle, back_url, back_label)` (тёмный hero-баннер как у хабов; поддержка `{% call %}` для кнопки справа через `{% if caller %}`). Применён к 10 подстраницам (orgs, subs, tariffs, stats, users, payment_settings, backups + billing/modules,grants,bundles) — раньше у них был простой `<h1>` без баннера и разнобой в кнопках «Назад»
- **Импорт макроса — ВНУТРИ `{% block content %}`** (`{% from "admin/_macros.html" import page_header %}`), не на верхнем уровне — иначе extends-дочерний шаблон его не подхватит
- **Нормализована dark-mode подсветка хлебных крошек** (`dark:hover:text-slate-300`) на 7 admin-страницах, где её не было
- **Проверки**: `test_imports.py` 0 ошибок; все 13 admin-шаблонов компилируются в Jinja2; макрос протестирован в обоих режимах (plain + `{% call %}`); code-review (architect) → PASS, severe issues нет

**Сессия 529 (2026-06-06) — Фикс: DM-поиск + FAB badge DM + аудит Web Push:**
- **DM search**: `search_dm_messages(query, user_id, limit)` в `database.py` — поиск по `direct_messages` (15 колонок: id, from_uid, peer_id, msg, файлы, created_at, from_name, peer_name); `_fmt_search_result_dm(row, my_db_id)` formatter в `chat.py`
- **Глобальный поиск**: `chat_search` route при `topic_id=0` теперь объединяет topic + DM результаты, сортирует по дате, отдаёт до 30; DM-результаты помечаются фиолетовым «💬 Личное»
- **goToResult DM**: при `result_type='dm'` переключает в DM-режим (`switchToDm()`), ждёт загрузки контактов, открывает `openDmConversation(contact)`
- **FAB badge**: разделён на `topicNew` + `dmUnread`; `fetchDmUnread()` — `/api/unread-count` поле `dms` при загрузке + каждые 60s; `_maybeClearChatBadge()` сбрасывает оба при `pathname.startsWith('/chat')`
- **sw.js null-guard**: `pushsubscriptionchange` делает early return если `!e.oldSubscription`
- **Push subscribe valидация**: endpoint ≤2048 символов и обязан начинаться с `https://`; p256dh ≤256; auth ≤128

**Сессия 528 (2026-06-06) — Web Push VAPID + browser push notifications:**
- **`push_subscriptions`** table в `shop_bot.db`: `telegram_id, endpoint, p256dh, auth, created_at` — UNIQUE(telegram_id, endpoint)
- **4 DB-метода**: `save_push_subscription`, `get_push_subscriptions`, `delete_push_subscription`, `delete_all_push_subscriptions`
- **`web/push_utils.py`**: `send_web_push(subscription, payload)` — pywebpush 2.3.0, VAPID-подпись; graceful при KeyError/ConnectionError; `_VAPID_PRIVATE` / `_VAPID_CLAIMS` инициализируются из env на импорте
- **3 API маршрута**: `GET /api/push/vapid-public-key`, `POST /api/push/subscribe`, `POST /api/push/unsubscribe`
- **SW v4**: `push` event → `showNotification()` + `navigator.setAppBadge(count)`; `notificationclick` → `clients.openWindow(data.url)`
- **Интеграция**: `send_web_push()` вызывается из `pos.py` (продажа), `tasks.py` (уведомления), `chat.py` (новое сообщение в теме)

**Сессия 527 (2026-06-06) — App Badge API + unread-count endpoint:**
- **`/api/my-notifications`** теперь возвращает поле `dms` (кол-во непрочитанных DM)
- **`/api/unread-count`** новый эндпоинт → `{ok, notifications, dms, total}` — используется FAB badge и SW
- **`_setAppBadge(count)`** в `base.html` — `navigator.setAppBadge(count)` / `clearAppBadge()` (с try/catch)
- **`window.dsRefreshBadge()`** — публичная функция для принудительного обновления badge из любого модуля
- **`visibilitychange`** listener: при возврате на вкладку — `_setAppBadge(0)` + refresh

**Сессии 520-526 (2026-06-06) — Direct Messages (DM) в корпоративном чате:**
- **`direct_messages`** table в org_*.db: `id, from_user_id, to_user_id, message, file_path, file_name, file_type, file_size, created_at, is_read, is_deleted`
- **DM DB-методы (9)**: `add_dm`, `get_dm_conversation`, `get_dm_contacts`, `get_dm_org_members`, `mark_dm_read`, `get_dm_unread_count`, `get_dm_message`, `soft_delete_dm`, `search_dm_messages`
- **DM файлы**: `add_dm_files`, `get_dm_files_bulk`, `get_dm_file`, `delete_dm_files` (аналог chat_message_files)
- **DM маршруты в chat.py** (9): `GET /chat/dm`, `GET /chat/dm/{peer_id}`, `GET /api/dm/contacts`, `GET /api/dm/members`, `GET /api/dm/conversation/{peer_id}`, `POST /chat/dm/send`, `POST /chat/dm/{msg_id}/delete`, `GET /chat/dm/file/{msg_id}`, `GET /chat/dm/file/attachment/{att_id}`
- **WebSocket**: `WS /ws/chat/dm` — broadcast read-receipts + real-time новые сообщения между участниками
- **Alpine.js DM**: `dmMode` toggle, `switchToDm()`/`switchToGroup()`, `openDmConversation(contact)`, `dmContacts[]` (с `unread`), `dmTotalUnread`, `dmMessages[]`, WS-канал `dmWs`

**Сессия 519 (2026-06-06) — Traceback-диагностика Excel-экспортов + актуализация MD:**
- **Traceback logging**: добавлен `exc_info=True` в `logging.error()` всех 4 Excel-экспортов (`web/routes/inventory.py`, `reports.py`, `rankings.py`, `salary.py`) — теперь полный стектрейс в логах Amvera при любой ошибке; поможет диагностировать ошибку экспорта остатков в продакшне (локально работает корректно — tenant БД только на Amvera)
- **Email-auth верифицирована**: вся email+пароль аутентификация уже реализована (сессия 469) — `email_auth.py` 9 маршрутов, `email_utils.py`, шаблоны, `/setweblogin`, секреты `YANDEX_EMAIL`+`YANDEX_SMTP_PASSWORD` настроены; новой работы не потребовалось
- **MD-файлы актуализированы**: PROJECT_MAP.md (`f29b527→13e53dc`), README.md (`48→51 модуль`), UI_MAP.md (v5), COMPETITIVE_ANALYSIS.md (метрики: 321+ методов DB, 200+ web-маршрутов, 756+ callback-handlers, 58 911 строк Python)
- **51/51 test_imports ✅ · 702 test_scenarios ✅**

**Веб-интерфейс:** `http://localhost:5000` (порт 5000, работает параллельно с ботом). Аутентификация через Telegram Login Widget **или email+пароль** (регистрация по инвайт-коду). Доступен всем ролям: продажи, инвентарь — сотрудникам; управление командой и зарплатой — owner/admin.

**Сессия 469 (2026-06-05) — Email + пароль аутентификация:**
- **DB**: `web_credentials` в `shop_bot.db`; 13 методов (`create_web_credential`, `get_web_credential_by_email/token/tg`, `set_email_verified`, `set_verify_token`, `set_reset_token`, `update_web_credential_telegram`, `set_web_credential_link_code`, `get_web_credential_by_link_code`, `update_web_credential_last_login`, `update_web_credential_email`, `update_web_credential_password`, `delete_web_credential_telegram`); `synthetic_tg_id = -(10_000_000 + cred_id)` для email-only юзеров
- **web/email_utils.py**: `send_verification_email()`, `send_reset_email()`, `send_link_notification()`; SMTP `smtp.yandex.ru:465` SSL; `is_configured()` guard — маршруты деградируют до 503 без SMTP
- **web/auth.py**: `hash_password()` / `verify_password()` (PBKDF2-SHA256, 390k итераций, stdlib)
- **web/routes/email_auth.py**: 9 маршрутов: `GET/POST /register`, `GET/POST /auth/email`, `GET /auth/verify`, `POST /auth/resend-verify`, `GET/POST /auth/reset`, `GET/POST /auth/reset/confirm`, `POST /settings/email-change`, `POST /settings/password-change`, `POST /settings/email-unlink`; rate limit 5 req/10min/IP
- **web_auth_handlers.py**: `/setweblogin` команда → 6-символьный код (TTL 10 мин) для привязки email к Telegram
- **Шаблоны**: `auth/login.html` (Telegram + email табы), `auth/register.html`, `auth/reset_request.html`, `auth/reset_confirm.html`, `auth/verify_sent.html`; email-раздел в `settings/index.html`
- **Деплой**: GitHub `f29b527` · Amvera `5e0d528` · хэши верифицированы

**Сессия 459 (2026-06-05) — Веб: динамический поиск в чате + Google Sheets статус инвентаря:**
- **Поиск в чате (B+C)**: `GET /chat/search?q=&topic_id=` — новый эндпоинт в `web/routes/chat.py`; `topic_id=0` → глобальный поиск по всем темам (до 30), `topic_id>0` → по одной теме (до 25); rate limit 30 req/min/IP; `_fmt_search_result` — расширение `_fmt_msg` с полями `result_topic_id`/`result_topic_name`; `_SEARCH_RATE_STORE` — in-memory per IP
- **`search_chat_messages(query, topic_id, limit)`** в `database.py`: SQLite `LOWER(m.message) LIKE LOWER(?)` — регистронезависимо включая кириллицу; 13 колонок (базовые + `t.id`, `t.name`); topic_id=None → по всем не-архивным темам через LEFT JOIN chat_topics
- **UI поиска в чате**: кнопка 🔍 в хедере чата; Ctrl+F/Cmd+F — открытие из клавиатуры; Alpine.js: `searchOpen`, `searchQ`, `searchScope`, `searchResults`, дебаунс 300 мс; переключатель «В теме / 🌐 Везде»; результаты с: инициалом, именем, временем, бейджем темы (global), highlighted сниппетом (`<mark>`); клик → `goToResult(r)`: `switchTopic()` если нужно + скролл + CSS-флеш `.search-flash` 1.6 сек; `#search-panel` — `position:absolute; inset:0; z-index:20` над областью сообщений; Escape закрывает
- **GSheets статус инвентаря**: `POST /inventory/adjust` теперь ждёт результата через `trigger_export_with_result` (timeout 8 с) и возвращает `gs_status: "ok"|"error"` в JSON; toast-уведомление в `inventory/index.html` (зелёный/красный, 4 с); показывается только когда интеграция настроена
- **robots.txt**: добавлен `Disallow: /chat/search`
- **Деплой**: GitHub `c4f7aa5` · Amvera `599f699` · сессия 459

**Сессия 329 (2026-06-02) — Веб: PWA, свайп-жесты, WAL, браузерные уведомления:**
- **PWA**: `web/static/manifest.json` (shortcuts: /pos, /dashboard, /sales), `web/static/icon.svg`, `web/static/icon-maskable.svg`, `web/static/sw.js` (Cache-first для `/static/`, network-only для auth-роутов, push-handler для будущего VAPID). Мета-теги в `base.html`: `<link rel="manifest">`, `theme-color`, `apple-mobile-web-app-*`.
- **Service Worker маршрут**: `GET /sw.js` в `web/app.py` → `FileResponse(web/static/sw.js)` с заголовком `Service-Worker-Allowed: /` + `Cache-Control: no-cache` — SW регистрируется на `/sw.js` со scope `/` из `base.html`.
- **Гамбургер убран на мобильном**: кнопка `lg:hidden` из топбара полностью удалена — нижний нав дублирует навигацию.
- **Свайп-жесты** (IIFE в `base.html`): MIN_DIST=70px, MAX_VERT=45px; пропускает горизонтально-скроллируемые элементы и input; (1) ищет `a[href*="period="]` с классом `bg-blue-600`/`border-blue-600` → клик по пред/след; (2) то же для `a[href*="tab="]`; (3) свайп вправо от левого края (startX<40px) → `history.back()`.
- **SQLite WAL**: `_enable_wal(db)` в `web/deps.py` — `PRAGMA journal_mode=WAL` + `PRAGMA synchronous=NORMAL` на каждый `get_web_db()` возврат; снижает блокировки при параллельных запросах бота и веба.
- **Браузерные уведомления**: промпт `#ds-notif-prompt` в шторке «Ещё» (показывается через 800 мс, если `Notification.permission==='default'`); `dsRequestNotif()` → `requestPermission()` → `dsStartNotifPolling()`; polling каждые 60 сек через `/api/sales-feed?since=ISO`; уведомление создаётся только когда `document.hidden`; чекпоинт хранится в `localStorage.ds_notif_since`.
- **API эндпоинт**: `web/routes/api.py` → `GET /api/sales-feed?since=ISO` — возвращает продажи других сотрудников (`u.telegram_id != current`) с момента `since`, max 10; поля: id, product, total, created_at, seller, shop. Требует авторизации (проверка `get_session_user`).
- **Аудит**: `get_connection()` существует в `database.py:112` ✅; `logo.jpg` в `web/static/` ✅; все изменения синтаксически чисты; приложение запустилось без ошибок.

**Сессия 331 (2026-06-02) — Веб: тёмная тема + горячие клавиши:**
- **Тёмная тема**: ранний скрипт в `<head>` (`localStorage.ds_dark`) применяет `.dark` до отрисовки (no flash); `tailwind.config = { darkMode: 'class' }`; 80+ CSS-переопределений в `<style>` (`html.dark .bg-white → #1e293b` и т.д.); охвачены: sidebar, topbar, bottom nav, шторка «Ещё», таблицы, inputs, badges, shadow; кнопка 🌙/☀️ в топбаре (`#ds-dark-icon`); CSS-переключатель в шторке `#ds-dark-toggle` (`.ds-dark-toggle` + `.ds-dark-knob` управляются через CSS `html.dark`); `dsToggleDark()` → `_dsApplyDark(isDark)` обновляет `<html class>`, иконку и `theme-color` мета.
- **Горячие клавиши** (IIFE в `base.html`): `Alt+D` — тёмная тема; `Alt+N` — кнопка `[data-shortcut="new"]` / `.ds-primary-btn`; `/` — фокус на поиске; `Escape` — закрытие шторки (custom event `ds-escape`); `?` — всплывающая подсказка 3.5 сек (тёмная карточка, список клавиш); все клавиши кроме Alt+D/Alt+N отключены если курсор в input.

**Сессия 332 (2026-06-02) — Веб: Chart.js диаграмма рейтингов:**
- **Диаграмма в рейтингах**: карточка с горизонтальным бар-чартом (Chart.js, `indexAxis:'y'`) — топ-10 по выручке; цвета: 🥇 amber-400, 🥈 slate-400, 🥉 orange-400, остальные blue-400; тултип показывает `N продаж · N шт.`; X-ось с форматированием (К/М); `MutationObserver` на `<html class>` синхронизирует цвета при переключении тёмной темы (`chart.update('none')`); коллапс-кнопка (на десктопе открыта по умолчанию, на мобильном — закрыта); canvas высота адаптивная: `N * 36 + 20 px`.

**Сессия 333 (2026-06-02) — Веб: серверная пагинация товаров:**
- **Пагинация /products**: добавлен `PRODUCTS_PAGE_SIZE=50`; роут принимает `page: int = 1`; после фильтрации по `q`/`category` список нарезается `[start:start+50]`; контекст: `page`, `total_pages`, `base_url` (сохраняет `q` и `category` в ссылках); шаблон: кнопки ← / → + номера страниц (эллипсис для >5 стр.); footer обновлён «N товаров · стр. X/Y».
- Итог плана улучшений полностью выполнен: PWA+SW ✅ · Swipe ✅ · WAL ✅ · Уведомления ✅ · Тёмная тема ✅ · Горячие клавиши ✅ · Графики ✅ · Excel-экспорт ✅ · Пагинация ✅

**Текущий статус (хэши):**
- GitHub: `cdb123c` · Amvera: `99d7a06`

**Что осталось:**
- Нет незакрытых задач из плана улучшений — всё выполнено.

**Сессия 294 (2026-06-02) — Веб: Рассылки, Мотивация, Ставки зарплаты, Личный заработок, Excel из отчётов:**
- **P1 /notifications** (`web/routes/notifications.py`, `web/templates/notifications/index.html`): полная страница рассылок для admin/owner/super_admin — форма создания рассылки (текст + получатели: всем/по магазину/по роли + «сейчас»/«запланировать»), список запланированных с кнопкой отмены (DELETE JSON CSRF), история отправленных. Запись в `scheduled_notifications` → APScheduler отправляет. Пункт «🔔 Рассылки» добавлен в сайдбар (admin+ only).
- **P1 /motivation** (`web/routes/motivation.py`, `web/templates/motivation/index.html`): матрица мотивации для admin+ — таблица всех товаров с комиссиями (% или фикс.), фильтр по категории, `POST /motivation/set` (set_product_motivation с пересчётом месяца), `POST /motivation/remove/{id}` (remove_product_motivation, JSON CSRF). Форма с превью расчёта. Пункт «🎯 Мотивация» в сайдбар.
- **P2 Ставки зарплаты**: `POST /salary/rate/set` в `web/routes/salary.py`; inline-редактирование ставки прямо в таблице salary page — клик на ставку → input → ✓/✕; Alpine.js `rateEdit()` компонент в `web/templates/salary/index.html`. Убрана заметка «настраивается через бота».
- **P2 Личный заработок**: `_salary_user_earnings()` helper в `salary.py` — при role==user редиректит на `web/templates/salary/earnings.html`; показывает оклад (смены × ставка) + детализацию комиссий по продажам + корректировки + итог к выплате. Использует `get_seller_earnings()` + `get_worked_days_count()` + `get_salary_rate()`.
- **P2 Excel из /reports**: `GET /reports/export.xlsx` в `web/routes/reports.py` — 3 листа (Сводка, По группам, Детализация); кнопка Excel в шапке `/reports`. Параметры period/date_from/date_to/shop/group_by передаются в URL.
- **base.html**: добавлен `{% block extra_js %}{% endblock %}` перед `</body>` для страничных скриптов.
- **settings/index.html**: ссылка «Создайте рассылку в разделе Рассылки» вместо «Создаются в боте».

**Сессия 317 (2026-06-02) — Веб: полное закрытие пробелов (T001–T012):**
- **T001 Профиль**: `POST /settings/profile` — редактирование имени/фамилии/отчества/телефона/email/сети/города; карточка «👤 Личные данные» в settings/index.html
- **T002 Мотивация по категории**: `POST /motivation/set_category` — bulk-установка ставки для всех товаров категории; сворачиваемый блок в мотивации
- **T004 Быстрый поиск + кнопки кол-ва**: строка 🔍 над списком товаров в sale modal + кнопки `[1,2,3,5,10,20,50]` для быстрого выбора
- **T005 Кросс-продажа**: чекбокс «Продать с другого магазина» для admin+ в sale modal; `cross_shop=1` в `POST /sales/create`; API `/api/products-for-shop` relaxed для admin
- **T006 Произвольный период рейтингов**: кнопка «📅 Произвольный» + datepicker + «Применить»; Excel-экспорт передаёт `date_from/date_to`
- **T008 Ручная выдача подписки**: вкладка «🎁 Выдать вручную» в `/payments`; `POST /payments/grant` — `create_subscription(user_id, plan_type)` без оплаты
- **T009 Текстовый импорт товаров**: `POST /products/import/text` — парсинг строк `Название, Категория, Цена`; сворачиваемая форма-textarea на странице импорта
- **T010 Реферальная программа**: карточка со статистикой + ваша реф-ссылка в правой колонке `/settings`
- **T011 Бэкап БД**: `GET /settings/backup` — скачивание `.db` файла org; карточка в `/settings`
- **T012 Избранное/Недавние**: вкладки «Все/⭐/🔄» в sale modal; API `/api/favorite-products` + `/api/recent-products`; Alpine methods `loadFavorites()`/`loadRecent()`/`switchProductTab()`
- **Аудит 2 (сессия 317)**: новые пробелы (см. ниже)

**Статус пробелов веб vs бот (актуально после сессии 317):**
- ✅ ЗАКРЫТЫ T001–T012 (сессии 316–317)
- 🟡 НОВЫЕ СРЕДНИЕ: Мотивация по месяцу+пересчёт; Доп.условия мотивации (коэффициент/мин.продавцов); Конкурсы per_sale тиры; Конкурсы индивид.цели по магазинам
- 🟢 НОВЫЕ НИЗКИЕ: Превью мотивации при записи продажи

**Сессия 292 (2026-06-02) — Веб: полные write-actions + аудит + обновление лендинга:**
- **Веб write-actions (Task #26)**: `POST /sales/create`, `POST /sales/{id}/delete`, `GET /api/products-for-shop` (scope-aware) — сотрудник записывает/удаляет продажи из браузера
- `POST /inventory/adjust` (JSON+CSRF) — корректировка остатков ±N или абсолютно; inline ±1 + popover в `inventory/index.html`
- `GET /staff/invite-code` + `POST /staff/invite-code/rotate` — инвайт-карточка с ротацией
- `POST /staff/{id}/set-role` + `POST /staff/{id}/remove` — иерархия: admin→только user-цели; owner→admin+user; super_admin→все
- `POST /salary/adjustment/add` + `POST /salary/adjustment/{id}/delete` — бонусы/штрафы из веба
- Шаблоны обновлены: `sales/index.html` (Alpine saleModal), `inventory/index.html` (inline adjust), `staff/index.html` (invite card), `staff/detail.html` (role+remove), `salary/index.html` (adjustments)
- **Аудит**: все файлы синтаксически чисты; code review APPROVED_WITH_COMMENTS (3 non-blocking: badge colour после inline-adjust; /api/products-for-shop без shop → всё; inventory_adjust передаёт telegram_id вместо internal id)
- **Лендинг**: «Веб-кабинет» карточка обновлена (для всей команды, запись продаж/инвентарь/сотрудники); stats-strip «5 форматов» → «Бот + Веб»; features subtitle и шаг 3 в How-it-works обновлены

**Сессии 287–289 (2026-06-02) — Веб-интерфейс: аудит + улучшения + лендинг:**
- **287**: JWT TTL 30→7 дней (`web/auth.py`); rate limit 5 req/60 s на `POST /auth/code/auto`; рейтинги — Alpine.js instant-search в `web/routes/rankings.py` + `rankings/index.html`
- **288**: Полный CRUD товаров в вебе — `GET/POST /products/new`, `/create`, `/{id}/edit`, `/update`, `/delete`; шаблон `web/templates/products/form.html`; кнопки «✏️ Редактировать» и «🗑 Удалить» на странице товара
- **289**: Аудит 24 шаблонов + 66 маршрутов: Jinja2-backslash-баг в `products/detail.html` исправлен (`{% set _safe %}`); rate limit добавлен на `POST /auth/code`; лендинг `web/templates/landing.html` (route `/` — умный: auth → dashboard, иначе promo); MD-файлы обновлены

**Сессии 234–237 (2026-05-31) — «Системный» shop fix + filter panel + perf:**
- 234-235: `get_all_shops()` / `get_inventory_shops()` — параметр `include_system=False`; суперадмин передаёт `include_system=True`
- 236: Пресет инвайта — UNION с inventory (ТЦ Бум теперь виден)
- 237: `filter_utils.py` — shops из users+shops+inventory; `_low_stock_count` — shops из users+shops; `asyncio.gather` в commission_handlers (3 места) и sales_plans_handlers

**Сессия 224 (2026-05-27) — INVITE SYSTEM IMPROVEMENTS (A–E):**
- **А) Deep-link**: `/start CODE` через `Command("start")` filter в `handlers.py`; парсит аргумент инвайт-кода и автоматически привязывает к орге без ручного ввода
- **Б) Ротация кода**: `rotate_invite_code(org_id)` в `tenant_manager.py`; кнопка «🔄 Сбросить код» на экране инвайта → новый код генерируется и обновляется в БД
- **В) Уведомление owner/admin**: `_notify_org_join()` в `handlers.py` — вызывается в `process_city` через `asyncio.create_task()`; получает список admin telegram_ids из `get_org_admin_telegram_ids()`
- **Г) Имя из Telegram**: `_show_name_prefill_fsm()` в `handlers.py` при join-режиме предлагает кнопки «✅ Использовать имя из профиля» / «✏️ Ввести вручную»; callbacks `name_tg_use` / `name_tg_manual`
- **Д) Пресет роли и магазина**: `invite_preset_role`, `invite_preset_shop` в таблице `organizations`; `set_invite_preset()`, `get_invite_preset_by_code()`, `get_invite_preset_by_org()` в tenant_manager; экран настройки через `invite_preset_start_N` → `ipr_role_X_N` → `ipr_shop_TOKEN_N`; при регистрации по инвайту пресет из БД пропускает шаги выбора роли/магазина
- **Экран инвайта**: `_show_invite_screen()` — universal helper в `admin_handlers.py`; показывает код + deep-link URL + пресет + кнопки «🔄 Сбросить» / «⚙️ Пресет» / «◀️ Назад»
- **bot_holder.py**: добавлены `set_username()` / `get_username()`; в `main.py` username бота сохраняется при старте через `get_me()`
- **README.md**: полная перепись с разделом про invite-систему
- **Тесты**: 45 импортов OK, 702 сценария OK

**Сессия 200 (2026-05-26) — фичи и фиксы:**
- **Фильтры получателей уведомлений** (`notifications_handlers.py`): после ввода текста → выбор «👥 Всем / 🏪 По магазину / 🎭 По роли»; превью с числом получателей; запланированные уведомления сохраняют фильтр в `recipients_list` (JSON); `check_scheduled_notifications` применяет фильтр при отправке. Супер-admin: только «Всем».
- **Google Sheets `invalid_grant` fix** (`integration/auth/google_oauth.py`, `integration/manager.py`, `sales_handlers.py`, `integration_handlers.py`): `OAuthTokenRevokedException` класс; `_normalize_gs_error()` конвертирует `google.auth.RefreshError` в дружелюбное сообщение; автоматически отключает подключение (`enabled=0`); в продаже показывает «🔑 Требуется переподключение»; в sync_motivation — инструкция по переподключению.
- **Перенос экспортов между подключениями** (`integration_handlers.py`, `database.py`): кнопка «📤 Перенести экспорты» на экране подключения (только если есть экспорты); выбор целевого подключения → экран подтверждения с превью экспортов → одна кнопка «✅ Перенести»; `move_exports_to_connection(from_id, to_id)` в database.py — UPDATE connection_id одной транзакцией; все псевдонимы и настройки сохраняются.

**Дополнительные секреты (Google Sheets):**
- `GOOGLE_OAUTH_CLIENT_ID` — OAuth client_id из Google Cloud Console
- `GOOGLE_OAUTH_CLIENT_SECRET` — OAuth client_secret

**Верификация Amvera:** После каждого пуша `deploy.sh` автоматически проверяет `git ls-remote` и печатает:
`Amvera verify: ✅ remote hash совпадает (hash)` или `⚠️ расхождение!`

---

## 1. АРХИТЕКТУРА ПРОЕКТА

```
Telegram API                       Browser (admin/owner)
    ↓                                     ↓
main.py  — polling, регистрация     web/app.py — FastAPI (порт 5000)
           роутеров, APScheduler    web/routes/*.py — 15 роутеров (read+write)
           (12 задач)               web/templates/*.html — Jinja2+Tailwind
    ↓                                     ↓
  [оба читают одни и те же SQLite БД через Database()]

main.py  — polling, регистрация роутеров, APScheduler (12 задач)
    ↓
┌──────────────────────────────────────────────────────────────────┐
│  25 РОУТЕРОВ (handlers)                                          │
│  main_router          ← handlers.py         (старт, профиль)    │
│  admin_router         ← admin_handlers.py   (орг, юзеры)        │
│  sales_router         ← sales_handlers.py   (продажи)           │
│  products_router      ← products_handlers.py (каталог, остатки) │
│  inventory_router     ← inventory_handlers.py (склад)           │
│  reports_router       ← reports_handlers.py  (отчёты+рейтинги)  │
│  commission_router    ← commission_handlers.py (мотивация)      │
│  earnings_router      ← earnings_handlers.py  (заработок)       │
│  sales_plans_router   ← sales_plans_handlers.py (планы продаж)  │
│  salary_router        ← salary_handlers.py    (зарплата/смены)  │
│  contacts_router      ← contacts_handlers.py  (контакты)        │
│  notifications_router ← notifications_handlers.py               │
│  payment_admin_router ← payment_admin_handlers.py               │
│  payment_system_router← payment_system_admin.py                 │
│  subscription_router  ← subscription_router.py                  │
│  backup_router        ← backup_handlers.py                      │
│  contests_router      ← contests_handlers.py  (конкурсы)        │
│  dashboard_router     ← dashboard_handlers.py (дашборд-сводка)  │
│  filter_router        ← filter_handlers.py    (общий фильтр)    │
│  integration_router   ← integration_handlers.py (Google Sheets) │
│  referral_router      ← referral_handlers.py  (реф. программа)  │
│  addon_router         ← addon_handlers.py     (надстройки)      │
│  web_auth_router      ← web_auth_handlers.py  (Telegram WebApp) │
│  absence_router       ← absence_handlers.py   (отсутствия)      │
│  tasks_bot_router     ← tasks_handlers.py     (задачи)          │
└──────────────────────────────────────────────────────────────────┘
    ↓
┌──────────────────────────────────────────────────────────────────┐
│  СЛОЙ ДАННЫХ                                                     │
│  db_utils.py        — get_db(), is_any_admin() [ГЛАВНЫЙ]        │
│  database.py        — класс Database (396 методов)              │
│  tenant_manager.py  — маршрутизация БД по организации           │
│  env_manager.py     — BOT_TOKEN, ADMIN_CHAT_ID                  │
└──────────────────────────────────────────────────────────────────┘
    ↓
┌──────────────────────────────────────────────────────────────────┐
│  БАЗЫ ДАННЫХ                                                     │
│  data/main.db              — organizations, user_org_mapping     │
│  data/shop_bot.db          — личный режим + платежи             │
│  data/tenants/org_*.db     — изолированные БД организаций       │
│  (Amvera prod: только org_huawei.db в persistent /app/data)     │
└──────────────────────────────────────────────────────────────────┘
```

**Вспомогательные модули:**

| Файл | Назначение |
|---|---|
| `payment_provider.py` | Фабрика провайдеров: `get_active_provider(db)`, `create_yookassa_payment(...)`, `check_yookassa_payment_status(...)`, `provider_label(provider)` |
| `plan_notifications.py` | Уведомления об изменениях тарифов (lazy-импорт из payment_system_admin.py) |
| `scheduler_module.py` | Синглтон APScheduler — set_scheduler() / get_scheduler() |
| `timezone_utils.py` | get_user_time(), get_current_user_time(), format_user_datetime(), get_utc_time() |
| `reports_access_control.py` | Проверка доступа к отчётам по подписке |
| `subscription_utils.py` | Лимиты подписки: `get_plan_limits(tg_id)` → dict; `_has_active_trial()` → bool (триал = `_UNLIMITED`); `check_integrations_permission()` / `check_export_permission()` / `check_analytics_permission()` / `check_notifications_permission()` / `check_product_limit()` / `check_sales_limit()` / `check_shop_limit()`; `get_subscription_warning_message()` показывает сравнение тарифов |
| `backup_manager.py` | Резервное копирование и восстановление всех БД |
| `restart_manager.py` | Управление перезапуском бота; restart_data_file = "data/restart_data.json" |
| `message_utils.py` | fsm_edit(), safe_edit_message() — anchor message pattern |
| `utils.py` | he(), escape_md(), format_currency(), format_price(), generate_excel_report() |
| `pickle_storage.py` | FSM persistence через PickleStorage (data/fsm_storage.pkl) |
| `filter_utils.py` | empty_filter(), get_available_filter_values(), merge_scope_with_filter(), build_filter_keyboard() |
| `filter_handlers.py` | filter_router: flt_open_{back_cb}, ftog_s/c/n_*, flt_reset |
| `hints.py` | hint_suffix(), maybe_send_welcome() — onboarding и inline-подсказки |
| `notif_utils.py` | add_read_btn() — добавляет «✅ Прочитано» ко всем push-уведомлениям |
| `pagination_utils.py` | paginate(), page_nav_row(), PAGE_SIZE_DEFAULT/SALES/USERS/ORGS |
| `scripts/post-merge.sh` | post-merge setup: `pip install -r requirements.txt`; зарегистрирован в `.replit [postMerge]`, таймаут 60s — запускается автоматически после каждого мержа задачи-агента |
| `integration/auth/google_oauth.py` | Device Flow OAuth 2.0: `get_client_credentials()` ← env vars; `initiate_device_flow()` → {device_code, user_code, verification_url}; `poll_for_token(device_code)` → access_token; `refresh_access_token(refresh_token)` → новый access_token |
| `integration/manager.py` | `integration_manager` синглтон; `trigger_export(db, export_type, event_data)` — вызывается из `complete_sale` для типа `'sales'`; читает `integration_connections`+`integration_exports` из той же org DB |
| `integration_handlers.py` | `integration_router`: настройка подключений GS, авторизация через Device Flow, просмотр/удаление связей |
| `referral_handlers.py` | `referral_router`: экран реф. программы (`subscription_referral`); статистика рефералов; deep-link `/start ref_TELEGRAMID` |
| `addon_handlers.py` | `addon_router`: надстройки к подписке (`subscription_addons`); покупка доп. магазинов (150₽/30д) и товаров (100₽/30д); `AddonStates.entering_qty`; маршрутизирует оплату в `confirm_payment_request` с prefix `addon_` |
| `pdf_utils.py` | `generate_pdf_report(org_name, start_date, end_date, sales_rows, summary, top_sellers, top_products)` → path\|None; `generate_pdf_for_report(db, scope, ...)` → path\|None — фасад для handlers; требует `reportlab` |

---

## 2. КРИТИЧЕСКИЕ ПРАВИЛА (нарушение → баги)

### 2.1 Доступ к БД — ТОЛЬКО через get_db()

```python
# ✅ ПРАВИЛЬНО — все обычные handlers (AsyncDatabase, все методы через await):
from db_utils import get_db
current_db = await get_db(callback.from_user.id, state)
result = await current_db.some_method()        # ← await обязателен!
items  = await current_db.get_all_products()

# ✅ ПРАВИЛЬНО — payment/subscription handlers (всегда shop_bot.db):
db = wrap_db(Database('data/shop_bot.db'))      # wrap_db() из db_utils!

# ✅ ПРАВИЛЬНО — APScheduler (sync контекст, не async):
db = get_db_sync(telegram_id)                  # возвращает Database, без await
result = db.some_method()                      # без await

# ✅ ПРАВИЛЬНО — parallel queries (asyncio.gather):
r1, r2, r3 = await asyncio.gather(
    current_db.get_sales_summary(...),
    current_db.get_plans_progress(),
    current_db.get_contests(status='active'),
    return_exceptions=True,
)

# ❌ НЕПРАВИЛЬНО — глобальный db в начале обычного файла:
db = Database('data/shop_bot.db')  # создаёт утечку изоляции между орг

# ❌ НЕПРАВИЛЬНО — вызов без await:
result = current_db.some_method()  # возвращает coroutine, не данные!
```

**get_db() логика (db_utils.py):**
```
super-admin + selected_org_db в state → AsyncDatabase(Database(selected_db))
user в org (tenant_manager)            → AsyncDatabase(Database(org_*.db))
иначе                                  → AsyncDatabase(Database(shop_bot.db))
```

**AsyncDatabase (db_utils.py):**
```python
class AsyncDatabase:
    # __getattr__ оборачивает ВСЕ вызываемые атрибуты Database в asyncio.to_thread()
    # db_file — explicit @property (без asyncio.to_thread)
    # sync доступ к внутреннему объекту: getattr(db, '_db', db) → Database
    wrap_db(db: Database) → AsyncDatabase   # явная обёртка
```

### 2.2 Проверка прав — ТОЛЬКО через is_any_admin()

```python
# ✅ ПРАВИЛЬНО:
from db_utils import is_any_admin
if is_any_admin(telegram_id):  # проверяет ADMIN_CHAT_ID ИЛИ роль owner/admin в org

# ❌ НЕПРАВИЛЬНО (не знает об org-ролях):
from env_manager import env_manager
if env_manager.is_admin(chat_id):
```

### 2.3 Anchor Message Pattern — порядок КРИТИЧЕН

```python
# ✅ ПРАВИЛЬНО — clear_state_keep_org ПОСЛЕ fsm_edit:
await fsm_edit(callback, state, text, keyboard)
await clear_state_keep_org(state)

# ❌ НЕПРАВИЛЬНО — clear ПЕРЕД edit (сбрасывает anchor_msg_id до его использования):
await clear_state_keep_org(state)
await fsm_edit(callback, state, text, keyboard)
```

### 2.4 Очистка state — ТОЛЬКО через clear_state_keep_org()

```python
# ✅ ПРАВИЛЬНО:
from db_utils import clear_state_keep_org
await clear_state_keep_org(state)

# ✅ С доп. ключами (например Excel):
await clear_state_keep_org(state, extra_keys=["excel_start", "excel_end", "excel_shop"])

# ❌ ЗАПРЕЩЕНО — теряет контекст орг у супер-админа:
await state.clear()
```

### 2.5 callback_data — КРИТИЧЕСКИЙ ЛИМИТ 64 байта

```python
# ✅ ПРАВИЛЬНО — для user-provided строк:
from keyboards import safe_cb, resolve_cb_name
builder.button(text=shop, callback_data=safe_cb("sale_shop_", shop))
shop = resolve_cb_name(raw, current_db.get_all_shops() or [])

# ❌ НЕПРАВИЛЬНО — кириллица >= 22 символов ломает API:
callback_data=f"sale_shop_{shop_name}"
```

### 2.6 HTML в сообщениях — ТОЛЬКО через he() для user-строк

```python
# ✅ ПРАВИЛЬНО:
from utils import he
text = f"Товар: <b>{he(product_name)}</b>\nМагазин: <b>{he(shop_name)}</b>"

# ✅ ИСКЛЮЧЕНИЕ — тексты кнопок Telegram НЕ парсит как HTML:
InlineKeyboardButton(text=f"🏪 {shop_name}")  # he() не нужен

# ❌ НЕПРАВИЛЬНО — < > & → TelegramBadRequest:
text = f"Товар: <b>{product_name}</b>"
```

### 2.7 Инициализация переменных перед try/except

```python
# ✅ ПРАВИЛЬНО:
plans_progress = []
try:
    plans_progress = current_db.get_plans_progress()
except Exception:
    pass

# ❌ НЕПРАВИЛЬНО — NameError если try бросит исключение:
try:
    plans_progress = current_db.get_plans_progress()
except Exception:
    pass
if plans_progress:  # NameError!
```

### 2.8 Timezone — отображение времени пользователю

```python
# ✅ ПРАВИЛЬНО — все отображения времени через timezone_utils:
from timezone_utils import format_user_datetime, get_current_user_time
user_tz = current_db.get_user_timezone(user_id)
now_str = get_current_user_time(user_tz).strftime('%d.%m.%Y · %H:%M')
time_str = format_user_datetime(raw_iso_str, user_tz, '%H:%M')

# ✅ ПРАВИЛЬНО — сохранять время в UTC:
from timezone_utils import get_utc_time
utc_dt = get_utc_time(naive_local_dt, admin_tz)  # admin вводит → конвертировать в UTC

# ❌ НЕПРАВИЛЬНО — datetime.now() на Amvera это UTC, не Москва:
now_str = datetime.now().strftime('%H:%M')
```

---

## 3. ДЕПЛОЙ И AMVERA

### deploy.sh — механика

- Синхронизирует файлы workspace → `/tmp/github-deploy` и `/tmp/amvera-deploy`
- **Исключает**: `.git`, `.local`, `__pycache__`, `data/backup`, `data/tenants`, `*.db`, `*.pkl`, `*.log`
- `AGENT_HANDOFF.md`, `replit.md`, `PROJECT_MAP.md`, `README.md` → только в GitHub, не в Amvera
- GitHub: `git push --force origin HEAD:main`
- Amvera: `git push amvera HEAD:master --force` (ВСЕГДА по умолчанию)
- После пуша: `git ls-remote amvera` → верификация хэша
- Флаг `--no-amvera` — только GitHub

### Amvera warnings ("вне persistenceMount") — ВСЕ ЛОЖНЫЕ

Amvera статически сканирует `sqlite3.connect('data/...')` → предупреждает. Но рабочая директория на Amvera = `/app`, поэтому `data/` = `/app/data/` = persistenceMount ✅.

### ⚠️ Amvera: битые сборки и "Internal server error"

**Симптомы:** Amvera запускает старый контейнер несмотря на успешный `push` + `git ls-remote ✅`.

**Причина:** `deploy.sh` проверяет только что код **дошёл** до Amvera git-репозитория. Это НЕ гарантирует, что сборка прошла. Amvera строит контейнер отдельно — если сборка падает, продолжает крутить последний успешный образ.

**Диагностика:** В разделе "Логи" → "Контроль версий" (Amvera UI) смотреть колонку "Используется":
- ✅ зелёный = задеплоен и работает
- ✅ хранится + ❌ не используется = собрался, но не задеплоился  
- отсутствует в списке = сборка упала с "Internal server error" и не была записана

**"Internal server error"** — инфраструктурная ошибка Amvera, не ошибка кода. Лечится повторным пушем (иногда несколько попыток). Если повторяется >3 раз — писать в поддержку Amvera.

**Форсированный перепуш** (триггер пересборки без изменений кода):
```bash
# Добавить комментарий с датой в requirements.txt, затем:
bash deploy.sh "chore: trigger rebuild YYYY-MM-DD"
```

**❌ ЗАПРЕЩЕНО в amvera.yml** — поля которых НЕ СУЩЕСТВУЕТ:
```yaml
# НЕ ДОБАВЛЯТЬ — вызывает "Configuration error: unknown fields":
build:
  quickBuild: false   # ← НЕ СУЩЕСТВУЕТ в Amvera
```
Правильная минимальная конфигурация — только `meta` + `run` (см. раздел выше).

**Два инстанса бота при рестарте** — норма: при перезапуске контейнера старый и новый процесс ~7 секунд работают параллельно → `TelegramConflictError`. APScheduler-задачи могут выполниться дважды. Само проходит, не баг.

### Amvera persistent data (production)

- Persistent mount: `/app/data`
- Файлы: `main.db`, `shop_bot.db`, `fsm_storage.pkl`, `tenants/org_huawei.db`, `backup/`
- `org_huawei.db` — единственная тенант-БД в production. `create_tables()` авто-применяет миграции.

---

## 4. БАЗЫ ДАННЫХ

### data/main.db — только через tenant_manager

| Таблица | Ключевые колонки |
|---|---|
| `organizations` | id, name, owner_id, invite_code, db_path, subscription_plan, is_active, invite_preset_role (NULL/'admin'/'user'), invite_preset_shop (NULL/str) |
| `user_org_mapping` | telegram_id, org_id, role (owner/admin/user), scope_type, scope_value (JSON array), custom_title, **org_role_id** (логич. back-ref на `org_roles`, без SQL FK), **department_id**, **scope_shops**, **scope_cities** — 4 последние NULL = старое поведение; ALTER TABLE без FK (`org_roles`/`departments` в org_*.db, не в main.db) |

### data/shop_bot.db и data/tenants/org_*.db — идентичная схема

| Таблица | Назначение |
|---|---|
| `users` | telegram_id, first_name, last_name, phone, email, trade_network, shop_name, city, timezone, **username** (индекс 12) |
| `products` | id, name, category, price, motivation_type, motivation_value, **photo_file_id** (Telegram file_id фото), **description** (TEXT, описание товара) — добавлены миграцией ALTER TABLE |
| `inventory` | shop_name, product_id, quantity, last_updated |
| `inventory_history` | shop_name, product_id, quantity_change, change_type, change_reason, user_id, timestamp |
| `sales` | product_id, shop_name, quantity_sold, sale_price, user_id, sale_date |
| `seller_earnings` | user_id, sale_id, product_id, quantity, base_amount, commission_amount, total_amount, sale_date |
| `motivation_rules` | product_id, commission_type, commission_value, admin_id |
| `motivation_conditions` | условия мотивации (сверхплан, коэф. смены, фильтр категорий) |
| `motivation_extra_conditions` | доп. условия (миграция в create_tables) |
| `salary_settings` | user_id, daily_rate, updated_by |
| `work_schedule` | user_id, work_date, **start_time**, **end_time**, marked_by — UNIQUE(user_id, work_date) |
| `shift_templates` | user_id, weekday (0=Пн..6=Вс), start_time, end_time — UNIQUE(user_id, weekday) |
| `sales_plans` | id, plan_type, metric_type, target_value, target_type, user_id, shop_name, filter_type, filter_value, is_active, created_by |
| `plan_milestone_alerts` | user_id, plan_id, milestone (50/75/100), period_start — UNIQUE(user_id, plan_id, milestone, period_start) |
| `notification_settings` | low_stock_alerts, daily_reports, sales_alerts, payment_alerts, admin_notifications, **shift_sale_alerts** (индекс 11, добавлен миграцией) |
| `integration_connections` | id, name, provider ('google_sheets'), config (JSON), enabled, created_at, updated_at |
| `integration_exports` | id, connection_id (FK), export_type, enabled, schedule, target_sheet, operation, mapping (JSON), lookup_config (JSON), extra (JSON), last_run |
| `integration_log` | id, connection_id, export_id, status, message, created_at |
| `gs_bonus_cache` | id, connection_id, model_name, chain, bonus, rrp, synced_at — UNIQUE(connection_id, model_name, chain) |
| `notification_history` | id, user_id, notification_type, message, created_at, is_read |
| `scheduled_notifications` | id, job_id(UUID), created_by(FK→users.id!), notification_text, recipients_type, scheduled_datetime(**UTC**), status |
| `contests` | id, title, contest_type, scope, metric, target_value, reward_type, reward_value, start_date, end_date, status, winner_user_id |
| `user_hints_seen` | user_id, hint_key, seen_at |
| `user_product_favorites` | user_id, product_id |
| `user_product_recent` | user_id, product_id, last_used |
| `absence_type_settings` | id, type TEXT UNIQUE (vacation/sick/compensatory/absence/other), is_paid, annual_limit, penalty_mode ('none'/'no_pay'), penalty_amount, updated_at — 5 строк по умолчанию; absence=0/no_pay, остальные=1/none |
| `absence_records` | id, user_id, type, start_date, end_date, status (pending/approved/rejected/cancelled), is_paid, comment, admin_comment, created_by, reviewed_by, created_at, reviewed_at |
| `departments` | id, name, type (default 'department'), parent_id (FK self → иерархия), manager_tg_id, sort_order, is_active, created_at — подразделения/регионы; **оргструктура** |
| `org_roles` | id, name, icon, color, base_role, scope_type, scope_values, can_manage_users, can_manage_products, can_view_salary, can_manage_salary, can_view_reports, can_manage_plans, modules (TEXT), is_active, created_at — кастомные роли; **оргструктура** |
| `user_module_access` | id, telegram_id, module_key, access ('allow'/'deny'), created_at — UNIQUE(telegram_id, module_key); персональный доступ к модулям; **оргструктура** |

### data/shop_bot.db ТОЛЬКО (платежи централизованы):

| Таблица | Назначение |
|---|---|
| `subscriptions` | user_id, plan_type, start_date, end_date, is_active |
| `subscription_reminder_log` | user_id, threshold, subscription_end, sent_at |
| `payment_requests` | заявки на оплату + file_id чека + promocode_id |
| `payment_settings` | card_number, recipient_name, bank_name, trial_days, trial_plan, **payment_provider** ('sbp'/'yookassa'), **yookassa_shop_id**, **yookassa_secret_key**, **yookassa_return_url** |
| `subscription_plans` | name, price, duration_days, max_products, max_shops, max_sales_per_month, can_export_reports, can_view_analytics, can_use_notifications, **can_use_integrations** (0 для Базового!), is_active |
| `promocodes` | code, discount_percent, max_usage, current_usage, is_active |
| `yookassa_payments` | yookassa_payment_id (UNIQUE), user_id, plan_type, amount, status, promocode_id, is_scheduled, schedule_date |
| `referrals` | id, referrer_id (telegram_id), referred_id (telegram_id), created_at, bonus_applied (0/1) — реф. программа |
| `subscription_addons` | id, user_id, addon_type ('extra_shops'/'extra_products'), quantity, expires_at, created_at — надстройки |
| `web_credentials` | id, email UNIQUE, password_hash, telegram_id (nullable), synthetic_tg_id, org_db, first_name, email_verified, verify_token, verify_expires, reset_token, reset_expires, last_login, created_at — email+пароль вход |

---

## 5. КЛЮЧЕВЫЕ ФУНКЦИИ

### db_utils.py

```
get_db(telegram_id, state)            → AsyncDatabase  — ОСНОВНАЯ функция (async, await обязателен)
get_db_sync(telegram_id)              → Database       — для синхронных контекстов (APScheduler)
wrap_db(db: Database)                 → AsyncDatabase  — явная обёртка для inline Database()
is_any_admin(telegram_id)             → bool           — ADMIN_CHAT_ID ИЛИ роль owner/admin в org
get_user_org_role(telegram_id)        → str|None       — 'owner'/'admin'/'user'/None
get_user_org_scope(telegram_id)       → (scope_type, list[str])
get_user_full_scope(telegram_id)      → (scope_type, list[str], custom_title)
get_role_display_label(role, scope_type, scope_values, custom_title=None) → str
clear_state_keep_org(state, extra_keys=None) → None
org_structure_level(telegram_id)      → 'full'|'minimal'  — 'full' если has_module(owner_tg,'org_structure'), иначе 'minimal'
get_user_module_access(telegram_id, module_key) → 'allow'|'deny'|None  — персональный override поверх биллинга (None = наследует)
```

**AsyncDatabase:**
```python
# db_utils.py — класс AsyncDatabase
# __init__: object.__setattr__(self, '_db', db)
# __getattr__: если атрибут callable → asyncio.to_thread(fn, *args, **kwargs)
#              если не callable → прямой return (числа, строки, None)
# db_file: explicit @property → self._db.db_file (без to_thread)
# sync доступ к внутреннему объекту: getattr(async_db, '_db', async_db) → Database
# Используется в hints.py, maybe_refresh_username для sync-совместимости
```

**Пул соединений (database.py):**
```python
# _conn_pool = threading.local()  — thread-local хранилище
# _get_pooled_conn(db_file)       — создаёт/возвращает existing conn для потока
#   check_same_thread=False, PRAGMA WAL/cache/temp/mmap при первом создании
# _PooledConn(conn)               — обёртка: close() = rollback (не disconnect!)
#   все другие методы/атрибуты → proxy к raw connection
# get_connection() → _PooledConn  — используется во всех 200+ методах Database
# Эффект: один поток = одно соединение = нет overhead открытия/закрытия
```

### timezone_utils.py

```python
get_user_time(naive_dt, tz_name)         → datetime  — naive UTC → tz-aware local
get_current_user_time(tz_name)           → datetime  — текущее время в TZ пользователя
format_user_datetime(raw_str, tz, fmt)   → str       — ISO UTC-строка → форматированное локальное
get_utc_time(naive_local_dt, tz_name)    → datetime  — local naive → UTC datetime

# raw_str в format_user_datetime может быть ISO datetime или "HH:MM" — оба обрабатываются
```

### utils.py — generate_excel_report()

```python
generate_excel_report(sales, title, start_date, end_date) → openpyxl.Workbook

# 4 листа:
# 1. «Детальный отчёт» — все строки (до 50000), столбец «Продавец», числовые форматы, ИТОГО
# 2. «По категориям» — сводка + BarChart
# 3. «По продавцам» — если данные есть (len(row) >= 11), BarChart
# 4. «По дням» — если >1 дата, BarChart

# Определение seller data: len(row) >= 11 → get_sales_report (11 col); len(row) == 9 → get_user_sales
```

### reports_handlers.py — Excel download handlers

```
_translit_filename(text) → str       — транслитерация для имён файлов
download_excel_full      — полный отчёт; scope-фильтр, лимит 50000
download_excel_period    — период из FSM (excel_start/excel_end/excel_shop)
download_excel_user      — отчёт пользователя
download_excel_shop      — отчёт по магазину
download_excel_city      — отчёт по городу (один SQL-запрос для city)
download_excel_my        — "Мои продажи" (текущий месяц)
```

### reports_handlers.py — отображение товаров в сообщениях

```
report_today            — все товары (MAX_ADMIN_LINES=25 для admin; пользователь — все)
report_full             — все категории и магазины (guard > 3500/3700 символов)
report_shop_generate    — все товары по категориям (guard > 3600)
report_my_shop          — все товары (guard > 3700 — УБРАН [:3])
generate_period_report  — все товары (guard > 3700 — УБРАН [:3])
```

**Единый принцип guard'ов:**
- `> 3600` на уровне магазинов → «скачайте Excel для полной детализации»
- `> 3700` на уровне товаров → «остальные товары в Excel»
- `> 3800` сплошная обрезка (report_today)

### dashboard_handlers.py

```python
build_admin_dashboard(current_db, today, now_str, user_id=0, telegram_id=0) → str
build_user_dashboard(current_db, user_id, telegram_id, today, now_str)      → str
_on_shift_details(db_file, today)     → list[(first_name, last_name, shop_name)]
_today_total_earnings(db_file, today) → float
_plan_summary_line(plan, actual, percent) → str  — канонический формат плана
# now_str вычисляется через get_current_user_time(user_tz) в вызывающем коде
```

### salary_handlers.py — callbacks смен и шаблонов

```
Callback prefixes:
  slr_rates              — ставки сотрудников
  slr_set_{uid}          — редактировать ставку
  slr_scheds             — список графиков
  slr_cal_{uid}_{y}_{m}  — календарь смен (admin)
  slr_tog_{uid}_{date}   — переключить день (добавить/снять, применяет шаблон при добавлении)
  slr_day_{uid}_{date}   — под-экран управления конкретным днём
  slr_rm_{uid}_{date}    — снять смену
  slr_ets_{uid}_{date}   — изменить время: пикер часов начала
  slr_etw_{uid}_{date}   — пикер часов конца
  slr_te_{h}_{uid}_{date}  — выбрать час начала → сразу к slr_etw_
  slr_tw_{h}_{uid}_{date}  — выбрать час конца → сохранить + slr_day_
  slr_tmpl_{uid}_{y}_{m}   — шаблон смен (⏰ Расписание смен)
  tmpl_day_{uid}_{wd}       — день шаблона (0=Пн..6=Вс)
  tmpl_off_{uid}_{wd}       — отметить выходным
  tmpl_te_{uid}_{wd}        — пикер начала шаблона
  tmpl_tw_{uid}_{wd}        — пикер конца шаблона
  tmpl_te_h_{h}_{uid}_{wd}  — выбрать час начала шаблона
  tmpl_tw_h_{h}_{uid}_{wd}  — выбрать час конца → сохранить
  my_schedule               — своё расписание (user)
  my_d_{date}               — детали дня (user, editable=False)
  slr_sum_{y}_{m}           — зарплатный итог за месяц
```

### filter_utils.py + filter_handlers.py

```
ADMIN_FILTER_KEY = "admin_filter"  — ключ в FSM data, сохраняется между экранами

empty_filter()                     → dict  — пустой фильтр {shops:[], cities:[], networks:[]}
get_available_filter_values(db, scope_type, scope_values) → dict
merge_scope_with_filter(scope_type, scope_values, active_filter) → dict  — scope потолок, фильтр пол
build_filter_keyboard(available, active, back_cb) → InlineKeyboardMarkup

Callbacks (filter_router):
  flt_open_{back_cb}  — открыть панель (back_cb = callback кнопки «✅ Применить»)
  ftog_s_{val}        — переключить магазин (safe_cb/resolve_cb_name)
  ftog_c_{val}        — переключить город
  ftog_n_{val}        — переключить торговую сеть
  flt_reset           — сбросить фильтр
```

### hints.py + notif_utils.py + pagination_utils.py

```python
# hints.py
hint_suffix(db, user_id, hint_key) → str   — подсказка при первом визите раздела
maybe_send_welcome(msg_or_cb, db, user_id, is_admin)  — popup при первом входе
# Ключи HINT_TEXTS: 'sales','reports','products','dashboard','plans','contests','rankings'
# hint_dismiss:{hint_key} → удаляет попап (registered in main.py)

# notif_utils.py
add_read_btn(existing_markup=None) → InlineKeyboardMarkup
# Добавляет «✅ Прочитано» (callback notif_read) ко всем push-уведомлениям

# pagination_utils.py
PAGE_SIZE_DEFAULT=8, PAGE_SIZE_USERS=10, PAGE_SIZE_ORGS=8, PAGE_SIZE_SALES=8
paginate(items, page=0, per_page) → (page_items, total_pages)
page_nav_row(page, total_pages, prefix) → list[InlineKeyboardButton]
```

### main.py — APScheduler (12 задач)

| ID задачи | Расписание | Назначение |
|---|---|---|
| `send_sales_alerts` | каждую минуту (сек 0) | уведомления о дневных целях продаж |
| `send_payment_alerts` | каждую минуту (сек 12) | напоминания об окончании подписки + trial reminders 14/7/3/1d |
| `send_daily_reports` | каждую минуту (сек 24) | ежедневные отчёты |
| `send_personalized_notifications` | каждую минуту (сек 36) | персонализированные уведомления |
| `check_scheduled_notifications` | каждую минуту (сек 48) | запланированные рассылки (UTC) |
| `send_trial_expired_upsell` | каждый час в :05 | upsell-пуш при истечении триала; dedup threshold=-1 |
| `auto_finish_contests` | каждый час в :00 | автозавершение конкурсов |
| `auto_reject_stale_payments` | 10:15 ежедневно | авто-отклонение pending СБП-заявок >72ч |
| `backup_job` | 03:00 ежедневно | авто-бэкап всех БД (retention 30 дней) |
| `cleanup_fsm_storage` | воскресенье 04:30 | удаление FSM-записей старше 30 дней из `fsm_data` |
| `prune_ai_tool_stats` | 03:15 ежедневно | обрезка старой статистики AI-инструментов |
| `auto_archive_ai_sessions` | 03:20 ежедневно | авто-разрыв AI-сессий без активности >30 дней (все org_*.db) |

**APScheduler config:** `misfire_grace_time=60`, `coalesce=True`, `max_instances=1` — никакого параллельного запуска, пропущенные таски схлопываются. Итого: **12 задач** (добавлены `prune_ai_tool_stats` 03:15 и `auto_archive_ai_sessions` 03:20).

**Timezone в APScheduler:** все задачи используют `datetime.now()` (UTC на Amvera), конвертируют через `.astimezone(user_tz)` для сравнения с настроенным временем.

**Scheduled notifications:** admin вводит время → `get_utc_time(naive, admin_tz)` → хранится UTC → `check_scheduled_notifications` сравнивает `datetime.now().isoformat()` (UTC) с UTC → корректно.

---

## 6. ИСТОРИЯ СЕССИЙ

**Сессия 2026-06-04 — SECURITY AUDIT FIXES + UX + MD UPDATE:**

1. **Invite back-button fix** (`admin_handlers.py` строка 933): `back_button("admin_management")` → `back_button("personnel_hub")` — кнопка «Назад» из формы редактирования инвайта возвращала на несуществующий хаб.

2. **FSM `updated_at` колонка** (`sqlite_storage.py`): таблица `fsm_data` теперь имеет колонку `updated_at TEXT`; миграция через `ALTER TABLE IF NOT EXISTS`. `set_state` и `set_data` штампуют `updated_at`. Еженедельный APScheduler-job `cleanup_fsm_storage` (воскресенье 04:30) удаляет записи старше 30 дней. **APScheduler-задач стало 10**.

3. **POS limit warning banner** (`web/routes/pos.py`, `web/templates/pos/index.html`): при достижении лимита продаж GET `/pos` возвращает `sales_limit_reached=True` + `sales_limit_msg`; шаблон показывает красный 🚫 баннер со ссылкой на `/subscription`.

4. **Stateless HMAC nonce для `/auth/code`** (`web/auth.py`): `generate_login_nonce()` создаёт HMAC-SHA256(secret, timestamp//300) — валиден 5 минут. `verify_login_nonce()` принимает ±1 временное окно (10 мин tolerance). GET `/auth/code` → форма с nonce; POST верифицирует nonce. Полностью stateless, без хранения в БД.

5. **Persistent SQLite rate limiting** (`web/rate_store.py`): новый модуль. `check_rate_limit(key, limit, window_sec)` → `(allowed: bool, retry_after: int)`. Хранит счётчики в `data/rate_limits.db`; выдерживает рестарты. `_check_rate_limit()` в `auth_routes.py` теперь делегирует в `web.rate_store.check_rate_limit`.

6. **Кликабельные уведомления** (`web/routes/api.py`, `web/routes/notifications.py`, `web/templates/base.html`, `web/templates/notifications/index.html`): добавлена функция `_notif_url(notification_type)` → URL страницы. `GET /api/sales-feed` и история уведомлений содержат поле `url`. В `base.html` mobile sheet и desktop dropdown: `<div @click="window.location.href=url">` с hover-эффектом. В `notifications/index.html`: items с url → `<a href=url>`. Иконки по типу: 📦 low_stock, 📊 daily_report, 💳 payment и др.

- **GitHub**: `2b553d4` (notifications) / последний `5dab31f` (Amvera)



**Сессии 261–273 (2026-06-02) — ВЕБ-ИНТЕРФЕЙС (задачи #1–#7, #10, #14, #15):**

Реализован полноценный веб-интерфейс (`web/`) на FastAPI + Jinja2 + Tailwind + HTMX + Alpine.js.
Работает на порту 5000 параллельно с ботом. Аутентификация: Telegram Login Widget → JWT cookie.

| Задача | URL / функциональность | GitHub hash |
|--------|------------------------|-------------|
| #1 График работы | `/schedule` — месячный календарь смен, редактирование времени, шаблоны по дням недели, bulk-fill | (merged) |
| #2 Планы продаж | `/plans`, `/plans/new`, `/plans/{id}/edit` — полный CRUD; форма с multi-select фильтров по категориям/товарам | (merged) |
| #3 Конкурсы | `/contests`, `/contests/new` — создание, завершение, отмена, лидерборд | (merged) |
| #4 Invite-система | `/settings` — блок приглашений: код, deep-link, сброс кода, пресет роли/магазина | (merged) |
| #5 Excel-импорт | `/products/import` — drag-and-drop .xlsx, preview с пагинацией (20 строк/стр), подтверждение | `13b9ebf` |
| #6 Платежи | `/payments` (super_admin only) — pending заявки, Подтвердить/Отклонить, История, бейдж в nav | `d1e196f` |
| #7 Google Sheets | `/integration` — Device Flow OAuth, список подключений, toggle, удаление, лог экспортов | (merged) |
| #10 Milestone badges | `/plans/{id}` — цветные бейджи 50%/75%/100% в истории milestone-алертов | (merged) |
| #14 Редактирование конкурсов | `/contests/{id}/edit` — изменение дат, цели, награды для active/pending | (merged) |
| #15 Победитель конкурса | `/contests` (при finished + leaderboard) — карточка победителя с результатом и призом | `82e2eec` |

**Ключевые web-паттерны (не нарушать):**
- **Auth check**: `get_session_user(request)` → dict `{sub: telegram_id, role, org_db}` или `None`
- **DB access**: `get_web_db(telegram_id, org_db)` из `web/deps.py` → `Database(path)` sync (не AsyncDatabase!)
- **CSRF**: `get_csrf_token(request)` в роуте → передать в ctx как `csrf_token`; в шаблоне `{{ csrf_token }}`; верифицировать `verify_csrf_token(request, form.get("csrf_token"))`
- **Flash**: через query-параметры (`?saved=1`, `?error=msg`, `?imported=N`, `?msg=confirmed_N`)
- **TemplateResponse**: первый аргумент всегда `request` → `TemplateResponse(request, "name.html", ctx)`
- **POST redirect**: `RedirectResponse(url=..., status_code=303)` (не 302!)
- **Pending badge**: `pending_payments_count()` — Jinja2 global в `web/app.py`; запрашивает shop_bot.db напрямую через sqlite3
- **In-memory sessions**: `_import_sessions` (products.py) и `_device_flow` (integration.py) — теряются при рестарте; известный риск
- **Платежи**: `payment_requests` всегда в `shop_bot.db` — всегда `Database("data/shop_bot.db")` напрямую, не через `get_web_db`
- **super_admin**: в web = `user.role == 'super_admin'`; устанавливается при логине через `env_manager.is_super_admin(telegram_id)`

**Сессии 1–10:** базовая архитектура, multi-tenancy, роли, FSM flows, меню.

**Сессии 11–15:** PickleStorage, кеш путей БД, bulk import, полный audit callback.answer() (58 хендлеров).

**Сессии 16–23:** callback.answer(); org-admin баги; рейтинги (date() vs ISO); escape_md().

**Сессии 24–25:** мотивационные условия (сверхплан, коэф.); планы продаж; зарплата и смены.

**Сессия 199:** cross-org broadcast баг; check_notifications_permission получал DB-id вместо telegram_id; JOIN sn.created_by = u.id; добавлена задача check_scheduled_notifications.

**Сессия 199:** итоговый аудит. HTML-инъекция устранена в 44 местах / 9 файлах (he()). 0 bare except, 0 print().

**Сессия 199:** аудит сессий 24–25. Критический баг salary_handlers (fsm_edit вместо edit_text). he() в commission_handlers и sales_plans_handlers.

**Сессия 199:** `is_any_admin()` переписан — роль в user_org_mapping приоритетнее ADMIN_CHAT_ID. role='user' всегда False.

**Сессия 199:** конкурсы (полный жизненный цикл); дашборд перенесён в Отчёты; инвайт для орг-admin.

**Сессии 31–32:** multi-select категорий в plan wizard; редактирование планов; дашборд пользователя (все планы, призы конкурсов).

**Сессия 199:**
1. Anchor message fix — `clear_state_keep_org` ПОСЛЕ `fsm_edit`.
2. Edit plan: все поля (Получатель/Период/Метрика/Фильтр). Callback prefixes: `epwho_*`, `epperiod_*`, `epmetric_*`, `epfilter_*`.
3. Admin dashboard: все планы через `_plan_summary_line()`.
4. Баг `plans_progress` NameError исправлен.
5. `create_tables()` теперь вызывается для супер-адмна в `get_db()`.
6. `deploy.sh exclusions` — исключены `.db`, `.pkl`, `data/tenants/` из GitHub.

**Сессия 199:**
1. Профиль пользователя — роль вверху, дата в DD.MM.YYYY, he() на всех полях.
2. Очистка архива конкурсов с подтверждением.
3. `is_any_admin()` приоритет: org-роль > env_manager.
4. Admin dashboard: `_on_shift_details()` + `_today_total_earnings()`.
5. Reports: «По городу» внутри «За период»; сотрудник — «Мои продажи» + «Текущий месяц».
6. Rankings: 8 колонок в `get_sales_ranking()`; `_ranking_period()`; `_period_kb()`; позиция вне топ-10.
7. `deploy.sh` — `--no-amvera` флаг; верификация через `git ls-remote`.

**Сессия 199:**
1. he() аудит: contacts_handlers, payment_admin_handlers, main.py, plan_notifications, payment_system_admin.
2. `clear_state_keep_org(state, extra_keys=[...])` — сохранение доп. ключей FSM.
3. Excel download → FSM (избавился от длинных callback_data).
4. `safe_cb()` / `resolve_cb_name()` в handlers.py и admin_handlers.py.
5. **Multi-scope**: scope_value = JSON array; toggle UI `adm_t_s/c/n_*` + `adm_scope_submit`.
6. **Custom title**: custom_title в user_org_mapping; `set_user_title()`; 🏷️ в карточке пользователя.

**Сессия 199:**
1. **QuickSaleStates** (`searching_product = State()`).
2. **`_make_qty_keyboard(max_qty)`** — кнопки [1,2,3,5,10,20,50] + «✏️ Ввести вручную».
3. **Быстрый поиск** — «🔍 Найти товар» в экране категорий; поиск по ненулевому остатку.
4. **`sq_qty_*`** — выбор количества без ввода текста.

**Сессия 199:**
1. **`shift_templates`** таблица — UNIQUE(user_id, weekday), weekday 0=Пн..6=Вс.
2. **`work_schedule`** — добавлены колонки `start_time`, `end_time`; миграция авто.
3. **6 новых методов database.py**: `set_shift_template`, `get_shift_templates`, `get_work_day_time`, `add_work_day`, `remove_work_day`, `set_work_day_time`.
4. **salary_handlers.py** (~796 строк): пикер часов `_hour_picker_kb()`; цепочки slr_te/slr_tw; при slr_tog → применяет шаблон дня недели.
5. **⏰ Расписание смен** — кнопка `slr_tmpl_` в календаре; экран просмотра/редактирования 7 дней.
6. **my_day_detail** — сотрудник: время из work_schedule или шаблона (fallback).

**Сессия 199:**
1. **«📝 Мои продажи»** восстановлена в `main_menu()` для обычных продавцов (keyboards.py).
2. **«query is too old»** TelegramBadRequest → DEBUG (не засоряет логи).
3. **Timezone earnings**: время продажи в «Моём заработке» → `format_user_datetime(sale_date, user_tz, '%H:%M')`.
4. **now_str**: дашборд и отчёты → `get_current_user_time(user_tz).strftime(...)`.
5. **Scheduled notifications UTC fix**: ввод → `get_utc_time(naive, admin_tz)` → хранить; список → `format_user_datetime(raw_utc, admin_tz)`.
6. **Аудит итог**: 34/34 модулей · 279/279 тестов · 0 кириллицы в callback_data.

**Сессия 199:**
1. **«Команда сегодня»** дашборд: `_staff_by_shop_with_names()` — для 'wide'/'org' scale показывает «ТЦ Лето: 3 (Иванов, Петров, Сидоров)».
2. **Reports Markdown→HTML**: `report_full`, `report_shop_generate`, `report_city_generate` — HTML + `he()`.
3. **Perf: 7 индексов в `create_tables()`**: idx_sales_user_date, idx_sales_shop_date, idx_inventory_shop_prod, idx_work_schedule_date, idx_seller_earnings_sale, idx_users_shop_name, idx_users_telegram_id — все `CREATE INDEX IF NOT EXISTS`.
4. **`busy_timeout=10000`** в `create_tables()` (ранее только в `get_connection()`).
5. **«message is not modified»** → тихий `answer()` без логирования.

**Сессия 199 (2026-05-07):**
1. **`earnings_handlers.py` — Markdown→HTML** (10 мест): все `parse_mode="Markdown"` → HTML.
2. **`earnings_handlers.py` — Русские названия месяцев**: `_MONTHS_RU[]` вместо `calendar.month_name[]`.
3. **`salary_handlers.py` — Markdown→HTML** (24 места): `escape_md()` → `he()`.
4. **`keyboards.py` — утечка соединения SQLite** в `main_menu()`: добавлен `try/finally` вокруг `conn.close()`.

**Сессия 199 (2026-05-07) — ЮKASSA:**
1. **`payment_provider.py`** (новый): `get_active_provider(db)`, `create_yookassa_payment(...)`, `check_yookassa_payment_status(...)`.
2. **`database.py`** — таблица `yookassa_payments`; 7 методов: `get/set_payment_provider`, `get/set_yookassa_config`, `create/get/update_yookassa_payment_*`.
3. **`payment_system_admin.py`** — «🔀 Провайдер оплаты»; экраны выбора/настройки ЮKassa.
4. **`subscription_handlers.py`** — маршрутизация по провайдеру: СБП = скриншот; ЮKassa = URL-оплата + «✅ Я оплатил — проверить» → auto-confirm.
5. **`states.py`** — `PaymentSystemStates`: waiting_yookassa_shop_id/secret_key/return_url.
6. **559 тестов ✅** (добавлены новые сценарии).

**Сессия 199 (2026-05-07) — EXCEL ОТЧЁТЫ:**
1. **`generate_excel_report()`** в `utils.py` переписан — 4 листа: «Детальный отчёт» (с колонкой «Продавец», числовые форматы, ИТОГО), «По категориям» (BarChart), «По продавцам» (если есть данные), «По дням» (если >1 день, BarChart). Определение продавца: `len(row) >= 11`.
2. **6 Excel-хендлеров** в `reports_handlers.py` обновлены: scope-фильтр, лимит 50000, `_translit_filename()`, `⏳ Формирую файл...`, подписи с числом строк.
3. **`_translit_filename(text)`** — хелпер транслитерации для имён .xlsx файлов.
4. GitHub `7a7f1a3` · Amvera `e5913d1`. 559 тестов ✅.

**Сессия 199 (2026-05-11) — Конкурсы: индивидуальные пороги (#3) + per_sale тиры (#4):**
1. **database.py**: `contests` +`reward_mode`/`individual_targets` (ALTER TABLE migration); новая таблица `contest_product_bonuses`; новые методы `save_contest_product_bonuses`, `get_contest_product_bonuses`, `get_user_plan_pct_for_contest`; `create_contest`/`update_contest` расширены; `compute_contest_results` разделён на total (учитывает `individual_targets.by_shop`) и per_sale (тиры × qty × plan_pct); `get_user_contest_rewards` читает `reward_mode`.
2. **contests_handlers.py**: новые FSM-состояния `configuring_tier_bonus`, `entering_individual_target`; новые шаги: выбор режима (`ctrm_`), wizard тиров per_sale (`cttc_`/`ctpct_`/bonus-ввод), индивидуальные пороги по магазинам (`ctind_yes/no`, `ctindval_skip`); `_show_contest_confirm` показывает тиры/инд.пороги; `contest_confirm_create` сохраняет `reward_mode`, `individual_targets`, тиры через `save_contest_product_bonuses`; `contest_view` отображает тиры/инд.пороги; `contest_results` ветвится по `reward_mode` (per_sale: бонус, plan_pct; total: победители + инд.пороги); `contest_notify_winners` — разные тексты для per_sale/total.
3. **dashboard_handlers.py**: `_contest_block` — per_sale конкурсы показывают накопленный бонус+qty; total конкурсы — прогресс по индивидуальному порогу (`individual_target` из результатов).
4. **main.py**: `auto_finish_contests` — per_sale уведомления (qty+бонус+plan_pct); total уведомления без изменений.
5. **Индексы `contests` таблицы**: `reward_mode` = col 22, `individual_targets` = col 23 (после `created_at`=21).

**Сессия 199 (2026-05-13) — БАГФИКСЫ + СОВМЕСТНАЯ МОТИВАЦИЯ:**
1. **Баг: `back_to_edit_sale` KeyError** — `data['sale_id']` → `data.get('sale_id')` с fallback к кешу продаж или `edit_sales_start` (`sales_handlers.py`).
2. **Баг: FakeCallback в тирах конкурса** — `_safe_tier_edit(callback, state, text, markup)` в `contests_handlers.py`: сначала пробует `callback.message.edit_text()`, при ошибке ищет `anchor_msg_id` из FSM и редактирует якорное сообщение. Применён в `_show_tier_bonus_step`, `_show_tier_pct_step`, `_show_period_step`.
3. **Баг: кнопка «Сменить магазин» не появлялась** для орг-пользователей без `trade_network` — `_build_cross_shop_screen` переписан: если `trade_network` пустой → использует `get_all_shops()` вместо `get_shops_by_network()`. `sale_select_network_shop` аналогично. Кнопка `allow_change=True` выставляется при 2+ магазинах в орге (`sales_handlers.py`).
4. **Фича: Совместный режим мотивации** (`commission_handlers.py`, `database.py`):
   - `motivation_extra_conditions` + колонка `calc_mode TEXT DEFAULT 'individual'`; автомиграция.
   - `ExtraConditionStates.selecting_calc_mode` — новый шаг 3/4 между min_sellers и coefficient.
   - Обработчик `coeff_calc_mode_selected` — кнопки «👤 Раздельный» / «🤝 Совместный».
   - `add_extra_condition(calc_mode=...)` — новый параметр.
   - `get_extra_conditions()` — SELECT добавлен `COALESCE(mec.calc_mode,'individual') as calc_mode` (индекс [12]).
   - `get_joint_bonus_adjustment(user_id, start_date, end_date)` — пул = SUM(комиссий всех продавцов в совместных магазинах). Математика: `joint_bonus = SUM(individual*coeff all sellers) = total_qty × motiv × coeff` — каждый получает одинаково.
   - `get_seller_total_earnings` — добавляет корректировку: `base + get_joint_bonus_adjustment(...)`.
   - `view_extra_conditions` — иконка режима 👤/🤝 рядом с каждым условием.

**Сессия 199 (2026-05-18) — БАГИ ПРОДАЖ + GOOGLE SHEETS OAUTH:**

**Корневые причины бага «продажи не сохраняются»:**
1. **`callback.answer()` без try/except** в `complete_sale` (до нашего фикса 7cbbd63): если Telegram отвечал «query is too old», outer except перехватывал исключение и удалял все `processed_sales` через `delete_sale`. Исправлено: `callback.answer()` и `get_user()` обёрнуты в try/except.
2. **`logger` не объявлен в `sales_handlers.py`**: при добавлении дебаг-логов использовали `logger.error()`, тогда как в файле объявлен только `import logging`. `NameError: name 'logger' is not defined` бросался ДО outer try, падал в aiogram error middleware. Исправлено: добавлен `import logging` в начало файла, все вызовы → `logging.error()`.
3. **Дополнительно исправлено** в той же сессии (коммит 7cbbd63): `_per_shop_breakdown` в `dashboard_handlers.py` использовал несуществующие колонки `s.quantity`/`s.total_price` → пустая сводка; `get_contest_manual_results` — ambiguous `shop_name` в JOIN.

**Диагностический метод:** временное `logging.error("[DEBUG complete_sale] ...")` в каждой ключевой точке хендлера — немедленно обнажило NameError в логах workflow.

**Google Sheets OAuth setup:**
- Тип OAuth клиента в Google Cloud Console: **«TVs and Limited Input devices»** — единственный тип, поддерживающий Device Flow (POST на `https://oauth2.googleapis.com/device/code`).
- Скачанный JSON имеет ключ `"installed"` — это нормально для этого типа.
- `client_id` и `client_secret` → Replit Secrets `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET`.
- `integration/auth/google_oauth.py`: `get_client_credentials()` читает из `os.environ`; `initiate_device_flow()` → user_code + verification_url; `poll_for_token()` — long-polling до подтверждения.
- GitHub `f7a76f8` · Amvera `7057da5`.

**Сессия 199 (2026-05-18) — ЗАДАЧА #9: ПОИСК ПО @USERNAME В СПИСКЕ ПОЛЬЗОВАТЕЛЕЙ + POST-MERGE SETUP:**
1. **`database.py`**: добавлена колонка `username TEXT` в `users` (CREATE TABLE + авто-миграция `ALTER TABLE`); `add_user()` и `update_user()` принимают `username=`.
2. **`admin_handlers.py`**: `_ADMIN_USERS_COLS` расширен на `username` (13-я колонка, индекс 12); поиск в `_build_admin_users_content` теперь включает `@username` в строку сравнения (с `lstrip('@')` для толерантности к вводу); кнопка в списке показывает `@username` если есть, иначе `(магазин)`.
3. **Все call-сайты `add_user`**: `handlers.py` (5 мест: super-admin, select_city callback, process_city message, shop_bot.db trial copy), `sales_handlers.py`, `reports_handlers.py`, `notifications_handlers.py`, `subscription_handlers.py`, `db_utils.py` (оба get_db и get_db_sync) — везде передаётся `username` (с guard `len > 12` для кортежей).
4. **`scripts/post-merge.sh`**: создан (`pip install -r requirements.txt`); зарегистрирован в `.replit [postMerge]` с таймаутом 60s — теперь при каждом мерже задачи-агента автоматически устанавливаются зависимости.
5. GitHub `5c7104b` · Amvera `bcd4ae2`.

**Сессия 199–72 (2026-05-18) — ЗАДАЧА #5: ПОИСК В ИЗБРАННОМ И НЕДАВНИХ (sale flow):**
1. **`states.py`**: добавлены `SearchStates.sale_favourites` и `SearchStates.sale_recent`.
2. **`sales_handlers.py`**: добавлены билдеры `_build_fav_list_content(fav_prods, cart, query="")` и `_build_recent_list_content(recent, cart, query="")` — возвращают `(text, markup)`, фильтрация по имени, кнопки 🔍/✖️/🛒/⬅️.
3. **Рефакторинг** `sale_show_favorites` и `sale_show_recent_handler`: сохраняют `anchor_msg_id` + `sale_srch_list_type` ('fav'/'recent') в FSM; используют новые билдеры.
4. **5 новых хендлеров**: `slr_fav_srch_start` / `slr_rec_srch_start` (запуск поиска), `slr_srch_cancel` (сброс, восстанавливает `MultipleSaleStates.adding_items`), `slr_fav_srch_process` / `slr_rec_srch_process` (обработка текста, `fsm_edit` + билдер).
5. GitHub `abb429c` · Amvera `a9b5b3f`.

**Сессия 199 (2026-05-18) — ЗАДАЧА #2 (смержена агентом): ПОМЕСЯЧНАЯ МОТИВАЦИЯ И АРХИВ:**
- Таблицы `motivation_schedule` (UNIQUE product_id+year+month) и `extra_conditions_schedule`.
- `get_effective_motivation_matrix(col_months)` — матрица всех товаров с мотивацией по месяцам.
- `set_motivation_for_month` / `get_motivation_for_month` (fallback на global).
- `set_extra_condition_for_month` / `get_extra_conditions_for_month`.
- `commission_handlers.py`: кнопка «📅 По месяцам», хендлеры выбора месяца, матрица (7 столбцов: 6 прошлых + следующий), ячейки `sched_cell_*`, архив `archive_months`.
- ИСПРАВЛЕН баг: `recalculate_month_earnings` теперь принимает конкретный year/month.
- GitHub (task agent) `5c7104b` (после мержа).

**Сессия 199 (2026-05-18) — ЗАДАЧА #1: ДИНАМИЧЕСКИЙ ПОИСК ВО ВСЕХ БОЛЬШИХ СПИСКАХ:**
1. **`states.py`**: добавлена группа `SearchStates` с 12 состояниями: `shop_commission`, `user_catfilt`, `shop_plans`, `user_plans`, `product_plans`, `category_plans`, `product_contests`, `category_contests`, `shop_reports`, `shop_inventory`, `product_inventory`, `shop_contacts`.
2. **`commission_handlers.py`**: поиск магазина (`coeff_srch_shop_*`, `SearchStates.shop_commission`) + поиск сотрудника в catfilt (`catfilt_srch_*`, `SearchStates.user_catfilt`).
3. **`reports_handlers.py`**: поиск магазина для периодического отчёта (`rep_srch_shop_*`, `SearchStates.shop_reports`).
4. **`contacts_handlers.py`**: поиск магазина (`contacts_srch_shop_*`, `SearchStates.shop_contacts`).
5. **`sales_plans_handlers.py`**: 4 поиска — магазин (`plnwiz_srch_shop_*`), сотрудник (`plnwiz_srch_user_*`), мультиселект категорий (`plnwiz_srch_cat_*`), мультиселект товаров (`plnwiz_srch_prd_*`). Хелперы `_show_plan_shop_list`, `_show_plan_user_list`. `_render_category_selection`/`_render_product_selection` получили параметр `query` и кнопку 🔍.
6. **`contests_handlers.py`**: поиск товара (`ct_srch_prd_*`, `SearchStates.product_contests`) + поиск категории (`ct_srch_cat_*`, `SearchStates.category_contests`). Хелперы `_show_contest_product_list`, `_show_contest_category_list`.
7. **`inventory_handlers.py`**: поиск магазина (`inv_srch_shop_*`, `SearchStates.shop_inventory`) + поиск товара (`inv_srch_prd_*`, `SearchStates.product_inventory`), хелпер `_show_inv_product_list`. Однoмагазинный путь в `add_inventory_start` тоже использует `_show_inv_product_list`.
8. **Паттерн**: везде используется `fsm_edit` + `anchor_msg_id`. После multiselect-поиска — `await state.set_state(SalesPlansStates.selecting_*)` для восстановления стейта toggle-хендлеров.
9. GitHub `98df9d8` · Amvera `0b13728`.

**Сессия 199 (2026-05-18) — ИСПРАВЛЕНИЕ КОРРЕКТИРОВОК КОНКУРСА:**
1. **Авто-значение с полными фильтрами** — добавлен метод `compute_contest_shop_auto_totals(contest_id)` в `database.py`. Использует тот же `_build_contest_sale_query()` что и `compute_contest_results` — с фильтрами по товарам/категориям/городам/пользователям. Ранее UI показывал упрощённый `SUM(sale_price * qty)` без фильтров конкурса.
2. **per_sale (тиры): корректировки недоступны** — `compute_contest_results` в ветке per_sale никогда не вызывал `get_contest_manual_results()`, корректировки молча игнорировались. Теперь при попытке открыть корректировку для per_sale конкурса — внятное сообщение с объяснением.
3. **Рефакторинг contest_manual_list / contest_manual_set_shop** — оба хендлера заменили самописный SQL на `compute_contest_shop_auto_totals()`.
4. GitHub `c2393e0` · Amvera `8350ba2`.

**Сессия 199 (2026-05-18) — КОРРЕКТИРОВКА ПОКАЗАТЕЛЕЙ КОНКУРСА В ЛЮБОЙ МОМЕНТ:**
1. **Переименование UX**: «✏️ Ручной ввод результатов» → «✏️ Скорректировать показатели».
2. **Кнопка добавлена в экран результатов** — доступна всегда, не только из карточки конкурса.
3. **contest_manual_list**: показывает авто-значение каждого магазина (🏪) или корректировку (✏️) с автором и датой.
4. **contest_manual_set_shop**: отображает «Авто (по продажам конкурса)» + текущую корректировку с историей.
5. **Удаление сообщений пользователя**: `await message.delete()` + try/except во всех 8 обработчиках текстового ввода конкурсов.
6. **Рефакторинг `_FakeCallback`**: 3 дублирующихся класса → 1 общий экземпляр в `contest_tier_bonus_entered`.
7. GitHub `3f1cf87` · Amvera `8350ba2`.

**Сессии 119–124 (2026-05-19) — МОНЕТИЗАЦИЯ: ПОЛНЫЙ ЦИКЛ:**

**Сессия 199 — `can_use_integrations` в тарифах:**
1. Добавлена колонка `can_use_integrations` в `subscription_plans` (миграция ALTER TABLE).
2. Платные планы: Базовый/Стандарт/Премиум → `can_use_integrations=1`; Бесплатный → 0.
3. `subscription_utils.py`: `check_integrations_permission(tg_id)` проверяет флаг.
4. `integration_handlers.py`: `integration_menu` закрыт через `check_integrations_permission`.
5. Все SELECT `subscription_plans` обновлены (7-й столбец = `can_use_integrations`).

**Сессия 199 — Триал = безлимит через `_has_active_trial()`:**
1. `subscription_utils.py`: `_has_active_trial(tg_id)` → True если `is_trial=1 AND end_date > now`.
2. `get_plan_limits()`: триал → немедленный return `_UNLIMITED` (минуя lookup плана 'Бизнес').
3. Устранена путаница: план триала 'Бизнес' не существует в `subscription_plans`, но `_has_active_trial()` перехватывает раньше.

**Сессия 199 — Trial upsell flow:**
1. `main.py`: `send_trial_expired_upsell(bot)` — ежечасно в :05; dedup через `subscription_reminder_log` (threshold=-1).
2. `database.py`: `get_recently_expired_trials()` — триалы истёкшие за последние 48ч.
3. `main.py`: `_TRIAL_FEATURES_LOST` — список потерянных функций; `_sub_markup()` — InlineKeyboardMarkup с «💳 Выбрать тариф» + «✅ Прочитано».
4. `send_payment_alerts`: расширены trial-specific reminders за 14/7/3/1d (список фич + кнопка).
5. GitHub `85476dc` · Amvera `ad56380`.

**Сессия 199 — Дифференциация тарифов + авто-отклонение СБП:**
1. **Тарифная сетка**: Базовый → `can_use_integrations=0` (Google Таблицы только Стандарт+).
2. `database.py`: always-running migration: `UPDATE subscription_plans SET can_use_integrations=0 WHERE name='Базовый'`.
3. **trial_plan 'Бизнес' → 'Премиум'**: `database.py` default_settings, `handlers.py` (2 места), `payment_system_admin.py` (2 места); always-running migration обновляет существующие БД.
4. `subscription_handlers.py`: добавлена строка «• Google Таблицы: ✅/❌» в статус подписки.
5. `subscription_utils.py`: `get_subscription_warning_message()` показывает сравнение Базовый/Стандарт/Премиум + для Базового без интеграций — конкретное сообщение.
6. `database.py`: `get_stale_pending_payments(hours=72)` → rows с telegram_id.
7. `main.py`: `auto_reject_stale_payments(bot)` — ежедневно 10:15; отклоняет pending СБП >72ч + уведомление юзеру через `_sub_markup()`.
8. GitHub `d3b2529` · Amvera `288b234`.

**Сессия 199 — Upsell-кнопка в интеграциях + квитанция при активации:**
1. `integration_handlers.py`: при `check_integrations_permission() = False` — редактирует сообщение с таблицей тарифов и кнопкой «💳 Выбрать тариф» (вместо `show_alert=True` без кнопок).
2. `payment_admin_handlers.py`: `confirm_payment_request` — пользователь получает полную квитанцию (тариф + сумма + дата истечения), данные тянутся из `subscription_plans`.
3. GitHub `3a9650d` · Amvera `46d94fa`.

**Сессия 199–132 — PERFORMANCE AUDIT: answer() + DB indexes + ⏳ indicators:**
1. **10 новых индексов** в `database.py` (create_tables): idx_sales_date, idx_users_city, idx_users_trade_network, idx_products_category, idx_subscriptions_user, idx_sales_plans_user, idx_notif_history_user, idx_sched_notif_dt, idx_plan_milestones — ускоряют выборки по дате/городу/пользователю.
2. **`answer("⏳ Загрузка...")` добавлен** в `_render_dashboard`, `reports_menu`, `view_ratings`, `report_today`, `report_my_shop`, `report_user_month`, `report_admin_month`, `my_plans` — тяжёлые экраны с несколькими DB-запросами.
3. **`answer()` перенесён перед DB** в 13 обработчиках: `my_schedule`, `my_schedule_nav` (salary_handlers), `my_plans`, `plnwiz_toggle_category`, `plnwiz_products_page` (sales_plans_handlers), `contest_toggle_category`, `ct_srch_shop_cancel`, `contest_toggle_shop` (contests_handlers), `_render_product_list`, `edit_product_choice`, `confirm_delete_product` (products_handlers), `user_inventory_menu` (inventory_handlers), `catfilt_toggle_category`, `catfilt_allow_all` (commission_handlers).
4. **`_show_schedule_matrix` (commission_handlers)**: перенесён pattern — answer() добавлен в `view_motivation_schedule` и `sched_page` ДО вызова хелпера; из самого хелпера `answer()` убран (дублирование).
5. **`edit_product_choice` / `confirm_delete_product`**: ошибка «не найден» изменена на `show_alert=True` (было без alert — плохой UX).
6. Принцип: `answer()` — первая строка если нет show_alert-валидации; сразу после последней валидации если есть; toggle-хендлеры — всегда первой строкой.
7. GitHub `b3c41f4` · Amvera `d04df11`. 45 импортов ✅.

**Сессия 199 — ВЕРИФИКАЦИЯ + GOOGLE SHEETS ДОКУМЕНТАЦИЯ:**
1. Верификация: все изменения за 2 дня присутствуют в коде (syntax check всех .py ✅, test_imports ✅).
2. git статус: local main = `48a148a` (Replit checkpoint); origin/main = `fb845fd` (stale tracking — нормально, deploy.sh пушит из /tmp/github-deploy отдельно). Расхождение 94 vs 50 коммитов — ожидаемо, не баг.
3. Документирован скоуп Google Sheets интеграции (см. ловушку #30 ниже).
4. Нет нового кода — только документация и верификация.

**Сессии 135–143 (2026-05-19) — PERFORMANCE OPTIMIZATIONS (3 шага):**

**Шаг 1 — PRAGMA WAL (database.py `get_connection()` + `create_tables()`):**
- `PRAGMA journal_mode=WAL` — параллельные читатели без блокировок
- `PRAGMA synchronous=NORMAL` — баланс надёжность/скорость
- `PRAGMA cache_size=-8000` — 8 MB page cache на соединение
- `PRAGMA temp_store=MEMORY` — временные таблицы в памяти
- `PRAGMA mmap_size=134217728` — 128 MB mmap для read-heavy путей
- `busy_timeout=10000` — 10 с ожидания при блокировке (без SQLITE_BUSY краша)
- GitHub `4a7f5ec` · Amvera `...`

**Шаг 2 — AsyncDatabase wrapper (db_utils.py):**
- Класс `AsyncDatabase` с `__getattr__` → `asyncio.to_thread(fn, *args, **kwargs)`
- `wrap_db(db)` — явная обёртка для inline `Database()`
- `get_db()` стал `async`, возвращает `AsyncDatabase`
- **577 `await`** добавлено во все handler-файлы (все вызовы `current_db.*`)
- 5 sync-хелперов переписаны в async: `build_admin_dashboard`, `build_user_dashboard`, `_build_sale_product_list_content`, etc.
- `hints.py` / `maybe_refresh_username` — sync-совместимость через `getattr(db, '_db', db)`
- Inline `Database()` в `admin_handlers`, `handlers`, `inventory_handlers`, `notifications_handlers` — обёрнуты через `wrap_db()`
- `main.py` `send_daily_reports` — полная async-конвертация
- 45/45 test_imports ✅
- GitHub `106222c` · Amvera `7c407f8`

**Шаг 3 — Thread-local connection pool + asyncio.gather() (database.py + dashboard_handlers.py):**
- `_conn_pool = threading.local()` в `database.py`
- `_get_pooled_conn(db_file)` — создаёт соединение с `check_same_thread=False` + все PRAGMA; переиспользует в том же потоке
- `_PooledConn` — обёртка, `close()` = только `rollback` (не разрывает соединение)
- `get_connection()` возвращает `_PooledConn`
- **187 вхождений** `sqlite3.connect(self.db_file...)` в 200+ методах заменены на `self.get_connection()`
- `build_admin_dashboard`: 7 запросов → `asyncio.gather(*_base_tasks, *_salary_tasks, return_exceptions=True)` — все параллельно (~80ms → ~10ms)
- `build_user_dashboard`: 8 запросов → `asyncio.gather(...)` — все параллельно (~80ms → ~10ms)
- 45/45 test_imports ✅ · runtime pool tests ✅ · runtime AsyncDatabase tests ✅
- GitHub `0ffc019` · Amvera `7355f9a`

**Сессия 199 (2026-05-07) — TOP-3 FIX + АУДИТ:**
1. **`report_full`** — убраны `[:3]` у категорий и магазинов; теперь все с guard `> 3500` / `> 3700`.
2. **`report_my_shop`** — убран `[:3]` у товаров; guard `> 3700` с сообщением «остальные товары в Excel».
3. **`generate_period_report`** — убран `[:3]` у товаров; аналогичный guard.
4. **Комплексный аудит** — 36 модулей, 559 тестов, 0 bare except, 0 print(), 0 hardcoded secrets, WAL+busy_timeout, APScheduler misfire/coalesce/max_instances=1, все соединения закрываются.
5. **Нераздражающий techdebt**: 3 строки `BROADCAST DEBUG` в `notifications_handlers.py` (logging.info) — не баги, но стоит убрать перед масштабированием.
6. GitHub `077ce67` · Amvera `790cabe`. 559 тестов ✅.

**Сессия 228 (2026-05-27) — РЕФЕРАЛЫ + НАДСТРОЙКИ + PDF + ФОТО/ОПИСАНИЕ ТОВАРОВ:**

**D) Реферальная программа** (`referral_handlers.py`, `database.py`):
- Новая таблица `referrals` в `shop_bot.db`: `referrer_id`, `referred_id`, `bonus_applied`.
- Deep-link: `/start ref_TELEGRAMID` → парсится в `handlers.py` (`select_city` callback + `process_city` message handler); сохраняется как `REF_{id.upper()}` в FSM.
- Бонус начисляется через `apply_referral_bonus(referrer_id)` → `_extend_subscription_by_days(tg_id, 30)` при создании организации приглашённым.
- Новые методы DB: `create_referral(referrer_id, referred_id)`, `apply_referral_bonus(referrer_id)`, `_extend_subscription_by_days(tg_id, days)`, `get_referral_stats(tg_id)` → (total, applied, bonus_days).
- Экран: `subscription_referral` callback → `referral_router`; ссылка через `get_me()` с fallback.

**E) Надстройки к подписке** (`addon_handlers.py`, `database.py`):
- Новая таблица `subscription_addons` в `shop_bot.db`: `addon_type`, `quantity`, `expires_at`.
- Типы: `extra_shops` (150₽/30д, +1 магазин), `extra_products` (100₽/30д, +100 товаров).
- `addon_router`: `subscription_addons` → меню; `addon_buy_shops_1` / `addon_buy_products_1` → экран покупки; оплата через стандартный `confirm_payment_request` с plan_type `addon_shops_1` / `addon_products_1` (prefix `addon_`).
- Новые методы DB: `create_subscription_addon(user_id, addon_type, qty, days)`, `get_active_addons(user_id)`, `get_addon_totals(user_id)` → dict {'extra_shops': N, 'extra_products': N}.
- `subscription_utils.py`: `get_plan_limits()` суммирует addon-значения с лимитами тарифа.
- `confirm_payment_request` (payment_admin_handlers.py): plan_type начинающийся с `addon_` → вызывает `create_subscription_addon` вместо `create_subscription`.

**F) PDF экспорт** (`pdf_utils.py`, `reports_handlers.py`):
- `generate_pdf_report(org_name, start_date, end_date, sales_rows, summary, top_sellers, top_products)` → path|None — строит A4 PDF с шапкой, таблицей продаж, топами.
- `generate_pdf_for_report(db, scope, start_date, end_date, scope_value)` → path|None — фасад: делает SQL-запросы, собирает данные, вызывает `generate_pdf_report`.
- Требует `reportlab` (в requirements.txt). Кириллица через системные шрифты с fallback на Helvetica.
- Кнопки `download_pdf_full`, `download_pdf_period`, `download_pdf_shop`, `download_pdf_user` в `reports_handlers.py`.

**G) Фото и описание товаров** (`products_handlers.py`, `database.py`):
- Таблица `products`: новые колонки `photo_file_id TEXT` и `description TEXT` — добавлены `ALTER TABLE IF NOT EXISTS` миграцией в `create_tables()`.
- `add_product` / `update_product` принимают `photo_file_id=None` и `description=None`.
- В карточке товара показывается фото (если есть) + описание. При продаже `sale_product_{id}` — также отображается фото/описание.
- `products_handlers.py`: `ProductStates` расширены `entering_photo` + `entering_description`; кнопка «📷 Фото» и «📝 Описание» в меню редактирования.

**Деплой:** GitHub `91ef931` · Amvera `ef1edd7` · 48/48 test_imports ✅

---

**Сессия 200 (2026-05-26) — ФИЛЬТРЫ УВЕДОМЛЕНИЙ + GOOGLE SHEETS FIXES + ПЕРЕНОС ЭКСПОРТОВ:**

**1. Фильтры получателей уведомлений** (`notifications_handlers.py`, `main.py`):
- После ввода текста → экран «👥 Выберите получателей»: Всем / По магазину / По роли.
- FSM-ключи: `ntf_rcpt_type` ('all'/'shop'/'role'), `ntf_rcpt_filter` (имя магазина или роль), `ntf_rcpt_label` (display).
- Превью с числом получателей (`_count_notif_recipients`) перед отправкой.
- Кнопка «↩️ Изменить получателей» для возврата.
- Callbacks: `ntf_rcpt_all`, `ntf_rcpt_shop`, `ntf_rcpt_s_{shop}`, `ntf_rcpt_role`, `ntf_rcpt_r_{role}`, `ntf_change_rcpt`.
- `admin_confirm_send_now`: читает `ntf_rcpt_type`/`ntf_rcpt_filter` из FSM, фильтрует `target_by_db`.
- `process_schedule_time`: сериализует фильтр в `recipients_list` JSON (`{"type":"shop","filter":"ЦУМ"}`).
- `check_scheduled_notifications` (main.py): разбирает `notif[5]` (recipients_list JSON) и применяет фильтр при отправке. Обратная совместимость: NULL → отправка всем.
- Супер-admin видит только «Всем» (кросс-орг фильтрация не реализована).

**2. Google Sheets `invalid_grant` обработка**:
- `integration/auth/google_oauth.py`: класс `OAuthTokenRevokedException`; `refresh_access_token` детектирует `error_code == 'invalid_grant'` и поднимает его вместо сырого `ValueError`.
- `integration/manager.py`: `_ensure_valid_token` перехватывает `OAuthTokenRevokedException` → `db.update_integration_connection(conn_id, enabled=0)` + лог → `ValueError` с дружелюбным текстом. Новый хелпер `_normalize_gs_error(e, db, conn_id)`: конвертирует двух-аргументный `google.auth.RefreshError` (формат `('invalid_grant:...', {...})`) → чистый `ValueError`; вызывается в `_run_export` и `_run_export_with_result`.
- `_run_export` при отозванном токене: `_notify_admins` получает «🔑 Google Sheets: требуется переподключение» вместо сырого tuple-текста.
- `sales_handlers.py`: отозванный токен → «🔑 Требуется переподключение → Управление орг. → Интеграции».
- `integration_handlers.py` sync_motivation: детект `invalid_grant` или 'Переподключите' → инструкция пользователю.

**3. Перенос экспортов между подключениями** (`integration_handlers.py`, `database.py`):
- Кнопка «📤 Перенести экспорты» на экране подключения (только если `len(exports) > 0`).
- `gs_move_exports_{from_id}` → список других подключений (с иконкой ✅/❌).
- `gs_move_exp_to_{from_id}_{to_id}` → экран подтверждения с превью экспортов (до 8 строк).
- `gs_move_exp_ok_{from_id}_{to_id}` → `db.move_exports_to_connection(from_id, to_id)` → отчёт.
- `database.py`: `move_exports_to_connection(from_conn_id, to_conn_id)` — `UPDATE integration_exports SET connection_id=? WHERE connection_id=?`, возвращает число перенесённых.
- Все псевдонимы, mapping, lookup, schedule сохраняются без изменений.
- GitHub `0983dfd` · Amvera `42fb73b`. 703 теста ✅, 0 проблем callback-анализатора.

---

**Сессии 344–345 (2026-06-02) — АУДИТ CALLBACK.ANSWER() + БАГИ РАСПИСАНИЯ:**

**Сессия 344 — аудит и фиксы:**
1. **Структурный баг `get_absence_days_map`** (`web/routes/schedule.py`, `web/routes/absences.py`): метод возвращает `{user_id: {day_num: {...}}}`, но вызывающий код не разворачивал внешний ключ user_id. Исправлено: добавлен `.get(user_id, {})` при извлечении дня-карты во всех call-сайтах.
2. **`state.clear()` в `handlers.py`** (строка 177): заменён на `clear_state_keep_org(state)` — сохраняет `selected_org_db` при очистке состояния.
3. **`he()` в `sales_handlers.py`**: добавлен в 3 местах (строки 1189, 1601, 1699) для имён товаров в HTML-сообщениях.
4. **49/49 test_imports ✅** после всех фиксов.
5. GitHub `7f4a32c` · Amvera `9e28ec1`.

**Сессия 345 — полный аудит callback.answer():**
1. **6 двойных ответов** исправлены (вызывалось `callback.answer()` дважды): `addon_buy`, `addon_upload_proof_start`, `reset_invite_handler`, `gs_exp_sync_week_confirm`, `edit_parameter_choice`, `salary_fill_month_confirm` — в каждом убран лишний вызов.
2. **Отсутствующий answer + неверный fsm_edit** в `absence_handlers.py` (3 хендлера `abs_my`, `abs_hist`, `abs_new`): исходный код неправильно вызывал `fsm_edit(callback, text, markup)` (функция принимает message, не callback). Заменено на `await callback.answer()` + `await callback.message.edit_text(text, reply_markup=markup)`.
3. **49/49 test_imports ✅** — без ошибок.
4. GitHub `ef29cac` · Amvera `933ef27`.

---

**Сессии 234–235 (2026-05-31) — ИСПРАВЛЕНИЕ ВИДИМОСТИ МАГАЗИНА «СИСТЕМНЫЙ»:**
1. `database.py`: добавлен параметр `include_system=False` в `get_all_shops()`, `get_shops_with_stats()`; все UNION-части (users + shops + inventory) фильтруют «Системный»/«System» для обычных пользователей. Супер-администратор передаёт `include_system=True`.
2. `database.py`: `get_inventory_shops()` — аналогично фильтрует «Системный».
3. `admin_handlers.py`: `_get_shops_with_stats()` + все 14 точек вызова обновлены под новый параметр.
4. GitHub `974aa37` · Amvera `c496f12`.

**Сессия 236 (2026-05-31) — ПРЕСЕТ: ИСПРАВЛЕНИЕ ОТСУТСТВИЯ ТЦ БУМ:**
1. `admin_handlers.py`: `invite_preset_role_handler` и `invite_preset_shop_handler` — добавлен третий UNION-блок `inventory` к UNION-запросу. Пресет теперь показывает магазины из users + shops + inventory.
2. GitHub `f86e21d` · Amvera `f86e21d`.

**Сессия 237 (2026-05-31) — АУДИТ + ФИКС filter_utils + LOW-STOCK + asyncio.gather:**
1. **`filter_utils.py` (Bug fix)**: `get_available_filter_values()` — список магазинов объединяет три источника: `users.shop_name`, `shops.name`, `inventory.shop_name` — каждый с отдельным `try/except`. Магазины без сотрудников (типа ТЦ Бум) теперь появляются в панели 🔍 Фильтр в Отчётах / Рейтингах / Прогрессе планов.
2. **`dashboard_handlers.py` (Bug fix)**: `_low_stock_count()` для scope city/network теперь ищет магазины в `users` и в `shops` (с `try/except` fallback). Инвентарные магазины без сотрудников включены в подсчёт.
3. **`commission_handlers.py` (Оптимизация)**: добавлен `import asyncio`; три хендлера переведены на `asyncio.gather()`: `view_motivations` (motivations+products параллельно), `remove_motivation_start` (то же), `remove_motiv_cat_selected` (categories + motivations + products — все три параллельно).
4. **`sales_plans_handlers.py` (Оптимизация)**: `plnwiz_metric_selected` — `get_all_categories()` + `get_all_products()` параллельно через `asyncio.gather()`.
5. GitHub `cc5507d` · Amvera `f4792e4`. 48/48 test_imports ✅.

---

## 7. ТИПИЧНЫЕ ЛОВУШКИ

1. **`clear_state_keep_org` ПОСЛЕ `fsm_edit`** — не до! Иначе anchor_msg_id теряется.
2. **Инициализировать переменные перед try/except** — если используются снаружи блока.
3. **`state.clear()` запрещён** → только `clear_state_keep_org(state)`.
4. **Пользовательские строки в HTML** → всегда `he(var)`. Тексты кнопок — не нужен.
5. **`get_users_for_notifications()`** → `[0]` = внутренний users.id, `[1]` = telegram_id.
6. **`scheduled_notifications.created_by`** хранит users.id (не telegram_id). JOIN = `ON sn.created_by = u.id`.
7. **Новый роутер** → зарегистрировать в main.py; новый модуль → добавить в test_imports.py.
8. **callback_data + кириллица** → `safe_cb(prefix, value)`, не f-строка.
9. **Планировщик** → `_get_scheduler_db_paths()` для итерации всех тенантов.
10. **plan_notifications.py** — lazy-импорт (внутри функций), не на уровне модуля.
11. **Database.db_file** (не .db_path!) — атрибут пути к файлу.
12. **`get_user_org_scope()`** → `(scope_type, list[str])` НЕ `(str, str)`.
13. **`clear_state_keep_org(state, extra_keys=[...])`** — для сохранения доп. ключей.
14. **InlineKeyboardButton.text** — Telegram НЕ парсит HTML. `he()` не нужен.
15. **`get_sales_ranking()`** → 8 колонок; 8-я = `u.id`; `row[:7]` для старого 7-кол. unpacking.
16. **Время на Amvera — UTC.** `datetime.now()` = UTC. Для отображения — `timezone_utils`.
17. **`shift_templates`**: weekday 0=Пн, 6=Вс. `date.weekday()` — та же нумерация.
18. **slr_te_/slr_tw_**: пикеры идут напрямую к slr_etw_ и slr_day_ (без промежуточного экрана).
19. **Excel лимит 50000 строк**: `get_sales_report(limit=50000)` во всех download-хендлерах.
20. **`generate_excel_report` seller detection**: `len(row) >= 11` = admin report (11 cols); `== 9` = user report.
21. **Telegram лимит сообщения 4096 символов**: guard'ы на уровне магазинов (`> 3600`) и товаров (`> 3700`), сообщение с подсказкой «скачайте Excel».
22. **`sales_handlers.py` НЕ имеет глобального `logger`** — файл использует `import logging` + `logging.error()`. Если добавить `logger.error()` без объявления `logger = logging.getLogger(...)` → `NameError` вне try/except → outer except удалит все `processed_sales`. Всегда использовать `logging.xxx()`.
23. **`complete_sale` outer except удаляет продажи** — любой необработанный exception внутри `try:` блока ПОСЛЕ `add_sale` вызовет `delete_sale(sale_id)` для каждого `processed_sales`. Все сетевые вызовы (answer, edit_text, bot.send_message, интеграции) должны быть обёрнуты в `try/except`. Текущее состояние: get_user(), callback.answer(), edit_text, GS-интеграция, notifications — все защищены.
24. **Google Sheets OAuth**: тип клиента в Google Cloud = **«TVs and Limited Input devices»** (Device Flow). Скачанный JSON будет с ключом `"installed"` — это нормально. `client_id` + `client_secret` → Replit Secrets `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET`. Читаются в `integration/auth/google_oauth.py` через `get_client_credentials()` из `os.environ`.
25. **`notification_settings.shift_sale_alerts`** — индекс 11 (после created_at[9], updated_at[10]); добавлен миграцией `ALTER TABLE`. В `get_notification_settings()` читается как `bool(settings[11]) if len(settings) > 11 else True`.
26. **`subscription_plans.can_use_integrations`** — **Базовый=0, Стандарт/Премиум=1**. Always-running migration в `_initialize_default_data()` принудительно держит Базовый=0. Не менять без проверки migration-блока (строка ~861 в database.py).
27. **`trial_plan` = 'Премиум'** (не 'Бизнес' — этого плана нет в БД). Триал проверяется через `_has_active_trial()` → `_UNLIMITED`, не через lookup плана. Fallback в handlers.py и payment_system_admin.py: `settings.get('trial_plan', 'Премиум')`.
28. **`subscription_reminder_log` threshold=-1** — специальный ключ для upsell-сообщения «триал истёк». Остальные ключи: 14, 7, 3, 1 (дней до истечения). `get_recently_expired_trials()` возвращает trials с end_date в последние 48ч.
29. **`auto_reject_stale_payments`** — ежедневно в 10:15; использует `db.get_stale_pending_payments(hours=72)` и `db.reject_payment_request(req_id, admin_id=0)`. admin_id=0 означает авто-отклонение (не конкретный admin).
30. **Google Sheets — скоуп ПО ОРГАНИЗАЦИИ, не по пользователю.** `integration_connections` и `integration_exports` хранятся в `org_*.db` (не personal). Настраивает только admin/owner с тарифом Стандарт+ (`is_any_admin` + `check_integrations_permission`). После настройки ВСЕ продажи/инвентарь ОТ ЛЮБОГО пользователя орга автоматически пишутся в таблицу — через `trigger_export(current_db, 'sales', event_data)` в `complete_sale`. Рядовые пользователи (sellers) не видят меню интеграций и ничего не настраивают — их продажи попадают в GS автоматически. Поле `seller_name` в экспорте = кто сделал продажу.
31. **`get_db()` возвращает `AsyncDatabase`** — все вызовы методов требуют `await`. Без `await` получаешь coroutine, а не данные. В APScheduler-задачах использовать `get_db_sync()` → возвращает `Database` (sync, без await).
32. **`wrap_db(db)` обязателен** для inline `Database('data/shop_bot.db')` в обычных handlers — иначе методы не будут async. Исключение: payment/subscription handlers, где `Database` допустим напрямую (всегда sync контекст).
33. **Thread-local pool**: каждый рабочий поток (`asyncio.to_thread`) получает своё соединение. `_PooledConn.close()` НЕ закрывает соединение — только откатывает незакрытые транзакции. Соединение живёт пока живёт поток. При добавлении нового метода в `Database` — используй `self.get_connection()`, не `sqlite3.connect(self.db_file)`.
34. **`asyncio.gather()` с `return_exceptions=True`**: используй в dashboard и любых экранах с 3+ независимыми DB-запросами. Всегда проверяй каждый результат: `if not isinstance(result, Exception)`. Паттерн: `_r = lambda i, default=None: results[i] if not isinstance(results[i], Exception) else default`.
35. **`filter_utils.py` — магазины без сотрудников**: `get_available_filter_values()` собирает список магазинов из трёх таблиц: `users.shop_name`, `shops.name`, `inventory.shop_name`. Если новый магазин добавлен только в `inventory` (0 сотрудников) — он появится в фильтре. При изменении логики — обязательно поддерживать все три источника. Инвалидация: `invalidate_filter_values_cache(db_path)` при добавлении/удалении/переименовании магазина.
36. **`_low_stock_count()` в dashboard**: при scope city/network магазины ищутся в `users` и `shops` таблицах. Таблица `shops` может не иметь сотрудников, но хранит city/trade_network — без неё inventory-only магазины исчезают из подсчёта низких остатков. Fallback `try/except` если таблица `shops` отсутствует.
37. **`get_absence_days_map(year, month, user_id=None)`** → `{user_id: {day_num: {type, status, id}}}` — ключ первого уровня = user_id (int). Вызывающий код ОБЯЗАН делать `.get(user_id, {})` для получения `{day_num: {...}}`. Без этого получишь dict с user_id-ключами вместо дня-карты.
38. **`fsm_edit(msg_or_cb, text, markup)` НЕ вызывается в callback-handlers** — функция принимает `message`, не `callback`. В callback-хендлерах: `await callback.answer()` + `await callback.message.edit_text(text, reply_markup=markup, parse_mode=...)`.
39. **Email-only user identity**: `synthetic_tg_id = -(10_000_000 + cred_id)` — хранится в `web_credentials.synthetic_tg_id`; используется как `tg_id` в JWT. При привязке к Telegram (`/setweblogin`) `telegram_id` в `web_credentials` обновляется. `org_db` для email-only берётся из `web_credentials.org_db` — НЕ из `user_org_mapping`.
40. **YANDEX_SMTP_PASSWORD** — это **пароль приложения** (16 символов), НЕ пароль аккаунта Яндекс. Генерируется в Яндекс ID → Безопасность → Пароли приложений. Обычный пароль Яндекс через SMTP не работает. `web/email_utils.is_configured()` проверять перед любым SMTP-вызовом; при отсутствии секретов `/register` и `/auth/reset` возвращают HTTP 503.

---

## 9. ВЕБ-ИНТЕРФЕЙС

### Стек и запуск
```
FastAPI + Uvicorn (порт 5000)  |  Jinja2 шаблоны  |  Tailwind CSS CDN
HTMX (динамические запросы)   |  Alpine.js (реактивность UI)
Chart.js (графики дашборда)    |  openpyxl (Excel импорт/экспорт)
```
Запуск: `main.py` запускает `uvicorn` через `threading.Thread` рядом с ботом.
Шаблоны: `web/templates/` — `base.html` (сайдбар, nav) + подпапки per-роут.
Статика: `web/static/` (авто-создаётся при старте).

### Маршруты (все файлы в `web/routes/`)
| Файл | URL prefix | Доступ | Назначение |
|------|-----------|--------|------------|
| `auth_routes.py` | `/login`, `/logout` | public | Telegram Login Widget + редирект на `/` |
| `email_auth.py` | `/register`, `/auth/email`, `/auth/verify`, `/auth/resend-verify`, `/auth/reset`, `/auth/reset/confirm`, `/settings/email-*` | public/auth | Email+пароль регистрация, вход, верификация, сброс пароля |
| `dashboard.py` | `/dashboard` | all roles | Сводка продаж, графики |
| `sales.py` | `/sales`, `/sales/export.xlsx` | all roles | Продажи + Excel-экспорт |
| `products.py` | `/products`, `/products/import` | all roles | Каталог + Excel-импорт |
| `inventory.py` | `/inventory`, `/inventory/export.xlsx` | all roles | Остатки по магазинам |
| `reports.py` | `/reports` | admin | Аналитика по продукту/категории/магазину/продавцу |
| `rankings.py` | `/rankings` | all roles | Рейтинги продавцов/магазинов/городов |
| `staff.py` | `/staff`, `/staff/{id}` | admin | Сотрудники + профиль |
| `plans.py` | `/plans`, `/plans/new`, `/plans/{id}` | admin | Планы продаж CRUD |
| `salary.py` | `/salary`, `/salary/export.xlsx` | admin | Зарплата + Excel |
| `schedule.py` | `/schedule` + 5 POST-эндпоинтов | admin | График смен + шаблоны |
| `contests.py` | `/contests`, `/contests/new`, `/contests/{id}` | admin | Конкурсы CRUD |
| `settings.py` | `/settings` + `/settings/rotate_invite` + `/settings/save_invite_preset` | all | Настройки уведомлений + invite |
| `integration.py` | `/integration` + auth endpoints | admin (Стандарт+) | Google Sheets |
| `payments.py` | `/payments`, `/payments/{id}/confirm|reject` | **super_admin only** | Управление платежами |
| `absences.py` | `/absences`, `/absences/add`, `/absences/update`, `/absences/settings` | all roles (admin видит всех) | Отсутствия/заявки |

### Auth flow
```
# Telegram Login Widget
GET /login → Telegram Login Widget (JS) → POST /login (с hash verification)
    → JWT cookie «web_session» (HS256, 7д) → redirect /dashboard
    → claims: sub=telegram_id, name, role, org_db

# Email + пароль
GET /register?invite=CODE → форма (email, пароль, имя) → POST /register
    → create web_credentials + send_verification_email() → GET /auth/verify?token=…
    → email_verified=1 → redirect /dashboard

GET /auth/email → форма входа → POST /auth/email (email + пароль)
    → verify_password(PBKDF2) → JWT cookie → redirect /dashboard

POST /auth/reset → email → send_reset_email() → GET /auth/reset/confirm?token=…
    → новый пароль → hash_password() → redirect /auth/email
```
CSRF: derived from JWT secret + telegram_id. `get_csrf_token(request)` / `verify_csrf_token(request, token)`.
Rate limit: `check_rate_limit(f"email_auth:{ip}", 5, 600)` → 5 попыток / 10 мин / IP (email_auth.py).

### Паттерны веб-кода (обязательны)
```python
# Auth check (первая строка каждого роута):
user = get_session_user(request)
if not user: return RedirectResponse(url="/login", status_code=302)

# DB access (sync! не AsyncDatabase):
db = get_web_db(int(user["sub"]), user.get("org_db"))

# Template response (request — ПЕРВЫЙ аргумент):
return request.app.state.templates.TemplateResponse(request, "name.html", ctx)

# POST → redirect (303, не 302):
return RedirectResponse(url="/page?saved=1", status_code=303)

# CSRF в роуте:
from web.auth import get_csrf_token, verify_csrf_token
ctx["csrf_token"] = get_csrf_token(request)
form = await request.form()
if not verify_csrf_token(request, form.get("csrf_token", "")): ...

# CSRF в шаблоне:
<input type="hidden" name="csrf_token" value="{{ csrf_token }}">
```

### Flash-сообщения (через query params, не сессию)
```
?saved=1          → «Сохранено»
?error=text       → красный баннер
?imported=N       → «Импортировано N товаров»
?msg=confirmed_N  → «Заявка #N подтверждена»
?msg=rejected_N   → «Заявка #N отклонена»
```

### Jinja2 globals (web/app.py)
```python
templates.env.globals['bot_username'] = lambda: bot_holder.get_username() or ''
templates.env.globals['pending_payments_count'] = get_pending_count  # из payments.py
```
Фильтры: `fmt_date` (YYYY-MM-DD → DD.MM.YYYY), `fmt_currency` (→ «1 234 ₽»).

### Ограничения веб vs бот
| Функция | Бот | Веб |
|---------|-----|-----|
| Запись продаж | ✅ | ❌ (intentional — mobile-first) |
| Корректировки зарплаты (бонусы/удержания) | ✅ | ❌ (только просмотр) |
| Управление сотрудниками (добавить/уволить/роль) | ✅ | ❌ (только просмотр) |
| Уведомления (отправить/запланировать) | ✅ | ❌ (только настройки) |
| Управление магазинами | ✅ | ❌ |
| Планы продаж CRUD | ✅ | ✅ |
| График смен | ✅ | ✅ |
| Конкурсы CRUD | ✅ | ✅ |
| Отчёты/рейтинги | ✅ | ✅ |
| Excel-экспорт | ✅ | ✅ |
| Excel-импорт товаров | ✅ | ✅ |
| Google Sheets интеграция | ✅ | ✅ |
| Управление платежами | ✅ | ✅ (super_admin) |
| Invite-система | ✅ | ✅ |

### Известные риски
- `_import_sessions` (products.py) и `_device_flow` (integration.py) — in-memory dict, теряются при рестарте
- Pagination в sales/reports — in-memory после fetch всех записей (медленно при 10k+ продаж)
- Платежи (`payment_requests`) — всегда в `data/shop_bot.db`, всегда через `sqlite3.connect` напрямую (не `get_web_db`)

---

## 10. ЧЕКЛИСТ ПЕРЕД ДЕПЛОЕМ

```bash
# 1. Импорт-аудит (50 модулей):
python test_imports.py   # должно быть: Итог: 50 ОК, 0 ошибок

# 2. Синтаксис:
python -c "
import ast, os
errors = []
for fn in os.listdir('.'):
    if fn.endswith('.py') and not fn.startswith('test_'):
        try:
            with open(fn) as f: ast.parse(f.read())
        except SyntaxError as e:
            errors.append(f'{fn}: {e}')
print('✅ OK' if not errors else '\n'.join(errors))
"

# 3. Деплой (GitHub + Amvera):
bash deploy.sh "commit message"

# Только GitHub:
bash deploy.sh "commit message" --no-amvera
```

---

## 11. КОНКУРЕНТНЫЙ АНАЛИЗ И СТРАТЕГИЯ МАСШТАБИРОВАНИЯ

> Полный файл: `COMPETITIVE_ANALYSIS.md` (корень проекта)
> План миграции на PostgreSQL: `.agents/memory/postgres-migration.md`

### Позиционирование продукта

**Целевая ниша:** малый ритейл, 2–30 сотрудников, 1–10 магазинов, Telegram как основной инструмент коммуникации команды.

**Итоговая оценка: 7.7/10**

| Критерий | Оценка | Комментарий |
|---|---|---|
| Функциональная полнота | 8/10 | Для ниши — практически всё; нет ОФД и штрихкодов |
| Техническое качество | 7/10 | Хорошая архитектура, но SQLite-потолок |
| Безопасность | 8/10 | Всё основное закрыто (5 раундов аудита) |
| UX / онбординг | 9/10 | Telegram-нативность — главное конкурентное оружие |
| Цена/возможности | 9/10 | 333₽/мес Премиум vs 500–3000₽/польз у конкурентов |
| Масштабируемость | 5/10 | SQLite потолок при 50+ одновременных пользователях |
| Рыночное позиционирование | 8/10 | Чёткая ниша, понятный продукт |

### Ключевые USP

1. **Telegram-native** — нулевое трение онбординга, продавцы уже в мессенджере
2. **Цена в 3–10x ниже** рынка (МойСклад, RetailCRM, Poster)
3. **Зарплатная прозрачность** — продавец видит Оклад + Мотивация + Итого в реальном времени; уникально на рынке
4. **Геймификация** — конкурсы с авто-расчётом, milestone-алерты, рейтинги
5. **Telegram-уведомления о продажах коллег** — прямо в мессенджер, не Email

### Критические слабые места

| Разрыв | Влияние |
|---|---|
| Нет 54-ФЗ / ОФД | Нельзя быть основной кассой для B2C наличных |
| Нет штрих-кодирования | При 500+ SKU ручной поиск замедляет кассира |
| Нет REST API | Нельзя подключить 1С / маркетплейс / интернет-магазин |
| SQLite — потолок при 50+ пользователях | Блокирует рост и горизонтальное масштабирование |

### Топ-5 приоритетов для роста

1. Штрих-кодирование через камеру (camera-barcode)
2. REST API / Webhook-out (открывает 1С, маркетплейсы)
3. Мультивалютность (СНГ-рынок)
4. Базовая CRM / клиентская база
5. Миграция на PostgreSQL (снятие технологического потолка)

### PostgreSQL — триггеры для начала миграции

- ≥ 30 платящих организаций активно пользуются системой, **или**
- В логах появляются `sqlite3.OperationalError: database is locked`, **или**
- Нужна горизонтальная репликация (несколько инстансов сервера)

**Стратегия:** schema-per-org в PostgreSQL + asyncpg + dual-write фаза + рефакторинг 279 методов.
Оценка: ~12–18 недель full-time. Полный план: `.agents/memory/postgres-migration.md`

---

## Сессия: универсальный конфигуратор импорта GS + фикс инверсии мотивации (2026-06-16)

**Мотивация — инверсия строк/столбцов.** Реальная структура листа: **строки=модели, столбцы=сети (chains)**. Раньше читалось транспонированно → пустые/неверные бонусы.
- `google_sheets.py`: новый `read_motivation_table(config, sheet, header_row, model_col, bonus_col_map: dict[int,str], rrp_col=None)` → `list[dict(model, rrp, bonuses={chain: float})]`. Старый `read_motivation_rows` — DEPRECATED.
- `manager.py`: `sync_motivation_from_sheet` (новая сигнатура; `clear_bonus_cache`+upsert; возвращает `{synced, models, sheet, chains}`); `get_motiv_config`/`save_motiv_config` (в `conn config['motiv_config']`); `run_motiv_sync_from_config`.

**Бот-визарды (кликабельные по реальным данным, по образцу экспортного «update_cell матрица»).**
- Мотивация (`gs_mtv_*`): строка-шапка → колонка модели → колонки-бонусы (мульти) → колонка РРЦ (опц.) → синхронизация+сохранение. «⚡ Быстрая синхронизация» при наличии config. `gs_show_motiv` динамичен по chains.
- Импорт (`gs_impc_*`): кликабельная привязка полей к реальным колонкам → 1-based → 0-based `col_mapping` → `run_import`. Префиксы `gs_impc_`/`gs_imptyp_` не конфликтуют с `gs_import_` (различие на idx 6).

**Веб-кабинет** (`web/routes/integration.py` + `templates/integration/index.html`):
- POST `/integration/{cid}/sync-motiv` (по сохранённому config) и POST `/integration/{cid}/import` (типы: products/inventory/sales/staff/plans; колонки — дефолтный порядок слева направо).
- Два аккордеона в карточке подключения; кнопка импорта disabled без токена.
- Async-роуты `await integration_manager...` — безопасно (gspread в `asyncio.to_thread`).

**Проверки:** `test_imports.py` 9/9 ОК (280 роутов), restart workflow чисто, architect review = PASS (без блокеров).
