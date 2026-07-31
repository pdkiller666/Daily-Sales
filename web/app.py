import asyncio
import base64
import json
import logging
import os
import secrets as _secrets
import sqlite3
import time as _time
from pathlib import Path
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse, PlainTextResponse
from jinja2 import pass_context
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware

from timezone_utils import DEFAULT_TZ

logger = logging.getLogger("web.app")

BASE_DIR = Path(__file__).parent
_SHOP_BOT_DB = "data/shop_bot.db"

# Event loop reference saved at startup so sync routes can schedule async tasks
# via asyncio.run_coroutine_threadsafe(_main_loop).
_main_loop: asyncio.AbstractEventLoop | None = None


def _fmt_plan_lim(v) -> str:
    return "∞" if v == -1 else str(v)


def _get_landing_plans() -> dict:
    plans: dict = {}
    try:
        conn = sqlite3.connect(_SHOP_BOT_DB)
        cur = conn.cursor()
        cur.execute("""
            SELECT name, price, duration_days, max_products, max_shops,
                   max_sales_per_month
            FROM subscription_plans WHERE is_active = 1 ORDER BY price ASC
        """)
        for row in cur.fetchall():
            name, price, days, mp, ms, msal = row
            price_int = int(price)
            plans[name] = {
                "price": price_int,
                "price_fmt": (
                    f"{price_int:,}".replace(",", "\u00a0") + "\u00a0₽"
                    if price_int > 0 else "0\u00a0₽"
                ),
                "duration_days": days,
                "duration_label": "навсегда" if days == 0 else f"{days} дней",
                "max_products": _fmt_plan_lim(mp),
                "max_shops": _fmt_plan_lim(ms),
                "max_sales": _fmt_plan_lim(msal),
            }
        conn.close()
    except Exception:
        logger.warning("landing: не удалось загрузить тарифы из БД", exc_info=True)
    return plans


def _fmt_date(s: str) -> str:
    if not s:
        return ""
    try:
        d = str(s)[:10]
        y, m, day = d.split('-')
        return f"{day}.{m}.{y}"
    except Exception:
        return str(s)


def _fmt_datetime(s: str) -> str:
    """2026-06-03 11:10:45  →  03.06.2026 11:10"""
    if not s:
        return ""
    try:
        raw = str(s)[:16].replace("T", " ")
        parts = raw.split(" ")
        d = _fmt_date(parts[0])
        return f"{d} {parts[1]}" if len(parts) > 1 else d
    except Exception:
        return str(s)


@pass_context
def _fmt_currency(ctx, amount) -> str:
    """Форматирует сумму с символом валюты текущей организации.
    Читает currency_symbol из request.state (устанавливается CurrencyMiddleware).
    Graceful-fallback → ₽ при любой ошибке."""
    try:
        request = ctx.get('request')
        symbol = getattr(request.state, 'currency_symbol', '₽') if request else '₽'
    except Exception:
        symbol = '₽'
    try:
        v = int(float(amount or 0))
        return f"{v:,}".replace(',', '\u00a0') + f"\u00a0{symbol}"
    except Exception:
        return f"0\u00a0{symbol}"


# ── LRU-кэш символов валют: {org_db_path: symbol} ─────────────────────────
_CURRENCY_SYMBOL_CACHE: dict[str, str] = {}


class CurrencyMiddleware(BaseHTTPMiddleware):
    """Определяет символ валюты для текущей сессии и кладёт его в request.state.
    Читает JWT из сессионной куки (payload декодируется без проверки подписи —
    только для кэш-поиска; подлинная аутентификация происходит в каждом роуте).
    """

    async def dispatch(self, request: Request, call_next):
        request.state.currency_symbol = '₽'  # default
        try:
            cookie = request.cookies.get('session')
            if cookie:
                parts = cookie.split('.')
                if len(parts) == 3:
                    padded = parts[1] + '=='
                    payload = json.loads(base64.urlsafe_b64decode(padded))
                    org_db = payload.get('org_db', '')
                    if org_db:
                        if org_db not in _CURRENCY_SYMBOL_CACHE:
                            try:
                                from database import Database as _DB
                                from currency_utils import get_currency_symbol as _gcs
                                _db = _DB(org_db)
                                _code = _db.get_org_currency()
                                _CURRENCY_SYMBOL_CACHE[org_db] = _gcs(_code)
                            except Exception:
                                _CURRENCY_SYMBOL_CACHE[org_db] = '₽'
                        request.state.currency_symbol = _CURRENCY_SYMBOL_CACHE.get(org_db, '₽')
        except Exception:
            pass
        return await call_next(request)


# CSP template.
# 'unsafe-eval' обязателен для Alpine.js 3 (использует new Function() при вычислении x-* атрибутов).
# Строгий XSS-хардненинг: 'unsafe-inline' УБРАН из script-src — используется per-request nonce
# ('nonce-{{nonce}}', подставляется SecurityHeadersMiddleware). Все inline event-handler
# атрибуты (onclick/onchange/oninput/onsubmit) переведены на делегированные слушатели в
# /static/ds-delegate.js (data-* атрибуты), т.к. на inline-обработчики nonce повесить нельзя.
# НЕ возвращать 'unsafe-inline' в script-src без обратной миграции на inline-обработчики.
_CSP_TEMPLATE = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-eval' 'nonce-{{nonce}}' "
    "https://telegram.org; "
    "style-src 'self' 'unsafe-inline'; "
    "font-src 'self' data:; "
    "img-src 'self' data: blob: https:; "
    "connect-src 'self'; "
    "frame-src https://telegram.org https://oauth.telegram.org; "
    "frame-ancestors 'self'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "object-src 'none';"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach security headers to every response."""

    async def dispatch(self, request: Request, call_next):
        # Генерируем нонс ДО call_next — шаблоны Jinja2 читают его через csp_nonce(request)
        nonce = _secrets.token_urlsafe(16)
        try:
            request.state.csp_nonce = nonce
        except Exception:
            pass
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(self), microphone=(self), geolocation=(), payment=(self)"
        )
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Content-Security-Policy"] = _CSP_TEMPLATE.replace("{{nonce}}", nonce)
        # HSTS — only on HTTPS (Amvera/production serves via HTTPS)
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        path = request.url.path
        ct = response.headers.get("content-type", "")
        if path == "/sw.js" or path.startswith("/sw.js?"):
            # Service Worker must NEVER be cached — browser must always check for
            # updates so a new SW version is picked up immediately on next page load.
            response.headers["Cache-Control"] = "no-store, max-age=0"
        elif path.startswith("/static/"):
            # Long-lived cache for immutable static assets (versioned by deploy)
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif "text/html" in ct or (
            not path.startswith("/api/")
            and not path.endswith((".js", ".css", ".png", ".jpg", ".ico", ".webp", ".woff2"))
        ):
            # No caching for HTML and dynamic responses
            response.headers["Cache-Control"] = "no-store"
        return response


