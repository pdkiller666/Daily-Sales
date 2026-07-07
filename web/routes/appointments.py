"""
Web-маршруты модуля Записи.
GET  /appointments                  — список записей
GET  /appointments/new              — форма создания записи
POST /appointments/new              — создать запись
GET  /appointments/{id}             — карточка записи
POST /appointments/{id}/status      — изменить статус
POST /appointments/{id}/edit        — редактировать запись
POST /appointments/{id}/delete      — удалить запись
GET  /appointments/calendar         — вид календаря
GET  /api/appointments/staff_slots  — занятые слоты мастера (JSON)
"""
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

router = APIRouter()
logger = logging.getLogger(__name__)

_PAGE_SIZE = 50
_STATUSES = {
    "planned": "Запланирована",
    "confirmed": "Подтверждена",
    "completed": "Выполнена",
    "cancelled": "Отменена",
    "no_show": "Неявка",
}


def _get_ctx(request: Request):
    from web.auth import get_session_user, get_csrf_token
    user = get_session_user(request) or {}
    return {
        "user": user,
        "csrf_token": get_csrf_token(request),
    }


def _row_to_appt(r) -> dict:
    return {
        "id": r[0], "service_id": r[1], "service_name": r[2] or "—",
        "client_id": r[3], "client_name": r[4] or "—", "client_phone": r[5] or "",
        "staff_user_id": r[6], "staff_name": r[7] or "—",
        "start_time": r[8], "end_time": r[9],
        "status": r[10], "status_label": _STATUSES.get(r[10], r[10]),
        "notes": r[11], "price": r[12],
    }


def _appt_query(conn, where: str = "1=1", params: list = None, limit: int = _PAGE_SIZE, offset: int = 0):
    p = params or []
    rows = conn.execute(
        f"SELECT a.id, a.service_id, sv.name, a.client_id, "
        f"c.first_name||' '||c.last_name, c.phone, "
        f"a.staff_user_id, u.first_name||' '||u.last_name, "
        f"a.start_time, a.end_time, a.status, a.notes, a.price "
        f"FROM appointments a "
        f"LEFT JOIN services sv ON a.service_id=sv.id "
        f"LEFT JOIN clients c ON a.client_id=c.id "
        f"LEFT JOIN users u ON a.staff_user_id=u.id "
        f"WHERE {where} ORDER BY a.start_time DESC LIMIT ? OFFSET ?",
        p + [limit, offset],
    ).fetchall()
    return [_row_to_appt(r) for r in rows]


@router.get("/appointments")
def appointments_list(request: Request, status: str = "", staff_id: int = 0,
                      date_from: str = "", date_to: str = "", page: int = 1):
    from web.auth import get_session_user
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not has_module(int(user.get("sub", 0)), "services"):
        return RedirectResponse("/subscription?msg=services_locked", status_code=302)

    tg_id = int(user.get("sub", 0))
    org_db = user.get("org_db", "")
    if not org_db:
        return RedirectResponse("/dashboard", status_code=302)
    db = get_web_db(tg_id, org_db)
    conn = db.get_connection()
    try:
        is_admin = user.get("role") in ("owner", "admin", "super_admin")
        params = []
        where = "1=1"

        if not is_admin:
            my_user = conn.execute(
                "SELECT id FROM users WHERE telegram_id=?", (int(user.get("sub", 0)),)
            ).fetchone()
            my_uid = my_user[0] if my_user else -1
            where += " AND a.staff_user_id=?"
            params.append(my_uid)

        if status:
            where += " AND a.status=?"
            params.append(status)
        if staff_id:
            where += " AND a.staff_user_id=?"
            params.append(staff_id)
        if date_from:
            where += " AND a.start_time >= ?"
            params.append(date_from)
        if date_to:
            where += " AND a.start_time <= ?"
            params.append(date_to + " 23:59:59")

        total = conn.execute(
            f"SELECT COUNT(*) FROM appointments a WHERE {where}", params
        ).fetchone()[0]
        offset = (page - 1) * _PAGE_SIZE
        appts = _appt_query(conn, where, params, _PAGE_SIZE, offset)
        total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)

        staff_list = conn.execute(
            "SELECT DISTINCT u.id, u.first_name||' '||u.last_name "
            "FROM appointments a JOIN users u ON a.staff_user_id=u.id ORDER BY u.first_name"
        ).fetchall() if is_admin else []

        ctx = _get_ctx(request)
        ctx.update({
            "appts": appts, "total": total, "page": page, "total_pages": total_pages,
            "status": status, "staff_id": staff_id,
            "date_from": date_from, "date_to": date_to,
            "statuses": _STATUSES, "staff_list": [{"id": r[0], "name": r[1]} for r in staff_list],
            "is_admin": is_admin,
        })
        return request.app.state.templates.TemplateResponse(request, "appointments/index.html", ctx)
    finally:
        conn.close()


