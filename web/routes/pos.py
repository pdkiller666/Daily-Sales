import logging
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, JSONResponse
from typing import Annotated

router = APIRouter()


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


def _get_user_allowed_shops(telegram_id: int, db) -> list:
    from db_utils import get_user_org_scope
    all_shops = db.get_all_shops() or []
    try:
        scope_type, scope_values = get_user_org_scope(telegram_id)
        if not scope_type or scope_type == "all":
            return all_shops
        if scope_type == "shop":
            filtered = [s for s in all_shops if s in scope_values]
            if filtered:
                return filtered
        elif scope_type in ("city", "network", "trade_network"):
            # Whitelist: col is only ever "city" or "trade_network" — no user input reaches here
            col = "city" if scope_type == "city" else "trade_network"
            assert col in ("city", "trade_network"), f"Unexpected col: {col}"
            placeholders = ",".join("?" * len(scope_values))
            conn = db.get_connection()
            cur = conn.cursor()
            cur.execute(
                f"SELECT DISTINCT shop_name FROM users WHERE {col} IN ({placeholders}) AND shop_name IS NOT NULL",
                scope_values,
            )
            allowed = {row[0] for row in cur.fetchall()}
            conn.close()
            filtered = [s for s in all_shops if s in allowed]
            if filtered:
                return filtered
    except Exception:
        pass
    # Fallback: user's shop from org DB
    try:
        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute("SELECT shop_name FROM users WHERE telegram_id = ?", (telegram_id,))
        row = cur.fetchone()
        conn.close()
        if row and row[0] and row[0] in all_shops:
            return [row[0]]
    except Exception:
        pass
    return all_shops


@router.get("/pos")
def pos_page(request: Request):
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    is_admin = user.get("role") in ("owner", "admin", "super_admin")
    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    ctx: dict = {
        "request": request,
        "user": user,
        "is_admin": is_admin,
        "shops": [],
        "all_shops": [],
        "csrf_token": get_csrf_token(request),
        "error": None,
    }

    try:
        db = get_web_db(telegram_id, org_db)
        ctx["shops"] = _get_user_allowed_shops(telegram_id, db)
        ctx["all_shops"] = db.get_all_shops() or []

        # Auto-select the shop this user is assigned to
        default_shop = ctx["shops"][0] if ctx["shops"] else ""
        try:
            conn = db.get_connection()
            cur = conn.cursor()
            cur.execute("SELECT shop_name FROM users WHERE telegram_id = ?", (telegram_id,))
            row = cur.fetchone()
            conn.close()
            if row and row[0] and row[0] in ctx["shops"]:
                default_shop = row[0]
        except Exception:
            pass
        ctx["default_shop"] = default_shop

        # Subscription limit: warn early if monthly sale cap reached
        try:
            from subscription_utils import check_sales_limit
            _limit_ok, _limit_msg = check_sales_limit(telegram_id)
            ctx["sales_limit_reached"] = not _limit_ok
            ctx["sales_limit_msg"] = _limit_msg if not _limit_ok else ""
        except Exception:
            ctx["sales_limit_reached"] = False
            ctx["sales_limit_msg"] = ""

    except Exception as e:
        ctx["error"] = "Произошла внутренняя ошибка. Попробуйте позже."
        ctx["default_shop"] = ""
        ctx["sales_limit_reached"] = False
        ctx["sales_limit_msg"] = ""

    return request.app.state.templates.TemplateResponse(request, "pos/index.html", ctx)


@router.get("/api/pos/meta")
def api_pos_meta(request: Request, shop: str = ""):
    """Return favorites (product IDs), recent products with current stock, motivations dict."""
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    try:
        db = get_web_db(telegram_id, org_db)
        internal_uid = _get_internal_uid(db, telegram_id)

        # Favorites — list of product IDs
        favorites: list[int] = []
        if internal_uid:
            try:
                favorites = db.get_favorite_products(internal_uid) or []
            except Exception:
                pass

        # Recent — last 8 unique products with current stock in selected shop
        recent: list[dict] = []
        if internal_uid and shop:
            try:
                raw_recent = db.get_user_recent_products(internal_uid, limit=8) or []
                # id[0] name[1] price[2] category[3]
                # Resolve current stock from inventory
                raw_inv = db.get_all_inventory(shop_name=shop) or []
                stock_map: dict[int, int] = {}
                for r in raw_inv:
                    stock_map[int(r[1])] = int(r[3] or 0)
                for row in raw_recent:
                    pid = row[0]
                    qty = stock_map.get(pid, 0)
                    if qty > 0:
                        recent.append({
                            "id": pid,
                            "name": row[1] or "",
                            "price": float(row[2] or 0),
                            "category": row[3] or "Без категории",
                            "stock": qty,
                        })
            except Exception:
                pass

        # Motivations — {product_id: {type, value, commission_per_unit}}
        motivations: dict = {}
        try:
            all_mot = db.get_all_product_motivations() or []
            # id[0] name[1] mot_type[2] mot_value[3] ...
            for row in all_mot:
                if row[2] and row[3] is not None:
                    motivations[str(row[0])] = {
                        "type": row[2],
                        "value": float(row[3]),
                    }
        except Exception:
            pass

        # Check for active shift coefficient (multi_seller_coeff)
        extra_coeff_note = ""
        try:
            raw_extra = db.get_extra_conditions(active_only=True) or []
            for ec in raw_extra:
                if ec[1] == 'multi_seller_coeff':
                    coeff = float(ec[5] or 1.0)
                    min_s = int(ec[4] or 0)
                    ec_shop = ec[3]  # None = all shops
                    if (not ec_shop) or ec_shop == shop:
                        extra_coeff_note = f"×{coeff} при ≥{min_s} прод."
                        break
        except Exception:
            pass

        return JSONResponse({
            "favorites": favorites,
            "recent": recent,
            "motivations": motivations,
            "extra_coeff_note": extra_coeff_note,
        })
    except Exception as e:
        return JSONResponse({"error": "Внутренняя ошибка сервера", "favorites": [], "recent": [], "motivations": {}})