# In-memory rate limit for /api/* endpoints: 60 req/min per IP
_api_rate_log: dict = {}
_API_RATE_WINDOW = 60
_API_RATE_MAX = 60


def _api_rate_ok(ip: str) -> bool:
    now = _time.time()
    hits = [t for t in _api_rate_log.get(ip, []) if now - t < _API_RATE_WINDOW]
    if len(hits) >= _API_RATE_MAX:
        return False
    hits.append(now)
    _api_rate_log[ip] = hits
    # Периодическая очистка устаревших IP-ключей (защита от утечки памяти)
    if len(_api_rate_log) > 500:
        stale = [k for k, v in _api_rate_log.items()
                 if not v or now - max(v) > _API_RATE_WINDOW]
        for k in stale:
            del _api_rate_log[k]
    return True


_ROBOTS_TXT = """\
User-agent: *
Allow: /$
Allow: /static/
Disallow: /dashboard
Disallow: /sales
Disallow: /products
Disallow: /inventory
Disallow: /reports
Disallow: /reports/heatmap
Disallow: /reports/abc
Disallow: /rankings
Disallow: /staff
Disallow: /org-structure
Disallow: /plans
Disallow: /salary
Disallow: /schedule
Disallow: /contests
Disallow: /settings
Disallow: /integration
Disallow: /payments
Disallow: /notifications
Disallow: /motivation
Disallow: /subscription
Disallow: /categories
Disallow: /promocodes
Disallow: /shops
Disallow: /pos
Disallow: /absences
Disallow: /support
Disallow: /chat
Disallow: /chat/search
Disallow: /chat/read
Disallow: /chat/dm
Disallow: /ws/dm
Disallow: /tasks
Disallow: /tasks/attachment/
Disallow: /tasks/views/
Disallow: /tasks/automation
Disallow: /tasks/calendar
Disallow: /tasks/gantt
Disallow: /tasks/workload
Disallow: /ai-insights
Disallow: /returns
Disallow: /api/returns/
Disallow: /clients
Disallow: /clients/duplicates
Disallow: /packages
Disallow: /services
Disallow: /appointments
Disallow: /api/appointments/
Disallow: /api/
Disallow: /payment-proof/
Disallow: /payment-proof-req/
Disallow: /login
Disallow: /register
Disallow: /auth/
Disallow: /admin
Disallow: /backups
Disallow: /logout
Disallow: /my-notifications
Disallow: /nav-config
Disallow: /orgs
Disallow: /push
Disallow: /stats
Disallow: /subs
Disallow: /switch_org
Disallow: /unread-count
Disallow: /users
Disallow: /download/
Disallow: /webhook/

Sitemap: https://dailysales.app/sitemap.xml
"""

_SITEMAP_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://dailysales.app/</loc>
    <changefreq>weekly</changefreq>
    <priority>1.0</priority>
  </url>
