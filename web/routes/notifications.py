import json
import logging
import uuid
from datetime import datetime, timezone

import pytz
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, JSONResponse, Response
from timezone_utils import DEFAULT_TZ

logger = logging.getLogger(__name__)
router = APIRouter()


def _notif_url(notification_type: str, message: str = "", created_at: str = "") -> str:
    """Map notification_type to the most relevant web page URL.
    For daily_report, parse date and shop from message text to build a deep link.
    For shift_sale/sales, parse shop from message and date from created_at."""
    _MAP = {
        "shift_sale":        "/sales",
        "sales":             "/sales",
        "plan_milestone":    "/plans",
        "low_stock":         "/inventory",
        "daily_report":      "/reports",
        "payment":           "/subscription",
        "trial_expired":     "/subscription",
        "trial_expiring":    "/subscription",
        "contest":           "/contests",
        "contest_winner":    "/contests",
        "salary_adjustment": "/salary/earnings",
        "task_assigned":     "/tasks",
        "task_status":       "/tasks",
        "task_deadline":     "/tasks",
        "task_overdue":      "/tasks",
        "dm":                "/chat/dm",
        "admin":             "",
    }
    base = _MAP.get(notification_type or "", "")
    import re as _re
    from urllib.parse import urlencode as _ue
    if notification_type == "daily_report" and message:
        dm = _re.search(r'\((\d{4}-\d{2}-\d{2})\)', message)
        if dm:
            date = dm.group(1)
            shop = ""
            sm = _re.search(r'🏪\s+(?:Магазин:\s*)?([^\n•<]+)', message)
            if sm:
                raw = _re.sub(r'<[^>]+>', '', sm.group(1)).strip()
                if raw and ',' not in raw:
                    shop = raw
            params: dict = {"period": "custom", "date_from": date, "date_to": date}
            if shop:
                params["shop"] = shop
            return "/reports?" + _ue(params)
    if notification_type in ("shift_sale", "sales") and message and created_at:
        date = ""
        raw_date = str(created_at)
        dm = _re.search(r'(\d{4}-\d{2}-\d{2})', raw_date)
        if dm:
            date = dm.group(1)
        if date:
            shop = ""
            sm = _re.search(r'Новая продажа в магазине ([^<\n]+)', _re.sub(r'<[^>]+>', '', message))
            if sm:
                shop = sm.group(1).strip()
            params: dict = {"period": "custom", "date_from": date, "date_to": date}
            if shop:
                params["shop"] = shop
            return "/sales?" + _ue(params)
    return base

ROLE_LABELS = {
    "owner": "Директор",
    "admin": "Администратор",
    "user": "Сотрудник",
}


def _get_user_db_id(db, telegram_id: int):
    try:
        conn = db.get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))
            row = cur.fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except Exception:
        return None


def _fmt_scheduled_dt(dt_str: str, tz_name: str) -> str:
    """Convert UTC ISO string to human-readable local time."""
    try:
        dt_utc = datetime.fromisoformat(str(dt_str).replace("Z", "+00:00"))
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=timezone.utc)
        tz = pytz.timezone(tz_name)
        dt_local = dt_utc.astimezone(tz)
        return dt_local.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return str(dt_str)[:16]


def _local_to_utc(dt_local_str: str, tz_name: str) -> datetime:
    """Convert local datetime string (YYYY-MM-DDTHH:MM) to UTC datetime."""
    tz = pytz.timezone(tz_name)
    naive = datetime.strptime(dt_local_str, "%Y-%m-%dT%H:%M")
    local_dt = tz.localize(naive)
    return local_dt.astimezone(pytz.UTC)


def _recipients_label(rtype: str, rfilter) -> str:
    if rtype == "shop" and rfilter:
        return f"🏪 Магазин: {rfilter}"
    if rtype == "role" and rfilter:
        return f"🎭 Роль: {ROLE_LABELS.get(rfilter, rfilter)}"
    if rtype == "users" and rfilter:
        n = len(rfilter) if isinstance(rfilter, list) else 1
        return f"👤 {n} сотр."
    return "👥 Всем сотрудникам"


