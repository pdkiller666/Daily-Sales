import io
import logging
from typing import Annotated
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, StreamingResponse
from datetime import date

router = APIRouter()

MONTH_NAMES = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
    5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
    9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}


def _adjacent_month(year: int, month: int, delta: int):
    """Return (year, month) shifted by delta months."""
    total = (year - 1) * 12 + (month - 1) + delta
    return (total // 12 + 1, total % 12 + 1)


def _salary_user_earnings(request, user, year: int, month: int):
    """Personal earnings view for user role."""
    from web.auth import get_csrf_token
    from web.deps import get_web_db
    from datetime import date

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    today = date.today()
    if not year:
        year = today.year
    if not month:
        month = today.month

    prev_y, prev_m = _adjacent_month(year, month, -1)
    next_y, next_m = _adjacent_month(year, month, 1)

    ctx: dict = {
        "request": request, "user": user,
        "is_admin": False,
        "year": year, "month": month,
        "month_name": MONTH_NAMES.get(month, str(month)),
        "prev_y": prev_y, "prev_m": prev_m,
        "next_y": next_y, "next_m": next_m,
        "is_future": (year, month) > (today.year, today.month),
        "csrf_token": get_csrf_token(request),
        "earnings": [],
        "total_commission": 0.0,
        "total_base": 0.0,
        "total_adj": 0.0,
        "grand_total": 0.0,
        "worked_days": 0,
        "daily_rate": 0.0,
        "error": None,
    }

    try:
        db = get_web_db(telegram_id, org_db)
        import sqlite3 as _sq
        conn_m = _sq.connect("data/main.db")
        uid_row = conn_m.execute(
            "SELECT org_id FROM user_org_mapping WHERE telegram_id=? AND is_active=1",
            (telegram_id,)
        ).fetchone()
        conn_m.close()

        conn_u = db.get_connection()
        user_row = conn_u.execute(
            "SELECT id FROM users WHERE telegram_id=?", (telegram_id,)
        ).fetchone()
        conn_u.close()

        if not user_row:
            ctx["error"] = "Пользователь не найден в базе"
            return request.app.state.templates.TemplateResponse(
                request, "salary/earnings.html", ctx
            )

        user_db_id = user_row[0]
        start_date = f"{year}-{month:02d}-01"
        if month == 12:
            end_date = f"{year}-12-31"
        else:
            import calendar as _cal
            last_day = _cal.monthrange(year, month)[1]
            end_date = f"{year}-{month:02d}-{last_day}"

        # Earnings from commissions
        raw_earnings = db.get_seller_earnings(user_db_id, start_date, end_date) or []
        # cols: commission_amount[0] motivation_type[1] motivation_value[2]
        #       product_name[3] quantity_sold[4] sale_price[5] sale_date[6] shop_name[7]
        earnings = []
        total_commission = 0.0
        for row in raw_earnings:
            comm = float(row[0] or 0)
            total_commission += comm
            mtype = row[1] or "percentage"
            mval = float(row[2] or 0)
            earnings.append({
                "date": str(row[6] or "")[:10],
                "product": row[3] or "—",
                "qty": int(row[4] or 0),
                "price": float(row[5] or 0),
                "mtype": mtype,
                "mval": mval,
                "rate_display": f"{mval:g}%" if mtype == "percentage" else f"{int(mval):,}".replace(",", "\u00a0") + "\u00a0₽/ед.",
                "commission": comm,
                "shop": row[7] or "—",
            })

        # Base salary from schedule × rate (+ paid approved absences)
        worked = db.get_worked_days_count(user_db_id, year, month)
        paid_abs = db.get_paid_absence_days_count(user_db_id, year, month)
        rate = db.get_salary_rate(user_db_id)
        base_salary = (worked + paid_abs) * rate
        adj_sum = db.get_salary_adjustments_sum(user_db_id, year, month)

        ctx.update({
            "earnings": earnings,
            "total_commission": round(total_commission, 2),
            "total_base": round(base_salary, 2),
            "total_adj": round(adj_sum, 2),
            "grand_total": round(base_salary + total_commission + adj_sum, 2),
            "worked_days": worked,
            "paid_absence_days": paid_abs,
            "daily_rate": rate,
        })

    except Exception as exc:
        import logging
        logging.error(f"_salary_user_earnings error: {exc}")
        ctx["error"] = str(exc)

    return request.app.state.templates.TemplateResponse(
        request, "salary/earnings.html", ctx
    )


@router.get("/salary")
def salary_page(
    request: Request,
    year: int = 0,
    month: int = 0,
    user_id: int = 0,
):
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    # Non-admin users see their personal earnings view
    if user.get("role") == "user":
        return _salary_user_earnings(request, user, year, month)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    today = date.today()
    if not year:
        year = today.year
    if not month:
        month = today.month

    prev_y, prev_m = _adjacent_month(year, month, -1)
    next_y, next_m = _adjacent_month(year, month, 1)
    is_future = (year, month) > (today.year, today.month)

    ctx: dict = {
        "request": request, "user": user,
        "is_admin": user.get("role") in ("owner", "admin", "super_admin"),
        "year": year, "month": month,
        "month_name": MONTH_NAMES.get(month, str(month)),
        "prev_y": prev_y, "prev_m": prev_m,
        "next_y": next_y, "next_m": next_m,
        "is_future": is_future,
        "staff_salary": [], "selected_user_id": user_id,
        "detail_user": None, "work_days_set": set(),
        "adjustments": [], "adj_sum": 0.0,
        "total_salary_fund": 0.0, "error": None,
        "csrf_token": get_csrf_token(request),
    }

    try:
        db = get_web_db(telegram_id, org_db)

        # All employees with their rates
        all_rates = db.get_all_salary_rates() or []
        # (user_id[0], first_name[1], last_name[2], daily_rate[3], telegram_id[4])

        staff_salary = []
        total_fund = 0.0

        for row in all_rates:
            uid = row[0]
            rate = float(row[3] or 0)
            worked = db.get_worked_days_count(uid, year, month)
            paid_abs = db.get_paid_absence_days_count(uid, year, month)
            adj_sum = db.get_salary_adjustments_sum(uid, year, month)
            base = rate * (worked + paid_abs)
            total = base + adj_sum
            total_fund += total

            staff_salary.append({
                "user_id": uid,
                "first_name": row[1] or "",
                "last_name": row[2] or "",
                "telegram_id": row[4],
                "daily_rate": rate,
                "worked_days": worked,
                "paid_absence_days": paid_abs,
                "base_salary": base,
                "adj_sum": adj_sum,
                "total": total,
                "shop": "",
            })

        # Sort: by total desc
        staff_salary.sort(key=lambda x: x["total"], reverse=True)
        ctx["staff_salary"] = staff_salary
        ctx["total_salary_fund"] = total_fund

        # If a specific user is selected, show their calendar + adjustments
        if user_id:
            import calendar as _cal
            work_days = db.get_work_schedule(user_id, year, month)
            adj_rows = db.get_salary_adjustments(user_id, year, month) or []
            adj_sum_val = db.get_salary_adjustments_sum(user_id, year, month)
            rate_row = next((s for s in staff_salary if s["user_id"] == user_id), None)

            # Build calendar grid: list of weeks, each week = list of (day_num | 0)
            first_weekday, days_in_month = _cal.monthrange(year, month)
            # first_weekday: 0=Mon..6=Sun
            cal_grid: list[list[int]] = []
            week: list[int] = [0] * first_weekday
            for d in range(1, days_in_month + 1):
                week.append(d)
                if len(week) == 7:
                    cal_grid.append(week)
                    week = []
            if week:
                week += [0] * (7 - len(week))
                cal_grid.append(week)

            ctx["detail_user"] = rate_row
            ctx["work_days_set"] = {int(d[8:10]) for d in work_days}  # day numbers as ints
            ctx["adjustments"] = adj_rows
            ctx["adj_sum"] = adj_sum_val
            ctx["cal_grid"] = cal_grid

    except Exception as exc:
        ctx["error"] = str(exc)

    return request.app.state.templates.TemplateResponse(
        request, "salary/index.html", ctx
    )


@router.get("/salary/export.xlsx")
def salary_export_xlsx(request: Request, year: int = 0, month: int = 0):
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/salary", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    today = date.today()
    if not year:
        year = today.year
    if not month:
        month = today.month

    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter

        db = get_web_db(telegram_id, org_db)

        all_rates = db.get_all_salary_rates() or []
        # (user_id[0], first_name[1], last_name[2], daily_rate[3], telegram_id[4])

        rows: list = []
        total_fund = 0.0
        for row in all_rates:
            uid = row[0]
            rate = float(row[3] or 0)
            worked = db.get_worked_days_count(uid, year, month)
            adj = db.get_salary_adjustments_sum(uid, year, month)
            base = rate * worked
            total = base + adj
            total_fund += total
            rows.append((
                f"{row[1] or ''} {row[2] or ''}".strip(),
                rate, worked, base, adj, total
            ))

        rows.sort(key=lambda r: -r[5])

        wb = openpyxl.Workbook()
        ws = wb.active
        mn = MONTH_NAMES.get(month, str(month))
        ws.title = f"Зарплата {mn} {year}"

        thin = Side(style="thin", color="D1D5DB")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)
        hdr_fill = PatternFill("solid", fgColor="1E3A5F")
        hdr_font = Font(bold=True, color="FFFFFF", size=11)
        even_fill = PatternFill("solid", fgColor="F0F8FF")
        tot_fill = PatternFill("solid", fgColor="DCFCE7")

        headers = ["Сотрудник", "Ставка/день", "Смен", "Оклад", "Корр.", "Итого"]
        col_widths = [28, 14, 9, 16, 14, 16]

        for i, (h, w) in enumerate(zip(headers, col_widths), 1):
            cell = ws.cell(row=1, column=i, value=h)
            cell.font = hdr_font
            cell.fill = hdr_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = border
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.row_dimensions[1].height = 28
        ws.freeze_panes = "A2"

        for row_idx, r in enumerate(rows, 2):
            row_fill = even_fill if row_idx % 2 == 0 else None
            for col_idx, val in enumerate(r, 1):
                cell = ws.cell(row=row_idx, column=col_idx, value=val)
                cell.border = border
                if row_fill:
                    cell.fill = row_fill
                if col_idx in (2, 4, 5, 6):
                    cell.number_format = '#,##0.00 ₽'
                    cell.alignment = Alignment(horizontal="right")
                elif col_idx == 3:
                    cell.alignment = Alignment(horizontal="center")
                if col_idx == 6:
                    cell.font = Font(bold=True, color="166534")

        # Total row
        tr = len(rows) + 2
        for col in range(1, 7):
            ws.cell(row=tr, column=col).border = border
            ws.cell(row=tr, column=col).fill = tot_fill
        ws.cell(row=tr, column=1, value="ИТОГО").font = Font(bold=True)
        ws.cell(row=tr, column=3, value=sum(r[2] for r in rows)).font = Font(bold=True)
        tot = ws.cell(row=tr, column=6, value=total_fund)
        tot.font = Font(bold=True)
        tot.number_format = '#,##0.00 ₽'
        tot.alignment = Alignment(horizontal="right")

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)

        filename = f"salary_{year}_{month:02d}.xlsx"
        return StreamingResponse(
            buf,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    except Exception as exc:
        return RedirectResponse(url=f"/salary?year={year}&month={month}&error={exc}", status_code=302)


def _get_internal_uid(db, telegram_id: int):
    try:
        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


@router.post("/salary/adjustment/add")
def salary_adj_add(
    request: Request,
    target_user_id: Annotated[int, Form()],
    amount: Annotated[float, Form()],
    year: Annotated[int, Form()],
    month: Annotated[int, Form()],
    comment: str = Form(default=""),
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/salary", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/salary?error=CSRF", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    try:
        db = get_web_db(telegram_id, org_db)
        creator_uid = _get_internal_uid(db, telegram_id)
        db.add_salary_adjustment(
            user_id=target_user_id,
            year=year,
            month=month,
            amount=amount,
            comment=comment or None,
            created_by=creator_uid,
        )
    except Exception as e:
        logging.error(f"salary_adj_add error: {e}")

    return RedirectResponse(
        url=f"/salary?year={year}&month={month}&user_id={target_user_id}",
        status_code=302,
    )


@router.post("/salary/adjustment/{adj_id}/delete")
def salary_adj_delete(
    request: Request,
    adj_id: int,
    year: Annotated[int, Form()],
    month: Annotated[int, Form()],
    target_user_id: Annotated[int, Form()],
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/salary", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/salary?error=CSRF", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    try:
        db = get_web_db(telegram_id, org_db)
        db.delete_salary_adjustment(adj_id)
    except Exception as e:
        logging.error(f"salary_adj_delete error: {e}")

    return RedirectResponse(
        url=f"/salary?year={year}&month={month}&user_id={target_user_id}",
        status_code=302,
    )


@router.post("/salary/rate/set")
def salary_rate_set(
    request: Request,
    csrf_token: str = Form(default=""),
    target_user_id: int = Form(...),
    daily_rate: str = Form(default="0"),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/salary", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/salary?error=CSRF", status_code=302)

    try:
        rate = float(daily_rate.replace(",", ".").strip())
        if rate < 0:
            raise ValueError("Ставка не может быть отрицательной")
    except (ValueError, AttributeError) as exc:
        return RedirectResponse(url=f"/salary?error={exc}", status_code=303)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    try:
        db = get_web_db(telegram_id, org_db)
        db.set_salary_rate(target_user_id, rate, updated_by=target_user_id)
        logging.info(f"Salary rate set: user={target_user_id} rate={rate} by={telegram_id}")
    except Exception as exc:
        logging.error(f"salary_rate_set error: {exc}")
        return RedirectResponse(url=f"/salary?error={exc}", status_code=303)

    return RedirectResponse(url=f"/salary?rate_saved=1&user_id={target_user_id}", status_code=303)
