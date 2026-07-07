"""
Web-маршруты модуля Услуги.
GET  /services                      — каталог услуг
GET  /services/new                  — форма создания
POST /services/new                  — создать услугу
GET  /services/{id}/edit            — форма редактирования
POST /services/{id}/edit            — сохранить изменения
POST /services/{id}/delete          — удалить услугу
GET  /services/categories           — управление категориями
POST /services/categories/new       — создать категорию
POST /services/categories/{id}/delete — удалить категорию
GET  /services/{id}/motivation      — настройка мотивации по услуге
POST /services/{id}/motivation      — сохранить мотивацию
"""
import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

router = APIRouter()
logger = logging.getLogger(__name__)

_PAGE_SIZE = 50


def _get_ctx(request: Request):
    return {
        "user": request.session.get("user", {}),
        "csrf_token": request.session.get("csrf_token", ""),
    }


def _row_to_service(r) -> dict:
    return {
        "id": r[0], "name": r[1], "description": r[2],
        "category_id": r[3], "category_name": r[4] or "—",
        "price": r[5], "duration_minutes": r[6],
        "group_max_participants": r[7], "is_active": bool(r[8]),
        "created_at": r[9],
    }


@router.get("/services")
def services_list(request: Request, q: str = "", category_id: int = 0, page: int = 1):
    from web.auth import get_session_user
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not has_module(user.get("telegram_id", 0), "services"):
        return RedirectResponse("/dashboard?msg=services_locked", status_code=302)

    db = get_web_db(request)
    conn = db.get_connection()
    try:
        categories = conn.execute(
            "SELECT id, name, color FROM service_categories WHERE is_active=1 ORDER BY sort_order, name"
        ).fetchall()
        cats = [{"id": r[0], "name": r[1], "color": r[2]} for r in categories]

        offset = (page - 1) * _PAGE_SIZE
        params = []
        where = "s.is_active=1"
        if q:
            where += " AND lower(s.name) LIKE ?"
            params.append(f"%{q.lower()}%")
        if category_id:
            where += " AND s.category_id=?"
            params.append(category_id)

        total = conn.execute(
            f"SELECT COUNT(*) FROM services s WHERE {where}", params
        ).fetchone()[0]

        rows = conn.execute(
            f"SELECT s.id, s.name, s.description, s.category_id, sc.name, s.price, "
            f"s.duration_minutes, s.group_max_participants, s.is_active, s.created_at "
            f"FROM services s LEFT JOIN service_categories sc ON s.category_id=sc.id "
            f"WHERE {where} ORDER BY s.name LIMIT ? OFFSET ?",
            params + [_PAGE_SIZE, offset],
        ).fetchall()
        services = [_row_to_service(r) for r in rows]
        total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)

        ctx = _get_ctx(request)
        ctx.update({
            "services": services, "categories": cats, "total": total,
            "q": q, "category_id": category_id, "page": page, "total_pages": total_pages,
            "is_admin": user.get("role") in ("owner", "admin"),
        })
        return request.app.state.templates.TemplateResponse(request, "services/index.html", ctx)
    finally:
        conn.close()


@router.get("/services/new")
def service_new_form(request: Request):
    from web.auth import get_session_user
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not has_module(user.get("telegram_id", 0), "services"):
        return RedirectResponse("/services", status_code=302)
    if user.get("role") not in ("owner", "admin"):
        return RedirectResponse("/services", status_code=302)

    db = get_web_db(request)
    conn = db.get_connection()
    try:
        cats = conn.execute(
            "SELECT id, name FROM service_categories WHERE is_active=1 ORDER BY sort_order, name"
        ).fetchall()
        ctx = _get_ctx(request)
        ctx.update({
            "service": None, "categories": [{"id": r[0], "name": r[1]} for r in cats],
            "error": request.query_params.get("error", ""),
        })
        return request.app.state.templates.TemplateResponse(request, "services/form.html", ctx)
    finally:
        conn.close()


@router.post("/services/new")
def service_create(
    request: Request,
    csrf_token: str = Form(""),
    name: str = Form(""),
    description: str = Form(""),
    category_id: str = Form(""),
    price: str = Form("0"),
    duration_minutes: str = Form("60"),
    group_max_participants: str = Form("1"),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/services/new?error=csrf", status_code=302)
    if not has_module(user.get("telegram_id", 0), "services"):
        return RedirectResponse("/services", status_code=302)
    if user.get("role") not in ("owner", "admin"):
        return RedirectResponse("/services", status_code=302)
    if not name.strip():
        return RedirectResponse("/services/new?error=name_required", status_code=302)

    try:
        price_val = float(price.replace(",", "."))
    except (ValueError, AttributeError):
        price_val = 0.0
    try:
        dur_val = int(duration_minutes)
    except (ValueError, TypeError):
        dur_val = 60
    try:
        group_val = max(1, int(group_max_participants))
    except (ValueError, TypeError):
        group_val = 1
    cat_val = int(category_id) if category_id and category_id.isdigit() else None

    db = get_web_db(request)
    conn = db.get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO services (name, description, category_id, price, duration_minutes, "
            "group_max_participants, created_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name.strip(), description.strip(), cat_val, price_val, dur_val,
             group_val, user.get("telegram_id")),
        )
        conn.commit()
        return RedirectResponse(f"/services/{cur.lastrowid}/edit?created=1", status_code=303)
    finally:
        conn.close()


