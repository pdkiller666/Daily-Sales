import json
import logging
import sqlite3
import time
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse
from typing import Annotated

router = APIRouter()

# In-memory Device Flow state: {telegram_id: {device_code, conn_id, user_code, verify_url, expires_at}}
_device_flow: dict[int, dict] = {}


def _purge_expired_device_flow() -> None:
    """Remove stale OAuth sessions that were never completed."""
    now = time.time()
    stale = [tid for tid, s in _device_flow.items() if now > s.get("expires_at", 0)]
    for tid in stale:
        _device_flow.pop(tid, None)


def _check_plan(telegram_id: int) -> tuple[bool, str]:
    """Returns (can_use_integrations, plan_name).
    Delegated to modular billing system (has_module 'integrations')."""
    try:
        from billing_utils import has_module
        return has_module(telegram_id, "integrations"), "—"
    except Exception as e:
        logging.error(f"_check_plan: {e}")
        return False, "—"


def _google_secrets_configured() -> bool:
    import os
    return bool(os.environ.get("GOOGLE_OAUTH_CLIENT_ID") and
                os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET"))


@router.get("/integration")
def integration_page(
    request: Request,
    auth: str = "",
    conn_id: int = 0,
    user_code: str = "",
    verify_url: str = "",
    msg: str = "",
    error: str = "",
):
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    _purge_expired_device_flow()
    can_use, plan_name = _check_plan(telegram_id)
    has_secrets = _google_secrets_configured()

    ctx: dict = {
        "request": request, "user": user,
        "is_admin": True,
        "can_use": can_use,
        "plan_name": plan_name,
        "has_secrets": has_secrets,
        "connections": [],
        "conn_exports": {},
        "conn_logs": {},
        "auth_pending": auth == "pending",
        "auth_conn_id": conn_id,
        "auth_user_code": user_code,
        "auth_verify_url": verify_url or "https://google.com/device",
        "msg": msg,
        "error": error,
        "csrf_token": "",
    }

    from web.auth import get_csrf_token
    ctx["csrf_token"] = get_csrf_token(request)

    if can_use:
        try:
            db = get_web_db(telegram_id, org_db)
            raw_conns = db.get_integration_connections() or []
            conns = []
            conn_exports: dict[int, list] = {}
            conn_logs: dict[int, list] = {}
            for c in raw_conns:
                cid = c[0]
                try:
                    cfg = json.loads(c[3] or "{}")
                except Exception:
                    cfg = {}
                has_token = bool(cfg.get("tokens", {}).get("access_token"))
                motiv_cfg = cfg.get("motiv_config") or {}
                motiv_preview = None
                if motiv_cfg:
                    try:
                        cache = db.get_bonus_cache(cid) or []
                        chains = sorted({r[1] for r in cache if r[1]})
                        models = {r[0] for r in cache if r[0]}
                        synced_ats = [r[4] for r in cache if r[4]]
                        last_synced = (
                            max(synced_ats)[:16].replace("T", " ") if synced_ats else ""
                        )
                        motiv_preview = {
                            "sheet": motiv_cfg.get("sheet_name", ""),
                            "chains": chains,
                            "model_count": len(models),
                            "last_synced": last_synced,
                            "alias_count": len(motiv_cfg.get("aliases") or {}),
                        }
                    except Exception:
                        motiv_preview = None
                conns.append({
                    "id": cid,
                    "name": c[1] or "—",
                    "provider": c[2] or "google_sheets",
                    "enabled": bool(c[4]),
                    "created_at": (c[5] or "")[:10],
                    "spreadsheet_id": cfg.get("spreadsheet_id", ""),
                    "has_token": has_token,
                    "token_expiry": cfg.get("tokens", {}).get("expiry", 0),
                    "has_motiv_config": bool(motiv_cfg),
                    "motiv_sheet": motiv_cfg.get("sheet_name", ""),
                    "motiv_preview": motiv_preview,
                })
                try:
                    exports_raw = db.get_integration_exports(cid) or []
                    conn_exports[cid] = [
                        {
                            "id": e[0], "export_type": e[1],
                            "enabled": bool(e[2]), "schedule": e[3] or "immediate",
                            "target_sheet": e[4] or "", "operation": e[5] or "",
                            "last_run": (e[8] or "")[:16].replace("T", " "),
                        }
                        for e in exports_raw
                    ]
                except Exception:
                    conn_exports[cid] = []
                try:
                    logs_raw = db.get_integration_logs(cid, limit=10) or []
                    conn_logs[cid] = [
                        {
                            "id": l[0], "status": l[3] or "",
                            "message": (l[4] or "")[:200],
                            "created_at": (l[5] or "")[:16].replace("T", " "),
                        }
                        for l in logs_raw
                    ]
                except Exception:
                    conn_logs[cid] = []
            ctx["connections"] = conns
            ctx["conn_exports"] = conn_exports
            ctx["conn_logs"] = conn_logs
        except Exception as exc:
            logging.error(f"integration_page: {exc}")
            ctx["error"] = "Произошла внутренняя ошибка. Попробуйте позже."

    return request.app.state.templates.TemplateResponse(
        request, "integration/index.html", ctx
    )


@router.post("/integration/create")
def integration_create(
    request: Request,
    name: Annotated[str, Form()],
    spreadsheet_id: Annotated[str, Form()] = "",
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/integration", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    can_use, _ = _check_plan(telegram_id)
    if not can_use:
        return RedirectResponse(url="/integration", status_code=302)

    if not name.strip():
        return RedirectResponse(url="/integration?error=Название+обязательно", status_code=302)

    try:
        db = get_web_db(telegram_id, org_db)
        cfg = json.dumps({
            "auth_type": "oauth",
            "provider": "google_sheets",
            "spreadsheet_id": spreadsheet_id.strip(),
        })
        db.add_integration_connection(name.strip(), cfg, provider="google_sheets")
    except Exception as e:
        logging.error(f"integration_create: {e}")
        return RedirectResponse(url="/integration?error=Ошибка+подключения.+Попробуйте+позже.", status_code=302)

    return RedirectResponse(url="/integration?msg=Подключение+создано", status_code=302)


@router.post("/integration/auth/start")
async def integration_auth_start(
    request: Request,
    conn_id: Annotated[int, Form()],
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/integration", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)

    telegram_id = int(user["sub"])
    can_use, _ = _check_plan(telegram_id)
    if not can_use:
        return RedirectResponse(url="/integration", status_code=302)

    if not _google_secrets_configured():
        return RedirectResponse(
            url="/integration?error=Секреты+GOOGLE_OAUTH_CLIENT_ID+и+GOOGLE_OAUTH_CLIENT_SECRET+не+заданы",
            status_code=302,
        )

    try:
        from integration.auth.google_oauth import initiate_device_flow
        flow = await initiate_device_flow()
        device_code = flow.get("device_code", "")
        user_code = flow.get("user_code", "")
        verify_url = flow.get("verification_url", "https://google.com/device")
        expires_in = int(flow.get("expires_in", 600))

        _device_flow[telegram_id] = {
            "device_code": device_code,
            "conn_id": conn_id,
            "expires_at": time.time() + expires_in,
        }
    except Exception as e:
        logging.error(f"integration_auth_start: {e}")
        return RedirectResponse(
            url="/integration?error=Ошибка+авторизации.+Попробуйте+позже.",
            status_code=302,
        )

    from urllib.parse import quote
    return RedirectResponse(
        url=(f"/integration?auth=pending&conn_id={conn_id}"
             f"&user_code={quote(user_code)}&verify_url={quote(verify_url)}"),
        status_code=302,
    )


@router.post("/integration/auth/poll")
async def integration_auth_poll(
    request: Request,
    conn_id: Annotated[int, Form()],
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from urllib.parse import quote

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/integration", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    state = _device_flow.get(telegram_id)
    if not state or state.get("conn_id") != conn_id:
        return RedirectResponse(
            url="/integration?error=Сессия+авторизации+не+найдена.+Начните+заново.",
            status_code=302,
        )

    if time.time() > state.get("expires_at", 0):
        _device_flow.pop(telegram_id, None)
        return RedirectResponse(
            url="/integration?error=Время+авторизации+истекло.+Начните+заново.",
            status_code=302,
        )

    try:
        from integration.auth.google_oauth import poll_for_token
        token_data = await poll_for_token(state["device_code"])
    except Exception as e:
        logging.error(f"integration_auth_poll error: {e}")
        return RedirectResponse(
            url="/integration?error=Ошибка+проверки+авторизации.+Попробуйте+позже.",
            status_code=302,
        )

    if token_data is None:
        return RedirectResponse(
            url=(f"/integration?auth=pending&conn_id={conn_id}"
                 f"&error=Ещё+не+авторизовано.+Перейдите+по+ссылке+и+введите+код,+затем+попробуйте+снова."),
            status_code=302,
        )

    try:
        db = get_web_db(telegram_id, org_db)
        conn_row = db.get_integration_connection(conn_id)
        if conn_row:
            existing_cfg = json.loads(conn_row[3] or "{}")
        else:
            existing_cfg = {"auth_type": "oauth", "provider": "google_sheets"}

        existing_cfg["tokens"] = {
            "access_token": token_data.get("access_token", ""),
            "refresh_token": token_data.get("refresh_token", ""),
            "expiry": token_data.get("expiry", time.time() + 3600),
        }
        db.update_integration_connection(conn_id, config=json.dumps(existing_cfg), enabled=1)
        db.add_integration_log(conn_id, None, "success", "OAuth авторизация выполнена через веб-интерфейс")
        _device_flow.pop(telegram_id, None)
    except Exception as e:
        logging.error(f"integration_auth_poll save: {e}")
        return RedirectResponse(
            url="/integration?error=Ошибка+сохранения+токена.+Попробуйте+позже.",
            status_code=302,
        )

    return RedirectResponse(url="/integration?msg=Google+аккаунт+успешно+подключён", status_code=302)


@router.post("/integration/{cid}/toggle")
def integration_toggle(request: Request, cid: int, csrf_token: str = Form(default="")):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/integration", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    try:
        db = get_web_db(telegram_id, org_db)
        conn_row = db.get_integration_connection(cid)
        if conn_row:
            new_state = 0 if conn_row[4] else 1
            db.update_integration_connection(cid, enabled=new_state)
    except Exception as e:
        logging.error(f"integration_toggle: {e}")

    return RedirectResponse(url="/integration", status_code=302)


@router.post("/integration/{cid}/delete")
def integration_delete(request: Request, cid: int, csrf_token: str = Form(default="")):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/integration", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    try:
        db = get_web_db(telegram_id, org_db)
        db.delete_integration_connection(cid)
    except Exception as e:
        logging.error(f"integration_delete: {e}")

    return RedirectResponse(url="/integration?msg=Подключение+удалено", status_code=302)


@router.post("/integration/{cid}/sync-motiv")
async def integration_sync_motiv(request: Request, cid: int, csrf_token: str = Form(default="")):
    """Re-run motivation sync from the saved config for this connection."""
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from urllib.parse import quote

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/integration", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    can_use, _ = _check_plan(telegram_id)
    if not can_use:
        return RedirectResponse(url="/integration", status_code=302)

    try:
        db = get_web_db(telegram_id, org_db)
        from integration.manager import integration_manager
        result = await integration_manager.run_motiv_sync_from_config(db, cid)
        synced = result.get("synced", 0)
        chains = result.get("chains", [])
        msg = f"Мотивация синхронизирована: {synced} моделей"
        if chains:
            msg += f", сети: {', '.join(str(c) for c in chains[:5])}"
        return RedirectResponse(url=f"/integration?msg={quote(msg)}", status_code=302)
    except ValueError as e:
        return RedirectResponse(url=f"/integration?error={quote(str(e))}", status_code=302)
    except Exception as e:
        logging.error(f"integration_sync_motiv: {e}")
        return RedirectResponse(
            url="/integration?error=" + quote("Ошибка синхронизации. Попробуйте позже."),
            status_code=302,
        )


@router.post("/integration/{cid}/import")
async def integration_import(
    request: Request,
    cid: int,
    import_type: Annotated[str, Form()],
    sheet_name: Annotated[str, Form()],
    header_row: Annotated[int, Form()] = 1,
    csrf_token: str = Form(default=""),
):
    """Run a data import from a Google Sheet using default column order."""
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from urllib.parse import quote

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/integration", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    can_use, _ = _check_plan(telegram_id)
    if not can_use:
        return RedirectResponse(url="/integration", status_code=302)

    if import_type not in ("products", "inventory", "sales", "staff", "plans"):
        return RedirectResponse(
            url="/integration?error=" + quote("Неизвестный тип импорта"),
            status_code=302,
        )
    if not sheet_name.strip():
        return RedirectResponse(
            url="/integration?error=" + quote("Укажите имя листа"),
            status_code=302,
        )

    try:
        db = get_web_db(telegram_id, org_db)
        from integration.manager import integration_manager
        result = await integration_manager.run_import(
            db, cid, import_type,
            sheet_name=sheet_name.strip(),
            header_row=int(header_row) if header_row else 1,
            col_mapping=None,
        )
        imported = result.get("imported", 0)
        skipped = result.get("skipped", 0)
        msg = f"Импорт «{import_type}»: добавлено {imported}, пропущено {skipped}"
        return RedirectResponse(url=f"/integration?msg={quote(msg)}", status_code=302)
    except ValueError as e:
        return RedirectResponse(url=f"/integration?error={quote(str(e))}", status_code=302)
    except Exception as e:
        logging.error(f"integration_import: {e}")
        return RedirectResponse(
            url="/integration?error=" + quote("Ошибка импорта. Попробуйте позже."),
            status_code=302,
        )