@router.get("/appointments/new")
def appointment_new_form(request: Request):
    from web.auth import get_session_user
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not has_module(int(user.get("sub", 0)), "services"):
        return RedirectResponse("/appointments", status_code=302)

    tg_id = int(user.get("sub", 0))
    org_db = user.get("org_db", "")
    if not org_db:
        return RedirectResponse("/dashboard", status_code=302)
    db = get_web_db(tg_id, org_db)
    conn = db.get_connection()
    try:
        services = conn.execute(
            "SELECT id, name, price, duration_minutes FROM services WHERE is_active=1 ORDER BY name"
        ).fetchall()
        clients = conn.execute(
            "SELECT id, first_name||' '||last_name, phone FROM clients ORDER BY first_name LIMIT 200"
        ).fetchall()
        staff = conn.execute(
            "SELECT id, first_name||' '||last_name FROM users ORDER BY first_name"
        ).fetchall()

        prefill_client = request.query_params.get("client_id", "")
        prefill_service = request.query_params.get("service_id", "")

        ctx = _get_ctx(request)
        ctx.update({
            "appt": None,
            "services": [{"id": r[0], "name": r[1], "price": r[2], "duration": r[3]} for r in services],
            "clients": [{"id": r[0], "name": r[1].strip(), "phone": r[2]} for r in clients],
            "staff": [{"id": r[0], "name": r[1].strip()} for r in staff],
            "statuses": _STATUSES,
            "prefill_client": prefill_client, "prefill_service": prefill_service,
            "error": request.query_params.get("error", ""),
        })
        return request.app.state.templates.TemplateResponse(request, "appointments/form.html", ctx)
    finally:
        conn.close()


