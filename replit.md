# DailySales — Telegram Bot для управления продажами

## Обзор проекта

Мультитенантный Telegram-бот + веб-кабинет для управления магазинами, продажами, персоналом и подписками. Каждая организация — изолированная SQLite-база в `data/tenants/`.

- **Бот**: aiogram 3.x, Python 3.11
- **Веб**: FastAPI + Starlette + Jinja2, порт 5000
- **Деплой**: Amvera (production), Replit (development)

## Как запустить

```
python start.py
```

Workflow: **Start application** → `python start.py`

Если `BOT_TOKEN` не задан — открывается страница настройки на порту 5000.

## Секреты (Replit Secrets)

| Ключ | Обязательный | Описание |
|------|-------------|---------|
| `BOT_TOKEN` | ✅ | Токен Telegram-бота (от @BotFather) |
| `ADMIN_CHAT_ID` | ✅ | Telegram ID супер-администратора |
| `SESSION_SECRET` | ✅ | JWT-секрет (уже задан) |
| `GITHUB_TOKEN` | деплой | Push на GitHub |
| `AMVERA_USER` | деплой | Логин Amvera |
| `AMVERA_PASS` | деплой | Пароль Amvera |
| `YANDEX_EMAIL` | опц. | Email для SMTP (email-авторизация) |
| `YANDEX_SMTP_PASSWORD` | опц. | Пароль приложения Яндекс |
| `VAPID_PUBLIC_KEY` | ✅ | Web Push (в Secrets) |
| `VAPID_PRIVATE_KEY` | ✅ | Web Push (в Secrets) |
| `VAPID_MAILTO` | опц. | Web Push email (задан в env) |
| `PAYMENT_CARD_NUMBER` | опц. | Номер карты для оплаты |
| `PAYMENT_RECIPIENT_NAME` | опц. | Получатель платежей |

## Деплой

```bash
bash deploy.sh "Описание изменений"           # GitHub + Amvera
bash deploy.sh "Описание изменений" --no-amvera  # только GitHub
bash deploy.sh "Описание" --skip-tests           # без smoke-тестов
```

## Архитектура

- `main.py` — точка входа бота (aiogram + APScheduler)
- `start.py` — обёртка: освобождает порт 5000, проверяет BOT_TOKEN
- `database.py` — единый DB-класс (~500 методов, SQLite per-org)
- `web/` — FastAPI веб-кабинет (routes/, templates/, static/)
- `data/` — базы данных (не деплоятся на Amvera, персистентный mount)
- `jobs/registry.py` — все APScheduler-задачи (34 джоба)

## User preferences

- Общаться только на **русском языке**
- Деплой: `bash deploy.sh "msg"` (GitHub + Amvera по умолчанию)
- Тестовый бот: @BotCraftAi_Test_3_bot
- Продакшн: https://dailysalesdeploy-pdkiller666.amvera.io/
- Super-admin Telegram ID: 921098636
