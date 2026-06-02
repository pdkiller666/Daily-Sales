import os
import sqlite3
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse

BASE_DIR = Path(__file__).parent
_SHOP_BOT_DB = "data/shop_bot.db"


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


def _fmt_currency(amount) -> str:
    try:
        v = int(float(amount or 0))
        return f"{v:,}".replace(',', '\u00a0') + "\u00a0₽"
    except Exception:
        return "0\u00a0₽"


def create_web_app() -> FastAPI:
    app = FastAPI(
        title="DailySales Web",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    templates.env.filters['fmt_date'] = _fmt_date
    templates.env.filters['fmt_currency'] = _fmt_currency

    import bot_holder
    templates.env.globals['bot_username'] = lambda: bot_holder.get_username() or ''

    from web.routes.payments import get_pending_count
    templates.env.globals['pending_payments_count'] = get_pending_count

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

    return app
