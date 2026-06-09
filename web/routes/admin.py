"""
Super-Admin Hub — web equivalent of system_admin_panel in the bot.
All routes require user.role == 'super_admin'.
"""
import contextlib
import os
import sqlite3
from datetime import datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from backup_manager import BackupManager
from database import Database
from tenant_manager import tenant_manager
from web.auth import get_csrf_token, get_session_user, verify_csrf_token

router = APIRouter(prefix="/admin")

SHOP_BOT_DB = "data/shop_bot.db"


def _raw_conn(path: str = SHOP_BOT_DB) -> sqlite3.Connection:
    """Open a raw SQLite connection with WAL mode and busy timeout."""
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    return conn


def _guard(user) -> bool:
    """Return True if access should be denied."""
    if user is None:
        return True
    # user is a dict (decoded JWT payload), not an object
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


# ─────────────────────────────────────────────────────────────────────────────
#  Hub
# ─────────────────────────────────────────────────────────────────────────────

@router.get("")
@router.get("/")
async def admin_hub(request: Request):
    user = get_session_user(request)
    if _guard(user):
        return RedirectResponse("/dashboard", 303)
    db = _db()
    stats = db.get_subscriptions_statistics()
    orgs = tenant_manager.get_all_organizations()
    active_subs = _count_active_subs()
    pending = _count_pending()
    bm = BackupManager()
    try:
        backups = bm.get_backup_list()
        last_backup = backups[0]["created"].strftime("%d.%m.%Y %H:%M") if backups else "—"
    except Exception:
        last_backup = "—"
    return request.app.state.templates.TemplateResponse(
        request,
        "admin/hub.html",
        _ctx(request, user, {
            "stats": stats,
            "orgs_count": len(orgs),
            "active_subs_count": active_subs,
            "pending_count": pending,
            "last_backup": last_backup,
        }),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Organizations
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/orgs")
async def admin_orgs(request: Request):
    user = get_session_user(request)
    if _guard(user):
        return RedirectResponse("/dashboard", 303)
    orgs_raw = tenant_manager.get_all_organizations()
    orgs = []
    for row in orgs_raw:
        org_id, name, db_path, owner_id, invite_code, plan, is_active, created = row
        users = tenant_manager.get_org_users(org_id)
        orgs.append({
            "id": org_id,
            "name": name,
            "db_path": db_path or "",
            "plan": plan or "Бесплатный",
            "is_active": bool(is_active),
            "created": (created or "")[:10],
            "users_count": len(users),
            "invite_code": invite_code or "—",
        })
    return request.app.state.templates.TemplateResponse(
        request,
        "admin/organizations.html",
        _ctx(request, user, {
            "orgs": orgs,
            "csrf_token": get_csrf_token(request),
            "msg": _flash(request),
        }),
    )


@router.post("/orgs/{org_id}/delete")
async def admin_delete_org(
    request: Request,
    org_id: int,
    csrf_token: str = Form(""),
):
    user = get_session_user(request)
    if _guard(user):
        return RedirectResponse("/dashboard", 303)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/dashboard", 303)
    try:
        tenant_manager.delete_organization(org_id)
        msg = "deleted"
    except Exception:
        msg = "error"
    return RedirectResponse(f"/admin/orgs?msg={msg}", 303)


# ─────────────────────────────────────────────────────────────────────────────
#  Subscriptions
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/subs")
async def admin_subs(request: Request, q: str = ""):
    user = get_session_user(request)
    if _guard(user):
        return RedirectResponse("/dashboard", 303)

    active_subs = _get_active_subs(q)
    users_for_grant = _get_all_shop_bot_users()

    return request.app.state.templates.TemplateResponse(
        request,
        "admin/subscriptions.html",
        _ctx(request, user, {
            "active_subs": active_subs,
            "users_for_grant": users_for_grant,
            "q": q,
            "csrf_token": get_csrf_token(request),
            "msg": _flash(request),
        }),
    )


@router.post("/subs/grant")
async def admin_grant_sub(request: Request):
    return RedirectResponse("/admin/billing/grants", 303)


@router.post("/subs/{user_id}/cancel")
async def admin_cancel_sub(request: Request, user_id: int):
    return RedirectResponse("/admin/billing/grants", 303)


# ─────────────────────────────────────────────────────────────────────────────
#  Payment Statistics
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/stats")
async def admin_stats(request: Request):
    user = get_session_user(request)
    if _guard(user):
        return RedirectResponse("/dashboard", 303)
    db = _db()
    detailed = db.get_detailed_payment_statistics()

    conn = None
    try:
        conn = _raw_conn()
        recent = [
            {
                "id": r[0],
                "name": f"{r[1] or ''} {r[2] or ''}".strip() or f"ID {r[0]}",
                "plan": r[3] or "—",
                "amount": r[4] or 0,
                "date": (r[5] or "")[:10],
            }
            for r in conn.execute("""
                SELECT pr.id, u.first_name, u.last_name, pr.plan_type, pr.amount, pr.created_at
                FROM payment_requests pr
                JOIN users u ON pr.user_id = u.id
                WHERE pr.status = 'approved'
                ORDER BY pr.created_at DESC LIMIT 30
            """).fetchall()
        ]
        chart_raw = conn.execute("""
            SELECT strftime('%Y-%m', created_at) as mo, SUM(amount)
            FROM payment_requests WHERE status = 'approved'
            GROUP BY mo ORDER BY mo ASC
        """).fetchall()
    except Exception:
        recent = []
        chart_raw = []
    finally:
        if conn:
            conn.close()

    chart = [{"month": r[0], "revenue": float(r[1] or 0)} for r in chart_raw]

    return request.app.state.templates.TemplateResponse(
        request,
        "admin/stats.html",
        _ctx(request, user, {
            "stats": detailed,
            "recent_payments": recent,
            "chart": chart,
        }),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Tariff Plans
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/tariffs")
async def admin_tariffs(request: Request):
    user = get_session_user(request)
    if _guard(user):
        return RedirectResponse("/dashboard", 303)
    db = _db()
    plans_raw = db.get_all_subscription_plans()
    # get_subscription_plan_details column order:
    # id[0] name[1] duration_days[2] price[3] description[4]
    # max_products[5] max_shops[6] max_sales_per_month[7]
    # can_export_reports[8] can_view_analytics[9] can_use_notifications[10]
    # can_use_integrations[11] is_active[12] created_at[13]
    # But get_all_subscription_plans uses SELECT * FROM subscription_plans ORDER BY price
    # which may have a different order. We fetch details per plan to be safe.
    plans = []
    for row in plans_raw:
        plan_id = row[0]
        detail = db.get_subscription_plan_details(plan_id)
        if not detail:
            continue
        plans.append({
            "id": detail[0],
            "name": detail[1],
            "duration_days": detail[2],
            "price": detail[3],
            "description": detail[4] or "",
            "max_products": detail[5],
            "max_shops": detail[6],
            "max_sales": detail[7],
            "can_export": bool(detail[8]),
            "can_analytics": bool(detail[9]),
            "can_notifications": bool(detail[10]),
            "can_integrations": bool(detail[11]),
            "is_active": bool(detail[12]),
        })
    return request.app.state.templates.TemplateResponse(
        request,
        "admin/tariffs.html",
        _ctx(request, user, {
            "plans": plans,
            "csrf_token": get_csrf_token(request),
            "msg": _flash(request),
        }),
    )


@router.post("/tariffs/{plan_id}/toggle")
async def admin_toggle_tariff(
    request: Request,
    plan_id: int,
    csrf_token: str = Form(""),
):
    user = get_session_user(request)
    if _guard(user):
        return RedirectResponse("/dashboard", 303)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/dashboard", 303)
    db = _db()
    detail = db.get_subscription_plan_details(plan_id)
    if detail:
        new_val = 0 if bool(detail[12]) else 1
        db.update_subscription_plan_field(plan_id, "is_active", new_val)
    return RedirectResponse("/admin/tariffs?msg=updated", 303)


@router.post("/tariffs/{plan_id}/update")
async def admin_update_tariff(
    request: Request,
    plan_id: int,
    csrf_token: str = Form(""),
    name: str = Form(""),
    price: str = Form(""),
    duration_days: str = Form(""),
    description: str = Form(""),
    max_products: str = Form(""),
    max_shops: str = Form(""),
    max_sales: str = Form(""),
    can_export: str = Form(default=""),
    can_analytics: str = Form(default=""),
    can_notifications: str = Form(default=""),
    can_integrations: str = Form(default=""),
):
    user = get_session_user(request)
    if _guard(user):
        return RedirectResponse("/dashboard", 303)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/dashboard", 303)
    db = _db()
    kwargs = {}
    if name.strip():
        kwargs["name"] = name.strip()
    if price.strip():
        try:
            kwargs["price"] = float(price.replace(",", "."))
        except ValueError:
            pass
    if duration_days.strip():
        try:
            kwargs["duration_days"] = int(duration_days)
        except ValueError:
            pass
    if description.strip():
        kwargs["description"] = description.strip()
    if max_products.strip():
        try:
            kwargs["max_products"] = int(max_products)
        except ValueError:
            pass
    if max_shops.strip():
        try:
            kwargs["max_shops"] = int(max_shops)
        except ValueError:
            pass
    if max_sales.strip():
        try:
            kwargs["max_sales_per_month"] = int(max_sales)
        except ValueError:
            pass
    kwargs["can_export_reports"] = 1 if can_export == "on" else 0
    kwargs["can_view_analytics"] = 1 if can_analytics == "on" else 0
    kwargs["can_use_notifications"] = 1 if can_notifications == "on" else 0
    kwargs["can_use_integrations"] = 1 if can_integrations == "on" else 0
    if kwargs:
        db.update_subscription_plan(plan_id, **kwargs)
    return RedirectResponse("/admin/tariffs?msg=updated", 303)


# ─────────────────────────────────────────────────────────────────────────────
#  Payment Settings
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/settings")
async def admin_pay_settings(request: Request):
    user = get_session_user(request)
    if _guard(user):
        return RedirectResponse("/dashboard", 303)
    db = _db()
    settings = db.get_payment_settings()
    web_url = db.get_web_interface_url() or ""
    provider = db.get_payment_provider() or "sbp"

    # Trial settings stored in payment_settings table
    trial_days = settings.get("trial_days", "14")
    trial_plan = settings.get("trial_plan", "Премиум")
    payment_instruction = settings.get("payment_instruction", "")

    return request.app.state.templates.TemplateResponse(
        request,
        "admin/payment_settings.html",
        _ctx(request, user, {
            "settings": settings,
            "web_url": web_url,
            "provider": provider,
            "trial_days": trial_days,
            "trial_plan": trial_plan,
            "payment_instruction": payment_instruction,
            "plans": ["Базовый", "Стандарт", "Премиум"],
            "csrf_token": get_csrf_token(request),
            "msg": _flash(request),
        }),
    )


@router.post("/settings")
async def admin_pay_settings_save(
    request: Request,
    csrf_token: str = Form(""),
    card_number: str = Form(""),
    recipient_name: str = Form(""),
    bank_name: str = Form(""),
    payment_instruction: str = Form(""),
    web_url: str = Form(""),
    provider: str = Form("sbp"),
    trial_days: str = Form("14"),
    trial_plan: str = Form("Премиум"),
):
    user = get_session_user(request)
    if _guard(user):
        return RedirectResponse("/dashboard", 303)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/dashboard", 303)
    db = _db()
    if card_number.strip():
        db.update_payment_setting("card_number", card_number.strip())
    if recipient_name.strip():
        db.update_payment_setting("recipient_name", recipient_name.strip())
    if bank_name.strip():
        db.update_payment_setting("bank_name", bank_name.strip())
    if payment_instruction.strip():
        db.update_payment_setting("payment_instruction", payment_instruction.strip())
    if trial_days.strip():
        db.update_payment_setting("trial_days", trial_days.strip())
    if trial_plan.strip():
        db.update_payment_setting("trial_plan", trial_plan.strip())
    db.set_web_interface_url(web_url.strip() or None)
    if provider in ("sbp", "yookassa"):
        db.set_payment_provider(provider)
    return RedirectResponse("/admin/settings?msg=saved", 303)


# ─────────────────────────────────────────────────────────────────────────────
#  Global User Search
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/users")
async def admin_users(request: Request, q: str = ""):
    user = get_session_user(request)
    if _guard(user):
        return RedirectResponse("/dashboard", 303)
    results = _search_global_users(q) if len(q.strip()) >= 2 else []
    return request.app.state.templates.TemplateResponse(
        request,
        "admin/users.html",
        _ctx(request, user, {"q": q, "results": results}),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Backups
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/backups")
async def admin_backups(request: Request):
    user = get_session_user(request)
    if _guard(user):
        return RedirectResponse("/dashboard", 303)
    bm = BackupManager()
    try:
        backups = bm.get_backup_list()
    except Exception:
        backups = []
    return request.app.state.templates.TemplateResponse(
        request,
        "admin/backups.html",
        _ctx(request, user, {
            "backups": backups,
            "csrf_token": get_csrf_token(request),
            "msg": _flash(request),
        }),
    )


@router.post("/backups/create")
async def admin_create_backup(request: Request, csrf_token: str = Form("")):
    user = get_session_user(request)
    if _guard(user):
        return RedirectResponse("/dashboard", 303)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/dashboard", 303)
    bm = BackupManager()
    try:
        bm.create_backup()
        bm.create_backup(custom_db_path=SHOP_BOT_DB, custom_label="shop_bot")
        bm.backup_all_tenants()
        msg = "created"
    except Exception:
        msg = "error"
    return RedirectResponse(f"/admin/backups?msg={msg}", 303)


@router.post("/backups/{filename}/delete")
async def admin_delete_backup(
    request: Request,
    filename: str,
    csrf_token: str = Form(""),
):
    user = get_session_user(request)
    if _guard(user):
        return RedirectResponse("/dashboard", 303)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/dashboard", 303)
    bm = BackupManager()
    try:
        path = os.path.join(bm.backup_dir, filename)
        if os.path.exists(path) and os.path.abspath(path).startswith(
            os.path.abspath(bm.backup_dir)
        ):
            os.remove(path)
        msg = "deleted"
    except Exception:
        msg = "error"
    return RedirectResponse(f"/admin/backups?msg={msg}", 303)


# ─────────────────────────────────────────────────────────────────────────────
#  Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _count_active_subs() -> int:
    conn = None
    try:
        conn = _raw_conn()
        n = conn.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE end_date > CURRENT_TIMESTAMP"
        ).fetchone()[0]
        return n
    except Exception:
        return 0
    finally:
        if conn:
            conn.close()


def _count_pending() -> int:
    conn = None
    try:
        conn = _raw_conn()
        n = conn.execute(
            "SELECT COUNT(*) FROM payment_requests WHERE status='pending'"
        ).fetchone()[0]
        return n
    except Exception:
        return 0
    finally:
        if conn:
            conn.close()


def _get_active_subs(q: str = "") -> list[dict]:
    conn = None
    try:
        conn = _raw_conn()
        rows = conn.execute("""
            SELECT s.id, s.user_id, s.plan_type, s.end_date,
                   u.first_name, u.last_name, u.username, u.shop_name, u.telegram_id
            FROM subscriptions s
            JOIN users u ON s.user_id = u.id
            WHERE s.end_date > CURRENT_TIMESTAMP
            ORDER BY s.end_date ASC
        """).fetchall()
    except Exception:
        return []
    finally:
        if conn:
            conn.close()

    result = []
    for r in rows:
        name = f"{r[4] or ''} {r[5] or ''}".strip() or (f"@{r[6]}" if r[6] else f"ID {r[1]}")
        item = {
            "id": r[0],
            "user_id": r[1],
            "plan": r[2] or "—",
            "end_date": (r[3] or "")[:10],
            "name": name,
            "shop": r[7] or "—",
            "telegram_id": r[8],
        }
        if q:
            q_lo = q.lower()
            if q_lo not in name.lower() and q_lo not in (r[7] or "").lower():
                continue
        result.append(item)
    return result


def _get_all_shop_bot_users() -> list[dict]:
    conn = None
    try:
        conn = _raw_conn()
        rows = conn.execute(
            "SELECT id, first_name, last_name, username, shop_name "
            "FROM users ORDER BY first_name, last_name"
        ).fetchall()
        return [
            {
                "id": r[0],
                "name": f"{r[1] or ''} {r[2] or ''}".strip()
                or (f"@{r[3]}" if r[3] else f"ID {r[0]}"),
                "shop": r[4] or "",
            }
            for r in rows
        ]
    except Exception:
        return []
    finally:
        if conn:
            conn.close()


def _search_global_users(q: str) -> list[dict]:
    results: list[dict] = []
    if not q or len(q.strip()) < 2:
        return results
    q_lo = q.lower()

    # shop_bot.db users
    conn = None
    try:
        conn = _raw_conn()
        for r in conn.execute("""
            SELECT u.id, u.first_name, u.last_name, u.username, u.shop_name,
                   u.telegram_id, s.plan_type, s.end_date
            FROM users u
            LEFT JOIN subscriptions s ON s.user_id = u.id
            WHERE LOWER(COALESCE(u.first_name,'') || ' ' || COALESCE(u.last_name,'')
                        || ' ' || COALESCE(u.username,'')) LIKE ?
               OR CAST(u.telegram_id AS TEXT) LIKE ?
            ORDER BY u.first_name LIMIT 50
        """, (f"%{q_lo}%", f"%{q}%")).fetchall():
            results.append({
                "source": "Центральная БД",
                "source_type": "central",
                "id": r[0],
                "telegram_id": r[5] or "—",
                "name": f"{r[1] or ''} {r[2] or ''}".strip()
                or (f"@{r[3]}" if r[3] else f"ID {r[0]}"),
                "shop": r[4] or "—",
                "plan": r[6] or "Бесплатный",
                "sub_end": (r[7] or "")[:10] or "—",
            })
    except Exception:
        pass
    finally:
        if conn:
            conn.close()

    # Per-org tenant DBs
    try:
        orgs = tenant_manager.get_all_organizations()
        for org_row in orgs:
            org_id, org_name, db_path = org_row[0], org_row[1], org_row[2]
            if not db_path or not os.path.exists(db_path):
                continue
            org_conn = None
            try:
                org_conn = _raw_conn(db_path)
                for r in org_conn.execute("""
                    SELECT id, first_name, last_name, phone, shop_name, telegram_id
                    FROM users
                    WHERE LOWER(COALESCE(first_name,'') || ' ' || COALESCE(last_name,'')
                                || ' ' || COALESCE(phone,'')) LIKE ?
                       OR CAST(telegram_id AS TEXT) LIKE ?
                    LIMIT 20
                """, (f"%{q_lo}%", f"%{q}%")).fetchall():
                    results.append({
                        "source": org_name,
                        "source_type": "org",
                        "id": r[0],
                        "telegram_id": r[5] or "—",
                        "name": f"{r[1] or ''} {r[2] or ''}".strip(),
                        "shop": r[4] or "—",
                        "plan": "—",
                        "sub_end": "—",
                    })
            except Exception:
                continue
            finally:
                if org_conn:
                    org_conn.close()
    except Exception:
        pass

    return results
