# Уведомления: Telegram + Web (колокольчик) + Web Push (PWA/APK)

Инструкция-референс по тому, как в DailySales устроена система уведомлений в трёх
каналах сразу, чтобы воспроизвести её в другом проекте. Описаны схема БД, бэкенд,
service worker, фронтенд-подписка и «веерная» рассылка одного события во все каналы.

---

## 0. Общая модель

Одно событие (продажа, низкий остаток, платёж, задача, сообщение в чате) рассылается
**веером в три независимых канала**:

| Канал | Доставка | Когда виден | Технология |
|---|---|---|---|
| **Telegram** | мгновенно, бот пишет в личку | всегда (даже без открытого приложения) | aiogram `bot.send_message` |
| **Web-колокольчик** | поллинг из открытой вкладки | только когда вкладка/PWA открыта | `notification_history` + `/api/my-notifications` |
| **Web Push** | через push-сервис браузера (FCM/APNs/Mozilla) | даже когда вкладка/приложение закрыты | VAPID + service worker `push` |

Ключевой принцип: **каждый канал независим и отказоустойчив**. Падение одного
(например, не настроен VAPID) не должно ломать остальные. Все вызовы push/history
оборачиваются в `try/except`, а тяжёлые/блокирующие — уводятся с event loop через
`asyncio.to_thread(...)`.

> **Идентификатор пользователя сквозной** — везде ключуемся по `telegram_id`.
> Это связывает три канала: бот знает `telegram_id`, веб-сессия хранит его в JWT
> (`sub`), push-подписки тоже привязаны к `telegram_id`.

---

## 1. Схема базы данных

Все таблицы уведомлений живут в **центральной** БД (`shop_bot.db`), не в пер-тенантных.
Push-таблицы строго в `shop_bot.db` (проверка `if 'shop_bot' in self.db_file`).

### 1.1. `notification_settings` — настройки пользователя (что слать)
```sql
CREATE TABLE notification_settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,            -- внутренний users.id
    low_stock_alerts   BOOLEAN DEFAULT TRUE,
    daily_reports      BOOLEAN DEFAULT FALSE,
    sales_alerts       BOOLEAN DEFAULT TRUE,
    payment_alerts     BOOLEAN DEFAULT TRUE,
    admin_notifications BOOLEAN DEFAULT TRUE,
    stock_threshold    INTEGER DEFAULT 5,   -- порог «низкого остатка»
    notification_time  TEXT DEFAULT '09:00',-- когда слать ежедневные (локальное время)
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id)
);
```

### 1.2. `notification_history` — лента для веб-колокольчика
```sql
CREATE TABLE notification_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,            -- внутренний users.id (НЕ telegram_id!)
    notification_type TEXT NOT NULL,     -- 'sales' | 'payment' | 'low_stock' | 'daily_report' | 'admin' | ...
    message TEXT NOT NULL,
    is_read BOOLEAN DEFAULT FALSE,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

### 1.3. `push_subscriptions` — подписки браузеров на Web Push
```sql
CREATE TABLE push_subscriptions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,         -- ВНИМАНИЕ: здесь telegram_id, не users.id
    endpoint   TEXT NOT NULL,            -- URL push-сервиса браузера
    p256dh     TEXT NOT NULL,            -- публичный ключ клиента (шифрование payload)
    auth       TEXT NOT NULL,            -- auth-секрет клиента
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(user_id, endpoint)            -- один пользователь = много устройств
);
CREATE INDEX idx_push_subs_user ON push_subscriptions(user_id);
```

### 1.4. `push_delivery_log` — диагностический журнал доставки (последние 20 на юзера)
```sql
CREATE TABLE push_delivery_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,            -- telegram_id
    title   TEXT,
    sent_at TEXT DEFAULT (datetime('now'))
);
```

### 1.5. `scheduled_notifications` — отложенные/ручные рассылки (через APScheduler)
```sql
CREATE TABLE scheduled_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT UNIQUE NOT NULL,         -- id задачи в APScheduler
    created_by INTEGER NOT NULL,
    notification_text TEXT NOT NULL,
    recipients_type TEXT NOT NULL,       -- 'all' | 'shop' | 'list' | ...
    recipients_list TEXT,                -- JSON со списком получателей
    scheduled_datetime TEXT NOT NULL,    -- ХРАНИТСЯ В UTC (строка)
    status TEXT DEFAULT 'pending',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