@router.post("/appointments/new")
def appointment_create(
    request: Request,
    csrf_token: str = Form(""),
    service_id: str = Form(""),
    client_id: str = Form(""),
    staff_user_id: str = Form(""),
    start_date: str = Form(""),
    start_time: str = Form(""),
    notes: str = Form(""),
    price: str = Form(""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/appointments/new?error=csrf", status_code=302)
    if not has_module(int(user.get("sub", 0)), "services"):
        return RedirectResponse("/appointments", status_code=302)
    if not service_id or not client_id or not start_date or not start_time:
        return RedirectResponse("/appointments/new?error=required_fields", status_code=302)

    tg_id = int(user.get("sub", 0))
    org_db = user.get("org_db", "")
    if not org_db:
        return RedirectResponse("/dashboard", status_code=302)
    db = get_web_db(tg_id, org_db)
    conn = db.get_connection()
    try:
        svc = conn.execute("SELECT price, duration_minutes FROM services WHERE id=?", (service_id,)).fetchone()
        if not svc:
            return RedirectResponse("/appointments/new?error=no_service", status_code=302)

        svc_price, duration = svc
        price_val = float(price.replace(",", ".")) if price.strip() else svc_price
        start_dt_str = f"{start_date} {start_time}:00"
        try:
            start_dt = datetime.fromisoformat(start_dt_str)
        except ValueError:
            return RedirectResponse("/appointments/new?error=bad_datetime", status_code=302)
        end_dt = start_dt + timedelta(minutes=duration)
        end_dt_str = end_dt.strftime("%Y-%m-%d %H:%M:%S")

        staff_val = int(staff_user_id) if staff_user_id and staff_user_id.isdigit() else None

        if staff_val:
            conflict = conn.execute(
                "SELECT id FROM appointments WHERE staff_user_id=? AND status NOT IN ('cancelled','no_show') "
                "AND start_time < ? AND end_time > ?",
                (staff_val, end_dt_str, start_dt_str),
            ).fetchone()
            if conflict:
                return RedirectResponse("/appointments/new?error=staff_busy", status_code=302)

        cur = conn.execute(
            "INSERT INTO appointments (service_id, client_id, staff_user_id, start_time, end_time, "
            "notes, price, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (int(service_id), int(client_id), staff_val, start_dt_str, end_dt_str,
             notes.strip(), price_val, int(user.get("sub", 0))),
        )
        conn.commit()
        appt_id = cur.lastrowid

        conn.execute(
            "INSERT INTO appointment_status_log (appointment_id, new_status, changed_by) VALUES (?, 'planned', ?)",
            (appt_id, int(user.get("sub", 0))),
        )
        conn.commit()

        return RedirectResponse(f"/appointments/{appt_id}", status_code=303)
    finally:
        conn.close()


@router.get("/appointments/calendar")
def appointments_calendar(request: Request, year: int = 0, month: int = 0):
    from web.auth import get_session_user
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not has_module(int(user.get("sub", 0)), "services"):
        return RedirectResponse("/appointments", status_code=302)

    now = datetime.now()
    year = year or now.year
    month = month or now.month

    from calendar import monthrange
    _, days_in_month = monthrange(year, month)
    date_from = f"{year:04d}-{month:02d}-01"
    date_to = f"{year:04d}-{month:02d}-{days_in_month:02d} 23:59:59"

    tg_id = int(user.get("sub", 0))
    org_db = user.get("org_db", "")
    if not org_db:
        return RedirectResponse("/dashboard", status_code=302)
    db = get_web_db(tg_id, org_db)
    conn = db.get_connection()
    try:
        is_admin = user.get("role") in ("owner", "admin", "super_admin")
        params = [date_from, date_to]
        where = "a.start_time >= ? AND a.start_time <= ?"
        if not is_admin:
            my_user = conn.execute("SELECT id FROM users WHERE telegram_id=?", (int(user.get("sub", 0)),)).fetchone()
            my_uid = my_user[0] if my_user else -1
            where += " AND a.staff_user_id=?"
            params.append(my_uid)

        appts = _appt_query(conn, where, params, limit=500, offset=0)

        by_day = {}
        for a in appts:
            try:
                day = int(a["start_time"][8:10])
            except (TypeError, IndexError, ValueError):
                continue
            by_day.setdefault(day, []).append(a)

        prev_month = month - 1 if month > 1 else 12
        prev_year = year if month > 1 else year - 1
        next_month = month + 1 if month < 12 else 1
        next_year = year if month < 12 else year + 1

        from calendar import weekday as cal_weekday
        first_weekday = cal_weekday(year, month, 1)

        ctx = _get_ctx(request)
        ctx.update({
            "year": year, "month": month, "days_in_month": days_in_month,
            "first_weekday": first_weekday, "by_day": by_day,
            "statuses": _STATUSES, "is_admin": is_admin,
            "prev_year": prev_year, "prev_month": prev_month,
            "next_year": next_year, "next_month": next_month,
            "month_name": ["", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
                           "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"][month],
        })
        return request.app.state.templates.TemplateResponse(request, "appointments/calendar.html", ctx)
    finally:
        conn.close()


@router.get("/appointments/{appt_id}")
def appointment_detail(request: Request, appt_id: int):
    from web.auth import get_session_user
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not has_module(int(user.get("sub", 0)), "services"):
        return RedirectResponse("/appointments", status_code=302)

    tg_id = int(user.get("sub", 0))
    org_db = user.get("org_db", "")
    if not org_db:
        return RedirectResponse("/dashboard", status_code=302)
    db = get_web_db(tg_id, org_db)
    conn = db.get_connection()
    try:
        rows = _appt_query(conn, "a.id=?", [appt_id])
        if not rows:
            return RedirectResponse("/appointments", status_code=302)
        appt = rows[0]

        is_admin = user.get("role") in ("owner", "admin", "super_admin")
        if not is_admin:
            my_user = conn.execute("SELECT id FROM users WHERE telegram_id=?", (int(user.get("sub", 0)),)).fetchone()
            my_uid = my_user[0] if my_user else -1
            if appt["staff_user_id"] != my_uid:
                return RedirectResponse("/appointments", status_code=302)

        log = conn.execute(
            "SELECT old_status, new_status, changed_by, note, created_at "
            "FROM appointment_status_log WHERE appointment_id=? ORDER BY created_at",
            (appt_id,),
        ).fetchall()
        history = [{"old": r[0], "new": r[1], "new_label": _STATUSES.get(r[1], r[1]),
                    "by": r[2], "note": r[3], "at": r[4]} for r in log]

        services = conn.execute(
            "SELECT id, name FROM services WHERE is_active=1 ORDER BY name"
        ).fetchall()
        clients = conn.execute(
            "SELECT id, first_name||' '||last_name FROM clients ORDER BY first_name LIMIT 200"
        ).fetchall()
        staff = conn.execute(
            "SELECT id, first_name||' '||last_name FROM users ORDER BY first_name"
        ).fetchall()

        ctx = _get_ctx(request)
        ctx.update({
            "appt": appt, "history": history, "statuses": _STATUSES,
            "services": [{"id": r[0], "name": r[1]} for r in services],
            "clients": [{"id": r[0], "name": r[1].strip()} for r in clients],
            "staff": [{"id": r[0], "name": r[1].strip()} for r in staff],
            "is_admin": is_admin,
        })
        return request.app.state.templates.TemplateResponse(request, "appointments/detail.html", ctx)
    finally:
        conn.close()


@router.post("/appointments/{appt_id}/status")
def appointment_change_status(
    request: Request,
    appt_id: int,
    csrf_token: str = Form(""),
    new_status: str = Form(""),
    note: str = Form(""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from billing_utils import has_module, has_extension
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(f"/appointments/{appt_id}", status_code=302)
    if not has_module(int(user.get("sub", 0)), "services"):
        return RedirectResponse("/subscription?msg=services_locked", status_code=302)
    if new_status not in _STATUSES:
        return RedirectResponse(f"/appointments/{appt_id}", status_code=302)

    tg_id = int(user.get("sub", 0))
    org_db = user.get("org_db", "")
    if not org_db:
        return RedirectResponse("/dashboard", status_code=302)
    db = get_web_db(tg_id, org_db)
    conn = db.get_connection()
    try:
        old = conn.execute("SELECT status, service_id, staff_user_id, price FROM appointments WHERE id=?", (appt_id,)).fetchone()
        if not old:
            return RedirectResponse("/appointments", status_code=302)

        old_status, service_id, staff_user_id, appt_price = old

        is_admin = user.get("role") in ("owner", "admin", "super_admin")
        if not is_admin:
            my_user = conn.execute(
                "SELECT id FROM users WHERE telegram_id=?", (int(user.get("sub", 0)),)
            ).fetchone()
            my_uid = my_user[0] if my_user else -1
            if staff_user_id != my_uid:
                return RedirectResponse(f"/appointments/{appt_id}", status_code=302)

        conn.execute(
            "UPDATE appointments SET status=?, updated_at=datetime('now') WHERE id=?",
            (new_status, appt_id),
        )
        conn.execute(
            "INSERT INTO appointment_status_log (appointment_id, old_status, new_status, changed_by, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (appt_id, old_status, new_status, int(user.get("sub", 0)), note.strip()),
        )

        if new_status == "completed" and old_status != "completed":
            if staff_user_id and has_extension(int(user.get("sub", 0)), "services", "services_motivation"):
                _calc_service_commission(conn, appt_id, service_id, staff_user_id, appt_price or 0)

        conn.commit()
        return RedirectResponse(f"/appointments/{appt_id}", status_code=303)
    finally:
        conn.close()


def _calc_service_commission(conn, appt_id: int, service_id: int, staff_user_id: int, price: float):
    try:
        rule = conn.execute(
            "SELECT type, value FROM service_motivation_rules "
            "WHERE service_id=? AND is_active=1 ORDER BY "
            "CASE scope_type WHEN 'user' THEN 1 WHEN 'shop' THEN 2 WHEN 'global' THEN 3 ELSE 4 END LIMIT 1",
            (service_id,),
        ).fetchone()
        if not rule:
            rule = conn.execute(
                "SELECT type, value FROM service_motivation_rules "
                "WHERE (service_id IS NULL OR service_id=0) AND scope_type='global' AND is_active=1 LIMIT 1",
            ).fetchone()
        if not rule:
            return

        mot_type, mot_value = rule
        if mot_type == "percentage":
            commission = (price * mot_value) / 100.0
        else:
            commission = float(mot_value)

        conn.execute(
            "INSERT OR REPLACE INTO service_earnings "
            "(appointment_id, user_id, service_id, commission_amount, motivation_type, motivation_value, motivation_source) "
            "VALUES (?, ?, ?, ?, ?, ?, 'global')",
            (appt_id, staff_user_id, service_id, commission, mot_type, mot_value),
        )
    except Exception as e:
        logger.warning("_calc_service_commission appt_id=%s: %s", appt_id, e)


@router.post("/appointments/{appt_id}/edit")
def appointment_edit(
    request: Request,
    appt_id: int,
    csrf_token: str = Form(""),
    service_id: str = Form(""),
    client_id: str = Form(""),
    staff_user_id: str = Form(""),
    start_date: str = Form(""),
    start_time: str = Form(""),
    notes: str = Form(""),
    price: str = Form(""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(f"/appointments/{appt_id}", status_code=302)
    if not has_module(int(user.get("sub", 0)), "services"):
        return RedirectResponse("/appointments", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(f"/appointments/{appt_id}", status_code=302)

    tg_id = int(user.get("sub", 0))
    org_db = user.get("org_db", "")
    if not org_db:
        return RedirectResponse("/dashboard", status_code=302)
    db = get_web_db(tg_id, org_db)
    conn = db.get_connection()
    try:
        svc = conn.execute("SELECT price, duration_minutes FROM services WHERE id=?", (service_id,)).fetchone()
        if not svc:
            return RedirectResponse(f"/appointments/{appt_id}?error=no_service", status_code=302)

        svc_price, duration = svc
        price_val = float(price.replace(",", ".")) if price.strip() else svc_price
        start_dt_str = f"{start_date} {start_time}:00"
        try:
            start_dt = datetime.fromisoformat(start_dt_str)
        except ValueError:
            return RedirectResponse(f"/appointments/{appt_id}?error=bad_datetime", status_code=302)
        end_dt = start_dt + timedelta(minutes=duration)
        end_dt_str = end_dt.strftime("%Y-%m-%d %H:%M:%S")
        staff_val = int(staff_user_id) if staff_user_id and staff_user_id.isdigit() else None

        conn.execute(
            "UPDATE appointments SET service_id=?, client_id=?, staff_user_id=?, start_time=?, end_time=?, "
            "notes=?, price=?, updated_at=datetime('now') WHERE id=?",
            (int(service_id), int(client_id), staff_val, start_dt_str, end_dt_str,
             notes.strip(), price_val, appt_id),
        )
        conn.commit()
        return RedirectResponse(f"/appointments/{appt_id}", status_code=303)
    finally:
        conn.close()


@router.post("/appointments/{appt_id}/delete")
def appointment_delete(request: Request, appt_id: int, csrf_token: str = Form("")):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(f"/appointments/{appt_id}", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(f"/appointments/{appt_id}", status_code=302)
    if not has_module(int(user.get("sub", 0)), "services"):
        return RedirectResponse("/appointments", status_code=302)

    tg_id = int(user.get("sub", 0))
    org_db = user.get("org_db", "")
    if not org_db:
        return RedirectResponse("/dashboard", status_code=302)
    db = get_web_db(tg_id, org_db)
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM appointment_status_log WHERE appointment_id=?", (appt_id,))
        conn.execute("DELETE FROM service_earnings WHERE appointment_id=?", (appt_id,))
        conn.execute("DELETE FROM appointments WHERE id=?", (appt_id,))
        conn.commit()
        return RedirectResponse("/appointments", status_code=303)
    finally:
        conn.close()


@router.get("/api/appointments/staff_slots")
def staff_slots_api(request: Request, staff_id: int = 0, date: str = ""):
    from web.auth import get_session_user
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user or not has_module(int(user.get("sub", 0)), "services"):
        return JSONResponse({"slots": []})

    if not staff_id or not date:
        return JSONResponse({"slots": []})

    tg_id = int(user.get("sub", 0))
    org_db = user.get("org_db", "")
    if not org_db:
        return JSONResponse({"slots": []})
    db = get_web_db(tg_id, org_db)
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT start_time, end_time, status FROM appointments "
            "WHERE staff_user_id=? AND date(start_time)=? AND status NOT IN ('cancelled','no_show')",
            (staff_id, date),
        ).fetchall()
        slots = [{"start": r[0], "end": r[1], "status": r[2]} for r in rows]
        return JSONResponse({"slots": slots})
    finally:
        conn.close()
