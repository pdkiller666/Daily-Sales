import io
import logging
import traceback
from typing import Annotated
from fastapi import APIRouter, Request, Form
from web.response_utils import content_disposition as _cd
from fastapi.responses import RedirectResponse, StreamingResponse, JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/inventory")
def inventory_page(request: Request, shop: str = "", q: str = "", category: str = "", status: str = ""):
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    if not org_db:
        return RedirectResponse("/dashboard", status_code=302)
    ctx: dict = {
        "request": request, "user": user,
        "is_admin": user.get("role") in ("owner", "admin", "super_admin"),
        "shops": [], "selected_shop": shop, "q": q,
        "categories": [], "selected_category": category,
        "selected_status": status,
        "inventory": [], "total_items": 0,
        "out_of_stock": 0, "low_stock": 0, "error": None,
        "csrf_token": get_csrf_token(request),
    }

    try:
        db = get_web_db(telegram_id, org_db)

        is_admin = user.get("role") in ("owner", "admin", "super_admin")
        all_inv_shops = db.get_inventory_shops() or []
        if is_admin:
            shops = all_inv_shops
            user_editable_shops = all_inv_shops
        else:
            from web.routes.sales import _get_user_allowed_shops
            allowed = _get_user_allowed_shops(telegram_id, db)
            shops = [s for s in all_inv_shops if s in allowed]
            # If user's shop has no inventory records yet, still show it
            if not shops and allowed:
                shops = [s for s in allowed if s]
            user_editable_shops = shops

        if not shop or shop not in shops:
            shop = shops[0] if shops else ""

        ctx["shops"] = shops
        ctx["selected_shop"] = shop

        # Пользователь может редактировать если выбранный магазин входит в его scope
        ctx["can_edit"] = bool(shop and shop in user_editable_shops)

        # inv: id[0] product_id[1] shop_name[2] quantity[3] updated_at[4]
        #       updated_by[5] name[6] category[7] price[8] updated_by_name[9]
        raw = db.get_all_inventory(shop_name=shop if shop else None) or []

        # Collect unique categories from this shop's inventory
        categories = sorted({r[7] for r in raw if r[7]})
        ctx["categories"] = categories

        # Sort: out of stock first (qty ≤ 0), then by qty asc, then name
        def _sort_key(r):
            qty = int(r[3] or 0)
            return (1 if qty > 0 else 0, qty, (r[6] or "").lower())

        inventory = sorted(raw, key=_sort_key)

        # Filter by search query (name, category or article[10])
        if q:
            ql = q.lower()
            inventory = [r for r in inventory if ql in (r[6] or "").lower() or ql in (r[7] or "").lower() or ql in (r[10] if len(r) > 10 and r[10] else "").lower()]

        # Filter by selected category
        if category:
            inventory = [r for r in inventory if (r[7] or "") == category]

        # Counts computed BEFORE status filter so the stat cards keep showing totals
        ctx["total_items"] = len(inventory)
        ctx["out_of_stock"] = sum(1 for r in inventory if int(r[3] or 0) <= 0)
        ctx["low_stock"] = sum(1 for r in inventory if 0 < int(r[3] or 0) <= 5)

        # Status filter (toggle from stat cards)
        if status == "out":
            inventory = [r for r in inventory if int(r[3] or 0) <= 0]
        elif status == "low":
            inventory = [r for r in inventory if 0 < int(r[3] or 0) <= 5]

        ctx["inventory"] = inventory

    except Exception as exc:
        logger.error("inventory_page tg=%s: %s", telegram_id, exc)
        ctx["error"] = "Произошла внутренняя ошибка. Попробуйте позже."
        ctx["can_edit"] = False

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
    if not org_db:
        return RedirectResponse("/dashboard", status_code=302)

    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter

        db = get_web_db(telegram_id, org_db)

        is_admin = user.get("role") in ("owner", "admin", "super_admin")
        if not is_admin:
            from web.routes.sales import _get_user_allowed_shops
            allowed = _get_user_allowed_shops(telegram_id, db)
            if shop and shop not in allowed:
                shop = allowed[0] if allowed else shop
        else:
            allowed = None

        def _fmt_inv_dt(raw):
            try:
                s = str(raw or "")[:16].replace("T", " ")
                d, t = s.split(" ")
                y, mo, dd = d.split("-")
                return f"{dd}.{mo}.{y} {t}"
            except Exception:
                return str(raw or "")[:16]

        # get_all_inventory: id[0] product_id[1] shop_name[2] quantity[3] last_updated[4]
        #                    updated_by[5] p.name[6] p.category[7] p.price[8] updated_by_name[9]
        if shop:
            raw = db.get_all_inventory(shop_name=shop) or []
            data = [(r[2], r[6], r[7], float(r[8] or 0), int(r[3] or 0), _fmt_inv_dt(r[4])) for r in raw]
        elif not is_admin and allowed is not None:
            data = []
            for s in allowed:
                raw = db.get_all_inventory(shop_name=s) or []
                data.extend((r[2], r[6], r[7], float(r[8] or 0), int(r[3] or 0), _fmt_inv_dt(r[4])) for r in raw)
        else:
            raw = db.get_all_inventory() or []
            data = [(r[2], r[6], r[7], float(r[8] or 0), int(r[3] or 0), _fmt_inv_dt(r[4])) for r in raw]

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
        tot_fill = PatternFill("solid", fgColor="DBEAFE")

        # 7 колонок: добавлены Цена и Стоимость
        headers = ["Магазин", "Товар", "Категория", "Цена (₽)", "Остаток (шт.)", "Стоимость (₽)", "Обновлено"]
        col_widths = [22, 32, 20, 14, 14, 16, 18]

        for i, (h, w) in enumerate(zip(headers, col_widths), 1):
            cell = ws.cell(row=1, column=i, value=h)
            cell.font = hdr_font
            cell.fill = hdr_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = border
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.row_dimensions[1].height = 28
        ws.freeze_panes = "A2"

        total_value = 0.0
        zero_count = 0
        low_count = 0
        for row_idx, row in enumerate(data, 2):
            shop_n, name, cat, price, qty, updated = row
            stock_value = price * qty
            total_value += stock_value
            is_zero = qty <= 0
            is_low = 0 < qty <= 5
            if is_zero:
                zero_count += 1
            elif is_low:
                low_count += 1
            row_fill = zero_fill if is_zero else low_fill if is_low else (even_fill if row_idx % 2 == 0 else None)

            for col_idx, val in enumerate([shop_n, name, cat, price, qty, stock_value, updated], 1):
                cell = ws.cell(row=row_idx, column=col_idx, value=val)
                cell.border = border
                if row_fill:
                    cell.fill = row_fill
                if col_idx in (4, 6):
                    cell.number_format = '#,##0.00 ₽'
                    cell.alignment = Alignment(horizontal="right")
                    if is_zero and col_idx == 6:
                        cell.font = Font(color="DC2626")
                elif col_idx == 5:
                    cell.alignment = Alignment(horizontal="center")
                    if is_zero:
                        cell.font = Font(bold=True, color="DC2626")
                    elif is_low:
                        cell.font = Font(bold=True, color="D97706")

        # ── Итого: 3 строки-сводки ────────────────────────────────────────────
        tr = len(data) + 2
        for col in range(1, 8):
            ws.cell(row=tr, column=col).border = border
            ws.cell(row=tr, column=col).fill = tot_fill
        ws.cell(row=tr, column=1, value=f"ИТОГО позиций: {len(data)}").font = Font(bold=True)
        ws.cell(row=tr, column=5, value=sum(r[4] for r in data)).font = Font(bold=True)
        tv_cell = ws.cell(row=tr, column=6, value=round(total_value, 2))
        tv_cell.font = Font(bold=True)
        tv_cell.number_format = '#,##0.00 ₽'
        tv_cell.alignment = Alignment(horizontal="right")

        tr2 = tr + 1
        ws.cell(row=tr2, column=1,
                value=f"Нулевые остатки: {zero_count}  |  Малые (≤5 шт.): {low_count}"
                ).font = Font(italic=True, color="6B7280", size=9)

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)

        filename = f"inventory{'_' + shop if shop else '_all'}.xlsx"
        return StreamingResponse(
            buf,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": _cd(filename)},
        )

    except Exception as exc:
        logger.error(f"inventory_export error: {exc}\n{traceback.format_exc()}")
        return RedirectResponse(url="/inventory?error=Ошибка+при+экспорте.+Попробуйте+позже.", status_code=302)


