import io
import logging
from datetime import date
from typing import Annotated
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, StreamingResponse, JSONResponse

router = APIRouter()
PAGE_SIZE = 50


def _summary_empty():
    return (0, 0, 0, 0)


def _get_internal_uid(db, telegram_id: int):
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


def _get_user_allowed_shops(telegram_id: int, db) -> list:
    """Return list of shop names the user is allowed to access.
    Applies scope restrictions; owners/unrestricted users get all shops."""
    from db_utils import get_user_org_scope
    all_shops = db.get_all_shops() or []
    try:
        scope_type, scope_values = get_user_org_scope(telegram_id)
        if not scope_type or scope_type == "all":
            return all_shops
        if scope_type == "shop":
            return [s for s in all_shops if s in scope_values]
        if scope_type in ("city", "network"):
            col = "city" if scope_type == "city" else "trade_network"
            placeholders = ",".join("?" * len(scope_values))
            conn = db.get_connection()
            cur = conn.cursor()
            cur.execute(
                f"SELECT DISTINCT shop_name FROM users WHERE {col} IN ({placeholders}) AND shop_name IS NOT NULL",
                scope_values,
            )
            allowed = {row[0] for row in cur.fetchall()}
            conn.close()
            return [s for s in all_shops if s in allowed]
    except Exception:
        pass
    return all_shops


@router.get("/api/products-for-shop")
def api_products_for_shop(request: Request, shop: str = ""):
    """Return JSON list of products with stock > 0 in the given shop."""
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return JSONResponse({"error": "Unauthorized", "products": []}, status_code=401)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    try:
        db = get_web_db(telegram_id, org_db)
        allowed_shops = _get_user_allowed_shops(telegram_id, db)
        if shop and shop not in allowed_shops:
            return JSONResponse({"error": "Forbidden", "products": []}, status_code=403)
        raw = db.get_all_inventory(shop_name=shop if shop else None) or []
        # id[0] product_id[1] shop_name[2] quantity[3] name[6] category[7] price[8]
        products = sorted(
            [
                {"id": r[1], "name": r[6] or "", "category": r[7] or "",
                 "price": float(r[8] or 0), "stock": int(r[3] or 0)}
                for r in raw if int(r[3] or 0) > 0
            ],
            key=lambda p: p["name"].lower(),
        )
        return JSONResponse({"products": products})
    except Exception as e:
        return JSONResponse({"error": str(e), "products": []})


@router.get("/sales")
def sales_page(
    request: Request,
    date_from: str = "",
    date_to: str = "",
    shop: str = "",
    seller_id: int = 0,
    page: int = 1,
):
    from web.auth import get_session_user, get_csrf_token
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
        "csrf_token": get_csrf_token(request),
        "flash_ok": request.query_params.get("ok") == "1",
        "flash_err": request.query_params.get("error", ""),
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

        ctx["shops"] = _get_user_allowed_shops(telegram_id, db)

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


