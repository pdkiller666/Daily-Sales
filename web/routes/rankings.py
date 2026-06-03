import io
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, StreamingResponse

router = APIRouter()

MEDALS = ["🥇", "🥈", "🥉"]


def _get_own_db_uid(db, telegram_id: int):
    """Return internal users.id for this telegram_id, or None."""
    try:
        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def _period_dates(period: str, today):
    from datetime import timedelta
    if period == "week":
        return (today - timedelta(days=6)).isoformat(), today.isoformat()
    if period == "prev_month":
        ms = today.replace(day=1)
        end = ms - timedelta(days=1)
        return end.replace(day=1).isoformat(), end.isoformat()
    if period == "all":
        return None, None
    # month
    return today.replace(day=1).isoformat(), today.isoformat()


@router.get("/rankings")
def rankings_page(
    request: Request,
    tab: str = "sellers",
    period: str = "month",
    date_from: str = "",
    date_to: str = "",
):
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    if date_from and date_to:
        period = "custom"

    ctx: dict = {
        "request": request, "user": user,
        "is_admin": user.get("role") in ("owner", "admin", "super_admin"),
        "tab": tab, "period": period,
        "ranking": [], "medals": MEDALS, "error": None,
        "date_from": date_from, "date_to": date_to,
        "own_rank": None,
    }

    try:
        db = get_web_db(telegram_id, org_db)

        from timezone_utils import get_current_user_time
        tz = db.get_user_timezone(telegram_id)
        today = get_current_user_time(tz).date()

        if date_from and date_to:
            df, dt = date_from, date_to
        else:
            df, dt = _period_dates(period, today)
        ctx["date_from"] = df or ""
        ctx["date_to"] = dt or ""

        kwargs: dict = {}
        if df:
            kwargs["start_date"] = df
        if dt:
            kwargs["end_date"] = dt

        if tab == "shops":
            raw = db.get_shop_ranking(**kwargs) or []
            # shop_name[0] total_sold[1] total_revenue[2] active_sellers[3] total_sales[4]
            max_rev = float(raw[0][2]) if raw else 1.0
            ranking = []
            for i, r in enumerate(raw):
                rev = float(r[2] or 0)
                ranking.append({
                    "pos": i + 1, "medal": MEDALS[i] if i < 3 else "",
                    "label": r[0] or "—", "sub": f"{r[3]} продавцов",
                    "qty": int(r[1] or 0), "revenue": rev,
                    "count": int(r[4] or 0), "earnings": float(r[5] or 0),
                    "pct": round(rev / max_rev * 100) if max_rev else 0,
                })
            ctx["ranking"] = ranking

        elif tab == "cities":
            raw = db.get_city_ranking(**kwargs) or []
            # city[0] total_sold[1] total_revenue[2] active_sellers[3] total_sales[4] total_earnings[5]
            max_rev = float(raw[0][2]) if raw else 1.0
            ranking = []
            for i, r in enumerate(raw):
                if not r[0]:
                    continue
                rev = float(r[2] or 0)
                ranking.append({
                    "pos": i + 1, "medal": MEDALS[i] if i < 3 else "",
                    "label": r[0], "sub": f"{r[3]} продавцов",
                    "qty": int(r[1] or 0), "revenue": rev,
                    "count": int(r[4] or 0), "earnings": float(r[5] or 0),
                    "pct": round(rev / max_rev * 100) if max_rev else 0,
                })
            ctx["ranking"] = ranking

        else:  # sellers
            raw = db.get_sales_ranking(**kwargs) or []
            # first_name[0] last_name[1] shop_name[2] total_sold[3] total_revenue[4]
            # total_sales[5] total_earnings[6] user_db_id[7] username[8]
            max_rev = float(raw[0][4]) if raw else 1.0
            ranking = []
            own_uid = _get_own_db_uid(db, telegram_id)
            for i, r in enumerate(raw):
                fname = (r[0] or "").strip()
                lname = (r[1] or "").strip()
                name = f"{fname} {lname}".strip() or (f"@{r[8]}" if r[8] else "—")
                rev = float(r[4] or 0)
                is_me = (own_uid is not None and r[7] == own_uid)
                entry = {
                    "pos": i + 1, "medal": MEDALS[i] if i < 3 else "",
                    "label": name, "sub": r[2] or "—",
                    "username": r[8] or "",
                    "user_db_id": r[7],
                    "qty": int(r[3] or 0), "revenue": rev,
                    "count": int(r[5] or 0), "earnings": float(r[6] or 0),
                    "pct": round(rev / max_rev * 100) if max_rev else 0,
                    "is_me": is_me,
                }
                ranking.append(entry)
                if is_me:
                    ctx["own_rank"] = entry
            ctx["ranking"] = ranking

    except Exception as exc:
        ctx["error"] = "Произошла внутренняя ошибка. Попробуйте позже."

    return request.app.state.templates.TemplateResponse(
        request, "rankings/index.html", ctx
    )


