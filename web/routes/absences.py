"""
Web-маршруты модуля учёта отсутствий.
GET  /absences            — список + календарь (admin видит всех, user — только себя)
POST /absences/add        — добавить / подать заявку
POST /absences/update     — одобрить / отклонить / отменить
GET  /absences/settings   — настройки типов (admin)
POST /absences/settings/update — сохранить настройку типа
"""
import html as _html
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

_RU_MONTHS_SHORT = {
    1: "янв", 2: "фев", 3: "мар", 4: "апр", 5: "май", 6: "июн",
    7: "июл", 8: "авг", 9: "сен", 10: "окт", 11: "ноя", 12: "дек",
}
_TYPE_LABELS_PLAIN = {
    "vacation": "Отпуск",
    "sick": "Больничный",
    "compensatory": "Отгул",
    "absence": "Прогул",
    "other": "Другое",
}


def _send_heavy_absence_alert(db, sd: str, ed: str) -> None:
    """Fire-and-forget: проверить дни [sd, ed] и оповестить всех админов если ≥ порог.

    Дни с флагом heavy_absence_muted_YYYY-MM-DD в org_config пропускаются.
    Каждый тяжёлый день отправляется отдельным сообщением с кнопкой «Не напоминать».
    """
    import threading
    def _run():
        try:
            threshold_raw = db.get_org_config('heavy_absence_threshold', '3')
            try:
                threshold = max(1, int(threshold_raw))
            except (ValueError, TypeError):
                threshold = 3

            try:
                d_start = date.fromisoformat(sd[:10])
                d_end   = date.fromisoformat(ed[:10])
            except Exception:
                return

            heavy_days: list[str] = []  # ISO YYYY-MM-DD, не заглушённые
            cur = d_start
            while cur <= d_end:
                ds = cur.strftime('%Y-%m-%d')
                if not db.get_org_config(f'heavy_absence_muted_{ds}', ''):
                    cnt = db.count_approved_absences_on_day(ds)
                    if cnt >= threshold:
                        heavy_days.append(ds)
                cur += timedelta(days=1)

            if not heavy_days:
                return

            token = os.environ.get("BOT_TOKEN", "")
            if not token:
                return
            try:
                admin_ids = db.get_all_admins_telegram_ids()
            except Exception:
                admin_ids = []

            url = f"https://api.telegram.org/bot{token}/sendMessage"
            for day_iso in heavy_days:
                friendly = date.fromisoformat(day_iso).strftime('%d.%m.%Y')
                text = (
                    f'⚠️ <b>Много отсутствующих!</b>\n\n'
                    f'📅 {friendly} — отсутствует {threshold}+ сотрудников.\n\n'
                    f'Проверьте расписание, чтобы не остаться без команды.'
                )
                reply_markup = {
                    "inline_keyboard": [
                        [{"text": "🔕 Понятно, не напоминать",
                          "callback_data": f"abs_mute_{day_iso}"}],
                        [{"text": "✅ Прочитано", "callback_data": "notif_read"}],
                    ]
                }
                payload_base = {"text": text, "parse_mode": "HTML",
                                "reply_markup": reply_markup}
                for adm_tg_id in admin_ids:
                    if not adm_tg_id:
                        continue
                    try:
                        payload = json.dumps({**payload_base,
                                              "chat_id": adm_tg_id}).encode("utf-8")
                        req = urllib.request.Request(
                            url, data=payload,
                            headers={"Content-Type": "application/json"},
                        )
                        urllib.request.urlopen(req, timeout=10)
                    except Exception:
                        pass
        except Exception as _e:
            logging.error("heavy_absence_alert web: %s", _e)
    threading.Thread(target=_run, daemon=True).start()


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
            text += f'\nПричина: {_html.escape(str(admin_comment))}'
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


