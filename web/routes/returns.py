"""Веб-роуты для возвратов товара (/returns)."""
import logging
from datetime import date, timedelta
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, JSONResponse
from timezone_utils import get_current_user_time, DEFAULT_TZ

router = APIRouter()
_RR = RedirectResponse
PAGE_SIZE = 20


def _get_all_shops(db):
    """Все магазины организации (без учёта scope)."""
    try:
        conn = db.get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT DISTINCT shop_name FROM inventory ORDER BY shop_name")
            shops = [r[0] for r in cur.fetchall()]
            if not shops:
                cur.execute("SELECT DISTINCT shop_name FROM users WHERE shop_name IS NOT NULL")
                shops = [r[0] for r in cur.fetchall()]
        finally:
            conn.close()
        return shops
    except Exception:
        return []


def _get_allowed_shops(db, telegram_id: int, is_admin: bool):
    """Список магазинов, доступных пользователю с учётом scope.

    Owner/admin без ограничения scope видят все магазины организации.
    Scoped-admin (admin с scope_type='shop'/'city'/'network') видит только
    магазины в пределах своего scope — та же модель, что и в web/routes/sales.py.
    Не-админы видят только свой магазин.
    """
    from db_utils import get_user_org_scope

    all_shops = _get_all_shops(db)

    if not is_admin:
        try:
            conn = db.get_connection()
            try:
                cur = conn.cursor()
                cur.execute("SELECT shop_name FROM users WHERE telegram_id = ?", (telegram_id,))
                row = cur.fetchone()
            finally:
                conn.close()
            return [row[0]] if row and row[0] else []
        except Exception:
            return []

    try:
        scope_type, scope_values = get_user_org_scope(telegram_id)
        if not scope_type or scope_type == "all":
            return all_shops
        if scope_type == "shop":
            filtered = [s for s in all_shops if s in scope_values]
            if filtered:
                return filtered
        elif scope_type in ("city", "network", "trade_network"):
            col = "city" if scope_type == "city" else "trade_network"
            assert col in ("city", "trade_network"), f"Unexpected col: {col}"
            conn = db.get_connection()
            try:
                cur = conn.cursor()
                placeholders = ",".join("?" * len(scope_values))
                cur.execute(
                    f"SELECT DISTINCT shop_name FROM users WHERE {col} IN ({placeholders}) AND shop_name IS NOT NULL",
                    scope_values,
                )
                allowed = {row[0] for row in cur.fetchall()}
            finally:
                conn.close()
            filtered = [s for s in all_shops if s in allowed]
            if filtered:
                return filtered
    except Exception:
        pass
    return all_shops


def _get_internal_uid(db, telegram_id: int):
    try:
        conn = db.get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))
            row = cur.fetchone()
        finally:
            conn.close()
        return row[0] if row else None
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# GET /returns — список возвратов
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/returns")
def returns_page(
    request: Request,
    date_from: str = "",
    date_to: str = "",
    shop: str = "",
    page: int = 1,
    sale_id: int = 0,
):
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return _RR("/login", status_code=303)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    is_admin = user.get("role") in ("owner", "admin", "super_admin")

    # Дефолтный период — 30 дней. Если пришли по фильтру конкретной продажи
    # (sale_id) — не сужаем диапазон датами, продажа могла быть давно.
    if not date_from and not date_to and not sale_id:
        date_to   = date.today().isoformat()
        date_from = (date.today() - timedelta(days=30)).isoformat()

    ctx = {
        "request": request, "user": user, "is_admin": is_admin,
        "returns": [], "shops": [], "summary": {"count": 0, "total_qty": 0, "total_amount": 0.0},
        "date_from": date_from, "date_to": date_to, "selected_shop": shop,
        "page": page, "total_pages": 1, "total_count": 0,
        "csrf_token": get_csrf_token(request),
        "flash_ok": request.query_params.get("ok") == "1",
        "flash_err": request.query_params.get("error", ""),
        "sale_id_filter": sale_id or None,
    }

    try:
        db = get_web_db(telegram_id, org_db)
        ctx["shops"] = _get_allowed_shops(db, telegram_id, is_admin)

        shop_filter = shop if shop else None
        offset = (page - 1) * PAGE_SIZE

        rows  = db.get_returns(shop_name=shop_filter, start_date=date_from or None,
                               end_date=date_to or None, sale_id=sale_id or None,
                               limit=PAGE_SIZE, offset=offset)
        total = db.get_returns_count(shop_name=shop_filter, start_date=date_from or None,
                                     end_date=date_to or None, sale_id=sale_id or None)
        summary = db.get_returns_summary(shop_name=shop_filter,
                                         start_date=date_from or None, end_date=date_to or None)

        ctx["returns"]     = rows
        ctx["total_count"] = total
        ctx["total_pages"] = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        ctx["summary"]     = summary
    except Exception as e:
        logging.error(f"returns_page error: {e}")
        ctx["flash_err"] = "Ошибка загрузки данных"

    return request.app.state.templates.TemplateResponse(request, "returns/index.html", ctx)