@router.get("/services/{service_id}/edit")
def service_edit_form(request: Request, service_id: int):
    from web.auth import get_session_user
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not has_module(user.get("telegram_id", 0), "services"):
        return RedirectResponse("/services", status_code=302)
    if user.get("role") not in ("owner", "admin"):
        return RedirectResponse("/services", status_code=302)

    db = get_web_db(request)
    conn = db.get_connection()
    try:
        r = conn.execute(
            "SELECT s.id, s.name, s.description, s.category_id, sc.name, s.price, "
            "s.duration_minutes, s.group_max_participants, s.is_active, s.created_at "
            "FROM services s LEFT JOIN service_categories sc ON s.category_id=sc.id "
            "WHERE s.id=?", (service_id,)
        ).fetchone()
        if not r:
            return RedirectResponse("/services", status_code=302)

        cats = conn.execute(
            "SELECT id, name FROM service_categories WHERE is_active=1 ORDER BY sort_order, name"
        ).fetchall()
        ctx = _get_ctx(request)
        ctx.update({
            "service": _row_to_service(r),
            "categories": [{"id": c[0], "name": c[1]} for c in cats],
            "created": request.query_params.get("created", ""),
            "error": request.query_params.get("error", ""),
        })
        return request.app.state.templates.TemplateResponse(request, "services/form.html", ctx)
    finally:
        conn.close()


@router.post("/services/{service_id}/edit")
def service_update(
    request: Request,
    service_id: int,
    csrf_token: str = Form(""),
    name: str = Form(""),
    description: str = Form(""),
    category_id: str = Form(""),
    price: str = Form("0"),
    duration_minutes: str = Form("60"),
    group_max_participants: str = Form("1"),
    is_active: str = Form("1"),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(f"/services/{service_id}/edit?error=csrf", status_code=302)
    if not has_module(user.get("telegram_id", 0), "services"):
        return RedirectResponse("/services", status_code=302)
    if user.get("role") not in ("owner", "admin"):
        return RedirectResponse("/services", status_code=302)
    if not name.strip():
        return RedirectResponse(f"/services/{service_id}/edit?error=name_required", status_code=302)

    try:
        price_val = float(price.replace(",", "."))
    except (ValueError, AttributeError):
        price_val = 0.0
    try:
        dur_val = int(duration_minutes)
    except (ValueError, TypeError):
        dur_val = 60
    try:
        group_val = max(1, int(group_max_participants))
    except (ValueError, TypeError):
        group_val = 1
    cat_val = int(category_id) if category_id and category_id.isdigit() else None
    active_val = 1 if str(is_active) in ("1", "on", "true") else 0

    db = get_web_db(request)
    conn = db.get_connection()
    try:
        conn.execute(
            "UPDATE services SET name=?, description=?, category_id=?, price=?, "
            "duration_minutes=?, group_max_participants=?, is_active=?, updated_at=datetime('now') "
            "WHERE id=?",
            (name.strip(), description.strip(), cat_val, price_val,
             dur_val, group_val, active_val, service_id),
        )
        conn.commit()
        return RedirectResponse("/services", status_code=303)
    finally:
        conn.close()


@router.post("/services/{service_id}/delete")
def service_delete(request: Request, service_id: int, csrf_token: str = Form("")):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/services?error=csrf", status_code=302)
    if user.get("role") not in ("owner", "admin"):
        return RedirectResponse("/services", status_code=302)
    if not has_module(user.get("telegram_id", 0), "services"):
        return RedirectResponse("/services", status_code=302)

    db = get_web_db(request)
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM services WHERE id=?", (service_id,))
        conn.commit()
        return RedirectResponse("/services", status_code=303)
    finally:
        conn.close()


@router.get("/services/categories")
def service_categories_page(request: Request):
    from web.auth import get_session_user
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not has_module(user.get("telegram_id", 0), "services"):
        return RedirectResponse("/services", status_code=302)
    if user.get("role") not in ("owner", "admin"):
        return RedirectResponse("/services", status_code=302)

    db = get_web_db(request)
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT id, name, color, sort_order, is_active FROM service_categories ORDER BY sort_order, name"
        ).fetchall()
        cats = [{"id": r[0], "name": r[1], "color": r[2], "sort_order": r[3], "is_active": bool(r[4])} for r in rows]
        ctx = _get_ctx(request)
        ctx.update({"categories": cats})
        return request.app.state.templates.TemplateResponse(request, "services/categories.html", ctx)
    finally:
        conn.close()