def _build_absence_tooltip_map(absences_list, year: int, month: int) -> tuple:
    """Build ({day_num: tooltip_text}, {day_num: count}) from the already-prepared absences list.

    Format: "Имя — Тип: ДД–ДД мес" (admin all-users) or "Тип: ДД–ДД мес" (per-user).
    Pending absences get " (ожидание)" appended.
    All absences per day are accumulated (newline-separated) — no first-wins truncation.
    count_map[day] gives the number of distinct absence entries covering that day.
    """
    _ABBR = {1: "янв", 2: "фев", 3: "мар", 4: "апр", 5: "май", 6: "июн",
             7: "июл", 8: "авг", 9: "сен", 10: "окт", 11: "ноя", 12: "дек"}
    last_day = _cal.monthrange(year, month)[1]
    month_start = date(year, month, 1)
    month_end = date(year, month, last_day)
    lines_by_day: dict = {}
    for a in (absences_list or []):
        try:
            sd = date.fromisoformat(str(a["start_date"])[:10])
            ed = date.fromisoformat(str(a["end_date"])[:10])
        except Exception:
            continue
        label = (a.get("type_label") or a.get("type", "")).lstrip("⚪🔵🟡🔴⬜ ")
        name = (a.get("name") or "").strip()
        if sd.month == ed.month:
            date_range = f"{sd.day}–{ed.day} {_ABBR[sd.month]}"
        else:
            date_range = f"{sd.day} {_ABBR[sd.month]} – {ed.day} {_ABBR[ed.month]}"
        tip = f"{name} — {label}: {date_range}" if name else f"{label}: {date_range}"
        if a.get("status") == "pending":
            tip += " (ожидание)"
        cur = max(sd, month_start)
        end = min(ed, month_end)
        while cur <= end:
            day_lines = lines_by_day.setdefault(cur.day, [])
            if tip not in day_lines:
                day_lines.append(tip)
            cur += timedelta(days=1)
    tooltip_map = {d: "\n".join(ls) for d, ls in lines_by_day.items()}
    count_map = {d: len(ls) for d, ls in lines_by_day.items()}
    return tooltip_map, count_map


