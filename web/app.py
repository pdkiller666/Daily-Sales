import os
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse

BASE_DIR = Path(__file__).parent


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

    @app.get("/")
    async def root():
        return RedirectResponse(url="/dashboard", status_code=302)

    @app.exception_handler(404)
    async def not_found(request: Request, exc):
        return templates.TemplateResponse(
            request, "errors/404.html",
            {"user": None},
            status_code=404,
        )

    return app
