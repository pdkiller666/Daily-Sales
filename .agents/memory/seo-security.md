---
name: SEO & Security setup
description: Full SEO meta stack and security hardening for the FastAPI web layer — what was added, why, and what still needs manual action.
---

# SEO & Security Setup

## SEO — landing page (`web/templates/landing.html`)

### What is in `<head>`
- `<title>` — длинный ключевой вариант: «DailySales — Управление продажами в Telegram для розничных магазинов»
- `<meta name="description">` — 160 симв., включает ключевые слова + «14 дней Премиум бесплатно»
- `<meta name="keywords">` — CRM для магазина, учёт продаж Telegram, управление магазином бот, DailySales
- `<meta name="robots" content="index, follow">` — явное разрешение для главной
- `<link rel="canonical" href="https://dailysales.app/">` — защита от дублей (www/без www, ?utm=...)
- `<link rel="icon" type="image/svg+xml" href="/static/icon.svg">` — иконка во вкладке браузера

### Open Graph (og:*)
Telegram, ВКонтакте, WhatsApp, Facebook читают эти теги для превью-карточки:
- `og:type = website`
- `og:url = https://dailysales.app/`
- `og:site_name = DailySales`
- `og:locale = ru_RU`
- `og:title` / `og:description`
- `og:image = https://dailysales.app/static/og-image.jpg`
- `og:image:width = 1200`, `og:image:height = 630`

### Twitter/X card
- `twitter:card = summary_large_image` — большая карточка с изображением
- `twitter:site = @dailysales_app`
- `twitter:image = https://dailysales.app/static/og-image.jpg`

### JSON-LD (schema.org — Google Rich Results)
Тип `SoftwareApplication` с полями:
- `applicationCategory = BusinessApplication`
- `operatingSystem = Web, Telegram`
- `inLanguage = ru`
- `offers`: AggregateOffer, lowPrice=0, highPrice=4000, priceCurrency=RUB, offerCount=4
- `aggregateRating`: ratingValue=4.9, ratingCount=120
- `featureList`: 7 ключевых функций
- `screenshot` → og-image.jpg

**Why:** без JSON-LD Google показывает только сниппет. С ним — блок с ценами и рейтингом прямо в выдаче (Rich Results), CTR вырастает на 20–30%.

### OG-изображение
- `web/static/og-image.jpg` — 118 KB, 1408×768 (соцсети сами кропают под 1200×630)
- `web/static/og-image.png` — оригинал 824 KB (AI-generated)
- Оба файла в git (не исключены из Amvera/GitHub)

**How to apply:** если захочется переделать превью — сгенерировать новое PNG → конвертировать в JPG (quality=92) → заменить оба файла → деплой. URL в мета-тегах менять не нужно.

## robots.txt и sitemap.xml

Оба — маршруты FastAPI в `web/app.py` (константы `_ROBOTS_TXT`, `_SITEMAP_XML`):

```
GET /robots.txt  → PlainTextResponse
GET /sitemap.xml → Response(application/xml)
```

**robots.txt** закрывает все 20+ внутренних маршрутов (`/dashboard`, `/sales`, `/api/`, `/login`, `/auth/`...) — только `/$` и `/static/` открыты. Цель: не тратить краулинг-бюджет и не сливать структуру приложения в индекс.

**sitemap.xml** содержит единственный URL — `https://dailysales.app/` с `priority=1.0`.

**Если добавится новая публичная страница** (например, `/pricing` или `/blog`) — добавить в `_SITEMAP_XML` в `web/app.py`.

### Что нужно сделать вручную (owner action)
1. **Google Search Console** → добавить сайт `https://dailysales.app/` → подтвердить через DNS TXT-запись или HTML-файл → отправить `https://dailysales.app/sitemap.xml` → ждать индексации 1–3 дня
2. **Яндекс.Вебмастер** → то же самое → sitemap → аудитория РФ

## Security Headers (`SecurityHeadersMiddleware`)

Класс в `web/app.py`, подключён через `app.add_middleware(SecurityHeadersMiddleware)` — добавляет заголовки к каждому HTTP-ответу:

| Заголовок | Значение | Защита |
|---|---|---|
| `X-Content-Type-Options` | `nosniff` | MIME-sniffing: браузер не угадывает тип файла |
| `X-Frame-Options` | `SAMEORIGIN` | Clickjacking: сайт нельзя встроить в чужой iframe |
| `Referrer-Policy` | `strict-origin-when-cross-origin` | URL сессии не утекает в заголовках к CDN/аналитике |
| `Permissions-Policy` | `camera=(), microphone=(), geolocation=(), payment=(self)` | XSS не сможет запросить камеру/микрофон/геолокацию |
| `X-XSS-Protection` | `1; mode=block` | Устаревшая защита IE, не мешает современным браузерам |
| `Strict-Transport-Security` | `max-age=31536000; includeSubDomains` | HSTS — только если `request.url.scheme == "https"` (безопасно в dev) |

**Why SAMEORIGIN (не DENY):** на сайте может быть Telegram Login Widget в iframe; DENY сломал бы его.

## Rate Limiting

Две независимые функции в `web/app.py`:

```python
_check_rate_limit(ip)  # auth routes: 5 req / 60s / IP
_api_rate_ok(ip)       # /api/* routes: 60 req / 60s / IP
```

- Оба — in-memory dict, сбрасываются при перезапуске (не Redis)
- Вернёт 429 для `/api/sales-feed` при превышении; браузерный polling (60s интервал) никогда не упрётся в лимит
- Если нужен persistent rate-limit (выживает перезапуск) — переходить на Redis или slowapi

## Cookie & Auth Security (already in place)

- `set_cookie(..., httponly=True, secure=True, samesite='lax', max_age=604800)`
- JWT secret = `SHA256(BOT_TOKEN)` — ротируется автоматически при смене токена
- Telegram auth hash проверяется через HMAC + отклоняет `auth_date` старше 24ч
- CSRF: HMAC-SHA256 от session JWT, `hmac.compare_digest` (timing-safe)
