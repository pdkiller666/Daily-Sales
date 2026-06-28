import json
import logging
import sqlite3
import time
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, JSONResponse
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

    from datetime import datetime as _dt, timedelta as _td
    _now = _dt.utcnow()
    _wstart = _now - _td(days=_now.weekday())
    _wend = _wstart + _td(days=6)

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
        "week_date_from": _wstart.strftime("%d.%m.%Y"),
        "week_date_to": _wend.strftime("%d.%m.%Y"),
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
                        last_sync_stats = motiv_cfg.get("last_sync_stats") or {}
                        chain_al_cnt = len(motiv_cfg.get("chain_aliases") or {})
                        model_al_cnt = len(motiv_cfg.get("model_aliases") or {})
                        leg_al_cnt   = len(motiv_cfg.get("aliases") or {})
                        motiv_preview = {
                            "sheet": motiv_cfg.get("sheet_name", ""),
                            "chains": chains,
                            "model_count": len(models),
                            "last_synced": last_sync_stats.get("synced_at") or last_synced,
                            "alias_count": chain_al_cnt + model_al_cnt + leg_al_cnt,
                            "rules_written": last_sync_stats.get("rules_written"),
                            "matched_models": last_sync_stats.get("matched_models"),
                            "unmatched_count": last_sync_stats.get("unmatched", 0),
                            "auto_sync": bool(motiv_cfg.get("auto_sync")),
                        }
                    except Exception:
                        motiv_preview = None
                motiv_bonus_cols_str = ""
                if motiv_cfg.get("bonus_col_map"):
                    _bcm_parts = []
                    for _k, _v in motiv_cfg["bonus_col_map"].items():
                        try:
                            _v_str = str(_v).strip()
                            _k_str = str(_k).strip()
                            if _v_str == _k_str or _v_str == str(int(_k)):
                                _bcm_parts.append(_k_str)
                            else:
                                _bcm_parts.append(f"{_k_str}:{_v_str}")
                        except (ValueError, TypeError):
                            _bcm_parts.append(str(_k))
                    motiv_bonus_cols_str = ", ".join(_bcm_parts)
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
                    "motiv_header_row": motiv_cfg.get("header_row", 1),
                    "motiv_model_col": motiv_cfg.get("model_col", 1),
                    "motiv_bonus_cols": motiv_bonus_cols_str,
                    "motiv_chain_aliases": motiv_cfg.get("chain_aliases") or {},
                    "motiv_model_aliases": motiv_cfg.get("model_aliases") or {},
                    "motiv_aliases": motiv_cfg.get("aliases") or {},
                    "motiv_auto_sync": bool(motiv_cfg.get("auto_sync")),
                    "motiv_auto_sync_hour": int(motiv_cfg.get("auto_sync_hour", 6)),
                    "motiv_preview": motiv_preview,
                })
                try:
                    exports_raw = db.get_integration_exports(cid) or []
                    _preset_scheds = {"immediate", "disabled",
                                      "0 9 * * *", "0 8 * * 1", "0 8 1 * *"}
                    conn_exports[cid] = [
                        {
                            "id": e[0], "export_type": e[1],
                            "enabled": bool(e[2]), "schedule": e[3] or "immediate",
                            "target_sheet": e[4] or "", "operation": e[5] or "",
                            "mapping": e[6] or "",
                            "lookup_config": json.loads(e[7]) if e[7] else {},
                            "last_run": (e[8] or "")[:16].replace("T", " "),
                            "is_custom_sched": (e[3] or "immediate") not in _preset_scheds,
                        }
                        for e in exports_raw
                    ]
                except Exception:
                    conn_exports[cid] = []
                try:
                    logs_raw = db.get_integration_logs(cid, limit=25) or []
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
        integration_manager.save_motiv_sync_stats(db, cid, result)
        synced = result.get("synced", 0)
        chains = result.get("chains", [])
        rules_written = result.get("rules_written", 0)
        unmatched = result.get("unmatched", [])
        unmatched_chains = result.get("unmatched_chains", [])
        msg = (f"Мотивация синхронизирована: {synced} моделей, "
               f"правил записано: {rules_written}")
        if chains:
            msg += f". Сети: {', '.join(str(c) for c in chains[:5])}"
        # Surface the same mismatch warnings the bot shows, so a 0-rule / wrong-rate
        # sync is not silently reported as success in the web cabinet either.
        if unmatched:
            up = ", ".join(str(u) for u in unmatched[:5])
            if len(unmatched) > 5:
                up += f" … ещё {len(unmatched) - 5}"
            msg += (f". ⚠️ Не найдено товаров ({len(unmatched)}): {up} — "
                    f"мотивация по ним не записана, задайте псевдонимы")
        if unmatched_chains:
            cp = ", ".join(str(c) for c in unmatched_chains[:5])
            msg += (f". ⚠️ Сети без совпадения в профилях: {cp} — "
                    f"ставки по ним не применятся, добавьте псевдоним сети "
                    f"(напр. DNS → Днс)")
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