> ⚠️ **`user_id` неоднозначен!** В `notification_history` / `notification_settings`
> это внутренний `users.id`. В `push_subscriptions` / `push_delivery_log` — это
> `telegram_id`. Не перепутать при джойнах. Причина: push-утилита намеренно
> автономна и не знает про внутренние id.

---

## 2. Бэкенд: ключевые функции

### 2.1. Кому слать — `get_users_for_notifications(notification_type)`
Возвращает пользователей, у которых включён нужный тип, вместе с порогом и временем:
```python
def get_users_for_notifications(self, notification_type):
    field_map = {
        'low_stock':    'low_stock_alerts',
        'daily_report': 'daily_reports',
        'sales':        'sales_alerts',
        'payment':      'payment_alerts',
        'admin':        'admin_notifications',
    }
    field = field_map.get(notification_type)
    if not field:
        return []
    cursor.execute(f'''
        SELECT u.id, u.telegram_id, u.first_name, u.shop_name,
               ns.stock_threshold, ns.notification_time
        FROM users u
        LEFT JOIN notification_settings ns ON u.id = ns.user_id
        WHERE ns.{field} = 1 AND u.telegram_id IS NOT NULL
    ''')
    return cursor.fetchall()
```
> Возвращаемый кортеж: `[0]=users.id`, `[1]=telegram_id`. **Не путать** — для
> Telegram и push нужен `[1]`, для записи в историю — `[0]`.

### 2.2. Запись в ленту колокольчика — `add_notification_to_history(user_id, type, message)`
Простой INSERT в `notification_history` (user_id = внутренний `users.id`).

### 2.3. Сохранение push-подписки — `save_push_subscription(user_id, endpoint, p256dh, auth)`
UPSERT по `UNIQUE(user_id, endpoint)` (user_id = telegram_id).

---

## 3. Web Push: VAPID-движок (`web/push_utils.py`)

Сердце push-доставки. Использует библиотеку **`pywebpush`** (НЕ httpx/прочее).

### 3.1. Переменные окружения
```
VAPID_PUBLIC_KEY   — url-safe base64 (он же applicationServerKey в браузере)
VAPID_PRIVATE_KEY  — приватный ключ (PEM или single-line DER)
VAPID_MAILTO       — контакт владельца (mailto:you@example.com); Apple/Mozilla троттлят без него
```

Сгенерировать пару ключей (один раз):
```python
from web.push_utils import generate_vapid_keypair
kp = generate_vapid_keypair()
# kp['private_single'] → в env VAPID_PRIVATE_KEY (предпочтительно, single-line, без переносов)
# kp['public_b64']     → в env VAPID_PUBLIC_KEY
```

### 3.2. ⚠️ Критичная нормализация ключа (`_normalize_vapid_key`)
`pywebpush` → `py_vapid.Vapid.from_string()` просто срезает переносы строк и
base64-декодирует **всю строку** — он **НЕ** убирает PEM-арматуру `-----BEGIN/END-----`.
Если скормить PEM, получите `ValueError: Could not deserialize key data ... invalid length`.

Решение: при импорте модуля **любой** формат ключа конвертируется в
**single-line url-safe base64 PKCS8 DER** (~184 символа, без переносов). Поддержаны:
PEM (в т.ч. «схлопнутый» env-редактором Amvera), EC с явными параметрами (fallback
через `openssl pkcs8 -topk8`), base64 DER, сырой 32-байтный скаляр (py_vapid 1.x).

> Это самый частый источник «push не работает». Если переносите код — переносите
> `_normalize_vapid_key` целиком.

