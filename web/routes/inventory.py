import io
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, StreamingResponse

router = APIRouter()


@router.get("/inventory")
def inventory_page(request: Request, shop: str = ""):
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
        "shops": [], "selected_shop": shop,
        "inventory": [], "total_items": 0,
        "out_of_stock": 0, "low_stock": 0, "error": None,
    }

    try:
        db = get_web_db(telegram_id, org_db)

        shops = db.get_inventory_shops() or []
        if not shop or shop not in shops:
            shop = shops[0] if shops else ""

        ctx["shops"] = shops
        ctx["selected_shop"] = shop

        # inv: id[0] product_id[1] shop_name[2] quantity[3] updated_at[4]
        #       updated_by[5] name[6] category[7] price[8] updated_by_name[9]
        raw = db.get_all_inventory(shop_name=shop if shop else None) or []

        # Sort: out of stock first (qty ≤ 0), then by qty asc, then name
        def _sort_key(r):
            qty = int(r[3] or 0)
            return (1 if qty > 0 else 0, qty, (r[6] or "").lower())

        inventory = sorted(raw, key=_sort_key)

        ctx["inventory"] = inventory
        ctx["total_items"] = len(inventory)
        ctx["out_of_stock"] = sum(1 for r in inventory if int(r[3] or 0) <= 0)
        ctx["low_stock"] = sum(1 for r in inventory if 0 < int(r[3] or 0) <= 5)

    except Exception as exc:
        ctx["error"] = str(exc)

    return request.app.state.templates.TemplateResponse(
        request, "inventory/index.html", ctx
    )


@router.get("/inventory/export.xlsx")
def inventory_export_xlsx(request: Request, shop: str = ""):
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

        # get_all_inventory_for_export: shop_name[0] name[1] category[2] quantity[3] last_updated[4]
        if shop:
            raw = db.get_all_inventory(shop_name=shop) or []
            data = [(r[2], r[6], r[7], r[3], (r[4] or "")[:16]) for r in raw]
        else:
            data = db.get_all_inventory_for_export() or []

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Остатки"

        thin = Side(style="thin", color="D1D5DB")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)
        hdr_fill = PatternFill("solid", fgColor="1E3A5F")
        hdr_font = Font(bold=True, color="FFFFFF", size=11)
        even_fill = PatternFill("solid", fgColor="F0F4FF")
        zero_fill = PatternFill("solid", fgColor="FFF1F2")
        low_fill = PatternFill("solid", fgColor="FFFBEB")

        headers = ["Магазин", "Товар", "Категория", "Остаток (шт.)", "Обновлено"]
        col_widths = [22, 32, 20, 14, 18]

        for i, (h, w) in enumerate(zip(headers, col_widths), 1):
            cell = ws.cell(row=1, column=i, value=h)
            cell.font = hdr_font
            cell.fill = hdr_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = border
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.row_dimensions[1].height = 28
        ws.freeze_panes = "A2"

        for row_idx, row in enumerate(data, 2):
            qty = int(row[3] or 0)
            is_zero = qty <= 0
            is_low = 0 < qty <= 5
            row_fill = zero_fill if is_zero else low_fill if is_low else (even_fill if row_idx % 2 == 0 else None)

            for col_idx, val in enumerate(row, 1):
                cell = ws.cell(row=row_idx, column=col_idx, value=val)
                cell.border = border
                if row_fill:
                    cell.fill = row_fill
                if col_idx == 4:
                    cell.alignment = Alignment(horizontal="center")
                    if is_zero:
                        cell.font = Font(bold=True, color="DC2626")
                    elif is_low:
                        cell.font = Font(bold=True, color="D97706")

        # Totals
        tr = len(data) + 2
        ws.cell(row=tr, column=1, value="ИТОГО позиций").font = Font(bold=True)
        ws.cell(row=tr, column=4, value=sum(int(r[3] or 0) for r in data)).font = Font(bold=True)
        for col in range(1, 6):
            ws.cell(row=tr, column=col).border = border
            ws.cell(row=tr, column=col).fill = PatternFill("solid", fgColor="DBEAFE")

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)

        filename = f"inventory{'_' + shop if shop else '_all'}.xlsx"
        return StreamingResponse(
            buf,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    except Exception as exc:
        return RedirectResponse(url=f"/inventory?error={exc}", status_code=302)