# ─────────────────────────────────────────────────────────────────────────────
#  Connection edit + test
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/integration/{cid}/edit")
def integration_edit(
    request: Request,
    cid: int,
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

    if not name.strip():
        return RedirectResponse(url="/integration?error=Название+обязательно", status_code=302)

    try:
        db = get_web_db(telegram_id, org_db)
        conn_row = db.get_integration_connection(cid)
        if not conn_row:
            return RedirectResponse(url="/integration?error=Подключение+не+найдено", status_code=302)
        existing_cfg = json.loads(conn_row[3] or "{}")
        existing_cfg["spreadsheet_id"] = spreadsheet_id.strip()
        db.update_integration_connection(
            cid, name=name.strip(), config=json.dumps(existing_cfg)
        )
    except Exception as e:
        logging.error(f"integration_edit: {e}")
        return RedirectResponse(url="/integration?error=Ошибка+сохранения", status_code=302)

    return RedirectResponse(url="/integration?msg=Подключение+обновлено", status_code=302)


@router.get("/integration/{cid}/test")
async def integration_test(request: Request, cid: int):
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "message": "Не авторизован"}, status_code=401)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return JSONResponse({"ok": False, "message": "Нет доступа"}, status_code=403)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    try:
        db = get_web_db(telegram_id, org_db)
        conn_row = db.get_integration_connection(cid)
        if not conn_row:
            return JSONResponse({"ok": False, "message": "Подключение не найдено"})
        cfg = json.loads(conn_row[3] or "{}")
        if not cfg.get("tokens", {}).get("access_token"):
            return JSONResponse({"ok": False, "message": "Подключение не авторизовано"})
        from integration.manager import integration_manager
        conn_config = await integration_manager._ensure_valid_token(db, cid, cfg)
        provider = integration_manager.providers.get("google_sheets")
        ok, message = await provider.test_connection(conn_config)
        return JSONResponse({"ok": ok, "message": message})
    except Exception as e:
        logging.error(f"integration_test: {e}")
        return JSONResponse({"ok": False, "message": f"Ошибка: {e}"})


