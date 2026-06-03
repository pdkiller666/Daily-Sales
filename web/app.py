import asyncio
import os
import sqlite3
import time as _time
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse, PlainTextResponse
from starlette.middleware.base import BaseHTTPMiddleware

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
                   max_sales_per_month, can_export_reports, can_view_analytics,
                   can_use_notifications, can_use_integrations
            FROM subscription_plans WHERE is_active = 1 ORDER BY price ASC
        """)
        for row in cur.fetchall():
            name, price, days, mp, ms, msal, exp, anal, notif, integ = row
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
                "can_export": bool(exp),
                "can_analytics": bool(anal),
                "can_notifications": bool(notif),
                "can_integrations": bool(integ),
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
    "script-src 'self' 'unsafe-inline' "
    "https://unpkg.com https://cdn.jsdelivr.net https://cdn.tailwindcss.com "
    "https://telegram.org; "
    "style-src 'self' 'unsafe-inline' "
    "https://cdn.tailwindcss.com https://cdn.jsdelivr.net "
    "https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    "img-src 'self' data: blob: https:; "
    "connect-src 'self'; "
    "frame-src https://telegram.org; "
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
Disallow: /rankings
Disallow: /staff
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
Disallow: /api/
Disallow: /login
Disallow: /auth/
Disallow: /admin

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

    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    templates.env.filters['fmt_date'] = _fmt_date
    templates.env.filters['fmt_datetime'] = _fmt_datetime
    templates.env.filters['fmt_currency'] = _fmt_currency

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

    app.include_router(auth_router)
    app.include_router(dash_router)
    app.include_router(sales_router)
    app.include_router(products_router)
    app.include_router(inventory_router)
    app.include_router(reports_router)
    app.include_router(rankings_router)
    app.include_router(staff_router)
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
            "plans": _get_landing_plans(),
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

    return app
