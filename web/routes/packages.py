"""
Web-маршруты модуля Абонементы.
GET  /packages                      — список шаблонов пакетов
POST /packages/new                  — создать шаблон
POST /packages/{id}/edit            — редактировать шаблон
POST /packages/{id}/toggle          — вкл/выкл шаблон
POST /packages/{id}/delete          — удалить шаблон
GET  /packages/sold                 — все проданные абонементы
POST /packages/sell                 — продать абонемент клиенту
GET  /packages/client/{client_id}   — абонементы конкретного клиента (JSON)
POST /packages/use/{cp_id}          — вручную списать занятие
POST /packages/freeze/{cp_id}       — заморозить/разморозить абонемент
"""
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from packages_utils import (
    auto_expire_packages as _auto_expire_packages,
    sell_package as _sell_package,
    consume_package_visit as _consume_package_visit,
)

router = APIRouter()
logger = logging.getLogger(__name__)

_PKG_STATUSES = {
    "active":   "Активен",
    "frozen":   "Заморожен",
    "exhausted":"Исчерпан",
    "expired":  "Истёк",
}

_SELL_ERROR_MAP = {
    "pkg_not_found": "pkg_not_found",
    "client_not_found": "client_not_found",
    "invalid_price": "invalid_price",
}


def _pkg_status_label(status: str) -> str:
    return _PKG_STATUSES.get(status, status)


# ─── Список шаблонов ─────────────────────────────────────────────────────────

@router.get("/packages")
def packages_list(request: Request):
    from web.auth import get_session_user
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not has_module(int(user.get("sub", 0)), "crm"):
        return RedirectResponse("/subscription?msg=crm_locked", status_code=302)

    org_db = user.get("org_db", "")
    if not org_db:
        return RedirectResponse("/dashboard", status_code=302)

    tg_id = int(user.get("sub", 0))
    db = get_web_db(tg_id, org_db)
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT p.id, p.name, s.name, p.visits_total, p.price, "
            "p.validity_days, p.description, p.is_active, p.created_at, "
            "(SELECT COUNT(*) FROM client_packages cp WHERE cp.package_id=p.id) "
            "FROM service_packages p "
            "LEFT JOIN services s ON s.id=p.service_id "
            "ORDER BY p.is_active DESC, p.name"
        ).fetchall()
        packages = [
            {
                "id": r[0], "name": r[1], "service_name": r[2] or "Любая услуга",
                "visits_total": r[3], "price": r[4], "validity_days": r[5],
                "description": r[6] or "", "is_active": bool(r[7]),
                "created_at": r[8], "sold_count": r[9],
            }
            for r in rows
        ]
        services = conn.execute(
            "SELECT id, name FROM services WHERE is_active=1 ORDER BY name"
        ).fetchall()
        # статистика проданных
        stats = conn.execute(
            "SELECT status, COUNT(*) FROM client_packages GROUP BY status"
        ).fetchall()
        stats_map = {r[0]: r[1] for r in stats}
        total_sold = sum(stats_map.values())
        total_active = stats_map.get("active", 0)
    finally:
        conn.close()

    is_admin = user.get("role") in ("owner", "admin", "super_admin")
    return request.app.state.templates.TemplateResponse(
        request, "packages/index.html",
        {
            "packages": packages, "services": services,
            "is_admin": is_admin, "total_sold": total_sold,
            "total_active": total_active, "stats_map": stats_map,
            "error": request.query_params.get("error", ""),
        },
    )


# ─── Создать шаблон ──────────────────────────────────────────────────────────

