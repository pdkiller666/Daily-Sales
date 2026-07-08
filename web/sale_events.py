"""
Post-sale side effects fired from the web interface after a successful add_sale().

Three effects mirror what the bot does in complete_sale:
  1. Google Sheets trigger_export
  2. Shift-sale push notifications to coworkers on duty
  3. Plan milestone check (50 / 75 / 100 %)

Called via asyncio.run_coroutine_threadsafe() from the sync sales_create route.
Each effect is fully isolated in try/except — a failure in one never affects the others.
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


async def post_sale_effects(
    org_db_path: str,
    sale_id: int,
    shop_name: str,
    telegram_id: int,
) -> None:
    """Fire all post-sale side effects for a web-originated sale.

    Args:
        org_db_path: Path to the org SQLite DB (e.g. 'data/tenants/org_xxx.db')
        sale_id:     ID returned by add_sale()
        shop_name:   Shop where the sale was recorded
        telegram_id: Telegram ID of the seller (session user)
    """
    try:
        from database import Database
        db = Database(org_db_path)
    except Exception as e:
        logger.error(f"post_sale_effects: cannot open DB {org_db_path!r}: {e}")
        return

    # ── Fetch sale details (needed by all effects) ───────────────────────────
    # get_sale_by_id returns:
    # (id[0], product_id[1], shop_name[2], quantity_sold[3], sale_price[4],
    #  user_id[5], sale_date[6], product_name[7], first_name[8], last_name[9])
    try:
        sale = db.get_sale_by_id(sale_id)
        if not sale:
            logger.warning(f"post_sale_effects: sale {sale_id} not found")
            return
        quantity   = sale[3]
        sale_price = float(sale[4] or 0)
        total      = quantity * sale_price
        product_name  = sale[7] or ""
        seller_name   = f"{sale[8] or ''} {sale[9] or ''}".strip()
        internal_uid  = sale[5]
    except Exception as e:
        logger.error(f"post_sale_effects: get_sale_by_id error: {e}")
        return

    # ── 1. Google Sheets trigger ─────────────────────────────────────────────
    try:
        from integration.manager import integration_manager as _int_mgr
        sale_event = {
            "date":         datetime.now().strftime("%Y-%m-%d %H:%M"),
            "shop_name":    shop_name,
            "quantity":     quantity,
            "total":        total,
            "seller_name":  seller_name,
            "product_name": product_name,
            "price":        sale_price,
            "category":     "",
        }
        await _int_mgr.trigger_export_with_result(db, "sales", sale_event)
        logger.debug(f"post_sale_effects: GSheets trigger ok (sale {sale_id})")
    except Exception as e:
        logger.warning(f"post_sale_effects: GSheets trigger error: {e}")

    # ── 2. Shift-sale push notifications ─────────────────────────────────────
    try:
        from bot_holder import get_bot as _get_bot
        from notif_utils import add_read_btn as _add_read_btn
        from timezone_utils import get_current_user_time as _gcur_tz
        from utils import he, format_currency

        bot = _get_bot()
        if bot and internal_uid:
            tz        = db.get_user_timezone(telegram_id)
            today_str = _gcur_tz(tz).date().isoformat()
            coworkers = db.get_shop_coworkers_on_shift(shop_name, today_str, internal_uid)
            if coworkers:
                notif_lines = [f"🛍 <b>Новая продажа в магазине {he(shop_name)}</b>"]
                if seller_name:
                    notif_lines.append(f"👤 Продавец: {seller_name}")
                notif_lines.append(
                    f"• {he(product_name)}: {quantity} шт."
                    f" × {format_currency(sale_price)}"
                    f" = {format_currency(total)}"
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
                        db.add_notification_to_history(cw_uid, "shift_sale", notif_text)
                    except Exception as send_err:
                        logger.warning(
                            f"post_sale_effects: shift notif to {cw_tgid}: {send_err}"
                        )
    except Exception as e:
        logger.error(f"post_sale_effects: shift alerts error: {e}")

    # ── 3. Plan milestone check (50 / 75 / 100 %) ────────────────────────────
    try:
        db.check_and_mark_plan_milestones(telegram_id)
        logger.debug(f"post_sale_effects: milestone check ok (tg={telegram_id})")
    except Exception as e:
        logger.warning(f"post_sale_effects: milestone check error: {e}")


async def post_package_sale_effects(
    org_db_path: str,
    client_package_id: int,
    telegram_id: int,
) -> None:
    """Fire post-sale side effects for a package (абонемент) sale: currently
    only the Google Sheets export (packages have no per-shift stock/inventory
    to notify coworkers about, and no sales-plan milestones tied to them).
    Called via asyncio.run_coroutine_threadsafe() from the sync package_sell route.
    """
    try:
        from database import Database
        db = Database(org_db_path)
    except Exception as e:
        logger.error(f"post_package_sale_effects: cannot open DB {org_db_path!r}: {e}")
        return

    try:
        conn = db.get_connection()
        try:
            row = conn.execute(
                "SELECT cp.price_paid, cp.visits_total, cp.shop_name, cp.purchased_at, "
                "sp.name, c.first_name, c.last_name, u.first_name, u.last_name "
                "FROM client_packages cp "
                "JOIN service_packages sp ON sp.id = cp.package_id "
                "JOIN clients c ON c.id = cp.client_id "
                "LEFT JOIN users u ON u.telegram_id = cp.sold_by "
                "WHERE cp.id=?",
                (client_package_id,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            logger.warning(f"post_package_sale_effects: client_package {client_package_id} not found")
            return
        price_paid, visits_total, shop_name, purchased_at, pkg_name, c_first, c_last, s_first, s_last = row
        client_name = f"{c_first or ''} {c_last or ''}".strip()
        seller_name = f"{s_first or ''} {s_last or ''}".strip()

        from integration.manager import integration_manager as _int_mgr
        package_event = {
            "date": (purchased_at or datetime.now().strftime("%Y-%m-%d %H:%M"))[:16],
            "package_name": pkg_name or "",
            "shop_name": shop_name or "",
            "client_name": client_name,
            "price": float(price_paid or 0),
            "visits_total": visits_total,
            "seller_name": seller_name,
        }
        await _int_mgr.trigger_export_with_result(db, "packages", package_event)
        logger.debug(f"post_package_sale_effects: GSheets trigger ok (cp {client_package_id})")
    except Exception as e:
        logger.warning(f"post_package_sale_effects: GSheets trigger error: {e}")