@router.get("/rankings/export.xlsx")
def rankings_export_xlsx(request: Request, tab: str = "sellers", period: str = "month", date_from: str = "", date_to: str = ""):
    """Export current ranking tab to Excel."""
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
        from timezone_utils import get_current_user_time

        telegram_id = int(user["sub"])
        org_db = user.get("org_db")
        db = get_web_db(telegram_id, org_db)

        tz = db.get_user_timezone(telegram_id)
        today = get_current_user_time(tz).date()
        if date_from and date_to:
            df, dt = date_from, date_to
        else:
            df, dt = _period_dates(period, today)
        kwargs: dict = {}
        if df:
            kwargs["start_date"] = df
        if dt:
            kwargs["end_date"] = dt

        thin = Side(style="thin", color="D1D5DB")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)
        hdr_fill = PatternFill("solid", fgColor="1E3A5F")
        hdr_font = Font(bold=True, color="FFFFFF", size=11)
        even_fill = PatternFill("solid", fgColor="F0F4FF")

        wb = openpyxl.Workbook()
        ws = wb.active

        if tab == "shops":
            raw = db.get_shop_ranking(**kwargs) or []
            ws.title = "Рейтинг магазинов"
            headers = ["#", "Магазин", "Продавцов", "Продано (шт.)", "Транзакций", "Выручка (₽)"]
            col_widths = [5, 28, 12, 14, 14, 18]
            rows = [(i + 1, r[0] or "—", int(r[3] or 0), int(r[1] or 0), int(r[4] or 0), float(r[2] or 0))
                    for i, r in enumerate(raw)]
        elif tab == "cities":
            raw = db.get_city_ranking(**kwargs) or []
            ws.title = "Рейтинг городов"
            headers = ["#", "Город", "Продавцов", "Продано (шт.)", "Транзакций", "Выручка (₽)"]
            col_widths = [5, 24, 12, 14, 14, 18]
            rows = [(i + 1, r[0] or "—", int(r[3] or 0), int(r[1] or 0), int(r[4] or 0), float(r[2] or 0))
                    for i, r in enumerate(raw) if r[0]]
        else:
            raw = db.get_sales_ranking(**kwargs) or []
            ws.title = "Рейтинг продавцов"
            headers = ["#", "Продавец", "Магазин", "Продано (шт.)", "Транзакций", "Выручка (₽)", "З/П (₽)"]
            col_widths = [5, 28, 22, 14, 14, 18, 14]
            rows = []
            for i, r in enumerate(raw):
                fname = (r[0] or "").strip()
                lname = (r[1] or "").strip()
                name = f"{fname} {lname}".strip() or f"@{r[8]}" if len(r) > 8 and r[8] else f"{fname} {lname}".strip()
                rows.append((i + 1, name, r[2] or "—", int(r[3] or 0), int(r[5] or 0), float(r[4] or 0), float(r[6] or 0)))

        for i, (h, w) in enumerate(zip(headers, col_widths), 1):
            cell = ws.cell(row=1, column=i, value=h)
            cell.font = hdr_font
            cell.fill = hdr_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = border
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.row_dimensions[1].height = 26
        ws.freeze_panes = "A2"

        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        for row_idx, row in enumerate(rows, 2):
            row_fill = even_fill if row_idx % 2 == 0 else None
            for col_idx, val in enumerate(row, 1):
                display_val = val
                if col_idx == 1:
                    display_val = f"{medals.get(val, '')} {val}".strip()
                cell = ws.cell(row=row_idx, column=col_idx, value=display_val)
                cell.border = border
                if row_fill:
                    cell.fill = row_fill
                if col_idx >= len(headers) - (1 if tab == "sellers" else 0):
                    cell.number_format = '#,##0.00'
                    cell.alignment = Alignment(horizontal="right")

        # Period note
        period_labels = {"week": "7 дней", "month": "Месяц", "prev_month": "Прошлый месяц", "all": "Всё время"}
        note_row = len(rows) + 3
        ws.cell(row=note_row, column=1, value=f"Период: {period_labels.get(period, period)}").font = Font(italic=True, color="94A3B8", size=9)

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)

        tab_names = {"sellers": "продавцы", "shops": "магазины", "cities": "города"}
        filename = f"ranking_{tab_names.get(tab, tab)}_{period}.xlsx"
        return StreamingResponse(
            buf,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as exc:
        return RedirectResponse(url=f"/rankings?tab={tab}&period={period}&error={exc}", status_code=302)