@router.post("/inventory/adjust")
def inventory_adjust(
    request: Request,
    shop_name: Annotated[str, Form()],
    product_id: Annotated[int, Form()],
    mode: Annotated[str, Form()],
    value: Annotated[float, Form()],
    reason: str = Form(default=""),
    csrf_token: str = Form(default=""),
):
    """Adjust stock: mode='add'|'subtract'|'set'. Returns JSON {success, new_qty}."""
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=401)
    if not verify_csrf_token(request, csrf_token):
        return JSONResponse({"success": False, "error": "CSRF error"}, status_code=403)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    if not org_db:
        return RedirectResponse("/dashboard", status_code=302)
    is_admin = user.get("role") in ("owner", "admin", "super_admin")
    try:
        db = get_web_db(telegram_id, org_db)

        # Обычный пользователь может редактировать только магазины из своего scope
        if not is_admin:
            from web.routes.sales import _get_user_allowed_shops
            allowed = _get_user_allowed_shops(telegram_id, db)
            if shop_name not in allowed:
                return JSONResponse({"success": False, "error": "Нет доступа к этому магазину"}, status_code=403)

        # Read current quantity for all modes
        conn = db.get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT quantity FROM inventory WHERE product_id = ? AND shop_name = ?",
                (product_id, shop_name),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        current = int(row[0] or 0) if row else 0

        if mode == "set":
            if int(value) < 0:
                return JSONResponse({"success": False, "error": "Количество не может быть отрицательным"}, status_code=400)
            delta = int(value) - current
        elif mode == "subtract":
            delta = -abs(int(value))
            if current + delta < 0:
                return JSONResponse({"success": False, "error": f"Недостаточно товара. В наличии: {current} шт."}, status_code=400)
        else:
            delta = abs(int(value))

        if delta == 0:
            return JSONResponse({"success": True, "new_qty": current})

        db.update_inventory(
            shop_name=shop_name,
            product_id=product_id,
            delta=delta,
            user_id=telegram_id,
            change_type="manual",
            change_reason=reason or "Ручная корректировка (веб)",
        )

        # Read new qty
        conn = db.get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT quantity FROM inventory WHERE product_id = ? AND shop_name = ?",
                (product_id, shop_name),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        new_qty = int(row[0] or 0) if row else 0

        # Google Sheets: ждём результата (до 8с), чтобы показать статус пользователю
        gs_status = None  # None = интеграция не настроена/недоступна
        try:
            from web.app import _main_loop
            from integration.manager import integration_manager as _int_mgr
            from datetime import datetime as _dt
            if _main_loop is not None:
                product_name = ""
                category = ""
                try:
                    p = db.get_product(product_id)
                    if p:
                        product_name = p[1] or ""
                        category = p[2] or ""
                except Exception:
                    pass
                import asyncio as _asyncio
                future = _asyncio.run_coroutine_threadsafe(
                    _int_mgr.trigger_export_with_result(db, "inventory", {
                        "shop_name": shop_name,
                        "product_name": product_name,
                        "category": category,
                        "quantity": new_qty,
                        "last_updated": _dt.now().strftime("%Y-%m-%d %H:%M"),
                    }),
                    _main_loop,
                )
                try:
                    results = future.result(timeout=8)
                    if results:  # пустой список = экспорт не настроен
                        gs_status = "ok" if all(r.get("success") for r in results) else "error"
                except Exception as _fe:
                    logging.warning(f"inventory_adjust: GSheets result error: {_fe}")
                    gs_status = "error"
        except Exception as _gs_err:
            logging.warning(f"inventory_adjust: GSheets trigger error: {_gs_err}")

        response: dict = {"success": True, "new_qty": new_qty}
        if gs_status is not None:
            response["gs_status"] = gs_status
        return JSONResponse(response)

    except Exception as e:
        logging.error(f"inventory_adjust error: {e}")
        return JSONResponse({"success": False, "error": "Внутренняя ошибка сервера"})