@router.get("/notifications")
def notifications_page(
    request: Request,
    msg: str = "",
):
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    is_admin = user.get("role") in ("owner", "admin", "super_admin")
    if not is_admin:
        return RedirectResponse(url="/dashboard", status_code=302)

    telegram_id = int(user["sub"])
    from billing_utils import has_module
    if not has_module(telegram_id, "notifications"):
        return RedirectResponse(url="/dashboard?msg=module_notifications_required", status_code=302)
    org_db = user.get("org_db")

    ctx: dict = {
        "request": request,
        "user": user,
        "is_admin": is_admin,
        "csrf_token": get_csrf_token(request),
        "shops": [],
        "scheduled": [],
        "history": [],
        "success": msg == "sent",
        "scheduled_success": msg == "scheduled",
        "error": None,
        "now_local": "",
        "user_tz": DEFAULT_TZ,
    }

    try:
        db = get_web_db(telegram_id, org_db)
        from timezone_utils import get_current_user_time
        tz_name = db.get_user_timezone(telegram_id) or DEFAULT_TZ

        ctx["now_local"] = get_current_user_time(tz_name).strftime("%Y-%m-%dT%H:%M")
        ctx["user_tz"] = tz_name
        ctx["shops"] = db.get_all_shops() or []

        raw_sched = db.get_scheduled_notifications(status=None) or []
        scheduled = []
        for sn in raw_sched:
            rcpt_raw = sn[5] or "{}"
            try:
                rcpt_info = json.loads(rcpt_raw)
                # Guard against double-encoded JSON (old bug): second decode if still a string
                if isinstance(rcpt_info, str):
                    rcpt_info = json.loads(rcpt_info)
                if not isinstance(rcpt_info, dict):
                    rcpt_info = {}
            except Exception:
                rcpt_info = {}
            rtype = rcpt_info.get("type", "all")
            rfilter = rcpt_info.get("filter")
            creator_name = f"{(sn[8] or '').strip()} {(sn[9] or '').strip()}".strip() or "—"
            status = sn[7] or "pending"
            scheduled.append({
                "id": sn[0],
                "job_id": sn[1],
                "text": sn[3] or "",
                "recipients_label": _recipients_label(rtype, rfilter),
                "scheduled_at": _fmt_scheduled_dt(sn[6], tz_name),
                "status": status,
                "creator": creator_name,
                "is_pending": status == "pending",
            })
        ctx["scheduled"] = scheduled

        user_db_id = _get_user_db_id(db, telegram_id)
        if user_db_id:
            hist_raw = db.get_notification_history(user_db_id, limit=30) or []
            ctx["history"] = [
                {
                    "id": h[0],
                    "type": h[2] or "admin",
                    "message": h[3] or "",
                    "is_read": bool(h[4]),
                    "created_at": _fmt_scheduled_dt(h[5], tz_name) if h[5] else "",
                    "url": _notif_url(h[2] or "admin", h[3] or "", str(h[5] or "")),
                }
                for h in hist_raw
            ]

    except Exception as exc:
        logger.error(f"notifications_page error: {exc}")
        ctx["error"] = "Произошла внутренняя ошибка. Попробуйте позже."

    return request.app.state.templates.TemplateResponse(
        request, "notifications/index.html", ctx
    )


@router.get("/notifications/search-users")
def notifications_search_users(request: Request, q: str = ""):
    """Return JSON list of org users matching search query (name / @username)."""
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return JSONResponse({"error": "Нет доступа"}, status_code=403)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    try:
        db = get_web_db(telegram_id, org_db)
        conn = db.get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, telegram_id, first_name, last_name, username, shop_name "
                "FROM users WHERE telegram_id IS NOT NULL ORDER BY first_name, last_name LIMIT 200"
            )
            rows = cur.fetchall()
        finally:
            conn.close()

        q_lower = q.strip().lower()
        result = []
        _skip_shops = {"системный", "system", ""}
        for r in rows:
            tid = r[1]
            try:
                tid_int = int(tid)
            except (TypeError, ValueError):
                continue
            if tid_int <= 0:
                continue
            fname = (r[2] or "").strip()
            lname = (r[3] or "").strip()
            uname = (r[4] or "").strip()
            shop = (r[5] or "").strip()
            if shop.lower() in _skip_shops:
                shop = ""
            name = f"{fname} {lname}".strip() or f"id{r[0]}"
            if q_lower and q_lower not in name.lower() and q_lower not in uname.lower():
                continue
            result.append({
                "id": r[0],
                "telegram_id": tid_int,
                "name": name,
                "username": uname,
                "shop": shop,
            })
            if len(result) >= 30:
                break
        return JSONResponse(result)
    except Exception as exc:
        logger.error(f"search_users error: {exc}")
        return JSONResponse([], status_code=500)