@router.post("/sales/create")
def sales_create(
    request: Request,
    product_id: Annotated[int, Form()],
    shop_name: Annotated[str, Form()],
    quantity: Annotated[int, Form()],
    sale_price: Annotated[float, Form()],
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/sales?error=CSRF+error", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    try:
        db = get_web_db(telegram_id, org_db)
        allowed_shops = _get_user_allowed_shops(telegram_id, db)
        if shop_name not in allowed_shops:
            return RedirectResponse(url="/sales?error=Магазин+недоступен", status_code=302)
        internal_uid = _get_internal_uid(db, telegram_id)
        if not internal_uid:
            return RedirectResponse(url="/sales?error=Пользователь+не+найден", status_code=302)
        if quantity < 1:
            return RedirectResponse(url="/sales?error=Неверное+количество", status_code=302)

        result = db.add_sale(
            product_id=product_id,
            shop_name=shop_name,
            quantity_sold=quantity,
            user_id=internal_uid,
            sale_price=sale_price,
        )
        if result is None:
            return RedirectResponse(url="/sales?error=Недостаточно+товара+на+складе", status_code=302)
    except Exception as e:
        logging.error(f"sales_create error: {e}")
        return RedirectResponse(url="/sales?error=Ошибка+записи", status_code=302)

    return RedirectResponse(url="/sales?ok=1", status_code=302)


@router.get("/api/sales/{sale_id}")
def api_get_sale(request: Request, sale_id: int):
    """Return JSON with sale data for pre-filling the edit modal."""
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    try:
        db = get_web_db(telegram_id, org_db)
        sale = db.get_sale_by_id(sale_id)
        if not sale:
            return JSONResponse({"error": "Not found"}, status_code=404)
        # Scope check: the sale's shop must be within the user's allowed shops
        allowed_shops = _get_user_allowed_shops(telegram_id, db)
        if sale[2] not in allowed_shops:
            return JSONResponse({"error": "Forbidden"}, status_code=403)
        return JSONResponse({
            "id": sale[0],
            "product_id": sale[1],
            "shop_name": sale[2],
            "quantity_sold": sale[3],
            "sale_price": float(sale[4] or 0),
            "sale_date": (sale[6] or "")[:10],
            "product_name": sale[7] or "",
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


def _get_inventory_qty(db, shop_name: str, product_id: int) -> int:
    """Return current inventory qty for a product in a shop, or 0 on error."""
    try:
        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT quantity FROM inventory WHERE shop_name = ? AND product_id = ?",
            (shop_name, product_id),
        )
        row = cur.fetchone()
        conn.close()
        return int(row[0]) if row else 0
    except Exception:
        return 0


@router.post("/sales/{sale_id}/edit")
def sales_edit(
    request: Request,
    sale_id: int,
    shop_name: Annotated[str, Form()],
    quantity: Annotated[int, Form()],
    sale_price: Annotated[float, Form()],
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return JSONResponse({"ok": False, "error": "Forbidden"}, status_code=403)
    if not verify_csrf_token(request, csrf_token):
        return JSONResponse({"ok": False, "error": "CSRF error"}, status_code=403)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    try:
        db = get_web_db(telegram_id, org_db)
        allowed_shops = _get_user_allowed_shops(telegram_id, db)

        # Validate target shop is in scope
        if shop_name not in allowed_shops:
            return JSONResponse({"ok": False, "error": "Магазин недоступен"}, status_code=403)
        if quantity < 1:
            return JSONResponse({"ok": False, "error": "Неверное количество"}, status_code=400)

        # Load existing sale and validate it is within scope
        sale = db.get_sale_by_id(sale_id)
        if not sale:
            return JSONResponse({"ok": False, "error": "Продажа не найдена"}, status_code=404)
        old_shop = sale[2]
        old_qty = sale[3]
        product_id = sale[1]
        if old_shop not in allowed_shops:
            return JSONResponse({"ok": False, "error": "Нет доступа к этой продаже"}, status_code=403)

        # Stock sufficiency check
        shop_changed = (shop_name != old_shop)
        if shop_changed:
            # Moving to new shop — need quantity units available there
            available = _get_inventory_qty(db, shop_name, product_id)
            if available < quantity:
                return JSONResponse(
                    {"ok": False, "error": f"Недостаточно товара в {shop_name}: {available} шт."},
                    status_code=400,
                )
        else:
            # Same shop: only a net increase requires a stock check
            extra = quantity - old_qty
            if extra > 0:
                available = _get_inventory_qty(db, shop_name, product_id)
                if available < extra:
                    return JSONResponse(
                        {"ok": False, "error": f"Недостаточно товара: {available} шт. в наличии"},
                        status_code=400,
                    )

        internal_uid = _get_internal_uid(db, telegram_id)
        ok = db.update_sale_full(
            sale_id=sale_id,
            quantity_sold=quantity,
            sale_price=sale_price,
            shop_name=shop_name,
            changed_by=internal_uid,
        )
        if not ok:
            return JSONResponse({"ok": False, "error": "Ошибка обновления"}, status_code=500)
        return JSONResponse({"ok": True})
    except Exception as e:
        logging.error(f"sales_edit error: {e}")
        return JSONResponse({"ok": False, "error": "Ошибка сохранения"}, status_code=500)


@router.post("/sales/{sale_id}/delete")
def sales_delete(
    request: Request,
    sale_id: int,
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/sales", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/sales?error=CSRF+error", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    try:
        db = get_web_db(telegram_id, org_db)
        db.delete_sale(sale_id)
    except Exception as e:
        logging.error(f"sales_delete error: {e}")

    return RedirectResponse(url="/sales", status_code=302)