@router.get("/inventory/missing-products")
def inventory_missing_products(request: Request, shop: str = ""):
    """JSON: товары, которых ещё нет в inventory для данного магазина."""
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=401)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return JSONResponse({"success": False, "error": "Forbidden"}, status_code=403)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    if not org_db:
        return JSONResponse({"success": False, "error": "No org"}, status_code=400)
    try:
        db = get_web_db(telegram_id, org_db)
        conn = db.get_connection()
        try:
            cur = conn.cursor()
            if shop:
                cur.execute('''
                    SELECT p.id, p.name,
                           COALESCE(p.category, '') AS category,
                           COALESCE(p.price, 0)    AS price
                    FROM products p
                    WHERE NOT EXISTS (
                        SELECT 1 FROM inventory i
                        WHERE i.product_id = p.id AND i.shop_name = ?
                    )
                    ORDER BY p.name
                ''', (shop,))
            else:
                cur.execute(
                    "SELECT id, name, COALESCE(category,''), COALESCE(price,0)"
                    " FROM products ORDER BY name"
                )
            rows = cur.fetchall()
        finally:
            conn.close()
        products = [
            {"id": r[0], "name": r[1], "category": r[2], "price": float(r[3])}
            for r in rows
        ]
        return JSONResponse({"success": True, "products": products})
    except Exception as e:
        logger.error(f"inventory_missing_products error: {e}")
        return JSONResponse({"success": False, "error": "Внутренняя ошибка"}, status_code=500)