</urlset>
"""


def create_web_app() -> FastAPI:
    global _main_loop
    try:
        _main_loop = asyncio.get_running_loop()
    except RuntimeError:
        _main_loop = asyncio.get_event_loop()

    app = FastAPI(
        title="DailySales Web",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(CurrencyMiddleware)
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    templates.env.auto_reload = False
    templates.env.filters['fmt_date'] = _fmt_date
    templates.env.filters['fmt_datetime'] = _fmt_datetime
    templates.env.filters['fmt_currency'] = _fmt_currency

    from timezone_utils import get_user_time as _get_user_time

    def _fmt_sale_dt(s, tz: str = DEFAULT_TZ, fmt: str = "%d.%m.%Y %H:%M") -> str:
        """Convert UTC sale_date string to user local time and format it."""
        if not s:
            return "—"
        try:
            local_dt = _get_user_time(str(s), tz or DEFAULT_TZ)
            if local_dt:
                return local_dt.strftime(fmt)
        except Exception:
            pass
        return _fmt_date(str(s)[:10])

    def _fmt_sale_time(s, tz: str = DEFAULT_TZ) -> str:
        """Return only the HH:MM part of a sale_date, converted to user's TZ."""
        if not s:
            return "—"
        try:
            local_dt = _get_user_time(str(s), tz or DEFAULT_TZ)
            if local_dt:
                return local_dt.strftime("%H:%M")
        except Exception:
            pass
        raw = str(s)
        return raw[11:16] if len(raw) > 10 else "—"

    _RU_MONTHS_SHORT = ['янв','фев','мар','апр','май','июн','июл','авг','сен','окт','ноя','дек']

    def _fmt_date_badge(s, tz: str = DEFAULT_TZ) -> str:
        """Return compact date like '15 июн' for mobile badge."""
        if not s:
            return "—"
        try:
            local_dt = _get_user_time(str(s), tz or DEFAULT_TZ)
            if local_dt:
                return f"{local_dt.day} {_RU_MONTHS_SHORT[local_dt.month - 1]}"
        except Exception:
            pass
        raw = str(s)
        if len(raw) >= 10:
            try:
                d, m = int(raw[8:10]), int(raw[5:7])
                return f"{d} {_RU_MONTHS_SHORT[m - 1]}"
            except Exception:
                pass
        return str(s)[:10]

    def _is_backdated(s, tz: str = DEFAULT_TZ) -> bool:
        """Return True if sale_date is NOT today in user's timezone."""
        if not s:
            return False
        try:
            from datetime import datetime
            import zoneinfo
            local_dt = _get_user_time(str(s), tz or DEFAULT_TZ)
            if local_dt:
                today = datetime.now(zoneinfo.ZoneInfo(tz or DEFAULT_TZ)).date()
                return local_dt.date() != today
        except Exception:
            pass
        return False

    templates.env.filters['fmt_sale_dt'] = _fmt_sale_dt
    templates.env.filters['fmt_sale_time'] = _fmt_sale_time
    templates.env.filters['fmt_date_badge'] = _fmt_date_badge
    templates.env.filters['is_backdated'] = _is_backdated

    import bot_holder
    templates.env.globals['bot_username'] = lambda: bot_holder.get_username() or ''

    from web.routes.payments import get_pending_count
    templates.env.globals['pending_payments_count'] = get_pending_count

    def _pending_absences_count(request):
        """Return count of pending absence requests for the current org."""
        try:
            from web.auth import get_session_user
            from web.deps import get_web_db
            user = get_session_user(request)
            if not user or user.get("role") not in ("owner", "admin", "super_admin"):
                return 0
            db = get_web_db(int(user["sub"]), user.get("org_db"))
            conn = db.get_connection()
            row = conn.execute(
                "SELECT COUNT(*) FROM absence_records WHERE status='pending'"
            ).fetchone()
            conn.close()
            return row[0] if row else 0
        except Exception:
            return 0

    templates.env.globals['pending_absences_count'] = _pending_absences_count

    def _current_org_name(request):
        """Return the display name of the org currently selected in the session."""
        try:
            from web.auth import get_session_user
            user = get_session_user(request)
            if not user:
                return ""
            org_db = user.get("org_db", "")
            if not org_db or org_db == "data/shop_bot.db":
                return ""
            from database import Database
            conn = Database("data/main.db").get_connection()
            row = conn.execute(
                "SELECT name FROM organizations WHERE db_path=?", (org_db,)
            ).fetchone()
            conn.close()
            return row[0] if row else os.path.basename(org_db)
        except Exception:
            return ""

    def _all_orgs_for_switcher():
        """Return list of active orgs for the super_admin org switcher."""
        try:
            from web.deps import get_all_active_orgs
            return get_all_active_orgs()
        except Exception:
            return []

    templates.env.globals['current_org_name'] = _current_org_name
    templates.env.globals['all_orgs_for_switcher'] = _all_orgs_for_switcher

    from web.auth import get_csrf_token as _get_csrf
    templates.env.globals['csrf_token_for'] = _get_csrf

    def _is_beta_mode() -> bool:
        """Return True while the project is in beta (default ON; toggle via super_admin /settings)."""
        try:
            from database import Database
            conn = Database(_SHOP_BOT_DB).get_connection()
            row = conn.execute(
                "SELECT value FROM payment_settings WHERE key='beta_mode'"
            ).fetchone()
            conn.close()
            if row is None:
                return True  # key absent → beta ON by default
            return row[0] != "0"
        except Exception:
            return True

    templates.env.globals['beta_mode'] = _is_beta_mode

    # CSP nonce — per-request значение, выставляется SecurityHeadersMiddleware в request.state
    templates.env.globals['csp_nonce'] = lambda request: getattr(
        getattr(request, 'state', None), 'csp_nonce', ''
    )

    def _is_chat_enabled(request=None) -> bool:
        """Return True if chat is not disabled (chat_min_plan != 'Отключён').

        Memoized on request.state — chat_enabled() is referenced several times in
        base.html, so this collapses repeated payment_settings lookups to one per
        render. Callable without request (backward compatible, just uncached).
        """
        state = getattr(request, "state", None) if request is not None else None
        if state is not None and hasattr(state, "_chat_enabled"):
            return state._chat_enabled
        try:
            from database import Database
            conn = Database(_SHOP_BOT_DB).get_connection()
            row = conn.execute(
                "SELECT value FROM payment_settings WHERE key='chat_min_plan'"
            ).fetchone()
            conn.close()
            # key absent → chat enabled by default
            val = True if row is None else (row[0] != "Отключён")
        except Exception:
            val = True
        if state is not None:
            try:
                state._chat_enabled = val
            except Exception:
                pass
        return val

    templates.env.globals['chat_enabled'] = _is_chat_enabled

    from web.changelog import CURRENT_VERSION as _CL_VER, ENTRIES as _CL_ENTRIES
    templates.env.globals['changelog_version'] = lambda: _CL_VER
    templates.env.globals['changelog_entries'] = lambda: _CL_ENTRIES

    def _my_db_id(request, telegram_id, db):
        """Внутренний users.id текущего юзера в его org-БД.

        Мемоизировано на request.state — open_tasks_count и dm_unread_count
        оба нуждаются в этом значении на одном и том же рендере страницы;
        раньше каждый делал свой отдельный SELECT.
        """
        state = getattr(request, "state", None)
        if state is not None and hasattr(state, "_my_db_id"):
            return state._my_db_id
        conn = db.get_connection()
        my_row = conn.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        conn.close()
        my_db_id = my_row[0] if my_row else 0
        if state is not None:
            try:
                state._my_db_id = my_db_id
            except Exception:
                pass
        return my_db_id

    def _open_tasks_count(request):
        """Счётчик незакрытых задач для сайдбара (кэш 30 с на пользователя)."""
        try:
            from web.auth import get_session_user
            from web.deps import get_web_db
            from web.perf_cache import cached as _pc
            user = get_session_user(request)
            if not user:
                return 0
            telegram_id = int(user["sub"])
            org_db = user.get("org_db")
            if not org_db or org_db == "data/shop_bot.db":
                return 0
            is_admin = user.get("role") in ("owner", "admin", "super_admin")
            db = get_web_db(telegram_id, org_db)
            my_db_id = _my_db_id(request, telegram_id, db)
            if not my_db_id:
                return 0
            cache_key = f"tasks:{telegram_id}:{org_db}:{is_admin}"
            return _pc(cache_key, 30, lambda: db.get_open_tasks_count(my_db_id, is_admin))
        except Exception:
            return 0

    templates.env.globals['open_tasks_count'] = _open_tasks_count

    def _dm_unread_count(request):
        """Счётчик непрочитанных ЛС для сайдбара (кэш 30 с на пользователя)."""
        try:
            from web.auth import get_session_user
            from web.deps import get_web_db
            from web.perf_cache import cached as _pc
            user = get_session_user(request)
            if not user:
                return 0
            telegram_id = int(user["sub"])
            org_db = user.get("org_db")
            if not org_db or org_db == "data/shop_bot.db":
                return 0
            db = get_web_db(telegram_id, org_db)
            my_db_id = _my_db_id(request, telegram_id, db)
            if not my_db_id:
                return 0
            cache_key = f"dm_unread:{telegram_id}:{org_db}"
            return _pc(cache_key, 30, lambda: db.get_dm_unread_count(my_db_id))
        except Exception:
            return 0

    templates.env.globals['dm_unread_count'] = _dm_unread_count

    def _subscription_grace(request):
        """{'in_grace': bool, 'days_left': int} для баннера soft-expiry.

        Показывается владельцу, чьи платные модули истекли, но ещё в окне grace.
        Мемоизируется на request.state (base.html вызывает один раз)."""
        state = getattr(request, "state", None)
        if state is not None and hasattr(state, "_sub_grace"):
            return state._sub_grace
        result = {"in_grace": False, "days_left": 0}
        try:
            from web.auth import get_session_user
            from billing_utils import in_grace_period, grace_days_left
            user = get_session_user(request)
            if user:
                tg_id = int(user["sub"])
                if tg_id > 0 and in_grace_period(tg_id):
                    result = {"in_grace": True,
                              "days_left": grace_days_left(tg_id) or 0}
        except Exception:
            logger.debug("subscription_grace: проверка grace-периода не удалась", exc_info=True)
        if state is not None:
            try:
                state._sub_grace = result
            except Exception:
                pass
        return result

    templates.env.globals['subscription_grace'] = _subscription_grace

    _NAV_MODULE_KEYS = ('analytics', 'team', 'plans_motivation', 'chat',
                        'integrations', 'notifications', 'ai_assistant', 'pos_retail',
                        'crm', 'services')

    def _ai_insights_enabled(request=None) -> bool:
        """Return True if the current user has the ai_network_insights extension.

        Memoized on request.state for efficiency (base.html calls it twice).
        """
        state = getattr(request, "state", None) if request is not None else None
        if state is not None and hasattr(state, "_ai_insights_enabled"):
            return state._ai_insights_enabled
        val = False
        try:
            from web.auth import get_session_user
            from billing_utils import has_extension
            user = get_session_user(request)
            if user:
                tg_id = int(user["sub"])
                val = has_extension(tg_id, "ai_network_insights")
        except Exception:
            logger.debug("ai_insights_enabled: проверка расширения не удалась", exc_info=True)
        if state is not None:
            try:
                state._ai_insights_enabled = val
            except Exception:
                pass
        return val

    templates.env.globals['ai_insights_enabled'] = _ai_insights_enabled

    def _nav_modules(request):
        """Returns dict {module_key: bool} for nav visibility gating. Fails open.

        Per-user override (user_module_access): 'deny' прячет модуль у сотрудника,
        'allow' принудительно показывает; None — наследует биллинг (has_module).
        """
        # Memoize per request — base.html calls nav_modules(request) twice
        # (sidebar + mobile "more" sheet); compute the module map only once.
        state = getattr(request, "state", None)
        if state is not None and hasattr(state, "_nav_modules"):
            return state._nav_modules
        try:
            from web.auth import get_session_user
            from billing_utils import get_modules_access
            from db_utils import get_user_module_access
            from tenant_manager import tenant_manager
            user = get_session_user(request)
            if not user:
                result = {k: False for k in _NAV_MODULE_KEYS}
            else:
                tg_id = int(user["sub"])
                overrides = {k: get_user_module_access(tg_id, k)
                             for k in _NAV_MODULE_KEYS}
                # Bulk billing lookups (≈3 queries each) instead of per-key.
                need_self = any(ov not in ('deny', 'allow')
                                for ov in overrides.values())
                need_owner = any(ov == 'allow' for ov in overrides.values())
                self_access = (get_modules_access(tg_id, _NAV_MODULE_KEYS)
                               if need_self else {})
                owner_access = {}
                if need_owner:
                    # 'allow' override gates on the OWNER's billing (not a bypass).
                    owner_tg = tenant_manager.get_org_owner_tg(tg_id) or tg_id
                    owner_access = get_modules_access(owner_tg, _NAV_MODULE_KEYS)
                result = {}
                for k in _NAV_MODULE_KEYS:
                    ov = overrides[k]
                    if ov == 'deny':
                        result[k] = False  # явный запрет — скрыт даже если оплачен
                    elif ov == 'allow':
                        result[k] = owner_access.get(k, False)
                    else:
                        result[k] = self_access.get(k, False)
        except Exception:
            logger.debug("nav_modules: гейтинг не удался, fail-open", exc_info=True)
            result = {k: True for k in _NAV_MODULE_KEYS}
        if state is not None:
            try:
                state._nav_modules = result
            except Exception:
                pass
        return result

    templates.env.globals['nav_modules'] = _nav_modules

    import re as _re
    _EMOJI_PREFIX_RE = _re.compile(
        r'^[\U0001F000-\U0001FAFF\u2190-\u2BFF\u2600-\u27BF\uFE0F\u200D\s]+'
    )

    def _strip_emoji_prefix(s: str) -> str:
        """Убирает ведущий эмодзи+пробел из name (иконка показывается отдельно)."""
        if not s:
            return s
        cleaned = _EMOJI_PREFIX_RE.sub('', s).strip()
        return cleaned or s

    def _landing_billing_modules() -> list:
        """Читает активные billing_modules из DB для лендинга."""
        import json as _json
        try:
            conn = sqlite3.connect(_SHOP_BOT_DB)
            cur = conn.cursor()
            cur.execute("""
                SELECT key, name, icon, price_monthly, description, features_json
                FROM billing_modules
                WHERE is_active = 1
                ORDER BY sort_order ASC, id ASC
            """)
            rows = cur.fetchall()
            conn.close()
            result = []
            for key, name, icon, price, desc, feat_json in rows:
                features = []
                if feat_json:
                    try:
                        features = _json.loads(feat_json)
                    except Exception:
                        pass
                result.append({
                    "key": key,
                    "name": _strip_emoji_prefix(name),
                    "icon": icon or "📦",
                    "price": int(price or 0),
                    "description": desc or "",
                    "features": features,
                })
            return result
        except Exception:
            return []

    templates.env.globals['landing_billing_modules'] = _landing_billing_modules

    def _landing_extensions() -> list:
        """Активные расширения, сгруппированные по родительскому модулю."""
        try:
            with sqlite3.connect(_SHOP_BOT_DB) as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT key, name, icon FROM billing_modules
                    WHERE is_active = 1 ORDER BY sort_order ASC, id ASC
                """)
                modules = cur.fetchall()
                cur.execute("""
                    SELECT module_key, name, icon, price_monthly, description
                    FROM billing_extensions
                    WHERE is_active = 1 ORDER BY sort_order ASC, id ASC
                """)
                ext_rows = cur.fetchall()
            by_mod: dict = {}
            for mk, name, icon, price, desc in ext_rows:
                by_mod.setdefault(mk, []).append({
                    "name": _strip_emoji_prefix(name),
                    "icon": icon or "🔧",
                    "price": int(price or 0),
                    "description": desc or "",
                })
            result = []
            for mk, mname, micon in modules:
                items = by_mod.get(mk)
                if items:
                    result.append({
                        "module_key": mk,
                        "module_name": _strip_emoji_prefix(mname),
                        "module_icon": micon or "📦",
                        "exts": items,
                    })
            return result
        except Exception:
            return []

    def _landing_bundles() -> list:
        """Активные пакеты модулей со скидкой для лендинга."""
        import json as _json
        try:
            with sqlite3.connect(_SHOP_BOT_DB) as conn:
                cur = conn.cursor()
                # Все модули (включая выключенные): состав/цена пакета должны
                # отражать реальную конфигурацию пакета, а не текущий статус модуля.
                cur.execute("SELECT key, name, price_monthly FROM billing_modules")
                mod_map = {r[0]: (r[1], int(r[2] or 0)) for r in cur.fetchall()}
                # Цены расширений — пакет может включать не только модули, но и
                # расширения (напр. «Аналитика Pro»); без них экономия считается неверно.
                cur.execute("SELECT key, name, price_monthly FROM billing_extensions")
                ext_map = {r[0]: (r[1], int(r[2] or 0)) for r in cur.fetchall()}
                cur.execute("""
                    SELECT key, name, icon, description, includes_json, price_monthly
                    FROM billing_bundles
                    WHERE is_active = 1 ORDER BY sort_order ASC, id ASC
                """)
                rows = cur.fetchall()
            result = []
            for key, name, icon, desc, inc_json, price in rows:
                try:
                    inc = _json.loads(inc_json or "{}")
                except Exception:
                    inc = {}
                mod_keys = inc.get("modules", []) or []
                ext_keys = inc.get("extensions", []) or []
                names = [mod_map[k][0] for k in mod_keys if k in mod_map]
                ext_names = [ext_map[k][0] for k in ext_keys if k in ext_map]
                full = (sum(mod_map[k][1] for k in mod_keys if k in mod_map)
                        + sum(ext_map[k][1] for k in ext_keys if k in ext_map))
                price_i = int(price or 0)
                result.append({
                    "name": name,
                    "icon": icon or "🎁",
                    "price": price_i,
                    "description": desc or "",
                    "module_names": names + ext_names,
                    "count": len(names) + len(ext_names),
                    "full_price": int(full),
                    "save": int(full - price_i) if full > price_i else 0,
                })
            return result
        except Exception:
            return []

    def _landing_pricing_meta() -> dict:
        """Реальные границы цен и число офферов для JSON-LD."""
        try:
            with sqlite3.connect(_SHOP_BOT_DB) as conn:
                cur = conn.cursor()
                cur.execute("SELECT price FROM subscription_plans WHERE is_active = 1")
                plans = [float(r[0] or 0) for r in cur.fetchall()]
                cur.execute("SELECT price_monthly FROM billing_modules WHERE is_active = 1")
                mods = [float(r[0] or 0) for r in cur.fetchall()]
                cur.execute("SELECT price_monthly FROM billing_extensions WHERE is_active = 1")
                exts = [float(r[0] or 0) for r in cur.fetchall()]
                cur.execute("SELECT price_monthly FROM billing_bundles WHERE is_active = 1")
                bund = [float(r[0] or 0) for r in cur.fetchall()]
            all_prices = plans + mods + exts + bund
            return {
                "low": int(min(all_prices)) if all_prices else 0,
                "high": int(max(all_prices)) if all_prices else 4000,
                "count": len(plans) + len(mods) + len(exts) + len(bund),
            }
        except Exception:
            return {"low": 0, "high": 4000, "count": 31}

    app.state.templates = templates

    static_dir = BASE_DIR / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    from web.routes.auth_routes import router as auth_router
    from web.routes.dashboard import router as dash_router
    from web.routes.sales import router as sales_router
    from web.routes.products import router as products_router
    from web.routes.inventory import router as inventory_router
    from web.routes.reports import router as reports_router
    from web.routes.rankings import router as rankings_router
    from web.routes.staff import router as staff_router
    from web.routes.org_structure import router as org_structure_router
    from web.routes.plans import router as plans_router
    from web.routes.salary import router as salary_router
    from web.routes.contests import router as contests_router
    from web.routes.settings import router as settings_router
    from web.routes.schedule import router as schedule_router
    from web.routes.integration import router as integration_router
    from web.routes.payments import router as payments_router
    from web.routes.notifications import router as notifications_router
    from web.routes.motivation import router as motivation_router
    from web.routes.subscription import router as subscription_router
    from web.routes.categories import router as categories_router
    from web.routes.promocodes import router as promocodes_router
    from web.routes.shops import router as shops_router
    from web.routes.pos import router as pos_router
    from web.routes.api import router as api_router
    from web.routes.absences import router as absences_router
    from web.routes.support import router as support_router
    from web.routes.chat import router as chat_router
    from web.routes.admin import router as admin_router
    from web.routes.admin_billing import router as admin_billing_router
    from web.routes.tasks import router as tasks_router
    from web.routes.email_auth import router as email_auth_router
    from web.routes.ai_routes import router as ai_router
    from web.routes.ai_insights import router as ai_insights_router
    from web.routes.security import router as security_router
    from web.routes.returns import router as returns_router
    from web.routes.clients import router as clients_router
    from web.routes.services import router as services_router
    from web.routes.appointments import router as appointments_router
    from web.routes.packages import router as packages_router

    app.include_router(auth_router)
    app.include_router(dash_router)
    app.include_router(sales_router)
    app.include_router(products_router)
    app.include_router(inventory_router)
    app.include_router(reports_router)
    app.include_router(rankings_router)
    app.include_router(staff_router)
    app.include_router(org_structure_router)
    app.include_router(plans_router)
    app.include_router(salary_router)
    app.include_router(contests_router)
    app.include_router(settings_router)
    app.include_router(schedule_router)
    app.include_router(integration_router)
    app.include_router(payments_router)
    app.include_router(notifications_router)
    app.include_router(motivation_router)
    app.include_router(subscription_router)
    app.include_router(categories_router)
    app.include_router(promocodes_router)
    app.include_router(shops_router)
    app.include_router(pos_router)
    app.include_router(api_router)
    app.include_router(absences_router)
    app.include_router(support_router)
    app.include_router(chat_router)
    app.include_router(admin_router)
    app.include_router(admin_billing_router)
    app.include_router(tasks_router)
    app.include_router(email_auth_router)
    app.include_router(ai_router)
    app.include_router(ai_insights_router)
    app.include_router(security_router)
    app.include_router(returns_router)
    app.include_router(clients_router)
    app.include_router(services_router)
    app.include_router(appointments_router)
    app.include_router(packages_router)

    _APK_LOCAL = Path("data/apk/DailySales-latest.apk")
    _APK_MIN_SIZE = 200_000  # 200 KB — реальные TWA APK ~500-600 KB

    async def _download_apk_to_local(apk_url: str) -> None:
        """Скачивает APK из GitHub Releases на persistent volume Amvera.

        ВАЖНО: для ПРИВАТНОГО репозитория `browser_download_url`
        (github.com/.../releases/download/TAG/NAME) отдаёт HTTP 404 даже с
        Bearer-токеном. Нужно резолвить его в API-URL ассета
        (api.github.com/.../releases/assets/ID) и качать с заголовком
        `Accept: application/octet-stream`.
        """
        import logging as _log
        import aiohttp
        import re as _re
        dest = _APK_LOCAL
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp")
        _gh_token = os.environ.get("GITHUB_TOKEN", "")
        try:
            async with aiohttp.ClientSession() as session:
                # 1) Резолвим browser_download_url → API-URL ассета (приватный репо).
                download_url = apk_url
                if _gh_token and "github.com" in apk_url and "/releases/assets/" not in apk_url:
                    m = _re.search(
                        r"github\.com/([^/]+)/([^/]+)/releases/download/([^/]+)/(.+)$",
                        apk_url,
                    )
                    if m:
                        owner, repo, tag, name = m.groups()
                        api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}"
                        try:
                            async with session.get(
                                api_url,
                                headers={
                                    "Authorization": f"Bearer {_gh_token}",
                                    "Accept": "application/vnd.github+json",
                                    "X-GitHub-Api-Version": "2022-11-28",
                                    "User-Agent": "DailySales-Server/1.0",
                                },
                                timeout=aiohttp.ClientTimeout(total=20),
                            ) as r:
                                if r.status == 200:
                                    rel = await r.json()
                                    for a in rel.get("assets", []):
                                        if a.get("name") == name:
                                            download_url = a.get("url") or download_url
                                            break
                                else:
                                    _log.warning(f"APK asset resolve HTTP {r.status} for tag {tag}")
                        except Exception as _re_err:
                            _log.warning(f"APK asset resolve failed: {_re_err}")

                # 2) Качаем. Для API-URL ассета обязателен Accept: application/octet-stream.
                _headers = {"User-Agent": "DailySales-Server/1.0"}
                if _gh_token and "github.com" in download_url:
                    _headers["Authorization"] = f"Bearer {_gh_token}"
                    if "/releases/assets/" in download_url:
                        _headers["Accept"] = "application/octet-stream"
                async with session.get(
                    download_url,
                    headers=_headers,
                    timeout=aiohttp.ClientTimeout(total=180),
                    allow_redirects=True,
                ) as resp:
                    if resp.status != 200:
                        _log.error(f"APK download HTTP {resp.status}: {download_url}")
                        return
                    with open(tmp, "wb") as f:
                        async for chunk in resp.content.iter_chunked(65536):
                            f.write(chunk)
            if tmp.exists() and tmp.stat().st_size >= _APK_MIN_SIZE:
                tmp.replace(dest)
                _log.info(f"APK saved to {dest} ({dest.stat().st_size:,} bytes)")
            else:
                tmp.unlink(missing_ok=True)
                _log.error(f"APK download too small or missing: {apk_url} ({tmp.stat().st_size if tmp.exists() else 0} bytes)")
        except Exception as e:
            tmp.unlink(missing_ok=True)
            _log.error(f"APK download failed: {e}")

    @app.get("/download/android", include_in_schema=False)
    async def download_android(request: Request):
        try:
            import anyio
            referrer = request.headers.get("referer", "") or ""
            user_agent = request.headers.get("user-agent", "") or ""
            _tg_id = ""
            _org_db = ""
            try:
                from web.auth import get_session_user as _gsu
                _u = _gsu(request)
                if _u:
                    _tg_id = str(_u.get("sub", ""))
                    _org_db = str(_u.get("org_db", "") or "")
            except Exception:
                pass
            tg_id_val, org_db_val = _tg_id, _org_db
            def _log():
                import sqlite3 as _sq
                c = _sq.connect(_SHOP_BOT_DB, timeout=5)
                try:
                    c.execute(
                        "INSERT INTO download_events (referrer, user_agent, telegram_id, owner_id) VALUES (?, ?, ?, ?)",
                        (referrer[:512], user_agent[:512], tg_id_val, org_db_val),
                    )
                    c.commit()
                finally:
                    c.close()
            await anyio.to_thread.run_sync(_log)
        except Exception:
            logger.debug("download/android: не удалось записать download_events", exc_info=True)
        # Сначала отдаём локальный APK с Amvera persistent volume
        if _APK_LOCAL.exists() and _APK_LOCAL.stat().st_size >= _APK_MIN_SIZE:
            from fastapi.responses import FileResponse
            return FileResponse(
                str(_APK_LOCAL),
                media_type="application/vnd.android.package-archive",
                filename="DailySales.apk",
            )
        # Локального файла нет — запускаем фоновую загрузку и показываем страницу ожидания.
        try:
            _apk_url_now = None
            try:
                _c2 = sqlite3.connect(_SHOP_BOT_DB, timeout=3)
                _row2 = _c2.execute(
                    "SELECT value FROM payment_settings WHERE key='apk_download_url'"
                ).fetchone()
                _c2.close()
                if _row2 and _row2[0]:
                    _apk_url_now = _row2[0]
            except Exception:
                pass
            if _apk_url_now:
                import asyncio as _aio
                _aio.ensure_future(_download_apk_to_local(_apk_url_now))
        except Exception:
            pass
        # Страница ожидания — пользователь остаётся на домене Amvera, авторефреш каждые 6 с.
        from fastapi.responses import HTMLResponse
        return HTMLResponse(
            content="""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="6;url=/download/android">
