import json
import logging
import uuid
from datetime import datetime, timezone

import pytz
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, JSONResponse, Response

logger = logging.getLogger(__name__)
router = APIRouter()

ROLE_LABELS = {
    "owner": "Директор",
    "admin": "Администратор",
    "user": "Сотрудник",
}


def _get_user_db_id(db, telegram_id: int):
    try:
        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None
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
    }

    try:
        db = get_web_db(telegram_id, org_db)
        from timezone_utils import get_current_user_time
        tz_name = db.get_user_timezone(telegram_id) or "Europe/Moscow"

        ctx["now_local"] = get_current_user_time(tz_name).strftime("%Y-%m-%dT%H:%M")
        ctx["shops"] = db.get_all_shops() or []

        raw_sched = db.get_scheduled_notifications(status=None) or []
        scheduled = []
        for sn in raw_sched:
            rcpt_raw = sn[5] or "{}"
            try:
                rcpt_info = json.loads(rcpt_raw)
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
                    "created_at": str(h[5] or "")[:16],
                }
                for h in hist_raw
            ]

    except Exception as exc:
        logger.error(f"notifications_page error: {exc}")
        ctx["error"] = "Произошла внутренняя ошибка. Попробуйте позже."

    return request.app.state.templates.TemplateResponse(
        request, "notifications/index.html", ctx
    )


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

    text = notification_text.strip()
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
        tz_name = db.get_user_timezone(telegram_id) or "Europe/Moscow"
        user_db_id = _get_user_db_id(db, telegram_id)
        if not user_db_id:
            return RedirectResponse(url="/notifications?error=noid", status_code=303)

        rcpt_info: dict = {}
        if recipients_type == "shop" and shop_filter:
            rcpt_info = {"type": "shop", "filter": shop_filter}
        elif recipients_type == "role" and role_filter:
            rcpt_info = {"type": "role", "filter": role_filter}
        else:
            rcpt_info = {"type": "all"}

        if send_when == "schedule" and scheduled_at:
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
            recipients_list=json.dumps(rcpt_info, ensure_ascii=False),
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