### 3.3. Публичный API
```python
send_web_push(tg_id, title, body, url="/dashboard", badge=1) -> dict
    # шлёт на ВСЕ устройства одного юзера; возвращает {sent, failed, gone, code, ...}
send_web_push_bulk(tg_ids, title, body, url, badge) -> int
    # та же нотификация многим; дедупит id, грузит VAPID/pywebpush один раз
apush(...)        # async-обёртка над send_web_push (через asyncio.to_thread)
apush_bulk(...)   # async-обёртка над bulk
is_configured()   # True, если VAPID-ключ валиден
```

### 3.4. Самоочистка «мёртвых» подписок
При ответе push-сервиса **404/410 Gone** подписка протухла — она автоматически
удаляется из `push_subscriptions` (`_delete_subscription`). Прочие ошибки логируются,
но не роняют рассылку.

### 3.5. payload и теги (`_build_payload`)
Тело — JSON `{title, body, url, badge, tag}`. `tag` маппится по префиксу URL
(`/sales`→`ds-sale`, `/chat`→`ds-chat`, …), чтобы уведомления разных типов **не
перетирали друг друга** в шторке. `body` никогда не пустой (fallback на title).

### 3.6. ⚠️ Правило async-безопасности
В async-коде **никогда** не зовите синхронный `send_web_push` напрямую — только
`apush`/`apush_bulk` или `asyncio.to_thread(send_web_push, ...)`. Иначе блокируется
event loop веб-сервера/бота.

---

## 4. Service Worker (`web/static/sw.js`)

Файл отдаётся с корня (`/sw.js`, scope `/`). Версионируется (`CACHE_NAME = 'dailysales-vN'`),
при изменении — поднять номер, чтобы браузеры обновили SW.

### 4.1. Обработчик `push`
```js
self.addEventListener('push', e => {
    let data; try { data = e.data.json(); } catch { data = {title:'App', body:e.data.text()}; }
    const showNotif = self.registration.showNotification(data.title, {
        body: data.body || data.title,
        icon: '/static/icon-192.png',
        badge: '/static/icon-192.png',
        tag: data.tag || 'ds-default',
        renotify: data.tag === 'ds-sale' || data.tag === 'ds-chat', // вибро повторно только для realtime
        data: { url: data.url || '/dashboard' },
    });
    // App Badge: тянем реальное число непрочитанных с сервера
    const updateBadge = fetch('/api/unread-count', {credentials:'same-origin'})
        .then(r => r.json())
        .then(d => navigator.setAppBadge?.(d.total ?? 1))
        .catch(() => navigator.setAppBadge?.(1));
    e.waitUntil(Promise.all([showNotif, updateBadge]));
});
```

### 4.2. Клик по уведомлению — фокус или открытие нужной вкладки
```js
self.addEventListener('notificationclick', e => {
    e.notification.close();
    navigator.clearAppBadge?.();
    const target = e.notification.data?.url || '/dashboard';
    e.waitUntil(clients.matchAll({type:'window', includeUncontrolled:true}).then(wins => {
        const existing = wins.find(w => w.url.includes(target));
        return existing ? existing.focus() : clients.openWindow(target);
    }));
});
```

### 4.3. ⚠️ `pushsubscriptionchange` — автопродление подписки
Браузер периодически ротирует подписку. Если не переподписаться — push молча
умирает. Стратегия (от лучшего к худшему): `e.newSubscription` → `getSubscription()`
→ `subscribe(e.oldSubscription.options)`. Результат POST-ится на `/api/push/subscribe`.
> Частый баг: `if (!e.oldSubscription) return` — на Chrome/Android `oldSubscription`
> бывает `null`, и подписка молча терялась. Не делать ранний выход по этому полю.

### 4.4. Стратегия кэша
`/static/*` — cache-first; все аутентифицированные маршруты — **network-only**
(нельзя кэшировать чувствительные данные).

---