<title>APK — DailySales</title>
<style>
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#f0fdf4;color:#166534;}
  .card{text-align:center;padding:2.5rem 2rem;background:#fff;border-radius:1.25rem;box-shadow:0 4px 24px rgba(0,0,0,.08);max-width:340px;width:90%;}
  .icon{font-size:3rem;margin-bottom:1rem;}
  h1{font-size:1.25rem;font-weight:700;margin:0 0 .5rem;}
  p{font-size:.875rem;color:#4b5563;margin:0 0 1.5rem;line-height:1.5;}
  .spinner{width:2rem;height:2rem;border:3px solid #d1fae5;border-top-color:#16a34a;border-radius:50%;animation:spin 0.8s linear infinite;margin:.5rem auto 0;}
  @keyframes spin{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<div class="card">
  <div class="icon">📱</div>
  <h1>Подготовка APK…</h1>
  <p>Файл загружается на сервер. Страница обновится автоматически — скачивание начнётся через несколько секунд.</p>
  <div class="spinner"></div>
</div>
</body>
</html>""",
            status_code=200,
        )

    @app.post("/webhook/apk-release", include_in_schema=False)
    async def webhook_apk_release(request: Request, background_tasks: BackgroundTasks):
        import hmac
        from fastapi.responses import JSONResponse
        secret = os.environ.get("APK_WEBHOOK_SECRET", "")
        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        if not secret or not hmac.compare_digest(token.encode(), secret.encode()):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        version = str(body.get("version", "")).strip()
        release_url = str(body.get("release_url", "")).strip()
        release_date = str(body.get("release_date", "")).strip()
        apk_url = str(body.get("apk_url", "")).strip()
        release_notes = str(body.get("release_notes", "")).strip()
        _native_raw = body.get("native", False)
        if isinstance(_native_raw, bool):
            native = _native_raw
        elif isinstance(_native_raw, str):
            native = _native_raw.strip().lower() in ("true", "1", "yes")
        elif isinstance(_native_raw, int):
            native = bool(_native_raw)
        else:
            native = False
        if not native and "[native]" in release_notes.lower():
            native = True
        if not version:
            return JSONResponse({"error": "version required"}, status_code=400)
        try:
            conn = sqlite3.connect(_SHOP_BOT_DB)
            conn.execute(
                "INSERT OR REPLACE INTO payment_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                ("apk_latest_version", version),
            )
            if release_url:
                conn.execute(
                    "INSERT OR REPLACE INTO payment_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                    ("apk_release_url", release_url),
                )
            if apk_url:
                conn.execute(
                    "INSERT OR REPLACE INTO payment_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                    ("apk_download_url", apk_url),
                )
            if release_date:
                conn.execute(
                    "INSERT OR REPLACE INTO payment_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                    ("apk_release_date", release_date),
                )
            conn.execute(
                """INSERT INTO apk_release_history (version, release_url, release_date)
                   VALUES (?, ?, ?)""",
                (version, release_url, release_date),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            import logging as _logging
            _logging.error(f"webhook_apk_release DB error: {e}")
            return JSONResponse({"error": "DB error"}, status_code=500)
        if native:
            try:
                import bot_holder as _bh
                import asyncio as _asyncio
                bot = _bh.get_bot()
                admin_id = os.environ.get("ADMIN_CHAT_ID", "").strip()
                _dl_link = apk_url or release_url
                if bot and admin_id:
                    from notif_utils import add_read_btn as _add_read_btn
                    msg_text = (
                        f"🔧 <b>Обновление оболочки APK опубликовано!</b>\n\n"
                        f"Версия: <code>{version}</code>\n"
                        f"Дата: {release_date or '—'}\n"
                        f"Веб-интерфейс уже обновлён автоматически.\n"
                    )
                    if _dl_link:
                        msg_text += f'<a href="{_dl_link}">⬇️ Скачать APK</a>'
                    if _main_loop and not _main_loop.is_closed():
                        _asyncio.run_coroutine_threadsafe(
                            bot.send_message(
                                int(admin_id), msg_text,
                                parse_mode="HTML",
                                disable_web_page_preview=True,
                                reply_markup=_add_read_btn(),
                            ),
                            _main_loop,
                        )
                # Notify all active org owners (skip super-admin already notified above)
                if bot and _main_loop and not _main_loop.is_closed():
                    owner_msg = (
                        f"🔧 <b>Обновление оболочки приложения</b> <code>v{version}</code>\n"
                        f"Веб-интерфейс уже обновлён автоматически — функции доступны без переустановки.\n"
                        f"Рекомендуем обновить APK для получения системных улучшений оболочки."
                    )
                    if _dl_link:
                        owner_msg += f'\n<a href="{_dl_link}">⬇️ Скачать APK</a>'
                    try:
                        _conn = sqlite3.connect("data/main.db")
                        _rows = _conn.execute(
                            "SELECT DISTINCT owner_id FROM organizations "
                            "WHERE is_active = 1 AND owner_id IS NOT NULL"
                        ).fetchall()
                        _conn.close()
                        def _apk_pref_ok(tid):
                            try:
                                c = sqlite3.connect("data/main.db")
                                c.execute(
                                    "CREATE TABLE IF NOT EXISTS apk_notif_prefs "
                                    "(telegram_id INTEGER PRIMARY KEY, enabled INTEGER DEFAULT 0)"
                                )
                                row = c.execute(
                                    "SELECT enabled FROM apk_notif_prefs WHERE telegram_id=?",
                                    (tid,),
                                ).fetchone()
                                c.close()
                                return bool(row[0]) if row else False
                            except Exception:
                                return False

                        owner_ids = [
                            r[0] for r in _rows
                            if r[0] and str(r[0]) != admin_id
                            and _apk_pref_ok(r[0])
                        ]
                    except Exception:
                        owner_ids = []

                    from notif_utils import add_read_btn as _add_read_btn_o

                    async def _notify_owners(_bot, _ids, _text):
                        for _tg_id in _ids:
                            try:
                                await _bot.send_message(
                                    _tg_id, _text,
                                    parse_mode="HTML",
                                    disable_web_page_preview=True,
                                    reply_markup=_add_read_btn_o(),
                                )
                            except Exception:
                                pass
                            await _asyncio.sleep(0.05)

                    if owner_ids:
                        _asyncio.run_coroutine_threadsafe(
                            _notify_owners(bot, owner_ids, owner_msg),
                            _main_loop,
                        )
            except Exception:
                pass
        # Фоновое скачивание APK на persistent volume Amvera
        if apk_url:
            background_tasks.add_task(_download_apk_to_local, apk_url)
        return JSONResponse({"ok": True, "version": version})

    @app.post("/webhook/apk-binary", include_in_schema=False)
    async def webhook_apk_binary(request: Request):
        """Принимает готовый APK-бинарь напрямую от GitHub Actions и сохраняет
        его на persistent volume Amvera. Не требует доступа сервера к GitHub
        (приватный репозиторий) — Actions сам шлёт файл."""
        import hmac
        import logging as _logging
        from fastapi.responses import JSONResponse
        secret = os.environ.get("APK_WEBHOOK_SECRET", "")
        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        if not secret or not hmac.compare_digest(token.encode(), secret.encode()):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        version = request.headers.get("X-APK-Version", "").strip()
        _APK_MAX_SIZE = 50 * 1024 * 1024  # 50 MB hard cap — guard against OOM
        try:
            _clen = int(request.headers.get("content-length", "0"))
        except (TypeError, ValueError):
            _clen = 0
        if _clen > _APK_MAX_SIZE:
            return JSONResponse(
                {"error": f"file too large ({_clen} bytes)"},
                status_code=413,
            )
        body = await request.body()
        if len(body) > _APK_MAX_SIZE:
            return JSONResponse({"error": "file too large"}, status_code=413)
        if len(body) < _APK_MIN_SIZE:
            return JSONResponse(
                {"error": f"file too small ({len(body)} bytes)"},
                status_code=400,
            )
        try:
            _APK_LOCAL.parent.mkdir(parents=True, exist_ok=True)
            tmp = _APK_LOCAL.with_suffix(".apk.tmp")
            tmp.write_bytes(body)
            tmp.replace(_APK_LOCAL)
        except Exception as e:
            _logging.error(f"webhook_apk_binary save error: {e}")
            return JSONResponse({"error": "save failed"}, status_code=500)
        _logging.info(
            f"APK binary received via webhook: {len(body):,} bytes "
            f"(version={version or 'n/a'}) → {_APK_LOCAL}"
        )
        return JSONResponse(
            {"ok": True, "bytes": len(body), "version": version}
        )

    @app.get("/.well-known/assetlinks.json", include_in_schema=False)
    async def assetlinks():
        from fastapi.responses import JSONResponse
        data = [{
            "relation": ["delegate_permission/common.handle_all_urls"],
            "target": {
                "namespace": "android_app",
                "package_name": "com.dailysales.app",
                "sha256_cert_fingerprints": [
                    "25:E3:EB:BB:2B:72:C7:12:CB:15:59:AD:1C:E9:6B:20:8A:4E:EB:19:97:B9:93:8F:37:47:31:96:82:BB:A1:1D"
                ]
            }
        }]
        return JSONResponse(data, headers={"Cache-Control": "no-cache"})

    @app.get("/robots.txt", include_in_schema=False)
    async def robots_txt():
        return PlainTextResponse(_ROBOTS_TXT, media_type="text/plain")

    @app.get("/sitemap.xml", include_in_schema=False)
    async def sitemap_xml():
        from fastapi.responses import Response
        return Response(_SITEMAP_XML, media_type="application/xml")

    @app.get("/sw.js")
    async def service_worker(request: Request):
        """Serve SW from root so it can control the full site scope."""
        import pathlib
        from fastapi.responses import FileResponse
        sw_path = pathlib.Path(__file__).parent / "static" / "sw.js"
        return FileResponse(str(sw_path), headers={
            "Service-Worker-Allowed": "/",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Content-Type": "application/javascript",
        })

    @app.get("/")
    async def root(request: Request):
        from web.auth import get_session_user
        user = get_session_user(request)
        if user:
            return RedirectResponse(url="/dashboard", status_code=302)
        return templates.TemplateResponse(request, "landing.html", {
            "bot_username": templates.env.globals.get("bot_username", ""),
            "billing_modules": _landing_billing_modules(),
            "landing_plans": _get_landing_plans(),
            "landing_extensions": _landing_extensions(),
            "landing_bundles": _landing_bundles(),
            "pricing_meta": _landing_pricing_meta(),
        })

    @app.get("/privacy", include_in_schema=False)
    async def privacy_policy(request: Request):
        return templates.TemplateResponse(request, "privacy.html", {})

    @app.exception_handler(404)
    async def not_found(request: Request, exc):
        return templates.TemplateResponse(
            request, "errors/404.html",
            {"user": None},
            status_code=404,
        )

    @app.exception_handler(500)
    async def internal_error(request: Request, exc):
        import logging as _logging
        _logging.error(f"500 error on {request.url}: {exc}")
        return templates.TemplateResponse(
            request, "errors/500.html",
            {"user": None},
            status_code=500,
        )

    @app.exception_handler(Exception)
    async def unhandled_exception(request: Request, exc):
        import logging as _logging
        _logging.error(f"Unhandled exception on {request.url}: {exc}", exc_info=True)
        return templates.TemplateResponse(
            request, "errors/500.html",
            {"user": None},
            status_code=500,
        )

    @app.on_event("startup")
    async def _startup_ensure_apk():
        """При старте: если APK отсутствует на persistent volume — скачать с GitHub Releases."""
        import logging as _log
        import aiohttp
        if _APK_LOCAL.exists() and _APK_LOCAL.stat().st_size >= _APK_MIN_SIZE:
            _log.info(f"APK already cached: {_APK_LOCAL} ({_APK_LOCAL.stat().st_size:,} bytes)")
            return
        try:
            api_url = "https://api.github.com/repos/pdkiller666/Daily-Sales/releases/latest"
            headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
            token = os.environ.get("GITHUB_TOKEN", "")
            if token:
                headers["Authorization"] = f"Bearer {token}"
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        _log.warning(f"startup APK check: GitHub API returned {resp.status}")
                        return
                    release = await resp.json()
            assets = release.get("assets", [])
            apk_asset = next((a for a in assets if a.get("name", "").endswith(".apk")), None)
            if not apk_asset:
                _log.warning("startup APK check: no .apk asset in latest GitHub release")
                return
            apk_url = apk_asset["browser_download_url"]
            version = release.get("tag_name", "")
            _log.info(f"startup APK check: downloading {version} from {apk_url}")
            await _download_apk_to_local(apk_url)
            # Сохраняем версию и URL в БД для webhook-совместимости
            try:
                conn = sqlite3.connect(_SHOP_BOT_DB)
                for k, v in [("apk_latest_version", version), ("apk_download_url", apk_url)]:
                    conn.execute(
                        "INSERT OR REPLACE INTO payment_settings (key, value) VALUES (?, ?)", (k, v)
                    )
                conn.commit()
                conn.close()
            except Exception:
                pass
        except Exception as e:
            _log.warning(f"startup APK check failed: {e}")

    return app
