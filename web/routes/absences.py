"""
Web-маршруты модуля учёта отсутствий.
GET  /absences            — список + календарь (admin видит всех, user — только себя)
POST /absences/add        — добавить / подать заявку
POST /absences/update     — одобрить / отклонить / отменить
GET  /absences/settings   — настройки типов (admin)
POST /absences/settings/update — сохранить настройку типа
"""
import json
import logging
import calendar as _cal
import os
import urllib.request
from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse

router = APIRouter()

MONTH_NAMES = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
    5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
    9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}
TYPE_LABELS = {
    'vacation':     '⚪ Отпуск',
    'sick':         '🔵 Больничный',
    'compensatory': '🟡 Отгул',
    'absence':      '🔴 Прогул',
    'other':        '⬜ Другое',
}
STATUS_LABELS = {
    'pending':   '❓ На рассмотрении',
    'approved':  '✅ Одобрено',
    'rejected':  '❌ Отклонено',
    'cancelled': '🚫 Отменено',
}
PENALTY_LABELS = {
    'none':   'Без штрафа',
    'no_pay': 'Не засчитывать день',
    'fine':   'Штраф (фиксированный)',
    'both':   'Не засчитывать + штраф',
}
TYPE_CSS = {
    'vacation':     'bg-slate-100 text-slate-500 border border-slate-300',
    'sick':         'bg-blue-100 text-blue-600 border border-blue-300',
    'compensatory': 'bg-amber-100 text-amber-600 border border-amber-300',
    'absence':      'bg-red-100 text-red-600 border border-red-300',
    'other':        'bg-purple-100 text-purple-600 border border-purple-300',
}


def _send_tg_absence_notify(
    employee_tg_id: int,
    new_status: str,
    atype: str,
    sd: str,
    ed: str,
    admin_comment: str | None,
    days: int,
) -> None:
    """Отправить сотруднику уведомление об изменении статуса заявки (fire-and-forget)."""
    token = os.environ.get("BOT_TOKEN", "")
    if not token or not employee_tg_id:
        return
    type_label = TYPE_LABELS.get(atype, atype)
    sd_fmt = sd[:10]
    ed_fmt = ed[:10]
    try:
        from datetime import date as _date
        sd_fmt = _date.fromisoformat(sd[:10]).strftime('%d.%m.%Y')
        ed_fmt = _date.fromisoformat(ed[:10]).strftime('%d.%m.%Y')
    except Exception:
        pass
    if new_status == "approved":
        text = (f'✅ <b>Заявка одобрена!</b>\n\n'
                f'{type_label}\n'
                f'📅 {sd_fmt}–{ed_fmt} ({days} дн.)')
    elif new_status == "rejected":
        text = (f'❌ <b>Заявка отклонена</b>\n\n'
                f'{type_label}\n'
                f'📅 {sd_fmt}–{ed_fmt} ({days} дн.)')
        if admin_comment:
            text += f'\nПричина: {admin_comment}'
    else:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = json.dumps({
            "chat_id": employee_tg_id,
            "text": text,
            "parse_mode": "HTML",
        }).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        import threading
        threading.Thread(
            target=lambda: urllib.request.urlopen(req, timeout=10),
            daemon=True,
        ).start()
    except Exception as e:
        logging.error("absence web notify: %s", e)