## 5. API-роуты (`web/routes/api.py`)

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/api/my-notifications?limit=20` | лента колокольчика + счётчик непрочитанных |
| POST | `/api/my-notifications/read-all` | пометить всё прочитанным |
| GET | `/api/unread-count` | `{total, notifs, dms}` — для App Badge / SW |
| GET | `/api/push/vapid-public-key` | отдаёт публичный VAPID-ключ фронту |
| POST | `/api/push/subscribe` | сохранить подписку (вызывается фронтом И из SW) |
| POST | `/api/push/unsubscribe` | удалить подписку |
| POST | `/api/push/test` | тестовый push «проверка связи» |
| GET | `/api/push/status` | диагностика (есть VAPID? есть подписки?) |
| GET | `/api/sales-feed?since=ISO` | новые продажи для toast-поллинга |

### 5.1. CSRF-нюанс для push-подписки
`/api/push/subscribe` **не требует CSRF-токена**: защищён `samesite=lax` cookie, и
к тому же SW (`pushsubscriptionchange`) не может добавить DOM-токен. Эндпоинт
валидирует, что `endpoint` начинается с `https://`, и обрезает длины полей.

---

## 6. Фронтенд (`web/templates/base.html`)

### 6.1. Регистрация SW
```js
if ('serviceWorker' in navigator) {
    window.addEventListener('load', () =>
        navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => {}));
}
```

### 6.2. Подписка на Web Push (`dsSubscribeWebPush`)
```js
const reg = await navigator.serviceWorker.ready;
let sub = await reg.pushManager.getSubscription();
if (!sub) {
    const { key } = await (await fetch('/api/push/vapid-public-key')).json();
    sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: _urlBase64ToUint8Array(key), // base64 → Uint8Array обязателен
    });
}
await fetch('/api/push/subscribe', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(sub.toJSON()), credentials:'same-origin',
});
```
Вызывается после `Notification.requestPermission() === 'granted'`. Хелпер
`_urlBase64ToUint8Array` обязателен — `applicationServerKey` не принимает строку.

### 6.3. Колокольчик (поллинг)
Alpine-компонент периодически дёргает `/api/my-notifications?limit=20`, обновляет
список и счётчик, и зовёт `_setAppBadge(unread + dms)` через App Badge API.
Внешние страницы могут форсировать обновление бейджа: `window.dsRefreshBadge()`.

---

## 7. Бот: кнопка «Прочитано» (`notif_utils.py`)

К любому уведомлению бота добавляется инлайн-кнопка «✅ Прочитано» (`callback_data="notif_read"`),
по нажатию сообщение удаляется (запись уже в `notification_history`):
```python
def add_read_btn(existing_markup=None) -> InlineKeyboardMarkup:
    btn = [InlineKeyboardButton(text="✅ Прочитано", callback_data="notif_read")]
    if existing_markup is None:
        return InlineKeyboardMarkup(inline_keyboard=[btn])
    rows = [list(r) for r in existing_markup.inline_keyboard]
    rows.append(btn)
    return InlineKeyboardMarkup(inline_keyboard=rows)
```

---

## 8. Веерная рассылка: один эталонный паттерн

Все APScheduler-джобы (`main.py`) и обработчики событий шлют событие в три канала
одинаково. Эталон (упрощённо, по образцу `send_payment_alerts` / `send_sales_alerts`):

```python
for user_row in current_db.get_users_for_notifications('sales'):
    user_id     = user_row[0]   # внутренний users.id  → для истории
    telegram_id = user_row[1]   # telegram_id          → для бота и push

    # 1) Telegram (мгновенно, всегда)
    try:
        await bot.send_message(telegram_id, message, parse_mode="HTML",
                               reply_markup=add_read_btn())
    except Exception as e:
        logging.warning("tg notify fail: %s", e)

    # 2) Веб-колокольчик (history) — блокирующий SQLite уводим с loop
    try:
        await asyncio.to_thread(current_db.add_notification_to_history,
                                user_id, 'sales', message)
    except Exception as e:
        logging.warning("history fail: %s", e)

    # 3) Web Push (фон/закрытое приложение)
    try:
        from web.push_utils import send_web_push
        _pb = _plain(message)  # снять HTML-теги для текста push
        await asyncio.to_thread(send_web_push, telegram_id,
                                "🛍️ Продажа", _pb, "/sales")
    except Exception as e:
        logging.warning("push fail: %s", e)
```