@router.post("/api/pos/favorite")
def api_pos_toggle_favorite(
    request: Request,
    product_id: Annotated[int, Form()],
    csrf_token: str = Form(default=""),
):
    """Toggle product in user favorites. Returns {ok, is_favorite}."""
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    if not verify_csrf_token(request, csrf_token):
        return JSONResponse({"ok": False, "error": "CSRF error"}, status_code=403)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    try:
        db = get_web_db(telegram_id, org_db)
        internal_uid = _get_internal_uid(db, telegram_id)
        if not internal_uid:
            return JSONResponse({"ok": False, "error": "Пользователь не найден"}, status_code=400)
        is_fav = db.toggle_favorite_product(internal_uid, product_id)
        return JSONResponse({"ok": True, "is_favorite": is_fav})
    except Exception as e:
        return JSONResponse({"ok": False, "error": "Внутренняя ошибка сервера"}, status_code=500)


@router.get("/api/pos/products")
def api_pos_products(request: Request, shop: str = ""):
    """Return all products with stock grouped by category for POS."""
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
            if user.get("role") not in ("owner", "admin", "super_admin"):
                return JSONResponse({"error": "Forbidden", "products": []}, status_code=403)

        raw = db.get_all_inventory(shop_name=shop if shop else None) or []
        # id[0] product_id[1] shop_name[2] quantity[3] … name[6] category[7] price[8]
        products = []
        for r in raw:
            qty = int(r[3] or 0)
            if qty <= 0:
                continue
            products.append({
                "id": r[1],
                "name": r[6] or "",
                "category": r[7] or "Без категории",
                "price": float(r[8] or 0),
                "stock": qty,
            })

        # Sort: by category then by name
        products.sort(key=lambda p: (p["category"].lower(), p["name"].lower()))

        # Group by category
        categories: dict = {}
        for p in products:
            cat = p["category"]
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(p)

        return JSONResponse({"products": products, "categories": categories})
    except Exception as e:
        return JSONResponse({"error": "Внутренняя ошибка сервера", "products": [], "categories": {}})