@router.post("/packages/new")
def package_create(
    request: Request,
    csrf_token: str = Form(""),
    name: str = Form(""),
    service_id: str = Form(""),
    visits_total: int = Form(1),
    price: float = Form(0),
    validity_days: str = Form(""),
    description: str = Form(""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/packages?error=csrf", status_code=302)
    if not has_module(int(user.get("sub", 0)), "crm"):
        return RedirectResponse("/subscription?msg=crm_locked", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse("/packages", status_code=302)
    if not name.strip() or visits_total < 1 or price < 0:
        return RedirectResponse("/packages?error=required_fields", status_code=302)

    org_db = user.get("org_db", "")
    if not org_db:
        return RedirectResponse("/dashboard", status_code=302)

    tg_id = int(user.get("sub", 0))
    db = get_web_db(tg_id, org_db)
    conn = db.get_connection()
    try:
        sid = int(service_id) if service_id.strip().isdigit() else None
        vd = int(validity_days) if validity_days.strip().isdigit() else None
        conn.execute(
            "INSERT INTO service_packages (name, service_id, visits_total, price, validity_days, description) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name.strip(), sid, visits_total, price, vd, description.strip()),
        )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/packages", status_code=303)


# ─── Редактировать шаблон ────────────────────────────────────────────────────

@router.post("/packages/{pkg_id}/edit")
def package_edit(
    request: Request,
    pkg_id: int,
    csrf_token: str = Form(""),
    name: str = Form(""),
    service_id: str = Form(""),
    visits_total: int = Form(1),
    price: float = Form(0),
    validity_days: str = Form(""),
    description: str = Form(""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/packages", status_code=302)
    if not has_module(int(user.get("sub", 0)), "crm"):
        return RedirectResponse("/subscription?msg=crm_locked", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse("/packages", status_code=302)

    org_db = user.get("org_db", "")
    if not org_db:
        return RedirectResponse("/dashboard", status_code=302)

    tg_id = int(user.get("sub", 0))
    db = get_web_db(tg_id, org_db)
    conn = db.get_connection()
    try:
        sid = int(service_id) if service_id.strip().isdigit() else None
        vd = int(validity_days) if validity_days.strip().isdigit() else None
        conn.execute(
            "UPDATE service_packages SET name=?, service_id=?, visits_total=?, price=?, "
            "validity_days=?, description=? WHERE id=?",
            (name.strip(), sid, visits_total, price, vd, description.strip(), pkg_id),
        )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/packages", status_code=303)


# ─── Вкл/выкл шаблон ─────────────────────────────────────────────────────────

@router.post("/packages/{pkg_id}/toggle")
def package_toggle(request: Request, pkg_id: int, csrf_token: str = Form("")):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/packages", status_code=302)
    if not has_module(int(user.get("sub", 0)), "crm"):
        return RedirectResponse("/subscription?msg=crm_locked", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse("/packages", status_code=302)

    org_db = user.get("org_db", "")
    db = get_web_db(int(user.get("sub", 0)), org_db)
    conn = db.get_connection()
    try:
        conn.execute(
            "UPDATE service_packages SET is_active = CASE WHEN is_active=1 THEN 0 ELSE 1 END WHERE id=?",
            (pkg_id,),
        )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/packages", status_code=303)


# ─── Удалить шаблон ──────────────────────────────────────────────────────────

@router.post("/packages/{pkg_id}/delete")
def package_delete(request: Request, pkg_id: int, csrf_token: str = Form("")):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/packages", status_code=302)
    if not has_module(int(user.get("sub", 0)), "crm"):
        return RedirectResponse("/subscription?msg=crm_locked", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse("/packages", status_code=302)

    org_db = user.get("org_db", "")
    db = get_web_db(int(user.get("sub", 0)), org_db)
    conn = db.get_connection()
    try:
        sold = conn.execute(
            "SELECT COUNT(*) FROM client_packages WHERE package_id=?", (pkg_id,)
        ).fetchone()[0]
        if sold > 0:
            return RedirectResponse("/packages?error=has_sold", status_code=302)
        conn.execute("DELETE FROM service_packages WHERE id=?", (pkg_id,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/packages", status_code=303)


# ─── Все проданные абонементы ─────────────────────────────────────────────────

@router.get("/packages/sold")
def packages_sold(
    request: Request,
    status: str = "",
    package_id: int = 0,
    page: int = 1,
):
    from web.auth import get_session_user
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not has_module(int(user.get("sub", 0)), "crm"):
        return RedirectResponse("/subscription?msg=crm_locked", status_code=302)

    org_db = user.get("org_db", "")
    if not org_db:
        return RedirectResponse("/dashboard", status_code=302)

    tg_id = int(user.get("sub", 0))
    db = get_web_db(tg_id, org_db)
    conn = db.get_connection()
    try:
        _auto_expire_packages(conn)
        conn.commit()

        where = "1=1"
        params: list = []
        if status:
            where += " AND cp.status=?"
            params.append(status)
        if package_id:
            where += " AND cp.package_id=?"
            params.append(package_id)

        total = conn.execute(
            f"SELECT COUNT(*) FROM client_packages cp WHERE {where}", params
        ).fetchone()[0]
        page_size = 50
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = max(1, min(page, total_pages))
        offset = (page - 1) * page_size

        rows = conn.execute(
            f"SELECT cp.id, (c.first_name || ' ' || COALESCE(c.last_name,'')), sp.name, cp.visits_total, cp.visits_used, "
            f"cp.price_paid, cp.purchased_at, cp.expires_at, cp.status, cp.notes "
            f"FROM client_packages cp "
            f"JOIN clients c ON c.id=cp.client_id "
            f"JOIN service_packages sp ON sp.id=cp.package_id "
            f"WHERE {where} ORDER BY cp.purchased_at DESC LIMIT ? OFFSET ?",
            params + [page_size, offset],
        ).fetchall()
        sold = [
            {
                "id": r[0], "client_name": r[1], "package_name": r[2],
                "visits_total": r[3], "visits_used": r[4],
                "visits_left": r[3] - r[4],
                "price_paid": r[5], "purchased_at": r[6][:10] if r[6] else "",
                "expires_at": r[7][:10] if r[7] else "",
                "status": r[8], "status_label": _pkg_status_label(r[8]),
                "notes": r[9] or "",
                "pct": int((r[4] / r[3]) * 100) if r[3] > 0 else 0,
            }
            for r in rows
        ]
        all_packages = conn.execute(
            "SELECT id, name FROM service_packages ORDER BY name"
        ).fetchall()
    finally:
        conn.close()

    is_admin = user.get("role") in ("owner", "admin", "super_admin")
    return request.app.state.templates.TemplateResponse(
        request, "packages/sold.html",
        {
            "sold": sold, "total": total, "page": page,
            "total_pages": total_pages, "status": status,
            "package_id": package_id, "all_packages": all_packages,
            "statuses": _PKG_STATUSES, "is_admin": is_admin,
            "error": request.query_params.get("error", ""),
        },
    )


# ─── Продать абонемент клиенту ────────────────────────────────────────────────

@router.post("/packages/sell")
def package_sell(
    request: Request,
    csrf_token: str = Form(""),
    package_id: int = Form(0),
    client_id: int = Form(0),
    price_paid: str = Form(""),
    notes: str = Form(""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/packages/sold?error=csrf", status_code=302)
    if not has_module(int(user.get("sub", 0)), "crm"):
        return RedirectResponse("/subscription?msg=crm_locked", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse("/packages/sold", status_code=302)
    if not package_id or not client_id:
        return RedirectResponse("/packages/sold?error=required_fields", status_code=302)

    org_db = user.get("org_db", "")
    tg_id = int(user.get("sub", 0))
    db = get_web_db(tg_id, org_db)
    conn = db.get_connection()
    try:
        cp_id, err = _sell_package(conn, package_id, client_id, price_paid, tg_id, notes)
        if err:
            return RedirectResponse(f"/packages/sold?error={_SELL_ERROR_MAP.get(err, err)}", status_code=302)
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(f"/clients/{client_id}?tab=packages&sold=1", status_code=303)


# ─── Абонементы клиента (JSON) ────────────────────────────────────────────────

@router.get("/packages/client/{client_id}")
def packages_for_client(request: Request, client_id: int):
    from web.auth import get_session_user
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not has_module(int(user.get("sub", 0)), "crm"):
        return JSONResponse({"items": []})

    org_db = user.get("org_db", "")
    db = get_web_db(int(user.get("sub", 0)), org_db)
    conn = db.get_connection()
    try:
        _auto_expire_packages(conn)
        conn.commit()
        rows = conn.execute(
            "SELECT cp.id, sp.name, cp.visits_total, cp.visits_used, cp.expires_at, cp.status "
            "FROM client_packages cp "
            "JOIN service_packages sp ON sp.id=cp.package_id "
            "WHERE cp.client_id=? AND cp.status='active' "
            "ORDER BY cp.purchased_at DESC",
            (client_id,),
        ).fetchall()
        items = [
            {
                "id": r[0], "name": r[1],
                "visits_total": r[2], "visits_used": r[3],
                "visits_left": r[2] - r[3],
                "expires_at": r[4][:10] if r[4] else None,
                "status": r[5],
            }
            for r in rows
        ]
    finally:
        conn.close()
    return JSONResponse({"items": items})


# ─── Вручную списать занятие ──────────────────────────────────────────────────

@router.post("/packages/use/{cp_id}")
def package_use(
    request: Request,
    cp_id: int,
    csrf_token: str = Form(""),
    note: str = Form(""),
    appt_id: str = Form(""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return JSONResponse({"error": "csrf"}, status_code=403)
    if not has_module(int(user.get("sub", 0)), "crm"):
        return JSONResponse({"error": "locked"}, status_code=403)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return JSONResponse({"error": "forbidden"}, status_code=403)

    org_db = user.get("org_db", "")
    tg_id = int(user.get("sub", 0))
    db = get_web_db(tg_id, org_db)
    conn = db.get_connection()
    try:
        appointment_id = int(appt_id) if appt_id.strip().isdigit() else None
        _auto_expire_packages(conn)
        result = _consume_package_visit(conn, cp_id, tg_id, appointment_id=appointment_id, note=note)
        if result.get("error"):
            status_code = 404 if result["error"] == "not_found" else 400
            return JSONResponse(result, status_code=status_code)
        conn.commit()
        return JSONResponse(result)
    finally:
        conn.close()


# ─── Заморозить/разморозить ───────────────────────────────────────────────────

@router.post("/packages/freeze/{cp_id}")
def package_freeze(request: Request, cp_id: int, csrf_token: str = Form("")):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return JSONResponse({"error": "csrf"}, status_code=403)
    if not has_module(int(user.get("sub", 0)), "crm"):
        return JSONResponse({"error": "locked"}, status_code=403)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return JSONResponse({"error": "forbidden"}, status_code=403)

    org_db = user.get("org_db", "")
    tg_id = int(user.get("sub", 0))
    db = get_web_db(tg_id, org_db)
    conn = db.get_connection()
    try:
        cp = conn.execute(
            "SELECT status FROM client_packages WHERE id=?", (cp_id,)
        ).fetchone()
        if not cp:
            return JSONResponse({"error": "not_found"}, status_code=404)
        new_status = "frozen" if cp[0] == "active" else "active"
        conn.execute(
            "UPDATE client_packages SET status=? WHERE id=?", (new_status, cp_id)
        )
        conn.commit()
        return JSONResponse({"ok": True, "status": new_status, "label": _pkg_status_label(new_status)})
    finally:
        conn.close()