@router.post("/notifications/send")
def notifications_send(
    request: Request,
    csrf_token: str = Form(default=""),
    notification_text: str = Form(default=""),
    recipients_type: str = Form(default="all"),
    shop_filter: str = Form(default=""),
    role_filter: str = Form(default=""),
    send_when: str = Form(default="now"),
    scheduled_at: str = Form(default=""),
    target_telegram_ids_json: str = Form(default="[]"),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    is_admin = user.get("role") in ("owner", "admin", "super_admin")
    if not is_admin:
        return RedirectResponse(url="/dashboard", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return Response(content="Недействительный CSRF-токен.", status_code=403)

    text = notification_text.strip()[:1000]
    if not text:
        return RedirectResponse(url="/notifications?error=empty", status_code=303)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    from subscription_utils import check_notifications_permission
    if not check_notifications_permission(telegram_id) and user.get("role") != "super_admin":
        return RedirectResponse(
            url="/notifications?error=Рассылка+уведомлений+недоступна+на+вашем+тарифе",
            status_code=303,
        )

    try:
        db = get_web_db(telegram_id, org_db)
        tz_name = db.get_user_timezone(telegram_id) or DEFAULT_TZ
        user_db_id = _get_user_db_id(db, telegram_id)
        if not user_db_id:
            return RedirectResponse(url="/notifications?error=noid", status_code=303)

        rcpt_info: dict = {}
        if recipients_type == "shop" and shop_filter:
            rcpt_info = {"type": "shop", "filter": shop_filter}
        elif recipients_type == "role" and role_filter:
            rcpt_info = {"type": "role", "filter": role_filter}
        elif recipients_type == "users":
            try:
                tids = [int(t) for t in json.loads(target_telegram_ids_json) if t]
            except Exception:
                tids = []
            if not tids:
                return RedirectResponse(
                    url="/notifications?error=Выберите+хотя+бы+одного+сотрудника",
                    status_code=303,
                )
            rcpt_info = {"type": "users", "filter": tids}
        else:
            rcpt_info = {"type": "all"}

        if send_when == "schedule" and scheduled_at:
            from billing_utils import has_extension as _has_ext
            if not _has_ext(telegram_id, "scheduled_notifs"):
                return RedirectResponse(
                    url="/notifications?error=Плановые+рассылки+недоступны.+Подключите+расширение+«Плановые+уведомления».",
                    status_code=303,
                )
            dt_utc = _local_to_utc(scheduled_at, tz_name)
            result_msg = "scheduled"
        else:
            dt_utc = datetime.now(pytz.UTC)
            result_msg = "sent"

        dt_str = dt_utc.strftime("%Y-%m-%dT%H:%M:%S")
        job_id = str(uuid.uuid4())

        is_super = user.get("role") == "super_admin"
        db_recipients_type = "all" if is_super else "org"

        db.add_scheduled_notification(
            job_id=job_id,
            created_by=user_db_id,
            notification_text=text,
            recipients_type=db_recipients_type,
            recipients_list=rcpt_info,
            scheduled_datetime=dt_str,
        )
        logger.info(f"Web notification scheduled: job={job_id}, when={dt_str}, by={telegram_id}")
        return RedirectResponse(url=f"/notifications?msg={result_msg}", status_code=303)

    except Exception as exc:
        logger.error(f"notifications_send error: {exc}")
        return RedirectResponse(url="/notifications?error=1", status_code=303)


@router.post("/notifications/scheduled/{notif_id}/delete")
def notifications_delete_scheduled(
    request: Request,
    notif_id: int,
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    is_admin = user.get("role") in ("owner", "admin", "super_admin")
    if not is_admin:
        return JSONResponse({"ok": False, "error": "Нет доступа"}, status_code=403)
    if not verify_csrf_token(request, csrf_token):
        return JSONResponse({"ok": False, "error": "CSRF"}, status_code=403)

    try:
        telegram_id = int(user["sub"])
        org_db = user.get("org_db")
        db = get_web_db(telegram_id, org_db)
        deleted = db.delete_scheduled_notification(notif_id)
        if deleted:
            return JSONResponse({"ok": True})
        return JSONResponse({"ok": False, "error": "Уведомление не найдено"}, status_code=404)
    except Exception as exc:
        logger.error(f"notifications_delete error: {exc}")
        return JSONResponse({"ok": False, "error": "Внутренняя ошибка сервера"}, status_code=500)
