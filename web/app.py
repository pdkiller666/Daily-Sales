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
        title="ShopBot Web",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    templates.env.filters['fmt_date'] = _fmt_date
    templates.env.filters['fmt_currency'] = _fmt_currency

    import bot_holder
    templates.env.globals['bot_username'] = lambda: bot_holder.get_username() or ''

    app.state.templates = templates

    static_dir = BASE_DIR / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    from web.routes.auth_routes import router as auth_router
    from web.routes.dashboard import router as dash_router

    app.include_router(auth_router)
    app.include_router(dash_router)

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