@router.get("/inventory/history")
def inventory_history(request: Request, shop: str = "", product_id: int = 0):
    """Возвращает HTML-фрагмент с историей изменений остатков для HTMX."""
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    if not org_db:
        return RedirectResponse("/dashboard", status_code=302)
    is_admin = user.get("role") in ("owner", "admin", "super_admin")

    rows = []
    product_name = ""
    error = None
    try:
        db = get_web_db(telegram_id, org_db)

        # Обычный пользователь видит историю только своих магазинов
        if not is_admin and shop:
            from web.routes.sales import _get_user_allowed_shops
            allowed = _get_user_allowed_shops(telegram_id, db)
            if shop not in allowed:
                return JSONResponse({"error": "Нет доступа"}, status_code=403)
        # Get product name
        p = db.get_product(product_id)
        if p:
            product_name = p[1]
        # id[0] old_qty[1] new_qty[2] delta[3] change_type[4]
        # change_reason[5] changed_by[6] changed_at[7] changer_name[8] username[9]
        raw = db.get_inventory_log_web(shop, product_id, limit=50) or []
        for r in raw:
            delta = r[3] or 0
            rows.append({
                "changed_at": (r[7] or "")[:16],
                "delta": delta,
                "new_qty": r[2],
                "old_qty": r[1],
                "change_type": r[4] or "manual",
                "reason": r[5] or "",
                "changer": r[8] or "—",
                "username": r[9] or "",
            })
    except Exception as e:
        logger.error(f"inventory_history error: {e}")
        error = "Внутренняя ошибка. Попробуйте позже."

    ctx = {
        "request": request,
        "rows": rows,
        "shop": shop,
        "product_name": product_name,
        "product_id": product_id,
        "error": error,
    }
    return request.app.state.templates.TemplateResponse(
        request, "inventory/history_fragment.html", ctx
    )
