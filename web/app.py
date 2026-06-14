import asyncio
import os
import sqlite3
import time as _time
from pathlib import Path
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse, PlainTextResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware

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
        pass
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


def _fmt_currency(amount) -> str:
    try:
        v = int(float(amount or 0))
        return f"{v:,}".replace(',', '\u00a0') + "\u00a0₽"
    except Exception:
        return "0\u00a0₽"


_CSP = (
    "default-src 'self'; "
    # 'unsafe-eval' is required by Alpine.js 3 which uses new Function() to
    # evaluate x-* expressions.  Without it the browser blocks every directive
    # evaluation, Alpine crashes silently, x-cloak is removed but x-show is
    # never applied, and all event handlers are dead.
    "script-src 'self' 'unsafe-inline' 'unsafe-eval' "
    "https://unpkg.com https://cdn.jsdelivr.net https://cdn.tailwindcss.com "
    "https://telegram.org; "
    "style-src 'self' 'unsafe-inline' "
    "https://cdn.tailwindcss.com https://cdn.jsdelivr.net "
    "https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com data:; "
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
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(self)"
        )
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Content-Security-Policy"] = _CSP
        # HSTS — only on HTTPS (Amvera/production serves via HTTPS)
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        path = request.url.path
        ct = response.headers.get("content-type", "")
        if path.startswith("/static/"):
            # Long-lived cache for immutable static assets (versioned by deploy)
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif "text/html" in ct or (
            not path.startswith("/sw.js")
            and not path.startswith("/api/")
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
Disallow: /chat/dm
Disallow: /ws/dm
Disallow: /tasks
Disallow: /tasks/attachment/
Disallow: /api/
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
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    templates.env.auto_reload = False
    templates.env.filters['fmt_date'] = _fmt_date
    templates.env.filters['fmt_datetime'] = _fmt_datetime
    templates.env.filters['fmt_currency'] = _fmt_currency

    from timezone_utils import get_user_time as _get_user_time

    def _fmt_sale_dt(s, tz: str = "Europe/Moscow", fmt: str = "%d.%m.%Y %H:%M") -> str:
        """Convert UTC sale_date string to user local time and format it."""
        if not s:
            return "—"
        try:
            local_dt = _get_user_time(str(s), tz or "Europe/Moscow")
            if local_dt:
                return local_dt.strftime(fmt)
        except Exception:
            pass
        return _fmt_date(str(s)[:10])

    def _fmt_sale_time(s, tz: str = "Europe/Moscow") -> str:
        """Return only the HH:MM part of a sale_date, converted to user's TZ."""
        if not s:
            return "—"
        try:
            local_dt = _get_user_time(str(s), tz or "Europe/Moscow")
            if local_dt:
                return local_dt.strftime("%H:%M")
        except Exception:
            pass
        raw = str(s)
        return raw[11:16] if len(raw) > 10 else "—"

    templates.env.filters['fmt_sale_dt'] = _fmt_sale_dt
    templates.env.filters['fmt_sale_time'] = _fmt_sale_time

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
            import sqlite3
            conn = sqlite3.connect("data/main.db")
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
            conn = sqlite3.connect(_SHOP_BOT_DB)
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

    def _is_chat_enabled() -> bool:
        """Return True if chat is not disabled (chat_min_plan != 'Отключён')."""
        try:
            conn = sqlite3.connect(_SHOP_BOT_DB)
            row = conn.execute(
                "SELECT value FROM payment_settings WHERE key='chat_min_plan'"
            ).fetchone()
            conn.close()
            if row is None:
                return True  # key absent → chat enabled by default
            return row[0] != "Отключён"
        except Exception:
            return True

    templates.env.globals['chat_enabled'] = _is_chat_enabled

    def _open_tasks_count(request):
        """Счётчик незакрытых задач для сайдбара."""
        try:
            from web.auth import get_session_user
            from web.deps import get_web_db
            user = get_session_user(request)
            if not user:
                return 0
            telegram_id = int(user["sub"])
            org_db = user.get("org_db")
            if not org_db or org_db == "data/shop_bot.db":
                return 0
            is_admin = user.get("role") in ("owner", "admin", "super_admin")
            db = get_web_db(telegram_id, org_db)
            conn = db.get_connection()
            my_row = conn.execute(
                "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
            conn.close()
            my_db_id = my_row[0] if my_row else 0
            if not my_db_id:
                return 0
            return db.get_open_tasks_count(my_db_id, is_admin)
        except Exception:
            return 0

    templates.env.globals['open_tasks_count'] = _open_tasks_count

    def _dm_unread_count(request):
        """Счётчик непрочитанных ЛС для сайдбара."""
        try:
            from web.auth import get_session_user
            from web.deps import get_web_db
            user = get_session_user(request)
            if not user:
                return 0
            telegram_id = int(user["sub"])
            org_db = user.get("org_db")
            if not org_db or org_db == "data/shop_bot.db":
                return 0
            db = get_web_db(telegram_id, org_db)
            conn = db.get_connection()
            my_row = conn.execute(
                "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
            conn.close()
            if not my_row:
                return 0
            return db.get_dm_unread_count(my_row[0])
        except Exception:
            return 0

    templates.env.globals['dm_unread_count'] = _dm_unread_count

    _NAV_MODULE_KEYS = ('analytics', 'team', 'plans_motivation', 'chat',
                        'integrations', 'notifications', 'ai_assistant')

    def _nav_modules(request):
        """Returns dict {module_key: bool} for nav visibility gating. Fails open.

        Per-user override (user_module_access): 'deny' прячет модуль у сотрудника,
        'allow' принудительно показывает; None — наследует биллинг (has_module).
        """
        try:
            from web.auth import get_session_user
            from billing_utils import has_module
            from db_utils import get_user_module_access
            from tenant_manager import tenant_manager
            user = get_session_user(request)
            if not user:
                return {k: False for k in _NAV_MODULE_KEYS}
            tg_id = int(user["sub"])
            owner_tg = None
            result = {}
            for k in _NAV_MODULE_KEYS:
                override = get_user_module_access(tg_id, k)
                if override == 'deny':
                    # Явный запрет — модуль скрыт даже если оплачен
                    result[k] = False
                elif override == 'allow':
                    # Явная выдача сотруднику — но только в пределах оплаченного
                    # организацией (биллинг привязан к владельцу). НЕ обход оплаты.
                    if owner_tg is None:
                        owner_tg = tenant_manager.get_org_owner_tg(tg_id) or tg_id
                    result[k] = has_module(owner_tg, k)
                else:
                    result[k] = has_module(tg_id, k)
            return result
        except Exception:
            return {k: True for k in _NAV_MODULE_KEYS}

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
                names = [mod_map[k][0] for k in mod_keys if k in mod_map]
                full = sum(mod_map[k][1] for k in mod_keys if k in mod_map)
                price_i = int(price or 0)
                result.append({
                    "name": name,
                    "icon": icon or "🎁",
                    "price": price_i,
                    "description": desc or "",
                    "module_names": names,
                    "count": len(names),
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

    _APK_LOCAL = Path("data/apk/DailySales-latest.apk")
    _APK_MIN_SIZE = 1_000_000  # 1 MB — минимальный размер валидного APK

    async def _download_apk_to_local(apk_url: str) -> None:
        """Скачивает APK из GitHub Releases на persistent volume Amvera."""
        import logging as _log
        import aiohttp
        dest = _APK_LOCAL
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    apk_url,
                    timeout=aiohttp.ClientTimeout(total=180),
                    allow_redirects=True,
                ) as resp:
                    if resp.status != 200:
                        _log.error(f"APK download HTTP {resp.status}: {apk_url}")
                        return
                    with open(tmp, "wb") as f:
                        async for chunk in resp.content.iter_chunked(65536):
                            f.write(chunk)
            if tmp.exists() and tmp.stat().st_size >= _APK_MIN_SIZE:
                tmp.replace(dest)
                _log.info(f"APK saved to {dest} ({dest.stat().st_size:,} bytes)")
            else:
                tmp.unlink(missing_ok=True)
                _log.error(f"APK download too small or missing: {apk_url}")
        except Exception as e:
            tmp.unlink(missing_ok=True)
            _log.error(f"APK download failed: {e}")

    @app.get("/download/android", include_in_schema=False)
    async def download_android(request: Request):
        try:
            import anyio
            referrer = request.headers.get("referer", "") or ""
            user_agent = request.headers.get("user-agent", "") or ""
            def _log():
                import sqlite3 as _sq
                c = _sq.connect(_SHOP_BOT_DB, timeout=5)
                try:
                    c.execute(
                        "INSERT INTO download_events (referrer, user_agent) VALUES (?, ?)",
                        (referrer[:512], user_agent[:512]),
                    )
                    c.commit()
                finally:
                    c.close()
            await anyio.to_thread.run_sync(_log)
        except Exception:
            pass
        # Сначала отдаём локальный APK с Amvera persistent volume
        if _APK_LOCAL.exists() and _APK_LOCAL.stat().st_size >= _APK_MIN_SIZE:
            from fastapi.responses import FileResponse
            return FileResponse(
                str(_APK_LOCAL),
                media_type="application/vnd.android.package-archive",
                filename="DailySales.apk",
            )
        # Fallback: прямая ссылка на APK (не страница GitHub)
        from fastapi.responses import RedirectResponse
        try:
            conn = sqlite3.connect(_SHOP_BOT_DB)
            rows = conn.execute(
                "SELECT key, value FROM payment_settings WHERE key IN ('apk_download_url', 'apk_release_url')"
            ).fetchall()
            conn.close()
            settings = {r[0]: r[1] for r in rows if r[1]}
            # Приоритет: прямая ссылка на файл → страница релиза → GitHub latest
            direct = settings.get("apk_download_url") or settings.get("apk_release_url")
            if direct:
                return RedirectResponse(direct, status_code=302)
        except Exception:
            pass
        return RedirectResponse(
            "https://github.com/pdkiller666/Daily-Sales/releases/latest",
            status_code=302,
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
        try:
            import bot_holder as _bh
            import asyncio as _asyncio
            bot = _bh.get_bot()
            admin_id = os.environ.get("ADMIN_CHAT_ID", "").strip()
            _dl_link = apk_url or release_url
            if bot and admin_id:
                from notif_utils import add_read_btn as _add_read_btn
                msg_text = (
                    f"📱 <b>Новая версия APK опубликована!</b>\n\n"
                    f"Версия: <code>{version}</code>\n"
                    f"Дата: {release_date or '—'}\n"
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
                owner_msg = f"📱 <b>Доступна новая версия приложения</b> <code>v{version}</code>"
                if _dl_link:
                    owner_msg += f'\n<a href="{_dl_link}">⬇️ Скачать APK</a>'
                try:
                    _conn = sqlite3.connect("data/main.db")
                    _rows = _conn.execute(
                        "SELECT DISTINCT owner_id FROM organizations "
                        "WHERE is_active = 1 AND owner_id IS NOT NULL"
                    ).fetchall()
                    _conn.close()
                    owner_ids = [
                        r[0] for r in _rows
                        if r[0] and str(r[0]) != admin_id
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
