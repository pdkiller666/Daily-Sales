"""
Super-Admin Billing Configurator
Routes: /admin/billing/*
All routes require role == 'super_admin'.
"""
import json
import sqlite3
from datetime import datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from database import Database
from web.auth import get_csrf_token, get_session_user, verify_csrf_token

router = APIRouter(prefix="/admin/billing")
SHOP_BOT_DB = "data/shop_bot.db"

ITEM_TYPE_LABELS = {
    "module":    "Модуль",
    "extension": "Расширение",
    "bundle":    "Пакет",
    "limit":     "Лимит-аддон",
}


def _guard(user) -> bool:
    if user is None:
        return True
    role = user.get("role") if isinstance(user, dict) else getattr(user, "role", None)
    return role != "super_admin"


def _db() -> Database:
    return Database(SHOP_BOT_DB)


def _ctx(request: Request, user, extra: dict) -> dict:
    base = {"request": request, "user": user, "_p": str(request.url.path)}
    base.update(extra)
    return base


def _flash(request: Request) -> str:
    return request.query_params.get("msg", "")


def _all_users_with_ids() -> list:
    """Return list of {telegram_id, first_name} for the grant form dropdown."""
    conn = None
    try:
        conn = sqlite3.connect(SHOP_BOT_DB, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=3000")
        rows = conn.execute(
            "SELECT telegram_id, first_name, last_name FROM users ORDER BY first_name LIMIT 500"
        ).fetchall()
        return [{"telegram_id": r[0], "name": f"{r[1]} {r[2] or ''}".strip()} for r in rows]
    except Exception:
        return []
    finally:
        if conn:
            conn.close()


# ─────────────────────────────────────────────────────────────────────────────
#  Hub — overview
# ─────────────────────────────────────────────────────────────────────────────

@router.get("")
@router.get("/")
async def billing_hub(request: Request):
    user = get_session_user(request)
    if _guard(user):
        return RedirectResponse("/dashboard", 303)
    db = _db()
    stats = db.get_billing_stats()
    modules = db.get_all_billing_modules()
    return request.app.state.templates.TemplateResponse(
        request,
        "admin/billing/hub.html",
        _ctx(request, user, {
            "stats": stats,
            "modules": modules,
            "msg": _flash(request),
            "csrf_token": get_csrf_token(request),
        }),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Modules + Extensions
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/modules")
async def billing_modules_page(request: Request, tab: str = "modules"):
    user = get_session_user(request)
    if _guard(user):
        return RedirectResponse("/dashboard", 303)
    # Whitelist tab — prevent XSS injection into Alpine x-data JS context
    safe_tab = tab if tab in ("modules", "extensions") else "modules"
    db = _db()
    modules = db.get_all_billing_modules()
    extensions = db.get_all_billing_extensions()
    return request.app.state.templates.TemplateResponse(
        request,
        "admin/billing/modules.html",
        _ctx(request, user, {
            "modules": modules,
            "extensions": extensions,
            "active_tab": safe_tab,
            "msg": _flash(request),
            "csrf_token": get_csrf_token(request),
        }),
    )


@router.post("/modules/add")
async def billing_module_add(
    request: Request,
    csrf_token: str = Form(""),
    key: str = Form(""),
    name: str = Form(""),
    icon: str = Form("📦"),
    description: str = Form(""),
    price_monthly: float = Form(0.0),
    price_annual: float = Form(0.0),
    sort_order: int = Form(0),
):
    user = get_session_user(request)
    if _guard(user) or not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/dashboard", 303)
    key = key.strip().lower().replace(" ", "_")
    if not key or not name.strip():
        return RedirectResponse("/admin/billing/modules?msg=bad_input&tab=modules", 303)
    db = _db()
    ok = db.upsert_billing_module(key, name.strip(), icon.strip() or "📦",
                                   description.strip(), price_monthly, sort_order,
                                   price_annual=price_annual)
    msg = "saved" if ok else "error"
    return RedirectResponse(f"/admin/billing/modules?msg={msg}&tab=modules", 303)


@router.post("/modules/{mod_id}/save")
async def billing_module_save(
    request: Request,
    mod_id: int,
    csrf_token: str = Form(""),
    name: str = Form(""),
    icon: str = Form("📦"),
    description: str = Form(""),
    price_monthly: float = Form(0.0),
    price_annual: float = Form(0.0),
    sort_order: int = Form(0),
    features_json: str = Form("[]"),
):
    user = get_session_user(request)
    if _guard(user) or not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/dashboard", 303)
    db = _db()
    mods = db.get_all_billing_modules()
    mod = next((m for m in mods if m["id"] == mod_id), None)
    if not mod:
        return RedirectResponse("/admin/billing/modules?msg=not_found&tab=modules", 303)
    try:
        json.loads(features_json)
    except Exception:
        features_json = "[]"
    ok = db.upsert_billing_module(
        mod["key"], name.strip() or mod["name"], icon.strip() or mod["icon"],
        description.strip(), price_monthly, sort_order, int(mod["is_active"]), features_json,
        price_annual=price_annual
    )
    msg = "saved" if ok else "error"
    return RedirectResponse(f"/admin/billing/modules?msg={msg}&tab=modules", 303)


@router.post("/modules/{mod_id}/toggle")
async def billing_module_toggle(
    request: Request,
    mod_id: int,
    csrf_token: str = Form(""),
):
    user = get_session_user(request)
    if _guard(user) or not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/dashboard", 303)
    _db().toggle_billing_module(mod_id)
    return RedirectResponse("/admin/billing/modules?msg=toggled&tab=modules", 303)


@router.post("/modules/{mod_id}/delete")
async def billing_module_delete(
    request: Request,
    mod_id: int,
    csrf_token: str = Form(""),
):
    user = get_session_user(request)
    if _guard(user) or not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/dashboard", 303)
    _db().delete_billing_module(mod_id)
    return RedirectResponse("/admin/billing/modules?msg=deleted&tab=modules", 303)


# ── Extensions ────────────────────────────────────────────────────────────────

@router.post("/extensions/add")
async def billing_ext_add(
    request: Request,
    csrf_token: str = Form(""),
    module_key: str = Form(""),
    key: str = Form(""),
    name: str = Form(""),
    icon: str = Form("⚡"),
    description: str = Form(""),
    price_monthly: float = Form(0.0),
    sort_order: int = Form(0),
):
    user = get_session_user(request)
    if _guard(user) or not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/dashboard", 303)
    key = key.strip().lower().replace(" ", "_")
    if not key or not name.strip() or not module_key:
        return RedirectResponse("/admin/billing/modules?msg=bad_input&tab=extensions", 303)
    ok = _db().upsert_billing_extension(
        module_key, key, name.strip(), icon.strip() or "⚡",
        description.strip(), price_monthly, sort_order
    )
    msg = "saved" if ok else "error"
    return RedirectResponse(f"/admin/billing/modules?msg={msg}&tab=extensions", 303)


@router.post("/extensions/{ext_id}/save")
async def billing_ext_save(
    request: Request,
    ext_id: int,
    csrf_token: str = Form(""),
    module_key: str = Form(""),
    name: str = Form(""),
    icon: str = Form("⚡"),
    description: str = Form(""),
    price_monthly: float = Form(0.0),
    sort_order: int = Form(0),
):
    user = get_session_user(request)
    if _guard(user) or not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/dashboard", 303)
    db = _db()
    exts = db.get_all_billing_extensions()
    ext = next((e for e in exts if e["id"] == ext_id), None)
    if not ext:
        return RedirectResponse("/admin/billing/modules?msg=not_found&tab=extensions", 303)
    ok = db.upsert_billing_extension(
        module_key or ext["module_key"], ext["key"],
        name.strip() or ext["name"], icon.strip() or ext["icon"],
        description.strip(), price_monthly, sort_order, int(ext["is_active"])
    )
    msg = "saved" if ok else "error"
    return RedirectResponse(f"/admin/billing/modules?msg={msg}&tab=extensions", 303)


@router.post("/extensions/{ext_id}/toggle")
async def billing_ext_toggle(
    request: Request,
    ext_id: int,
    csrf_token: str = Form(""),
):
    user = get_session_user(request)
    if _guard(user) or not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/dashboard", 303)
    _db().toggle_billing_extension(ext_id)
    return RedirectResponse("/admin/billing/modules?msg=toggled&tab=extensions", 303)


@router.post("/extensions/{ext_id}/delete")
async def billing_ext_delete(
    request: Request,
    ext_id: int,
    csrf_token: str = Form(""),
):
    user = get_session_user(request)
    if _guard(user) or not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/dashboard", 303)
    _db().delete_billing_extension(ext_id)
    return RedirectResponse("/admin/billing/modules?msg=deleted&tab=extensions", 303)


# ─────────────────────────────────────────────────────────────────────────────
#  Bundles
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/bundles")
async def billing_bundles_page(request: Request):
    user = get_session_user(request)
    if _guard(user):
        return RedirectResponse("/dashboard", 303)
    db = _db()
    bundles = db.get_all_billing_bundles()
    modules = db.get_all_billing_modules()
    extensions = db.get_all_billing_extensions()
    return request.app.state.templates.TemplateResponse(
        request,
        "admin/billing/bundles.html",
        _ctx(request, user, {
            "bundles": bundles,
            "modules": modules,
            "extensions": extensions,
            "msg": _flash(request),
            "csrf_token": get_csrf_token(request),
        }),
    )


@router.post("/bundles/add")
async def billing_bundle_add(
    request: Request,
    csrf_token: str = Form(""),
    key: str = Form(""),
    name: str = Form(""),
    icon: str = Form("🎁"),
    description: str = Form(""),
    includes_modules: str = Form(""),
    includes_extensions: str = Form(""),
    price_monthly: float = Form(0.0),
    price_annual: float = Form(0.0),
    sort_order: int = Form(0),
):
    user = get_session_user(request)
    if _guard(user) or not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/dashboard", 303)
    key = key.strip().lower().replace(" ", "_")
    if not key or not name.strip():
        return RedirectResponse("/admin/billing/bundles?msg=bad_input", 303)
    mods = [m.strip() for m in includes_modules.split(",") if m.strip()]
    exts = [e.strip() for e in includes_extensions.split(",") if e.strip()]
    includes_json = json.dumps({"modules": mods, "extensions": exts})
    ok = _db().upsert_billing_bundle(
        key, name.strip(), icon.strip() or "🎁", description.strip(),
        includes_json, price_monthly, sort_order, price_annual=price_annual
    )
    msg = "saved" if ok else "error"
    return RedirectResponse(f"/admin/billing/bundles?msg={msg}", 303)


@router.post("/bundles/{bnd_id}/save")
async def billing_bundle_save(
    request: Request,
    bnd_id: int,
    csrf_token: str = Form(""),
    name: str = Form(""),
    icon: str = Form("🎁"),
    description: str = Form(""),
    includes_modules: str = Form(""),
    includes_extensions: str = Form(""),
    price_monthly: float = Form(0.0),
    price_annual: float = Form(0.0),
    sort_order: int = Form(0),
):
    user = get_session_user(request)
    if _guard(user) or not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/dashboard", 303)
    db = _db()
    bnds = db.get_all_billing_bundles()
    bnd = next((b for b in bnds if b["id"] == bnd_id), None)
    if not bnd:
        return RedirectResponse("/admin/billing/bundles?msg=not_found", 303)
    mods = [m.strip() for m in includes_modules.split(",") if m.strip()]
    exts = [e.strip() for e in includes_extensions.split(",") if e.strip()]
    includes_json = json.dumps({"modules": mods, "extensions": exts})
    ok = db.upsert_billing_bundle(
        bnd["key"], name.strip() or bnd["name"], icon.strip() or bnd["icon"],
        description.strip(), includes_json, price_monthly, sort_order, int(bnd["is_active"]),
        price_annual=price_annual
    )
    msg = "saved" if ok else "error"
    return RedirectResponse(f"/admin/billing/bundles?msg={msg}", 303)


@router.post("/bundles/{bnd_id}/toggle")
async def billing_bundle_toggle(
    request: Request,
    bnd_id: int,
    csrf_token: str = Form(""),
):
    user = get_session_user(request)
    if _guard(user) or not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/dashboard", 303)
    _db().toggle_billing_bundle(bnd_id)
    return RedirectResponse("/admin/billing/bundles?msg=toggled", 303)


@router.post("/bundles/{bnd_id}/delete")
async def billing_bundle_delete(
    request: Request,
    bnd_id: int,
    csrf_token: str = Form(""),
):
    user = get_session_user(request)
    if _guard(user) or not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/dashboard", 303)
    _db().delete_billing_bundle(bnd_id)
    return RedirectResponse("/admin/billing/bundles?msg=deleted", 303)


# ─────────────────────────────────────────────────────────────────────────────
#  Grants (org/user module subscriptions)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/grants")
async def billing_grants_page(request: Request, q: str = "", page: int = 1):
    user = get_session_user(request)
    if _guard(user):
        return RedirectResponse("/dashboard", 303)
    db = _db()
    subs = db.get_billing_module_subs(active_only=False, limit=1000)

    # Filter by tg_id or item_key
    if q.strip():
        q_low = q.strip().lower()
        subs = [
            s for s in subs
            if q_low in str(s["user_telegram_id"])
            or q_low in (s["item_key"] or "").lower()
            or q_low in (s["granted_by"] or "").lower()
        ]

    # Pagination
    per_page = 20
    total = len(subs)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    offset = (page - 1) * per_page
    subs_page = subs[offset: offset + per_page]

    # Build item options for the grant form
    modules = db.get_all_billing_modules()
    extensions = db.get_all_billing_extensions()
    bundles = db.get_all_billing_bundles()
    users = _all_users_with_ids()

    # Build key → human name lookup for the list display
    key_name_map = {}
    for m in modules:
        key_name_map[m['key']] = m['name']
    for e in extensions:
        icon = e.get('icon', '')
        key_name_map[e['key']] = f"{icon} {e['name']}".strip() if icon else e['name']
    for b in bundles:
        icon = b.get('icon', '')
        key_name_map[b['key']] = f"{icon} {b['name']}".strip() if icon else b['name']

    return request.app.state.templates.TemplateResponse(
        request,
        "admin/billing/grants.html",
        _ctx(request, user, {
            "subs": subs_page,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "modules": modules,
            "extensions": extensions,
            "bundles": bundles,
            "users": users,
            "key_name_map": key_name_map,
            "q": q,
            "msg": _flash(request),
            "csrf_token": get_csrf_token(request),
            "item_type_labels": ITEM_TYPE_LABELS,
        }),
    )


@router.post("/grant")
async def billing_grant(
    request: Request,
    csrf_token: str = Form(""),
    user_telegram_id: int = Form(0),
    item_type: str = Form("module"),
    item_key: str = Form(""),
    duration_days: int = Form(30),
    price_paid: float = Form(0.0),
    note: str = Form(""),
):
    user = get_session_user(request)
    if _guard(user) or not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/dashboard", 303)
    if not user_telegram_id or not item_key.strip():
        return RedirectResponse("/admin/billing/grants?msg=bad_input", 303)
    sub_id = _db().grant_billing_item(
        user_telegram_id=user_telegram_id,
        item_type=item_type,
        item_key=item_key.strip(),
        duration_days=duration_days,
        price_paid=price_paid,
        granted_by="admin_grant",
        note=note.strip(),
    )
    msg = "granted" if sub_id else "error"
    try:
        from web.audit import log_admin_action
        log_admin_action(
            request, user, "billing_grant",
            target=f"tg:{user_telegram_id}",
            details=f"{item_type}:{item_key.strip()} {duration_days}д {price_paid}₽ → {msg}",
        )
    except Exception:
        pass
    return RedirectResponse(f"/admin/billing/grants?msg={msg}", 303)


@router.post("/revoke/{sub_id}")
async def billing_revoke(
    request: Request,
    sub_id: int,
    csrf_token: str = Form(""),
):
    user = get_session_user(request)
    if _guard(user) or not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/dashboard", 303)
    result = _db().revoke_billing_item(sub_id, cascade=True)
    cascaded = result.get("cascaded", 0)
    msg = f"revoked_cascade_{cascaded}" if cascaded else "revoked"
    try:
        from web.audit import log_admin_action
        log_admin_action(
            request, user, "billing_revoke",
            target=f"sub:{sub_id}", details=f"cascaded={cascaded}",
        )
    except Exception:
        pass
    return RedirectResponse(f"/admin/billing/grants?msg={msg}", 303)