# ─────────────────────────────────────────────────────────────────────────────
#  Export rules CRUD
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/integration/{cid}/export/create")
def integration_export_create(
    request: Request,
    cid: int,
    export_type: Annotated[str, Form()],
    target_sheet: Annotated[str, Form()],
    operation: Annotated[str, Form()],
    schedule: Annotated[str, Form()] = "immediate",
    schedule_custom: Annotated[str, Form()] = "",
    row_search_col: Annotated[str, Form()] = "",
    row_search_field: Annotated[str, Form()] = "",
    col_search_row: Annotated[str, Form()] = "",
    col_search_field: Annotated[str, Form()] = "",
    value_field: Annotated[str, Form()] = "",
    data_start_row: Annotated[str, Form()] = "",
    data_start_col: Annotated[str, Form()] = "",
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
    can_use, _ = _check_plan(telegram_id)
    if not can_use:
        return RedirectResponse(url="/integration", status_code=302)

    valid_types = ("sales", "products", "inventory", "plans", "staff")
    valid_ops = ("append_row", "update_cell", "replace_sheet")
    if export_type not in valid_types:
        return RedirectResponse(url=f"/integration?error={quote('Неверный тип экспорта')}", status_code=302)
    if operation not in valid_ops:
        return RedirectResponse(url=f"/integration?error={quote('Неверная операция')}", status_code=302)
    if not target_sheet.strip():
        return RedirectResponse(url=f"/integration?error={quote('Укажите лист назначения')}", status_code=302)

    schedule = schedule.strip()
    if not schedule and schedule_custom.strip():
        schedule = schedule_custom.strip()
    schedule = schedule or "immediate"

    _DEFAULT_FIELDS = {
        'sales':     ['date', 'product_name', 'shop_name', 'quantity', 'price', 'total', 'seller_name', 'category'],
        'inventory': ['shop_name', 'product_name', 'category', 'quantity', 'last_updated'],
        'products':  ['name', 'category', 'price', 'description'],
        'staff':     ['name', 'shop_name', 'role', 'phone'],
        'plans':     ['type', 'metric', 'target', 'period', 'shop_name', 'seller_name'],
    }
    _DEFAULT_LOOKUP = {
        'sales':     {'row_search_col': 1, 'row_search_field': 'shop_name',
                      'col_search_row': 1, 'col_search_field': 'product_name',
                      'operation': 'set', 'value_field': 'quantity',
                      'data_start_row': 2, 'data_start_col': 2},
        'inventory': {'row_search_col': 1, 'row_search_field': 'shop_name',
                      'col_search_row': 1, 'col_search_field': 'product_name',
                      'operation': 'set', 'value_field': 'quantity',
                      'data_start_row': 2, 'data_start_col': 2},
        'products':  {'row_search_col': 1, 'row_search_field': 'name',
                      'col_search_row': 1, 'col_search_field': 'category',
                      'operation': 'set', 'value_field': 'price',
                      'data_start_row': 2, 'data_start_col': 2},
        'plans':     {'row_search_col': 1, 'row_search_field': 'shop_name',
                      'col_search_row': 1, 'col_search_field': 'metric',
                      'operation': 'set', 'value_field': 'target',
                      'data_start_row': 2, 'data_start_col': 2},
        'staff':     {'row_search_col': 1, 'row_search_field': 'shop_name',
                      'col_search_row': 1, 'col_search_field': 'name',
                      'operation': 'set', 'value_field': 'role',
                      'data_start_row': 2, 'data_start_col': 2},
    }
    mapping_json = None
    lookup_json = None
    if operation == 'append_row':
        fields = _DEFAULT_FIELDS.get(export_type, [])
        mapping_json = json.dumps({f: f for f in fields}, ensure_ascii=False)
    elif operation == 'update_cell':
        default_lc = _DEFAULT_LOOKUP.get(export_type, {})
        def _int_or(s, default):
            try:
                return int(s)
            except (ValueError, TypeError):
                return default
        def _str_or(s, default):
            return s.strip() if s and s.strip() else default
        lc = {
            "row_search_col":   _int_or(row_search_col,   default_lc.get("row_search_col", 1)),
            "row_search_field": _str_or(row_search_field, default_lc.get("row_search_field", "shop_name")),
            "col_search_row":   _int_or(col_search_row,   default_lc.get("col_search_row", 1)),
            "col_search_field": _str_or(col_search_field, default_lc.get("col_search_field", "product_name")),
            "value_field":      _str_or(value_field,      default_lc.get("value_field", "quantity")),
            "data_start_row":   _int_or(data_start_row,   default_lc.get("data_start_row", 2)),
            "data_start_col":   _int_or(data_start_col,   default_lc.get("data_start_col", 2)),
            "operation":        default_lc.get("operation", "set"),
        }
        lookup_json = json.dumps(lc, ensure_ascii=False)

    enabled = 0 if schedule == 'disabled' else 1

    try:
        db = get_web_db(telegram_id, org_db)
        conn_row = db.get_integration_connection(cid)
        if not conn_row:
            return RedirectResponse(url=f"/integration?error={quote('Подключение не найдено')}", status_code=302)
        db.add_integration_export(
            cid, export_type, operation, target_sheet.strip(), schedule,
            enabled=enabled, mapping=mapping_json, lookup_config=lookup_json
        )
    except Exception as e:
        logging.error(f"integration_export_create: {e}")
        return RedirectResponse(url=f"/integration?error={quote('Ошибка создания правила')}", status_code=302)

    return RedirectResponse(url="/integration?msg=Правило+экспорта+создано", status_code=302)


@router.post("/integration/{cid}/export/{eid}/toggle")
def integration_export_toggle(
    request: Request, cid: int, eid: int, csrf_token: str = Form(default="")
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

    try:
        db = get_web_db(telegram_id, org_db)
        exp = db.get_integration_export(eid)
        if exp and exp[1] == cid:
            new_enabled = 0 if exp[2] else 1
            db.update_integration_export(eid, enabled=new_enabled)
    except Exception as e:
        logging.error(f"integration_export_toggle: {e}")

    return RedirectResponse(url="/integration", status_code=302)


@router.post("/integration/{cid}/export/{eid}/delete")
def integration_export_delete(
    request: Request, cid: int, eid: int, csrf_token: str = Form(default="")
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

    try:
        db = get_web_db(telegram_id, org_db)
        exp = db.get_integration_export(eid)
        if exp and exp[1] == cid:
            db.delete_integration_export(eid)
    except Exception as e:
        logging.error(f"integration_export_delete: {e}")

    return RedirectResponse(url="/integration?msg=Правило+удалено", status_code=302)


@router.post("/integration/{cid}/export/{eid}/edit")
def integration_export_edit(
    request: Request,
    cid: int,
    eid: int,
    target_sheet: Annotated[str, Form()],
    schedule: Annotated[str, Form()] = "immediate",
    schedule_custom: Annotated[str, Form()] = "",
    row_search_col: Annotated[str, Form()] = "",
    row_search_field: Annotated[str, Form()] = "",
    col_search_row: Annotated[str, Form()] = "",
    col_search_field: Annotated[str, Form()] = "",
    value_field: Annotated[str, Form()] = "",
    data_start_row: Annotated[str, Form()] = "",
    data_start_col: Annotated[str, Form()] = "",
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

    if not target_sheet.strip():
        return RedirectResponse(url=f"/integration?error={quote('Укажите лист назначения')}", status_code=302)

    sched = schedule.strip()
    if not sched and schedule_custom.strip():
        sched = schedule_custom.strip()
    sched = sched or "immediate"

    try:
        db = get_web_db(telegram_id, org_db)
        exp = db.get_integration_export(eid)
        if not exp or exp[1] != cid:
            return RedirectResponse(url=f"/integration?error={quote('Правило не найдено')}", status_code=302)

        update_kwargs = {
            "target_sheet": target_sheet.strip(),
            "schedule": sched,
            "enabled": 0 if sched == "disabled" else (1 if bool(exp[2]) else 0),
        }

        operation = exp[5] or ""
        if operation == "update_cell":
            existing_lc = json.loads(exp[7]) if exp[7] else {}
            def _int_or(s, default):
                try:
                    return int(s)
                except (ValueError, TypeError):
                    return default
            def _str_or(s, default):
                return s.strip() if s.strip() else default
            lc = {
                "row_search_col":   _int_or(row_search_col, existing_lc.get("row_search_col", 1)),
                "row_search_field": _str_or(row_search_field, existing_lc.get("row_search_field", "shop_name")),
                "col_search_row":   _int_or(col_search_row, existing_lc.get("col_search_row", 1)),
                "col_search_field": _str_or(col_search_field, existing_lc.get("col_search_field", "product_name")),
                "value_field":      _str_or(value_field, existing_lc.get("value_field", "quantity")),
                "data_start_row":   _int_or(data_start_row, existing_lc.get("data_start_row", 2)),
                "data_start_col":   _int_or(data_start_col, existing_lc.get("data_start_col", 2)),
                "operation":        existing_lc.get("operation", "set"),
            }
            update_kwargs["lookup_config"] = json.dumps(lc, ensure_ascii=False)

        db.update_integration_export(eid, **update_kwargs)
    except Exception as e:
        logging.error(f"integration_export_edit: {e}")
        return RedirectResponse(url=f"/integration?error={quote('Ошибка сохранения')}", status_code=302)

    return RedirectResponse(url="/integration?msg=Правило+обновлено", status_code=302)


@router.post("/integration/{cid}/export/{eid}/run")
async def integration_export_run(
    request: Request, cid: int, eid: int, csrf_token: str = Form(default="")
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
    can_use, _ = _check_plan(telegram_id)
    if not can_use:
        return RedirectResponse(url="/integration", status_code=302)

    try:
        db = get_web_db(telegram_id, org_db)
        exp = db.get_integration_export(eid)
        if not exp:
            return RedirectResponse(url=f"/integration?error={quote('Правило не найдено')}", status_code=302)
        actual_conn_id = exp[1]
        if actual_conn_id != cid:
            return RedirectResponse(url=f"/integration?error={quote('Правило не принадлежит этому подключению')}", status_code=302)
        conn_row = db.get_integration_connection(actual_conn_id)
        if not conn_row:
            return RedirectResponse(url=f"/integration?error={quote('Подключение не найдено')}", status_code=302)
        export_row = (
            eid, actual_conn_id, exp[0], exp[3], exp[4],
            exp[5], exp[6], exp[7], conn_row[3],
        )
        from integration.manager import integration_manager
        result = await integration_manager._run_export_with_result(db, export_row, {})
        if result.get("success"):
            return RedirectResponse(url="/integration?msg=Экспорт+выполнен", status_code=302)
        else:
            err = quote(result.get("error") or "Ошибка экспорта")
            return RedirectResponse(url=f"/integration?error={err}", status_code=302)
    except Exception as e:
        logging.error(f"integration_export_run: {e}")
        return RedirectResponse(url=f"/integration?error={quote('Ошибка запуска экспорта')}", status_code=302)


@router.post("/integration/{cid}/export/{eid}/sync-week")
async def integration_export_sync_week(
    request: Request, cid: int, eid: int, csrf_token: str = Form(default="")
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from datetime import datetime, timedelta
    import json as _json

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    if not verify_csrf_token(request, csrf_token):
        return JSONResponse({"ok": False, "error": "Неверный CSRF токен"}, status_code=403)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return JSONResponse({"ok": False, "error": "Нет доступа"}, status_code=403)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    can_use, _ = _check_plan(telegram_id)
    if not can_use:
        return JSONResponse({"ok": False, "error": "Модуль интеграций не подключён"})

    try:
        db = get_web_db(telegram_id, org_db)
        exp = db.get_integration_export(eid)
        if not exp:
            return JSONResponse({"ok": False, "error": "Правило не найдено"})
        actual_conn_id = exp[1]
        if actual_conn_id != cid:
            return JSONResponse({"ok": False, "error": "Правило не принадлежит этому подключению"})

        export_type = exp[0]
        operation = exp[5]

        if export_type != "sales":
            return JSONResponse({"ok": False, "error": "Синхронизация за неделю доступна только для правил с типом «sales»"})
        if operation not in ("update_cell", "replace_sheet"):
            return JSONResponse({"ok": False, "error": "Синхронизация за неделю поддерживается только для операций «update_cell» и «replace_sheet»"})

        now = datetime.utcnow()
        week_start = now - timedelta(days=now.weekday())
        week_end = week_start + timedelta(days=6)
        date_from = week_start.strftime("%Y-%m-%d")
        date_to = week_end.strftime("%Y-%m-%d")
        date_from_disp = week_start.strftime("%d.%m.%Y")
        date_to_disp = week_end.strftime("%d.%m.%Y")

        from integration.manager import integration_manager

        if operation == "update_cell":
            result = await integration_manager.sync_matrix_for_period(db, eid, date_from, date_to)
            return JSONResponse({
                "ok": True,
                "rows_written": result.get("cells_updated", 0),
                "cells_skipped": result.get("cells_skipped", 0),
                "sheet": result.get("sheet", ""),
                "date_from": date_from_disp,
                "date_to": date_to_disp,
                "errors": result.get("errors", []),
            })

        # replace_sheet: fetch week-filtered sales, write full sheet
        conn_row = db.get_integration_connection(actual_conn_id)
        if not conn_row:
            return JSONResponse({"ok": False, "error": "Подключение не найдено"})
        conn_config = _json.loads(conn_row[3] or "{}")
        conn_config = await integration_manager._ensure_valid_token(db, actual_conn_id, conn_config)
        sheet_tpl = exp[4] or "Sheet1"
        sheet_name = integration_manager._render_sheet_name(sheet_tpl, {})
        rows_raw = db.get_sales_for_week_replace(date_from, date_to)
        headers = ["Дата", "Товар", "Категория", "Магазин", "Количество", "Цена", "Сумма", "Продавец"]
        rows = [[str(r[i]) for i in range(min(len(r), 8))] for r in rows_raw]
        provider = integration_manager.providers.get("google_sheets")
        await provider.replace_sheet(conn_config, sheet_name, headers, rows)
        db.add_integration_log(actual_conn_id, eid, "success",
                               f"sync-week replace_sheet {date_from}–{date_to}: {len(rows)} строк на «{sheet_name}»")
        db.update_integration_export_last_run(eid)
        return JSONResponse({
            "ok": True,
            "rows_written": len(rows),
            "sheet": sheet_name,
            "date_from": date_from_disp,
            "date_to": date_to_disp,
            "errors": [],
        })
    except Exception as e:
        logging.error(f"integration_export_sync_week: {e}")
        return JSONResponse({"ok": False, "error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
#  Motivation config
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/integration/{cid}/motiv/save")
def integration_motiv_save(
    request: Request,
    cid: int,
    sheet_name: Annotated[str, Form()],
    header_row: Annotated[int, Form()] = 1,
    model_col: Annotated[str, Form()] = "1",
    bonus_cols: Annotated[str, Form()] = "",
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
    can_use, _ = _check_plan(telegram_id)
    if not can_use:
        return RedirectResponse(url="/integration", status_code=302)

    if not sheet_name.strip():
        return RedirectResponse(url=f"/integration?error={quote('Укажите имя листа мотивации')}", status_code=302)

    try:
        db = get_web_db(telegram_id, org_db)
        conn_row = db.get_integration_connection(cid)
        if not conn_row:
            return RedirectResponse(url=f"/integration?error={quote('Подключение не найдено')}", status_code=302)
        existing_cfg = json.loads(conn_row[3] or "{}")
        motiv = existing_cfg.get("motiv_config") or {}
        motiv["sheet_name"] = sheet_name.strip()
        motiv["header_row"] = max(1, int(header_row))
        try:
            col_val = int(model_col.strip())
        except (ValueError, AttributeError):
            import string
            col_str = model_col.strip().upper()
            col_val = 0
            for ch in col_str:
                col_val = col_val * 26 + (ord(ch) - ord('A') + 1)
        motiv["model_col"] = col_val if col_val > 0 else 1
        if bonus_cols.strip():
            parts = [p.strip() for p in bonus_cols.split(",") if p.strip()]
            bonus_col_map = {}
            for part in parts:
                chain_name = None
                col_str_raw = part
                if ':' in part:
                    col_str_raw, chain_part = part.split(':', 1)
                    col_str_raw = col_str_raw.strip()
                    chain_name = chain_part.strip() or None
                try:
                    idx = int(col_str_raw)
                except ValueError:
                    idx = 0
                    for ch in col_str_raw.upper():
                        idx = idx * 26 + (ord(ch) - ord('A') + 1)
                if idx > 0:
                    if not chain_name:
                        letters = ''
                        n = idx
                        while n > 0:
                            n, rem = divmod(n - 1, 26)
                            letters = chr(ord('A') + rem) + letters
                        chain_name = f"Кол.{letters}"
                    bonus_col_map[str(idx)] = chain_name
            motiv["bonus_col_map"] = bonus_col_map
        existing_cfg["motiv_config"] = motiv
        db.update_integration_connection(cid, config=json.dumps(existing_cfg, ensure_ascii=False))
    except Exception as e:
        logging.error(f"integration_motiv_save: {e}")
        return RedirectResponse(url=f"/integration?error={quote('Ошибка сохранения настроек мотивации')}", status_code=302)

    return RedirectResponse(url="/integration?msg=Настройки+мотивации+сохранены", status_code=302)


@router.post("/integration/{cid}/motiv/alias/add")
def integration_motiv_alias_add(
    request: Request,
    cid: int,
    alias_from: Annotated[str, Form()],
    alias_to: Annotated[str, Form()],
    alias_type: str = Form(default="chain"),
    csrf_token: str = Form(default=""),
):
    """Add a chain alias (alias_type='chain') or model alias (alias_type='model')."""
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

    if not alias_from.strip() or not alias_to.strip():
        return RedirectResponse(url=f"/integration?error={quote('Заполните оба поля псевдонима')}", status_code=302)

    try:
        db = get_web_db(telegram_id, org_db)
        conn_row = db.get_integration_connection(cid)
        if not conn_row:
            return RedirectResponse(url=f"/integration?error={quote('Подключение не найдено')}", status_code=302)
        cfg = json.loads(conn_row[3] or "{}")
        motiv = cfg.get("motiv_config") or {}
        key = "model_aliases" if alias_type == "model" else "chain_aliases"
        aliases = motiv.get(key) or {}
        aliases[alias_from.strip()] = alias_to.strip()
        motiv[key] = aliases
        cfg["motiv_config"] = motiv
        db.update_integration_connection(cid, config=json.dumps(cfg, ensure_ascii=False))
    except Exception as e:
        logging.error(f"integration_motiv_alias_add: {e}")
        return RedirectResponse(url=f"/integration?error={quote('Ошибка сохранения псевдонима')}", status_code=302)

    return RedirectResponse(url="/integration?msg=Псевдоним+добавлен", status_code=302)


@router.post("/integration/{cid}/motiv/alias/delete")
def integration_motiv_alias_delete(
    request: Request,
    cid: int,
    alias_from: Annotated[str, Form()],
    alias_type: str = Form(default="chain"),
    csrf_token: str = Form(default=""),
):
    """Delete a chain alias (alias_type='chain') or model alias (alias_type='model')."""
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

    try:
        db = get_web_db(telegram_id, org_db)
        conn_row = db.get_integration_connection(cid)
        if not conn_row:
            return RedirectResponse(url=f"/integration?error={quote('Подключение не найдено')}", status_code=302)
        cfg = json.loads(conn_row[3] or "{}")
        motiv = cfg.get("motiv_config") or {}
        key = "model_aliases" if alias_type == "model" else "chain_aliases"
        aliases = motiv.get(key) or {}
        aliases.pop(alias_from.strip(), None)
        motiv[key] = aliases
        # Also clean up legacy aliases dict for backward compat
        if "aliases" in motiv:
            motiv["aliases"].pop(alias_from.strip(), None)
        cfg["motiv_config"] = motiv
        db.update_integration_connection(cid, config=json.dumps(cfg, ensure_ascii=False))
    except Exception as e:
        logging.error(f"integration_motiv_alias_delete: {e}")
        return RedirectResponse(url=f"/integration?error={quote('Ошибка удаления псевдонима')}", status_code=302)

    return RedirectResponse(url="/integration?msg=Псевдоним+удалён", status_code=302)


@router.post("/integration/{cid}/motiv/auto-sync-toggle")
def integration_motiv_auto_sync_toggle(
    request: Request,
    cid: int,
    csrf_token: str = Form(default=""),
):
    """Toggle auto-sync (daily APScheduler job) for this connection's motiv config."""
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

    try:
        db = get_web_db(telegram_id, org_db)
        conn_row = db.get_integration_connection(cid)
        if not conn_row:
            return RedirectResponse(url=f"/integration?error={quote('Подключение не найдено')}", status_code=302)
        cfg = json.loads(conn_row[3] or "{}")
        motiv = cfg.get("motiv_config") or {}
        motiv["auto_sync"] = not bool(motiv.get("auto_sync"))
        motiv.setdefault("auto_sync_hour", 6)
        cfg["motiv_config"] = motiv
        db.update_integration_connection(cid, config=json.dumps(cfg, ensure_ascii=False))
    except Exception as e:
        logging.error(f"integration_motiv_auto_sync_toggle: {e}")
        return RedirectResponse(url=f"/integration?error={quote('Ошибка изменения настройки автосинка')}", status_code=302)

    return RedirectResponse(url="/integration?msg=Настройка+автосинка+обновлена", status_code=302)