# ─────────────────────────────────────────────────────────────────────────────
# POST /returns/create — создать возврат (modal из /sales)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/api/returns/create")
def api_returns_create(
    request: Request,
    sale_id: int = Form(...),
    quantity_returned: int = Form(...),
    reason: str = Form(default=""),
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    if not verify_csrf_token(request, csrf_token):
        return JSONResponse({"ok": False, "error": "CSRF error"}, status_code=403)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return JSONResponse({"ok": False, "error": "Нет доступа"}, status_code=403)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    try:
        db = get_web_db(telegram_id, org_db)

        # Получаем продажу
        sale = db.get_sale_by_id(sale_id)
        if not sale:
            return JSONResponse({"ok": False, "error": "Продажа не найдена"}, status_code=404)

        # sale: (id[0], product_id[1], shop_name[2], quantity_sold[3], sale_price[4],
        #        user_id[5], sale_date[6], product_name[7], first_name[8], last_name[9])

        # Учитываем уже возвращённые единицы — нельзя вернуть больше, чем осталось
        already_returned = db.get_already_returned_qty(sale_id)
        available_qty = max(0, sale[3] - already_returned)
        if quantity_returned < 1 or quantity_returned > available_qty:
            if available_qty == 0:
                return JSONResponse({"ok": False, "error": "По этой продаже уже возвращены все единицы"}, status_code=409)
            return JSONResponse({"ok": False, "error": f"Доступно для возврата: {available_qty} шт. (уже возвращено: {already_returned})"}, status_code=400)

        # Проверка прав на магазин — учитываем scope (scoped-admin видит не все магазины)
        allowed = _get_allowed_shops(db, telegram_id, is_admin=True)
        if sale[2] not in allowed:
            return JSONResponse({"ok": False, "error": "Нет доступа к этому магазину"}, status_code=403)

        returned_by_uid = _get_internal_uid(db, telegram_id)
        if not returned_by_uid:
            return JSONResponse({"ok": False, "error": "Пользователь не найден"}, status_code=404)

        # Дата в таймзоне пользователя (на Amvera сервер UTC, user может быть UTC+N)
        try:
            user_tz = db.get_user_timezone(telegram_id) or DEFAULT_TZ
        except Exception:
            user_tz = DEFAULT_TZ
        return_date = get_current_user_time(user_tz).date().isoformat()
        try:
            return_id = db.create_sale_return(
                sale_id             = sale_id,
                product_id          = sale[1],
                product_name        = sale[7],
                shop_name           = sale[2],
                quantity_returned   = quantity_returned,
                return_price        = sale[4] or 0,
                seller_user_id      = sale[5],
                returned_by_user_id = returned_by_uid,
                return_date         = return_date,
                reason              = reason.strip()[:200] or None,
            )
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=409)
        if return_id:
            amount = quantity_returned * (sale[4] or 0)
            return JSONResponse({
                "ok": True,
                "return_id": return_id,
                "product_name": sale[7],
                "quantity": quantity_returned,
                "amount": amount,
            })
        return JSONResponse({"ok": False, "error": "Ошибка создания возврата"}, status_code=500)
    except Exception as e:
        logging.error(f"api_returns_create error: {e}")
        return JSONResponse({"ok": False, "error": "Внутренняя ошибка"}, status_code=500)