@router.post("/services/categories/new")
def service_category_create(
    request: Request,
    csrf_token: str = Form(""),
    name: str = Form(""),
    color: str = Form("#6366f1"),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/services/categories", status_code=302)
    if not has_module(user.get("telegram_id", 0), "services"):
        return RedirectResponse("/services", status_code=302)
    if not name.strip():
        return RedirectResponse("/services/categories", status_code=302)

    db = get_web_db(request)
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO service_categories (name, color) VALUES (?, ?)",
            (name.strip(), color.strip() or "#6366f1"),
        )
        conn.commit()
        return RedirectResponse("/services/categories", status_code=303)
    finally:
        conn.close()


@router.post("/services/categories/{cat_id}/delete")
def service_category_delete(request: Request, cat_id: int, csrf_token: str = Form("")):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/services/categories", status_code=302)
    if user.get("role") not in ("owner", "admin"):
        return RedirectResponse("/services/categories", status_code=302)
    if not has_module(user.get("telegram_id", 0), "services"):
        return RedirectResponse("/services", status_code=302)

    db = get_web_db(request)
    conn = db.get_connection()
    try:
        conn.execute("UPDATE services SET category_id=NULL WHERE category_id=?", (cat_id,))
        conn.execute("DELETE FROM service_categories WHERE id=?", (cat_id,))
        conn.commit()
        return RedirectResponse("/services/categories", status_code=303)
    finally:
        conn.close()


@router.get("/services/{service_id}/motivation")
def service_motivation_page(request: Request, service_id: int):
    from web.auth import get_session_user
    from web.deps import get_web_db
    from billing_utils import has_module, has_extension
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not has_module(user.get("telegram_id", 0), "services"):
        return RedirectResponse("/services", status_code=302)
    if user.get("role") not in ("owner", "admin"):
        return RedirectResponse("/services", status_code=302)

    tg_id = user.get("telegram_id", 0)
    if not has_extension(tg_id, "services", "services_motivation"):
        ctx = _get_ctx(request)
        ctx["upsell"] = True
        ctx["service_id"] = service_id
        return request.app.state.templates.TemplateResponse(request, "services/motivation.html", ctx)

    db = get_web_db(request)
    conn = db.get_connection()
    try:
        svc = conn.execute("SELECT id, name FROM services WHERE id=?", (service_id,)).fetchone()
        if not svc:
            return RedirectResponse("/services", status_code=302)

        rules = conn.execute(
            "SELECT id, scope_type, scope_value, type, value, is_active "
            "FROM service_motivation_rules WHERE service_id=? ORDER BY id",
            (service_id,),
        ).fetchall()
        rules_list = [{"id": r[0], "scope_type": r[1], "scope_value": r[2],
                       "type": r[3], "value": r[4], "is_active": bool(r[5])} for r in rules]

        ctx = _get_ctx(request)
        ctx.update({
            "service": {"id": svc[0], "name": svc[1]},
            "rules": rules_list, "upsell": False,
        })
        return request.app.state.templates.TemplateResponse(request, "services/motivation.html", ctx)
    finally:
        conn.close()


@router.post("/services/{service_id}/motivation")
def service_motivation_save(
    request: Request,
    service_id: int,
    csrf_token: str = Form(""),
    scope_type: str = Form("global"),
    scope_value: str = Form(""),
    mot_type: str = Form("percentage"),
    value: str = Form("0"),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from billing_utils import has_module, has_extension
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(f"/services/{service_id}/motivation", status_code=302)
    if not has_module(user.get("telegram_id", 0), "services"):
        return RedirectResponse("/services", status_code=302)
    if not has_extension(user.get("telegram_id", 0), "services", "services_motivation"):
        return RedirectResponse(f"/services/{service_id}/motivation", status_code=302)

    try:
        val = float(value.replace(",", "."))
    except (ValueError, AttributeError):
        val = 0.0

    db = get_web_db(request)
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO service_motivation_rules (service_id, scope_type, scope_value, type, value) "
            "VALUES (?, ?, ?, ?, ?)",
            (service_id, scope_type, scope_value.strip() or None, mot_type, val),
        )
        conn.commit()
        return RedirectResponse(f"/services/{service_id}/motivation", status_code=303)
    finally:
        conn.close()