def _adjacent_month(year: int, month: int, delta: int):
    total = (year - 1) * 12 + (month - 1) + delta
    return (total // 12 + 1, total % 12 + 1)


def _days_count(sd: str, ed: str) -> int:
    try:
        return (date.fromisoformat(ed[:10]) - date.fromisoformat(sd[:10])).days + 1
    except Exception:
        return 1


def _build_cal_grid(year: int, month: int):
    fw, dim = _cal.monthrange(year, month)
    grid, week = [], [0] * fw
    for d in range(1, dim + 1):
        week.append(d)
        if len(week) == 7:
            grid.append(week); week = []
    if week:
        grid.append(week + [0] * (7 - len(week)))
    return grid


@router.get("/absences")
def absences_page(request: Request, year: int = 0, month: int = 0,
                  user_id: int = 0, msg: str = ""):
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    is_admin = user.get("role") in ("owner", "admin", "super_admin")
    today = date.today()
    if not year:  year = today.year
    if not month: month = today.month

    prev_y, prev_m = _adjacent_month(year, month, -1)
    next_y, next_m = _adjacent_month(year, month, 1)

    ctx: dict = {
        "request": request, "user": user, "is_admin": is_admin,
        "year": year, "month": month,
        "month_name": MONTH_NAMES.get(month, str(month)),
        "prev_y": prev_y, "prev_m": prev_m,
        "next_y": next_y, "next_m": next_m,
        "today_day": today.day if (today.year == year and today.month == month) else 0,
        "staff_list": [], "selected_user_id": user_id,
        "absences": [], "absence_map": {},
        "cal_grid": _build_cal_grid(year, month),
        "weekday_names": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"],
        "type_labels": TYPE_LABELS, "status_labels": STATUS_LABELS,
        "type_css": TYPE_CSS,
        "csrf_token": get_csrf_token(request),
        "msg": msg, "error": None,
    }

    try:
        db = get_web_db(telegram_id, org_db)

        if is_admin:
            # Список сотрудников для выбора
            import sqlite3 as _sq
            conn = db.get_connection()
            staff = conn.execute(
                "SELECT id, first_name, last_name, shop_name, telegram_id "
                "FROM users ORDER BY first_name"
            ).fetchall() or []
            conn.close()
            ctx["staff_list"] = [
                {"id": r[0], "first_name": r[1] or "", "last_name": r[2] or "",
                 "shop_name": r[3] or "", "telegram_id": r[4]}
                for r in staff
            ]
            # Все отсутствия за месяц
            rows = db.get_all_absences_admin(year, month)
            absences = []
            for r in rows:
                (ab_id, uid, atype, sd, ed, status, is_paid, comment,
                 admin_comment, created_at, fn, ln, shop) = r
                if user_id and uid != user_id:
                    continue
                absences.append({
                    "id": ab_id, "user_id": uid, "type": atype,
                    "type_label": TYPE_LABELS.get(atype, atype),
                    "type_css": TYPE_CSS.get(atype, ""),
                    "start_date": sd, "end_date": ed,
                    "days": _days_count(sd, ed),
                    "status": status, "status_label": STATUS_LABELS.get(status, status),
                    "is_paid": is_paid,
                    "comment": comment or "", "admin_comment": admin_comment or "",
                    "created_at": (created_at or "")[:10],
                    "name": f"{fn or ''} {ln or ''}".strip(),
                    "shop": shop or "",
                })
            ctx["absences"] = absences
            _abs_raw = db.get_absence_days_map(year, month, user_id or None)
            if user_id:
                ctx["absence_map"] = _abs_raw.get(user_id, {})
            else:
                # admin без фильтра: merge всех пользователей (для красивого
                # вида — используем только первого встреченного на каждый день)
                merged: dict = {}
                for _uid_key, _day_map in _abs_raw.items():
                    for _day, _info in _day_map.items():
                        if _day not in merged:
                            merged[_day] = _info
                ctx["absence_map"] = merged
        else:
            # Сотрудник видит только свои записи
            import sqlite3 as _sq
            conn = db.get_connection()
            row = conn.execute(
                "SELECT id FROM users WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
            conn.close()
            if row:
                uid = row[0]
                ctx["selected_user_id"] = uid
                raw = db.get_absences_for_user(uid, year, month)
                absences = []
                for r in raw:
                    ab_id, atype, sd, ed, status, is_paid, comment, admin_comment, created_at = r
                    absences.append({
                        "id": ab_id, "user_id": uid, "type": atype,
                        "type_label": TYPE_LABELS.get(atype, atype),
                        "type_css": TYPE_CSS.get(atype, ""),
                        "start_date": sd, "end_date": ed,
                        "days": _days_count(sd, ed),
                        "status": status, "status_label": STATUS_LABELS.get(status, status),
                        "is_paid": is_paid,
                        "comment": comment or "", "admin_comment": admin_comment or "",
                        "created_at": (created_at or "")[:10],
                        "name": "", "shop": "",
                    })
                ctx["absences"] = absences
                _abs_raw2 = db.get_absence_days_map(year, month, uid)
                ctx["absence_map"] = _abs_raw2.get(uid, {})

    except Exception as exc:
        logging.error(f"absences_page error: {exc}")
        ctx["error"] = "Произошла внутренняя ошибка. Попробуйте позже."

    return request.app.state.templates.TemplateResponse(
        request, "absences/index.html", ctx
    )


@router.post("/absences/add")
def absences_add(
    request: Request,
    csrf_token: Annotated[str, Form()] = "",
    user_id: Annotated[int, Form()] = 0,
    atype: Annotated[str, Form()] = "vacation",
    start_date: Annotated[str, Form()] = "",
    end_date: Annotated[str, Form()] = "",
    comment: Annotated[str, Form()] = "",
    status: Annotated[str, Form()] = "pending",
    year: Annotated[int, Form()] = 0,
    month: Annotated[int, Form()] = 0,
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/absences?msg=csrf_error", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    is_admin = user.get("role") in ("owner", "admin", "super_admin")
    today = date.today()
    if not year:  year = today.year
    if not month: month = today.month

    try:
        db = get_web_db(telegram_id, org_db)
        # Определить target user_id в org db
        if is_admin and user_id:
            target_uid = user_id
            final_status = status  # admin может сразу одобрить
        else:
            conn = db.get_connection()
            row = conn.execute(
                "SELECT id FROM users WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
            conn.close()
            target_uid = row[0] if row else 0
            final_status = "pending"  # сотрудник подаёт заявку

        if not target_uid:
            return RedirectResponse(
                url=f"/absences?year={year}&month={month}&msg=user_not_found",
                status_code=302
            )
        if not start_date or not end_date:
            return RedirectResponse(
                url=f"/absences?year={year}&month={month}&msg=missing_dates",
                status_code=302
            )

        # Проверить лимит
        settings = db.get_absence_type_settings()
        limit = settings.get(atype, {}).get('annual_limit', 0)
        if limit:
            used = db.get_absence_used_days(target_uid, atype, int(start_date[:4]))
            if used >= limit:
                return RedirectResponse(
                    url=f"/absences?year={year}&month={month}&msg=limit_exceeded",
                    status_code=302
                )

        ab_id = db.add_absence(
            target_uid, atype, start_date, end_date,
            comment or None, target_uid, final_status
        )

        # Штраф за прогул (если admin добавляет approved absence)
        if is_admin and final_status == 'approved' and atype == 'absence':
            s = settings.get('absence', {})
            pmode = s.get('penalty_mode', 'no_pay')
            pamt = s.get('penalty_amount', 0.0)
            if pmode in ('fine', 'both') and pamt:
                try:
                    sd_d = date.fromisoformat(start_date[:10])
                    ed_d = date.fromisoformat(end_date[:10])
                    days = (ed_d - sd_d).days + 1
                    conn_adm = db.get_connection()
                    adm_row = conn_adm.execute(
                        "SELECT id FROM users WHERE telegram_id=?", (telegram_id,)
                    ).fetchone()
                    conn_adm.close()
                    admin_org_id = adm_row[0] if adm_row else target_uid
                    db.apply_absence_penalty(
                        target_uid, ab_id,
                        sd_d.year, sd_d.month,
                        pamt * days, admin_org_id
                    )
                except Exception as e:
                    logging.error(f"absences_add penalty: {e}")

        return RedirectResponse(
            url=f"/absences?year={year}&month={month}&msg=added",
            status_code=302
        )
    except Exception as exc:
        logging.error(f"absences_add error: {exc}")
        return RedirectResponse(
            url=f"/absences?year={year}&month={month}&msg=error",
            status_code=302
        )


@router.post("/absences/update")
def absences_update(
    request: Request,
    csrf_token: Annotated[str, Form()] = "",
    absence_id: Annotated[int, Form()] = 0,
    action: Annotated[str, Form()] = "",
    admin_comment: Annotated[str, Form()] = "",
    year: Annotated[int, Form()] = 0,
    month: Annotated[int, Form()] = 0,
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/absences?msg=csrf_error", status_code=302)

    is_admin = user.get("role") in ("owner", "admin", "super_admin")
    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    today = date.today()
    if not year:  year = today.year
    if not month: month = today.month

    if not is_admin and action != "cancel":
        return RedirectResponse(url="/absences?msg=no_access", status_code=302)

    try:
        db = get_web_db(telegram_id, org_db)
        rec = db.get_absence_by_id(absence_id)
        if not rec:
            return RedirectResponse(
                url=f"/absences?year={year}&month={month}&msg=not_found",
                status_code=302
            )
        (ab_id, uid, atype, sd, ed, old_status, is_paid,
         comment, old_admin_comment, created_by, reviewed_by, created_at, _) = rec

        # Проверка прав для cancel (сотрудник может отменить только свои pending)
        if action == "cancel" and not is_admin:
            conn = db.get_connection()
            row = conn.execute(
                "SELECT id FROM users WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
            conn.close()
            if not row or row[0] != uid or old_status != "pending":
                return RedirectResponse(
                    url=f"/absences?year={year}&month={month}&msg=no_access",
                    status_code=302
                )
        status_map = {"approve": "approved", "reject": "rejected", "cancel": "cancelled"}
        new_status = status_map.get(action, "cancelled")
        conn2 = db.get_connection()
        try:
            reviewer_uid = conn2.execute(
                "SELECT id FROM users WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
            reviewer_id = reviewer_uid[0] if reviewer_uid else None
        finally:
            conn2.close()
        db.update_absence_status(absence_id, new_status,
                                  admin_comment or None, reviewer_id)

        # Реверс штрафа при отмене/отклонении ранее одобренного прогула
        if new_status in ("cancelled", "rejected") and old_status == "approved" and atype == "absence":
            try:
                db.delete_absence_penalty(absence_id)
            except Exception as e:
                logging.error(f"absences_update penalty reversal: {e}")

        # Штраф при одобрении прогула (apply_absence_penalty сам удаляет дубли)
        if new_status == "approved" and atype == "absence":
            settings = db.get_absence_type_settings()
            s = settings.get('absence', {})
            pmode = s.get('penalty_mode', 'no_pay')
            pamt = s.get('penalty_amount', 0.0)
            if pmode in ('fine', 'both') and pamt:
                try:
                    d_start = date.fromisoformat(sd[:10])
                    d_end = date.fromisoformat(ed[:10])
                    days = (d_end - d_start).days + 1
                    if reviewer_id:
                        db.apply_absence_penalty(
                            uid, absence_id,
                            d_start.year, d_start.month,
                            pamt * days, reviewer_id
                        )
                except Exception as e:
                    logging.error(f"absences_update penalty: {e}")

        # Уведомить сотрудника в Telegram при одобрении / отклонении
        if new_status in ("approved", "rejected") and action != "cancel":
            try:
                conn3 = db.get_connection()
                tg_row = conn3.execute(
                    "SELECT telegram_id FROM users WHERE id=?", (uid,)
                ).fetchone()
                conn3.close()
                employee_tg_id = tg_row[0] if tg_row else None
                if employee_tg_id:
                    abs_days = (
                        date.fromisoformat(ed[:10]) - date.fromisoformat(sd[:10])
                    ).days + 1
                    _send_tg_absence_notify(
                        employee_tg_id, new_status, atype, sd, ed,
                        admin_comment or None, abs_days,
                    )
            except Exception as e:
                logging.error(f"absences_update notify: {e}")

        return RedirectResponse(
            url=f"/absences?year={year}&month={month}&msg={new_status}",
            status_code=302
        )
    except Exception as exc:
        logging.error(f"absences_update error: {exc}")
        return RedirectResponse(
            url=f"/absences?year={year}&month={month}&msg=error",
            status_code=302
        )


@router.get("/absences/settings")
def absences_settings_page(request: Request):
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/absences", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    ctx: dict = {
        "request": request, "user": user,
        "csrf_token": get_csrf_token(request),
        "settings": {}, "type_labels": TYPE_LABELS,
        "penalty_labels": PENALTY_LABELS, "error": None,
    }
    try:
        db = get_web_db(telegram_id, org_db)
        ctx["settings"] = db.get_absence_type_settings()
    except Exception as exc:
        logging.error(f"absences_settings_page error: {exc}")
        ctx["error"] = "Произошла внутренняя ошибка. Попробуйте позже."

    return request.app.state.templates.TemplateResponse(
        request, "absences/settings.html", ctx
    )


@router.post("/absences/settings/update")
def absences_settings_update(
    request: Request,
    csrf_token: Annotated[str, Form()] = "",
    atype: Annotated[str, Form()] = "",
    is_paid: Annotated[int, Form()] = 1,
    annual_limit: Annotated[int, Form()] = 0,
    penalty_mode: Annotated[str, Form()] = "none",
    penalty_amount: Annotated[float, Form()] = 0.0,
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/absences/settings?msg=csrf", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/absences", status_code=302)

    try:
        db = get_web_db(int(user["sub"]), user.get("org_db"))
        db.set_absence_type_setting(atype, is_paid, annual_limit,
                                     penalty_mode, penalty_amount)
    except Exception as exc:
        logging.error(f"absences_settings_update error: {exc}")

    return RedirectResponse(url="/absences/settings?msg=saved", status_code=302)