Правила паттерна:
1. **Каждый канал в своём `try/except`** — падение одного не трогает другие.
2. **Telegram — `telegram_id`**, **история — `users.id`**, **push — `telegram_id`**.
3. **Всё блокирующее — `asyncio.to_thread`** (SQLite, `send_web_push`).
4. **URL push** = страница, куда вести по клику (он же задаёт `tag`).
5. **HTML только в Telegram**; для push снять теги (push body — plain text).

### Планировщик
APScheduler-джобы стартуют в `main.py` (`scheduler.add_job(...)`). Примеры:
`send_payment_alerts`, `send_sales_alerts` (агрегат за 24ч),
`send_personalized_notifications` (низкий остаток по `stock_threshold`),
ежедневный отчёт в `notification_time` пользователя, дедлайны задач.
> **Время в БД хранится в UTC** (Amvera = UTC). `scheduled_datetime` —
> UTC-строка; для отображения конвертировать в таймзону пользователя.

---

## 9. Android APK (TWA/PWA) — что нужно знать

APK — это **TWA-обёртка** над тем же веб-приложением, поэтому **отдельной push-системы
для Android нет**: внутри работает тот же service worker и тот же Web Push.

Условия, чтобы фоновый push работал в APK/Chromium:
- Устройство с **Google Play Services (GMS)** — Web Push на Chromium будит браузер
  через **FCM**. Без GMS (например, новые Huawei) фон не работает — уведомления
  приходят пачкой при открытии приложения. Это ограничение **устройства**, не сервера.
- В манифесте APK должно быть разрешение, если используете камеру/иное; для push
  специальных разрешений Android не требуется (всё через web push permission).
- `manifest.json` PWA + иконки (`icon-192.png`, `icon-512.png`, maskable) — обязательны
  для установки и для иконки в шторке уведомления.

---

## 10. Чек-лист переноса в новый проект

1. **БД**: создать 5 таблиц из §1 (push-таблицы — в центральной БД).
2. **ENV**: сгенерировать VAPID-пару (`generate_vapid_keypair`), положить
   `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY` (single-line!) / `VAPID_MAILTO` в секреты.
3. **push_utils.py**: перенести целиком (особенно `_normalize_vapid_key` и
   самоочистку 404/410). Установить `pywebpush` + `cryptography`.
4. **sw.js**: положить в корень статики, отдавать как `/sw.js`. Перенести
   `push` / `notificationclick` / `pushsubscriptionchange`.
5. **API-роуты** из §5 (subscribe/unsubscribe/vapid-public-key/my-notifications/
   unread-count/test/status).
6. **Фронт**: регистрация SW + `dsSubscribeWebPush` + `_urlBase64ToUint8Array` +
   поллинг колокольчика; запрос `Notification.requestPermission()` по действию юзера.
7. **Бот**: `add_read_btn` + хендлер `notif_read` (удаляет сообщение).
8. **Рассылка**: реализовать веерный паттерн из §8 во всех точках событий и
   APScheduler-джобах. Каждый канал — свой `try/except`, блокирующее — в `to_thread`.
9. **PWA manifest** + иконки для установки/APK.

---

## 11. Грабли (выстраданное)

- **VAPID PEM не работает** — только single-line DER; нормализовать ключ при импорте (§3.2).
- **В async нельзя синхронный `send_web_push`** — только `apush`/`to_thread` (§3.6).
- **`pushsubscriptionchange`**: не делать ранний `return` по `oldSubscription==null` (§4.3).
- **`user_id` двусмыслен**: history/settings = `users.id`, push = `telegram_id` (§1).
- **404/410 = протухшая подписка** → удалять, не ретраить (§3.4).
- **HTML только в Telegram**, push/история — plain text.
- **Время в UTC** на проде; отображать через конвертацию в таймзону юзера.
- **App Badge** тянуть реальным числом из `/api/unread-count`, а не инкрементом.
- **`tag` по типу** — иначе уведомления перетирают друг друга в шторке.
- **Huawei/без GMS** — фоновый push физически не работает; не баг сервера.
- **Версия SW** (`CACHE_NAME`) — поднимать при каждом изменении `sw.js`.
- **`/api/push/subscribe` без CSRF** — намеренно (samesite cookie + вызов из SW).