async def _post_sale_async(db, telegram_id: int, internal_uid: int, shop_name: str, sold_items: list):
    """Fire-and-forget: уведомления коллегам по смене + Google Sheets после веб-POS продажи."""
    import html as _html
    from datetime import datetime as _dt

    # ── 1. Уведомления коллегам по смене ──────────────────────────────────
    try:
        from bot_holder import get_bot as _get_bot
        from notif_utils import add_read_btn as _add_read_btn
        from zoneinfo import ZoneInfo

        bot = _get_bot()
        if bot:
            try:
                user_tz = db.get_user_timezone(telegram_id) or 'Europe/Moscow'
            except Exception:
                user_tz = 'Europe/Moscow'

            today_str = _dt.now(ZoneInfo(user_tz)).date().isoformat()

            try:
                coworkers = db.get_shop_coworkers_on_shift(shop_name, today_str, internal_uid) or []
            except Exception:
                coworkers = []

            if coworkers:
                try:
                    user_row = db.get_user(telegram_id)
                    seller_name = f"{user_row[2] or ''} {user_row[3] or ''}".strip() if user_row else ""
                except Exception:
                    seller_name = ""

                shop_esc = _html.escape(shop_name)
                notif_lines = [f"🛍 <b>Новая продажа в магазине {shop_esc}</b>"]
                if seller_name:
                    notif_lines.append(f"👤 Продавец: {_html.escape(seller_name)}")
                for item in sold_items:
                    total_fmt = f"{item['total']:,.0f} ₽".replace(",", " ")
                    price_fmt = f"{item['price']:,.0f} ₽".replace(",", " ")
                    notif_lines.append(
                        f"• {_html.escape(item['name'])}: {item['qty']} шт. "
                        f"× {price_fmt} = {total_fmt}"
                    )
                notif_text = "\n".join(notif_lines)

                for cw_uid, cw_tgid, _cw_name in coworkers:
                    try:
                        await bot.send_message(
                            int(cw_tgid),
                            notif_text,
                            parse_mode="HTML",
                            reply_markup=_add_read_btn(),
                        )
                        try:
                            db.add_notification_to_history(cw_uid, 'shift_sale', notif_text)
                        except Exception:
                            pass
                        try:
                            from web.push_utils import send_web_push
                            send_web_push(int(cw_tgid), "💰 Продажа в смене", f"{shop_name} · новая продажа", "/sales")
                        except Exception:
                            pass
                    except Exception as _send_err:
                        logging.warning(f"web pos shift_sale notif to {cw_tgid}: {_send_err}")
    except Exception as _notif_err:
        logging.error(f"web pos shift_sale notifications error: {_notif_err}")

    # ── 2. Google Sheets integration trigger ──────────────────────────────
    try:
        from integration.manager import integration_manager as _int_mgr
        _sync_db = object.__getattribute__(db, '_db') if hasattr(db, '_db') else db
        _now_str = _dt.now().strftime('%Y-%m-%d %H:%M')
        try:
            _user_row2 = db.get_user(telegram_id)
            _seller = f"{_user_row2[2] or ''} {_user_row2[3] or ''}".strip() if _user_row2 else ""
        except Exception:
            _seller = ""
        for item in sold_items:
            _sale_event = {
                'date': _now_str,
                'shop_name': shop_name,
                'quantity': item['qty'],
                'total': item['total'],
                'seller_name': _seller,
                'product_name': item['name'],
                'price': item['price'],
                'category': '',
            }
            try:
                await _int_mgr.trigger_export(_sync_db, 'sales', _sale_event)
            except Exception as _item_err:
                logging.warning(f"web pos GS trigger item error: {_item_err}")
    except Exception as _gs_err:
        logging.warning(f"web pos GS trigger error: {_gs_err}")


@router.post("/pos/checkout")
async def pos_checkout(
    request: Request,
    items_json: Annotated[str, Form()],
    shop_name: Annotated[str, Form()],
    csrf_token: str = Form(default=""),
):
    """Process a POS cart checkout — records multiple sales at once."""
    import json
    import asyncio
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    if not verify_csrf_token(request, csrf_token):
        return JSONResponse({"ok": False, "error": "CSRF error"}, status_code=403)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    try:
        items = json.loads(items_json)
        if not items:
            return JSONResponse({"ok": False, "error": "Корзина пуста"}, status_code=400)

        db = get_web_db(telegram_id, org_db)
        allowed_shops = _get_user_allowed_shops(telegram_id, db)

        if shop_name not in allowed_shops:
            if user.get("role") not in ("owner", "admin", "super_admin"):
                return JSONResponse({"ok": False, "error": "Магазин недоступен"}, status_code=403)
            all_shops = db.get_all_shops() or []
            if shop_name not in all_shops:
                return JSONResponse({"ok": False, "error": "Магазин не найден"}, status_code=400)

        internal_uid = _get_internal_uid(db, telegram_id)
        if not internal_uid:
            return JSONResponse({"ok": False, "error": "Пользователь не найден"}, status_code=400)

        from subscription_utils import check_sales_limit
        _ok, _msg = check_sales_limit(telegram_id)
        if not _ok:
            return JSONResponse({"ok": False, "error": _msg or "Достигнут лимит продаж по тарифу"}, status_code=403)

        sold = []
        sold_items = []  # детализация для пост-обработки
        errors = []
        for item in items:
            product_id = int(item.get("id", 0))
            qty = int(item.get("qty", 1))
            price = float(item.get("price", 0))
            name = item.get("name", "")

            if qty < 1 or product_id < 1:
                continue

            try:
                result = db.add_sale(
                    product_id=product_id,
                    shop_name=shop_name,
                    quantity_sold=qty,
                    user_id=internal_uid,
                    sale_price=price,
                )
                if result is None:
                    errors.append(f"«{name}»: недостаточно на складе")
                else:
                    sold.append(name)
                    sold_items.append({"name": name, "qty": qty, "price": price, "total": qty * price})
            except Exception as e:
                errors.append(f"«{name}»: {e}")

        if not sold:
            return JSONResponse({"ok": False, "error": "; ".join(errors) or "Ошибка записи"}, status_code=400)

        # Запускаем пост-обработку (нотификации + GS) в фоне — не блокируем ответ
        if sold_items:
            try:
                asyncio.create_task(_post_sale_async(db, telegram_id, internal_uid, shop_name, sold_items))
            except Exception:
                pass

        msg = f"Продано {len(sold)} поз."
        if errors:
            msg += f" Ошибки: {'; '.join(errors)}"
        return JSONResponse({"ok": True, "message": msg, "sold_count": len(sold), "errors": errors})

    except Exception as e:
        logging.error(f"pos_checkout error: {e}")
        return JSONResponse({"ok": False, "error": "Внутренняя ошибка"}, status_code=500)
