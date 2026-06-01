import io
from datetime import date
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, StreamingResponse

router = APIRouter()
PAGE_SIZE = 50


def _summary_empty():
    return (0, 0, 0, 0)


@router.get("/sales")
def sales_page(
    request: Request,
    date_from: str = "",
    date_to: str = "",
    shop: str = "",
    seller_id: int = 0,
    page: int = 1,
):
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    ctx: dict = {
        "request": request, "user": user,
        "is_admin": user.get("role") in ("owner", "admin", "super_admin"),
        "sales": [], "shops": [], "sellers": [],
        "date_from": date_from, "date_to": date_to, "selected_shop": shop,
        "selected_seller_id": seller_id,
        "page": 1, "total_pages": 1, "total_count": 0,
        "summary": _summary_empty(), "error": None,
    }

    try:
        db = get_web_db(telegram_id, org_db)

        from timezone_utils import get_current_user_time
        tz = db.get_user_timezone(telegram_id)
        today = get_current_user_time(tz).date()
        month_start = today.replace(day=1)

        if not date_from:
            date_from = month_start.isoformat()
        if not date_to:
            date_to = today.isoformat()
        ctx["date_from"] = date_from
        ctx["date_to"] = date_to

        ctx["shops"] = db.get_all_shops() or []

        # Build sellers list for dropdown
        try:
            all_users = db.get_all_users() or []
            # users: id[0] telegram_id[1] first_name[2] last_name[3] ... shop_name[8]
            sellers = [
                {"id": u[0], "name": f"{u[2] or ''} {u[3] or ''}".strip()}
                for u in all_users if u[8] not in ("Системный", "System", None)
            ]
            ctx["sellers"] = sorted(sellers, key=lambda x: x["name"].lower())
        except Exception:
            pass

        kwargs: dict = {"start_date": date_from, "end_date": date_to}
        if shop:
            kwargs["shop_name"] = shop

        all_sales = db.get_sales_report(**kwargs) or []

        # Filter by seller if requested
        if seller_id:
            all_sales = [s for s in all_sales if s[5] == seller_id]

        total = len(all_sales)
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(1, min(page, total_pages))
        start = (page - 1) * PAGE_SIZE

        ctx["sales"] = all_sales[start : start + PAGE_SIZE]
        ctx["total_count"] = total
        ctx["total_pages"] = total_pages
        ctx["page"] = page
        ctx["summary"] = db.get_sales_summary(
            start_date=date_from, end_date=date_to,
            shop_name=shop if shop else None
        ) or _summary_empty()

    except Exception as exc:
        ctx["error"] = str(exc)

    return request.app.state.templates.TemplateResponse(
        request, "sales/index.html", ctx
    )


@router.get("/sales/export.xlsx")
def sales_export_xlsx(
    request: Request,
    date_from: str = "",
    date_to: str = "",
    shop: str = "",
):
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter

        db = get_web_db(telegram_id, org_db)

        today = date.today()
        if not date_from:
            date_from = today.replace(day=1).isoformat()
        if not date_to:
            date_to = today.isoformat()

        kwargs: dict = {"start_date": date_from, "end_date": date_to}
        if shop:
            kwargs["shop_name"] = shop

        # id[0] pid[1] shop[2] qty[3] price[4] uid[5] date[6]
        # product_name[7] category[8] first_name[9] last_name[10]
        sales = db.get_sales_report(**kwargs) or []

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Продажи"

        thin = Side(style="thin", color="D1D5DB")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)
        hdr_fill = PatternFill("solid", fgColor="1E40AF")
        hdr_font = Font(bold=True, color="FFFFFF", size=11)
        even_fill = PatternFill("solid", fgColor="F0F4FF")
        tot_fill = PatternFill("solid", fgColor="DBEAFE")

        headers = ["Дата", "Товар", "Категория", "Магазин", "Кол-во", "Цена", "Сумма", "Продавец"]
        col_widths = [14, 30, 18, 20, 8, 12, 14, 22]

        for i, (h, w) in enumerate(zip(headers, col_widths), 1):
            cell = ws.cell(row=1, column=i, value=h)
            cell.font = hdr_font
            cell.fill = hdr_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = border
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.row_dimensions[1].height = 28
        ws.freeze_panes = "A2"

        total_amount = 0.0
        total_qty = 0
        for row_idx, s in enumerate(sales, 2):
            qty = int(s[3] or 0)
            price = float(s[4] or 0)
            amount = qty * price
            total_amount += amount
            total_qty += qty
            seller = f"{s[9] or ''} {s[10] or ''}".strip()
            row_fill = even_fill if row_idx % 2 == 0 else None

            for col_idx, val in enumerate(
                [(s[6] or "")[:10], s[7] or "", s[8] or "", s[2] or "",
                 qty, price, amount, seller], 1
            ):
                cell = ws.cell(row=row_idx, column=col_idx, value=val)
                cell.border = border
                if row_fill:
                    cell.fill = row_fill
                if col_idx in (6, 7):
                    cell.number_format = '#,##0.00 ₽'
                    cell.alignment = Alignment(horizontal="right")
                elif col_idx == 5:
                    cell.alignment = Alignment(horizontal="center")

        # Totals row
        if sales:
            tr = len(sales) + 2
            for col in range(1, 9):
                ws.cell(row=tr, column=col).border = border
                ws.cell(row=tr, column=col).fill = tot_fill
            ws.cell(row=tr, column=1, value="ИТОГО").font = Font(bold=True)
            ws.cell(row=tr, column=5, value=total_qty).font = Font(bold=True)
            total_cell = ws.cell(row=tr, column=7, value=total_amount)
            total_cell.font = Font(bold=True)
            total_cell.number_format = '#,##0.00 ₽'
            total_cell.alignment = Alignment(horizontal="right")

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)

        filename = f"sales_{date_from}__{date_to}.xlsx"
        return StreamingResponse(
            buf,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    except Exception as exc:
        return RedirectResponse(url=f"/sales?error={exc}", status_code=302)
