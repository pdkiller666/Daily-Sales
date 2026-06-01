import io
from fastapi import APIRouter, Request
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


@router.get("/salary")
def salary_page(
    request: Request,
    year: int = 0,
    month: int = 0,
    user_id: int = 0,
):
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

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
            adj_sum = db.get_salary_adjustments_sum(uid, year, month)
            base = rate * worked
            total = base + adj_sum
            total_fund += total

            staff_salary.append({
                "user_id": uid,
                "first_name": row[1] or "",
                "last_name": row[2] or "",
                "telegram_id": row[4],
                "daily_rate": rate,
                "worked_days": worked,
                "base_salary": base,
                "adj_sum": adj_sum,
                "total": total,
                "shop": "",  # optional: fill from users table
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