def _approved_distinct_per_day(absences_list, year: int, month: int) -> dict:
    """Return {day_num: distinct_approved_user_count} for the month.

    Only 'approved' absences are counted; each user_id is counted at most once
    per day — matching the semantics of count_approved_absences_on_day() in DB.
    """
    import calendar as _c
    last_day = _c.monthrange(year, month)[1]
    month_start = date(year, month, 1)
    month_end = date(year, month, last_day)
    users_by_day: dict = {}
    for a in (absences_list or []):
        if a.get("status") != "approved":
            continue
        uid = a.get("user_id")
        try:
            sd = date.fromisoformat(str(a["start_date"])[:10])
            ed = date.fromisoformat(str(a["end_date"])[:10])
        except Exception:
            continue
        cur = max(sd, month_start)
        end = min(ed, month_end)
        while cur <= end:
            users_by_day.setdefault(cur.day, set()).add(uid)
            cur += timedelta(days=1)
    return {d: len(uids) for d, uids in users_by_day.items()}


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
                  user_id: int = 0, msg: str = "",
                  pending_id: int = 0, overlap_id: int = 0):
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    telegram_id = int(user["sub"])
    from billing_utils import has_module
    if not has_module(telegram_id, "team"):
        return RedirectResponse(url="/dashboard?msg=module_team_required", status_code=302)
    org_db = user.get("org_db")
    is_admin = user.get("role") in ("owner", "admin", "super_admin")
    today = date.today()
    if not year:  year = today.year
    if not month: month = today.month
    year  = max(2015, min(year,  2040))
    month = max(1,    min(month, 12))

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
        "absences": [], "absence_map": {}, "absent_today": {},
        "absence_count_map": {},
        "is_current_month": (year == today.year and month == today.month),
        "cal_grid": _build_cal_grid(year, month),
        "absence_tooltip_map": {},
        "weekday_names": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"],
        "type_labels": TYPE_LABELS, "status_labels": STATUS_LABELS,
        "type_css": TYPE_CSS,
        "csrf_token": get_csrf_token(request),
        "msg": msg, "error": None,
        "merge_pending_id": pending_id,
        "merge_overlap_id": overlap_id,
    }

    try:
        db = get_web_db(telegram_id, org_db)

        # Validate that the two records to merge actually overlap before
        # showing the merge button.  If they don't, hide the button so the
        # user never gets a silent failure.
        if pending_id and overlap_id:
            _p = db.get_absence_by_id(pending_id)
            _o = db.get_absence_by_id(overlap_id)
            _overlap_ok = (
                _p and _o
                and _p[1] == _o[1]           # same user_id (col 1)
                and _p[2] == _o[2]           # same type    (col 2)
                and not (_p[3] > _o[4] or _o[3] > _p[4])  # date ranges touch/overlap
            )
            if not _overlap_ok:
                pending_id = 0
                overlap_id = 0
                ctx["merge_pending_id"] = 0
                ctx["merge_overlap_id"] = 0
                if not ctx.get("msg"):
                    ctx["msg"] = "no_overlap"

        if is_admin:
            # Список сотрудников для выбора
            conn = db.get_connection()
            try:
                staff = conn.execute(
                    "SELECT id, first_name, last_name, shop_name, telegram_id "
                    "FROM users ORDER BY first_name"
                ).fetchall() or []
            finally:
                conn.close()

            is_current_month = (year == today.year and month == today.month)
            # Все отсутствия за месяц — нужны ДО построения sidebar
            rows = db.get_all_absences_admin(year, month)

            if is_current_month:
                try:
                    absent_viewed = db.get_absent_users_today(today.isoformat())
                except Exception:
                    absent_viewed = {}
            else:
                # Для прошлых/будущих месяцев берём одобренные отсутствия месяца
                absent_viewed = {}
                for _r in rows:
                    _uid, _atype, _status = _r[1], _r[2], _r[5]
                    if _status == "approved" and _uid not in absent_viewed:
                        absent_viewed[_uid] = _atype

            ctx["absent_today"] = absent_viewed
            ctx["is_current_month"] = is_current_month
            ctx["staff_list"] = [
                {"id": r[0], "first_name": r[1] or "", "last_name": r[2] or "",
                 "shop_name": r[3] or "", "telegram_id": r[4],
                 "absence_type": absent_viewed.get(r[0], "")}
                for r in staff
            ]
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
                    "absent_today_type": absent_viewed.get(uid, ""),
                })
            ctx["absences"] = absences
            _abs_raw = db.get_absence_days_map(year, month, user_id or None)
            if user_id:
                ctx["absence_map"] = _abs_raw.get(user_id, {})
            else:
                # admin без фильтра: merge всех пользователей (для красивого
                # вида — используем только первого встреченного на каждый день)
                _staff_names = {s["id"]: s["first_name"] for s in ctx["staff_list"]}
                merged: dict = {}
                for _uid_key, _day_map in _abs_raw.items():
                    for _day, _info in _day_map.items():
                        if _day not in merged:
                            merged[_day] = {
                                **_info,
                                "user_id": _uid_key,
                                "user_name": _staff_names.get(_uid_key, ""),
                            }
                ctx["absence_map"] = merged
        else:
            # Сотрудник видит только свои записи
            conn = db.get_connection()
            try:
                row = conn.execute(
                    "SELECT id FROM users WHERE telegram_id=?", (telegram_id,)
                ).fetchone()
            finally:
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

        # Build tap/hover tooltip map from whichever absences branch was taken
        try:
            ctx["absence_tooltip_map"], ctx["absence_count_map"] = _build_absence_tooltip_map(
                ctx["absences"], year, month
            )
        except Exception:
            pass

        # Heavy-absence highlighting — only meaningful for admin view.
        # Uses approved-only, distinct-user counts to match DB alert semantics.
        if is_admin:
            try:
                threshold = max(1, int(db.get_org_config('heavy_absence_threshold', '3') or 3))
            except (ValueError, TypeError):
                threshold = 3
            ctx["heavy_threshold"] = threshold
            try:
                _approved_counts = _approved_distinct_per_day(ctx["absences"], year, month)
                ctx["heavy_days"] = {d for d, cnt in _approved_counts.items() if cnt >= threshold}
            except Exception:
                ctx["heavy_days"] = set()

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
    year  = max(2015, min(year,  2040))
    month = max(1,    min(month, 12))

    import re as _re
    if not _re.match(r'^[a-z_]{1,50}$', atype):
        return RedirectResponse(
            url=f"/absences?year={year}&month={month}&msg=invalid_type",
            status_code=302,
        )

    try:
        db = get_web_db(telegram_id, org_db)
        # Определить target user_id в org db
        if is_admin and user_id:
            target_uid = user_id
            final_status = status  # admin может сразу одобрить
        else:
            conn = db.get_connection()
            try:
                row = conn.execute(
                    "SELECT id FROM users WHERE telegram_id=?", (telegram_id,)
                ).fetchone()
            finally:
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

        # Проверить корректность дат
        try:
            sd_check = date.fromisoformat(start_date[:10])
            ed_check = date.fromisoformat(end_date[:10])
            if ed_check < sd_check:
                return RedirectResponse(
                    url=f"/absences?year={year}&month={month}&msg=invalid_dates",
                    status_code=302
                )
        except ValueError:
            return RedirectResponse(
                url=f"/absences?year={year}&month={month}&msg=invalid_dates",
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
            (comment or "").strip()[:500] or None, target_uid, final_status
        )

        # Оповестить админов если admin добавил approved absence с нагрузкой на смену
        if is_admin and final_status == 'approved':
            try:
                _send_heavy_absence_alert(db, start_date, end_date)
            except Exception as _hae:
                logging.error(f"absences_add heavy_alert: {_hae}")

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
                    try:
                        adm_row = conn_adm.execute(
                            "SELECT id FROM users WHERE telegram_id=?", (telegram_id,)
                        ).fetchone()
                    finally:
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
            try:
                row = conn.execute(
                    "SELECT id FROM users WHERE telegram_id=?", (telegram_id,)
                ).fetchone()
            finally:
                conn.close()
            if not row or row[0] != uid or old_status != "pending":
                return RedirectResponse(
                    url=f"/absences?year={year}&month={month}&msg=no_access",
                    status_code=302
                )
        status_map = {"approve": "approved", "reject": "rejected", "cancel": "cancelled"}
        new_status = status_map.get(action, "cancelled")

        # Защита от дублирующего одобрения: проверить пересечения с уже одобренными
        if new_status == "approved":
            overlaps = db.get_overlapping_approved_absences(uid, atype, sd, ed,
                                                            exclude_id=absence_id)
            if overlaps:
                ov_id = overlaps[0][0]
                return RedirectResponse(
                    url=(f"/absences?year={year}&month={month}"
                         f"&msg=duplicate_approved"
                         f"&pending_id={absence_id}&overlap_id={ov_id}"),
                    status_code=302
                )

        conn2 = db.get_connection()
        try:
            reviewer_uid = conn2.execute(
                "SELECT id FROM users WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
            reviewer_id = reviewer_uid[0] if reviewer_uid else None
        finally:
            conn2.close()
        db.update_absence_status(absence_id, new_status,
                                  (admin_comment or "").strip()[:500] or None, reviewer_id)

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
                try:
                    tg_row = conn3.execute(
                        "SELECT telegram_id FROM users WHERE id=?", (uid,)
                    ).fetchone()
                finally:
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

        # Оповестить админов если дни стали «тяжёлыми» (много отсутствий)
        if new_status == "approved":
            try:
                _send_heavy_absence_alert(db, sd, ed)
            except Exception as _hae:
                logging.error(f"absences_update heavy_alert: {_hae}")

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


@router.post("/absences/merge")
def absences_merge(
    request: Request,
    csrf_token: Annotated[str, Form()] = "",
    keep_id: Annotated[int, Form()] = 0,
    drop_id: Annotated[int, Form()] = 0,
    year: Annotated[int, Form()] = 0,
    month: Annotated[int, Form()] = 0,
):
    """Слить перекрывающиеся отсутствия: расширить keep_id, отменить drop_id."""
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/absences?msg=csrf_error", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/absences?msg=no_access", status_code=302)

    today = date.today()
    if not year:  year = today.year
    if not month: month = today.month

    try:
        telegram_id = int(user["sub"])
        org_db = user.get("org_db")
        db = get_web_db(telegram_id, org_db)

        keep_rec = db.get_absence_by_id(keep_id)
        drop_rec = db.get_absence_by_id(drop_id)
        if not keep_rec or not drop_rec:
            return RedirectResponse(
                url=f"/absences?year={year}&month={month}&msg=not_found",
                status_code=302
            )

        if keep_rec[1] != drop_rec[1]:
            return RedirectResponse(
                url=f"/absences?year={year}&month={month}&msg=no_access",
                status_code=302
            )

        # Explicit overlap check so the user gets a clear message instead of a
        # silent no-op when the two records don't actually touch.
        # keep_rec/drop_rec columns: id[0] user_id[1] type[2] start_date[3] end_date[4]
        if keep_rec[3] > drop_rec[4] or drop_rec[3] > keep_rec[4]:
            return RedirectResponse(
                url=f"/absences?year={year}&month={month}&msg=no_overlap",
                status_code=302
            )

        ok = db.merge_absences(keep_id, drop_id)
        if not ok:
            return RedirectResponse(
                url=f"/absences?year={year}&month={month}&msg=error",
                status_code=302
            )

        # Уведомить сотрудника о слиянии
        try:
            uid = keep_rec[1]
            atype = keep_rec[2]
            new_sd = min(keep_rec[3], drop_rec[3])
            new_ed = max(keep_rec[4], drop_rec[4])
            new_days = _days_count(new_sd, new_ed)
            conn_tg = db.get_connection()
            try:
                tg_row = conn_tg.execute(
                    "SELECT telegram_id FROM users WHERE id=?", (uid,)
                ).fetchone()
            finally:
                conn_tg.close()
            if tg_row and tg_row[0]:
                _send_tg_absence_notify(
                    tg_row[0], "approved", atype, new_sd, new_ed, None, new_days
                )
        except Exception as e:
            logging.error(f"absences_merge notify: {e}")

        return RedirectResponse(
            url=f"/absences?year={year}&month={month}&msg=merged",
            status_code=302
        )
    except Exception as exc:
        logging.error(f"absences_merge error: {exc}")
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
        try:
            ctx["heavy_threshold"] = int(db.get_org_config('heavy_absence_threshold', '3') or 3)
        except (ValueError, TypeError):
            ctx["heavy_threshold"] = 3
    except Exception as exc:
        logging.error(f"absences_settings_page error: {exc}")
        ctx["error"] = "Произошла внутренняя ошибка. Попробуйте позже."
        ctx.setdefault("heavy_threshold", 3)

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


@router.post("/absences/settings/threshold")
def absences_settings_threshold(
    request: Request,
    csrf_token: Annotated[str, Form()] = "",
    heavy_threshold: Annotated[int, Form()] = 3,
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
        threshold = max(1, min(int(heavy_threshold), 100))
        db = get_web_db(int(user["sub"]), user.get("org_db"))
        db.set_org_config('heavy_absence_threshold', str(threshold))
    except Exception as exc:
        logging.error(f"absences_settings_threshold error: {exc}")

    return RedirectResponse(url="/absences/settings?msg=saved", status_code=302)
