import sqlite3
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from timezone_utils import DEFAULT_TZ

router = APIRouter()

PLAN_LABELS = {
    "Бесплатный": "Бесплатный",
    "Базовый": "Базовый",
    "Стандарт": "Стандарт",
    "Премиум": "Премиум",
    "addon_shops_1": "+1 магазин",
    "addon_shops_3": "+3 магазина",
    "addon_shops_5": "+5 магазинов",
    "addon_products_1": "+100 товаров",
    "addon_products_3": "+300 товаров",
    "addon_products_5": "+500 товаров",
}

SHOP_BOT_DB = "data/shop_bot.db"


def _get_all_users_with_subs(limit: int = 500) -> list[dict]:
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT u.id, u.first_name, u.last_name, u.username, u.shop_name,
                       s.plan_type, s.end_date
                FROM users u
                LEFT JOIN subscriptions s ON s.user_id = u.id
                ORDER BY u.first_name, u.last_name
                LIMIT ?
                """,
                (limit,),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        return [
            {
                "id": r[0],
                "name": f"{r[1] or ''} {r[2] or ''}".strip() or (f"@{r[3]}" if r[3] else f"ID {r[0]}"),
                "shop": r[4] or "—",
                "plan": r[5] or "Бесплатный",
                "end_date": (r[6] or "")[:10],
            }
            for r in rows
        ]
    except Exception:
        return []


def _get_admin_db_id(telegram_id: int) -> int | None:
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))
            row = cur.fetchone()
        finally:
            conn.close()
        return row[0] if row else None
    except Exception:
        return None


def _get_pending(limit: int = 200) -> list[dict]:
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT pr.id, pr.user_id, pr.plan_type, pr.amount, pr.created_at,
                       u.first_name, u.last_name, u.shop_name, pr.payment_proof_file_id
                FROM payment_requests pr
                JOIN users u ON pr.user_id = u.id
                WHERE pr.status = 'pending'
                ORDER BY pr.created_at DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        return [
            {
                "id": r[0],
                "user_id": r[1],
                "plan_type": r[2] or "",
                "plan_label": PLAN_LABELS.get(r[2] or "", r[2] or "—"),
                "amount": r[3] or 0,
                "created_at": (r[4] or "")[:16].replace("T", " "),
                "user_name": f"{r[5] or ''} {r[6] or ''}".strip() or "—",
                "shop_name": r[7] or "—",
                "has_proof": bool(r[8] and str(r[8]).startswith("web_proof:")),
            }
            for r in rows
        ]
    except Exception:
        return []


def _get_history(limit: int = 50) -> list[dict]:
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT pr.id, pr.plan_type, pr.amount, pr.status,
                       pr.created_at, pr.processed_at,
                       u.first_name, u.last_name, u.shop_name,
                       a.first_name, a.last_name, pr.payment_proof_file_id
                FROM payment_requests pr
                JOIN users u ON pr.user_id = u.id
                LEFT JOIN users a ON pr.processed_by = a.id
                WHERE pr.status IN ('approved', 'rejected')
                ORDER BY pr.processed_at DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        return [
            {
                "id": r[0],
                "plan_type": r[1] or "",
                "plan_label": PLAN_LABELS.get(r[1] or "", r[1] or "—"),
                "amount": r[2] or 0,
                "status": r[3] or "",
                "created_at": (r[4] or "")[:16].replace("T", " "),
                "processed_at": (r[5] or "")[:16].replace("T", " "),
                "user_name": f"{r[6] or ''} {r[7] or ''}".strip() or "—",
                "shop_name": r[8] or "—",
                "admin_name": f"{r[9] or ''} {r[10] or ''}".strip() or "—",
                "has_proof": bool(r[11] and str(r[11]).startswith("web_proof:")),
            }
            for r in rows
        ]
    except Exception:
        return []


_PENDING_CACHE: list = []   # [expires_at, count]  — simple 1-slot TTL store
_PENDING_TTL = 60           # секунд

def get_pending_count() -> int:
    import time as _t
    if _PENDING_CACHE and _t.monotonic() < _PENDING_CACHE[0]:
        return _PENDING_CACHE[1]
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM payment_requests WHERE status = 'pending'")
            row = cur.fetchone()
        finally:
            conn.close()
        result = row[0] if row else 0
    except Exception:
        result = 0
    if _PENDING_CACHE:
        _PENDING_CACHE[0] = _t.monotonic() + _PENDING_TTL
        _PENDING_CACHE[1] = result
    else:
        _PENDING_CACHE.extend([_t.monotonic() + _PENDING_TTL, result])
    return result


@router.get("/payments")
def payments_page(request: Request, msg: str = "", tab: str = "pending"):
    from web.auth import get_session_user

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") != "super_admin":
        return request.app.state.templates.TemplateResponse(
            request, "errors/403.html", {"user": user}, status_code=403
        )

    pending = _get_pending()
    history = _get_history()
    grant_users = _get_all_users_with_subs() if tab == "grant" else []

    from web.auth import get_csrf_token

    user_tz = DEFAULT_TZ
    try:
        from web.deps import get_web_db as _gwdb
        _db = _gwdb(int(user["sub"]), user.get("org_db"))
        user_tz = _db.get_user_timezone(int(user["sub"])) or DEFAULT_TZ
    except Exception:
        pass

    ctx = {
        "request": request,
        "user": user,
        "pending": pending,
        "grant_users": grant_users,
        "history": history,
        "tab": tab if tab in ("pending", "history", "grant") else "pending",
        "msg": msg,
        "csrf_token": get_csrf_token(request),
        "user_tz": user_tz,
    }
    return request.app.state.templates.TemplateResponse(
        request, "payments/index.html", ctx
    )


@router.post("/payments/{payment_id}/confirm")
async def confirm_payment(request: Request, payment_id: int):
    from web.auth import get_session_user

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") != "super_admin":
        return RedirectResponse(url="/payments", status_code=302)

    from web.auth import verify_csrf_token

    form = await request.form()
    if not verify_csrf_token(request, form.get("csrf_token", "")):
        return RedirectResponse(
            url="/payments?msg=csrf_error&tab=pending", status_code=302
        )

    telegram_id = int(user["sub"])
    admin_db_id = _get_admin_db_id(telegram_id)

    from database import Database

    db = Database(SHOP_BOT_DB)
    ok = db.confirm_payment_request(payment_id, admin_db_id)

    if ok:
        return RedirectResponse(
            url=f"/payments?msg=confirmed_{payment_id}&tab=pending", status_code=303
        )
    return RedirectResponse(
        url=f"/payments?msg=error_{payment_id}&tab=pending", status_code=303
    )


@router.post("/payments/{payment_id}/reject")
async def reject_payment(request: Request, payment_id: int):
    from web.auth import get_session_user

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") != "super_admin":
        return RedirectResponse(url="/payments", status_code=302)

    from web.auth import verify_csrf_token

    form = await request.form()
    if not verify_csrf_token(request, form.get("csrf_token", "")):
        return RedirectResponse(
            url="/payments?msg=csrf_error&tab=pending", status_code=302
        )

    telegram_id = int(user["sub"])
    admin_db_id = _get_admin_db_id(telegram_id)

    from database import Database

    db = Database(SHOP_BOT_DB)
    ok = db.reject_payment_request(payment_id, admin_db_id)

    if ok:
        return RedirectResponse(
            url=f"/payments?msg=rejected_{payment_id}&tab=pending", status_code=303
        )
    return RedirectResponse(
        url=f"/payments?msg=error_{payment_id}&tab=pending", status_code=303
    )


@router.post("/payments/grant")
async def payments_grant(request: Request):
    return RedirectResponse(url="/admin/billing/grants", status_code=303)
