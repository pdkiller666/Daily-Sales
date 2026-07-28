import html as _html
import json
import logging
import os
import re
import sqlite3
import threading
import urllib.request
import uuid
from datetime import date as _date
from pathlib import Path
from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse

router = APIRouter()

SHOP_BOT_DB = "data/shop_bot.db"


def _notify_admin_new_request(plan_type: str, amount: int, user_display: str, telegram_id: int) -> None:
    token = os.environ.get("BOT_TOKEN", "")
    admin_id = os.environ.get("ADMIN_CHAT_ID", "")
    if not token or not admin_id:
        return
    label = _plan_type_label(plan_type)
    text = (
        "🔔 <b>Новая заявка из веб-кабинета!</b>\n\n"
        f"👤 <b>Пользователь:</b> {_html.escape(str(user_display))}\n"
        f"🆔 <b>Telegram ID:</b> {telegram_id}\n"
        f"📦 <b>Позиция:</b> {_html.escape(label)}\n"
        f"💰 <b>Сумма:</b> {amount}\u00a0₽\n\n"
        "⏰ Заявка ожидает рассмотрения в боте."
    )
    payload = json.dumps({"chat_id": admin_id, "text": text, "parse_mode": "HTML"}).encode()
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=4):
            pass
    except Exception as exc:
        _is_timeout = "timeout" in type(exc).__name__.lower() or "timed out" in str(exc).lower()
        if _is_timeout:
            logging.warning("_notify_admin_new_request: timeout после 4 с")
        else:
            logging.warning("_notify_admin_new_request: %s", exc)


def _notify_admin_async(plan_type: str, amount: int, user_display: str, telegram_id: int) -> None:
    """Fire-and-forget wrapper: отправляет уведомление в daemon-thread."""
    t = threading.Thread(
        target=_notify_admin_new_request,
        args=(plan_type, amount, user_display, telegram_id),
        daemon=True,
    )
    t.start()


def _notify_admin_batch_async(plan_types: list, total_amount: int, user_display: str, telegram_id: int) -> None:
    """Одно сводное уведомление при пакетной заявке (несколько позиций за раз)."""
    def _send() -> None:
        token = os.environ.get("BOT_TOKEN", "")
        admin_id = os.environ.get("ADMIN_CHAT_ID", "")
        if not token or not admin_id:
            return
        lines = "\n".join(f"  • {_html.escape(_plan_type_label(pt))}" for pt in plan_types)
        text = (
            "🛒 <b>Пакетная заявка из веб-кабинета!</b>\n\n"
            f"👤 <b>Пользователь:</b> {_html.escape(str(user_display))}\n"
            f"🆔 <b>Telegram ID:</b> {telegram_id}\n"
            f"📦 <b>Позиции ({len(plan_types)}):</b>\n{lines}\n"
            f"💰 <b>Итого:</b> {total_amount}\u00a0₽/мес.\n\n"
            "⏰ Заявки ожидают рассмотрения в боте (/pending_payments)."
        )
        payload = json.dumps({"chat_id": admin_id, "text": text, "parse_mode": "HTML"}).encode()
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5):
                pass
        except Exception as exc:
            _is_timeout = "timeout" in type(exc).__name__.lower() or "timed out" in str(exc).lower()
            if _is_timeout:
                logging.warning("_notify_admin_batch_async: timeout")
            else:
                logging.warning("_notify_admin_batch_async: %s", exc)

    threading.Thread(target=_send, daemon=True).start()


def _plan_type_label(plan_type: str) -> str:
    if not plan_type:
        return "—"
    if plan_type.startswith("module_"):
        _k = plan_type[7:]
        if _k.startswith("annual_"):
            return f"Модуль (год): {_k[len('annual_'):]}"
        return f"Модуль: {_k}"
    if plan_type.startswith("bundle_"):
        _k = plan_type[7:]
        if _k.startswith("annual_"):
            return f"Пакет (год): {_k[len('annual_'):]}"
        return f"Пакет: {_k}"
    if plan_type.startswith("extension_"):
        return f"Расширение: {plan_type[10:]}"
    if plan_type.startswith("addon_"):
        parts = plan_type.split("_")
        labels = {"shops": "Доп. магазин", "products": "Доп. товары"}
        return labels.get(parts[1] if len(parts) > 1 else "", plan_type)
    return f"Тариф: {plan_type}"


def _get_user_id_in_shop_bot(telegram_id: int) -> int | None:
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        try:
            row = conn.execute(
                "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        finally:
            conn.close()
        return row[0] if row else None
    except Exception:
        return None


def _strip_leading_emoji(name: str, icon: str) -> str:
    """Возвращает name без ведущего эмодзи+пробел, если name начинается с icon."""
    if icon and name and name.startswith(icon):
        return name[len(icon):].lstrip()
    return name


def _annual_fields(price_monthly: int, price_annual: int) -> dict:
    """Поля годовой опции (рядом с месячной). Пустой dict-набор, если год не задан.

    has_annual=True только когда price_annual>0. Показываем эквивалент ₽/мес при
    годовой оплате и % выгоды относительно 12×месячной — чтобы выгода была явной."""
    if not price_annual or price_annual <= 0:
        return {"has_annual": False, "price_annual": 0,
                "price_annual_fmt": "", "annual_per_month": 0,
                "annual_per_month_fmt": "", "annual_save_pct": 0}
    per_month = round(price_annual / 12)
    full_year = price_monthly * 12
    save_pct = int(round((full_year - price_annual) / full_year * 100)) if full_year > 0 else 0
    return {
        "has_annual": True,
        "price_annual": int(price_annual),
        "price_annual_fmt": f"{int(price_annual):,}".replace(",", "\u00a0") + "\u00a0₽/год",
        "annual_per_month": per_month,
        "annual_per_month_fmt": f"{per_month:,}".replace(",", "\u00a0") + "\u00a0₽/мес.",
        "annual_save_pct": max(save_pct, 0),
    }


def _get_all_billing_modules() -> list[dict]:
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        try:
            rows = conn.execute(
                """SELECT key, name, icon, description, price_monthly, features_json,
                          COALESCE(price_annual,0)
                   FROM billing_modules WHERE is_active=1
                   ORDER BY sort_order, id"""
            ).fetchall()
        finally:
            conn.close()
        result = []
        for r in rows:
            key, name, icon, desc, price, feats_json, price_annual = r
            try:
                features = json.loads(feats_json or "[]")
            except Exception:
                features = []
            _icon = icon or "🔧"
            item = {
                "key": key,
                "name": name,
                "name_display": _strip_leading_emoji(name, _icon),
                "icon": _icon,
                "description": desc or "",
                "price_monthly": int(price or 0),
                "price_fmt": f"{int(price or 0):,}".replace(",", "\u00a0") + "\u00a0₽/мес.",
                "features": features,
            }
            item.update(_annual_fields(int(price or 0), int(price_annual or 0)))
            result.append(item)
        return result
    except Exception:
        return []


def _get_all_billing_extensions() -> list[dict]:
    """Расширения из billing_extensions, сгруппированные по module_key."""
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        try:
            rows = conn.execute(
                """SELECT module_key, key, name, icon, description, price_monthly,
                          COALESCE(price_annual,0)
                   FROM billing_extensions WHERE is_active=1
                   ORDER BY module_key, sort_order, id"""
            ).fetchall()
        finally:
            conn.close()
        result = []
        for r in rows:
            module_key, key, name, icon, desc, price, price_annual = r
            _icon = icon or "🔧"
            item = {
                "module_key": module_key,
                "key": key,
                "name": name,
                "name_display": _strip_leading_emoji(name, _icon),
                "icon": _icon,
                "description": desc or "",
                "price_monthly": int(price or 0),
                "price_fmt": f"{int(price or 0):,}".replace(",", "\u00a0") + "\u00a0₽/мес.",
                "price_annual": int(price_annual or 0),
            }
            item.update(_annual_fields(int(price or 0), int(price_annual or 0)))
            result.append(item)
        return result
    except Exception:
        return []


def _modules_map(modules: list[dict], extensions: list[dict] | None = None) -> dict:
    """key → {name, icon} для отображения дружественных названий в шаблоне.
    Включает и модули, и расширения, чтобы chips в hero-карточке показывали
    friendly names для всех активных подписок."""
    result = {m["key"]: {"name": m["name"], "name_display": m.get("name_display", m["name"]), "icon": m["icon"]} for m in modules}
    if extensions:
        for e in extensions:
            result[e["key"]] = {"name": e["name"], "name_display": e.get("name_display", e["name"]), "icon": e["icon"]}
    return result


def _get_all_billing_bundles() -> list[dict]:
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        try:
            rows = conn.execute(
                """SELECT key, name, icon, description, includes_json, price_monthly,
                          COALESCE(price_annual,0)
                   FROM billing_bundles WHERE is_active=1
                   ORDER BY sort_order, id"""
            ).fetchall()
        finally:
            conn.close()
        result = []
        for r in rows:
            key, name, icon, desc, inc_json, price, price_annual = r
            try:
                includes = json.loads(inc_json or '{"modules":[],"extensions":[]}')
            except Exception:
                includes = {"modules": [], "extensions": []}
            item = {
                "key": key,
                "name": name,
                "icon": icon or "📦",
                "description": desc or "",
                "includes": includes,
                "modules_count": len(includes.get("modules", [])),
                "price_monthly": int(price or 0),
                "price_fmt": f"{int(price or 0):,}".replace(",", "\u00a0") + "\u00a0₽/мес.",
            }
            item.update(_annual_fields(int(price or 0), int(price_annual or 0)))
            result.append(item)
        return result
    except Exception:
        return []


def _fmt_sub_date(iso: str) -> str:
    """Конвертирует YYYY-MM-DD → ДД.ММ.ГГГГ. Прочие значения возвращает как есть."""
    if iso and len(iso) >= 10 and iso != "∞":
        try:
            from datetime import datetime as _dt2
            return _dt2.strptime(iso[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
        except Exception:
            pass
    return iso


def _get_user_active_module_subs(telegram_id: int) -> dict:
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        try:
            # Включаем и истёкшие позиции (до 90 дней назад) — чтобы пользователь
            # видел что именно закончилось, а не просто пустую строку
            rows = conn.execute(
                """SELECT item_key, item_type, end_date FROM billing_module_subs
                   WHERE user_telegram_id=? AND is_active=1
                     AND (end_date IS NULL OR end_date > datetime('now', '-90 days'))""",
                (telegram_id,)
            ).fetchall()
        finally:
            conn.close()
        today = _date.today()
        result = {}
        for r in rows:
            end_str = str(r[2] or "")[:10]
            days_remaining = None
            is_expired = False
            if end_str:
                try:
                    from datetime import datetime as _dt
                    end_d = _dt.strptime(end_str, "%Y-%m-%d").date()
                    diff = (end_d - today).days
                    if diff < 0:
                        is_expired = True
                        days_remaining = diff  # отрицательное — сколько дней назад
                    else:
                        days_remaining = diff
                except Exception:
                    pass
            result[r[0]] = {
                "item_type": r[1],
                "end_date": _fmt_sub_date(end_str) if end_str else "∞",
                "days_remaining": days_remaining,
                "is_expired": is_expired,
            }
        return result
    except Exception:
        return {}


def _get_active_trial(user_id: int | None) -> dict | None:
    if not user_id:
        return None
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        try:
            row = conn.execute(
                """SELECT end_date FROM subscriptions
                   WHERE user_id=? AND is_trial=1 AND end_date > datetime('now')
                   ORDER BY end_date DESC LIMIT 1""",
                (user_id,)
            ).fetchone()
        finally:
            conn.close()
        if row:
            return {"end_date": str(row[0] or "")[:10], "is_trial": True}
        return None
    except Exception:
        return None


def _get_payment_history(user_id: int, limit: int = 10) -> tuple[list[dict], bool]:
    """Returns (history_rows, has_more). has_more=True when total records > limit."""
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM payment_requests WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
            rows = conn.execute(
                """SELECT id, plan_type, amount, status, created_at, processed_at
                   FROM payment_requests
                   WHERE user_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (user_id, limit),
            ).fetchall()
        finally:
            conn.close()
        STATUS_LABELS = {
            "pending": ("⏳ Ожидает", "text-amber-600 bg-amber-50"),
            "approved": ("✅ Подтверждено", "text-emerald-600 bg-emerald-50"),
            "rejected": ("❌ Отклонено", "text-red-600 bg-red-50"),
            "cancelled": ("🚫 Отозвано", "text-slate-500 bg-slate-100"),
        }
        result = []
        for row in rows:
            st = row[3] or "pending"
            label, css = STATUS_LABELS.get(st, (st, "text-slate-500 bg-slate-50"))
            result.append({
                "id": row[0],
                "plan_type": row[1],
                "plan_label": _plan_type_label(row[1] or ""),
                "amount": int(row[2] or 0),
                "status": st,
                "status_label": label,
                "status_css": css,
                "created_at": str(row[4] or "")[:16].replace("T", " "),
                "processed_at": str(row[5] or "")[:16].replace("T", " ") if row[5] else "—",
            })
        return result, total > limit
    except Exception:
        return [], False


def _has_pending_request(user_id: int) -> bool:
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        try:
            row = conn.execute(
                "SELECT id FROM payment_requests WHERE user_id = ? AND status = 'pending'",
                (user_id,),
            ).fetchone()
        finally:
            conn.close()
        return row is not None
    except Exception:
        return False


def _get_pending_plan_types(user_id: int) -> set:
    """Возвращает множество plan_type строк с pending-заявками для этого пользователя."""
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        try:
            rows = conn.execute(
                "SELECT plan_type FROM payment_requests WHERE user_id = ? AND status = 'pending'",
                (user_id,),
            ).fetchall()
        finally:
            conn.close()
        return {r[0] for r in rows if r[0]}
    except Exception:
        return set()


def _calc_monthly_total(user_mod_subs: dict, modules: list, extensions: list, bundles: list) -> int:
    """Суммирует месячную стоимость всех активных платных подписок."""
    price_map: dict = {}
    for m in modules:
        price_map[m["key"]] = m.get("price_monthly", 0)
    for e in extensions:
        price_map[e["key"]] = e.get("price_monthly", 0)
    for b in bundles:
        price_map[b["key"]] = b.get("price_monthly", 0)
    total = 0
    for key in user_mod_subs:
        total += price_map.get(key, 0)
    return total


def _fmt_cap(value) -> str:
    """Форматирует лимит: -1 → ∞, иначе число с неразрывным пробелом."""
    try:
        v = int(value)
    except Exception:
        return "—"
    if v < 0:
        return "∞"
    return f"{v:,}".replace(",", "\u00a0")


def _get_tariff_overview(telegram_id: int) -> dict | None:
    """Текущий тариф (ось «объём»): план, лимиты объёма и фактическое использование.
    Не меняет логику гейтинга — только читает данные для отображения."""
    try:
        import subscription_utils as su
    except Exception:
        return None

    try:
        org_plan = su._get_org_plan_for_user(telegram_id)
        plan_name = org_plan if org_plan is not None else su._get_personal_plan(telegram_id)
        limits = su.get_plan_limits(telegram_id)
        days_remaining = su.get_subscription_days_remaining(telegram_id)
        addons = su._get_addon_totals_for_user(telegram_id)
    except Exception:
        return None

    extra_products = int(addons.get("extra_products", 0) or 0) * 100
    extra_shops = int(addons.get("extra_shops", 0) or 0)
    extra_sales = int(addons.get("extra_sales", 0) or 0) * 500

    def _eff(base, extra):
        try:
            b = int(base)
        except Exception:
            return base
        return b if b < 0 else b + extra

    max_products = _eff(limits.get("max_products", 0), extra_products)
    max_shops = _eff(limits.get("max_shops", 0), extra_shops)
    max_sales = _eff(limits.get("max_sales_per_month", 0), extra_sales)

    used_products = used_shops = used_sales = None
    try:
        from tenant_manager import tenant_manager
        from database import Database
        from datetime import datetime as _dt
        db_path = tenant_manager.get_user_db_path(telegram_id)
        db = Database(db_path)
        _cnt_conn = db.get_connection()
        try:
            used_products = _cnt_conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
            used_shops = _cnt_conn.execute("SELECT COUNT(*) FROM shops").fetchone()[0]
        finally:
            _cnt_conn.close()
        month_start = _dt.now().strftime("%Y-%m-01")
        conn = db.get_connection()
        try:
            used_sales = conn.execute(
                "SELECT COUNT(*) FROM sales WHERE sale_date >= ?", (month_start,)
            ).fetchone()[0]
        finally:
            conn.close()
    except Exception:
        pass

    def _pct(used, cap):
        try:
            c = int(cap)
            u = int(used)
        except Exception:
            return None
        if c <= 0:
            return None
        return min(100, round(u * 100 / c))

    return {
        "plan_name": plan_name or "Бесплатный",
        "is_trial": bool(su._has_active_trial(telegram_id)),
        "days_remaining": days_remaining,
        "addon_products": extra_products,
        "addon_shops": extra_shops,
        "addon_sales": extra_sales,
        "rows": [
            {"icon": "🛍", "label": "Товары", "used": used_products,
             "cap": max_products, "cap_fmt": _fmt_cap(max_products),
             "pct": _pct(used_products, max_products)},
            {"icon": "🏪", "label": "Магазины", "used": used_shops,
             "cap": max_shops, "cap_fmt": _fmt_cap(max_shops),
             "pct": _pct(used_shops, max_shops)},
            {"icon": "🧾", "label": "Продажи в месяц", "used": used_sales,
             "cap": max_sales, "cap_fmt": _fmt_cap(max_sales),
             "pct": _pct(used_sales, max_sales)},
        ],
    }


def _get_tariff_plans(current_plan_name: str | None, user_id: int | None = None) -> list[dict]:
    """Доступные тарифы (ось «объём») из subscription_plans для смены прямо из веба.
    Только активные платные планы; текущий помечается is_current.

    Понижение тарифа определяется канонической логикой бота
    (Database.check_subscription_downgrade) — без дублирования enforcement:
    помеченные is_downgrade тарифы предупреждают владельца, что остаток
    текущей подписки сгорит при немедленной замене."""
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        try:
            rows = conn.execute(
                """SELECT id, name, duration_days, price, description,
                          max_products, max_shops, max_sales_per_month
                   FROM subscription_plans
                   WHERE is_active=1 AND price > 0
                   ORDER BY price"""
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []

    # Канонический downgrade-чек из бота (Database.check_subscription_downgrade).
    _dg_db = None
    if user_id:
        try:
            from database import Database
            _dg_db = Database(SHOP_BOT_DB)
        except Exception:
            _dg_db = None

    try:
        result = []
        for r in rows:
            pid, name, duration, price, desc, mp, ms, msl = r
            _price = int(price or 0)
            is_downgrade = False
            dg_current_plan = ""
            dg_end_date = ""
            dg_days_left = None
            if _dg_db is not None:
                try:
                    _is_dg, _info = _dg_db.check_subscription_downgrade(user_id, name)
                    if _is_dg and _info:
                        from datetime import datetime as _dt
                        is_downgrade = True
                        dg_current_plan = _info.get("current_plan") or ""
                        _end = _info.get("end_datetime")
                        if _end is not None:
                            dg_end_date = _end.strftime("%d.%m.%Y")
                            dg_days_left = max(0, (_end - _dt.now()).days)
                except Exception:
                    pass
            result.append({
                "id": pid,
                "name": name,
                "duration_days": int(duration or 30),
                "price": _price,
                "price_fmt": f"{_price:,}".replace(",", "\u00a0") + "\u00a0₽",
                "description": desc or "",
                "max_products_fmt": _fmt_cap(mp),
                "max_shops_fmt": _fmt_cap(ms),
                "max_sales_fmt": _fmt_cap(msl),
                "is_current": bool(current_plan_name and name == current_plan_name),
                "is_downgrade": is_downgrade,
                "downgrade_current_plan": dg_current_plan,
                "downgrade_end_date": dg_end_date,
                "downgrade_days_left": dg_days_left,
            })
        return result
    finally:
        if _dg_db is not None:
            try:
                _dg_db.get_connection().close()
            except Exception:
                pass


def _get_item_price(plan_type: str) -> int | None:
    """Возвращает реальную цену из БД по plan_type (module_/bundle_/extension_).
    Используется вместо клиентского amount, чтобы пользователь не мог подделать сумму."""
    # Addon prices are hardcoded (matching database.py confirm_payment_request logic)
    _ADDON_PRICES: dict[str, int] = {
        "addon_shops_1": 150,
        "addon_products_1": 100,
        "addon_sales_1": 200,
    }
    if plan_type in _ADDON_PRICES:
        return _ADDON_PRICES[plan_type]

    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        try:
            price = None
            if plan_type.startswith("module_"):
                key = plan_type[7:]
                # module_annual_<key> → годовая цена; иначе месячная
                _col = "price_monthly"
                if key.startswith("annual_"):
                    key = key[len("annual_"):]
                    _col = "COALESCE(price_annual,0)"
                row = conn.execute(
                    f"SELECT {_col} FROM billing_modules WHERE key=? AND is_active=1 LIMIT 1",
                    (key,),
                ).fetchone()
                if row:
                    price = int(row[0] or 0)
            elif plan_type.startswith("bundle_"):
                key = plan_type[7:]
                _col = "price_monthly"
                if key.startswith("annual_"):
                    key = key[len("annual_"):]
                    _col = "COALESCE(price_annual,0)"
                row = conn.execute(
                    f"SELECT {_col} FROM billing_bundles WHERE key=? AND is_active=1 LIMIT 1",
                    (key,),
                ).fetchone()
                if row:
                    price = int(row[0] or 0)
            elif plan_type.startswith("extension_"):
                key = plan_type[10:]
                _col = "price_monthly"
                if key.startswith("annual_"):
                    key = key[len("annual_"):]
                    _col = "COALESCE(price_annual,0)"
                row = conn.execute(
                    f"SELECT {_col} FROM billing_extensions WHERE key=? AND is_active=1 LIMIT 1",
                    (key,),
                ).fetchone()
                if row:
                    price = int(row[0] or 0)
        finally:
            conn.close()
        return price
    except Exception:
        return None


def _get_addon_options(excluded_dims: set | None = None) -> list[dict]:
    """Доп. объёмные аддоны (+магазины, +товары, +продажи) с реальными ценами.

    excluded_dims — набор строк {'products', 'shops', 'sales'}:
    если лимит по измерению безлимитный (∞), аддон по нему скрывается."""
    _skip = excluded_dims or set()
    candidates = [
        ("addon_products_1", "+100 товаров", "🛍", "Увеличить лимит товаров на 100 единиц", "products"),
        ("addon_shops_1", "+1 магазин", "🏪", "Добавить ещё один магазин к тарифу", "shops"),
        ("addon_sales_1", "+500 продаж/мес", "🧾", "Увеличить лимит продаж на 500 в месяц", "sales"),
    ]
    opts = []
    for plan_type, label, icon, desc, dim in candidates:
        if dim in _skip:
            continue
        price = _get_item_price(plan_type)
        if price is not None and price > 0:
            opts.append({
                "plan_type": plan_type,
                "label": label,
                "icon": icon,
                "description": desc,
                "price": price,
                "price_fmt": f"{price:,}".replace(",", "\u00a0") + "\u00a0₽",
            })
    return opts


def _get_tariff_plan_by_name(name: str) -> dict | None:
    """Валидация: активный платный тариф с таким именем (для заявки из веба)."""
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        try:
            row = conn.execute(
                """SELECT name, price FROM subscription_plans
                   WHERE name=? AND is_active=1 AND price > 0""",
                (name,),
            ).fetchone()
        finally:
            conn.close()
        if row:
            return {"name": row[0], "price": int(row[1] or 0)}
        return None
    except Exception:
        return None


def _get_payment_requisites() -> str:
    """Возвращает реквизиты оплаты из payment_settings."""
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        try:
            row = conn.execute(
                "SELECT value FROM payment_settings WHERE key='card_number'"
            ).fetchone()
        finally:
            conn.close()
        return row[0] if row and row[0] else ""
    except Exception:
        return ""


def _load_shop_bot_data(telegram_id: int) -> dict:
    """Загружает ВСЕ данные из shop_bot.db за ОДНО соединение.

    Возвращает словарь: user_id, modules, bundles, extensions, mmap,
    requisites, user_mod_subs, has_pending, history, history_has_more, trial.
    При любой ошибке — возвращает пустую структуру, страница не падает."""
    _EMPTY: dict = {
        "user_id": None,
        "modules": [],
        "bundles": [],
        "extensions": [],
        "mmap": {},
        "requisites": "",
        "user_mod_subs": {},
        "has_pending": False,
        "pending_plan_types": set(),
        "monthly_total": 0,
        "monthly_total_fmt": "0\u00a0₽",
        "history": [],
        "history_has_more": False,
        "trial": None,
    }
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        try:
            # user_id
            _uid_row = conn.execute(
                "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
            user_id = _uid_row[0] if _uid_row else None

            # modules
            mod_rows = conn.execute(
                """SELECT key, name, icon, description, price_monthly, features_json,
                          COALESCE(price_annual,0)
                   FROM billing_modules WHERE is_active=1
                   ORDER BY sort_order, id"""
            ).fetchall()

            # bundles
            bun_rows = conn.execute(
                """SELECT key, name, icon, description, includes_json, price_monthly,
                          COALESCE(price_annual,0)
                   FROM billing_bundles WHERE is_active=1
                   ORDER BY sort_order, id"""
            ).fetchall()

            # extensions
            ext_rows = conn.execute(
                """SELECT module_key, key, name, icon, description, price_monthly
                   FROM billing_extensions WHERE is_active=1
                   ORDER BY module_key, sort_order, id"""
            ).fetchall()

            # requisites
            req_row = conn.execute(
                "SELECT value FROM payment_settings WHERE key='card_number'"
            ).fetchone()

            # user billing_module_subs — включаем истёкшие до 90 дней назад
            sub_rows = conn.execute(
                """SELECT item_key, item_type, end_date FROM billing_module_subs
                   WHERE user_telegram_id=? AND is_active=1
                     AND (end_date IS NULL OR end_date > datetime('now', '-90 days'))""",
                (telegram_id,),
            ).fetchall()

            # user-specific: pending, history, trial (only if user_id known)
            pending_pt_rows = []
            hist_total = 0
            hist_rows = []
            trial_row = None
            if user_id:
                pending_pt_rows = conn.execute(
                    "SELECT plan_type FROM payment_requests WHERE user_id=? AND status='pending'",
                    (user_id,),
                ).fetchall()
                hist_total = conn.execute(
                    "SELECT COUNT(*) FROM payment_requests WHERE user_id=?", (user_id,)
                ).fetchone()[0]
                hist_rows = conn.execute(
                    """SELECT id, plan_type, amount, status, created_at, processed_at
                       FROM payment_requests WHERE user_id=?
                       ORDER BY created_at DESC LIMIT 10""",
                    (user_id,),
                ).fetchall()
                trial_row = conn.execute(
                    """SELECT end_date FROM subscriptions
                       WHERE user_id=? AND is_trial=1 AND end_date > datetime('now')
                       ORDER BY end_date DESC LIMIT 1""",
                    (user_id,),
                ).fetchone()
        finally:
            conn.close()

        # ── Process modules ──
        modules: list[dict] = []
        for r in mod_rows:
            key, name, icon, desc, price, feats_json, price_annual = r
            try:
                features = json.loads(feats_json or "[]")
            except Exception:
                features = []
            _icon = icon or "🔧"
            item: dict = {
                "key": key,
                "name": name,
                "name_display": _strip_leading_emoji(name, _icon),
                "icon": _icon,
                "description": desc or "",
                "price_monthly": int(price or 0),
                "price_fmt": f"{int(price or 0):,}".replace(",", "\u00a0") + "\u00a0₽/мес.",
                "features": features,
            }
            item.update(_annual_fields(int(price or 0), int(price_annual or 0)))
            modules.append(item)

        # ── Process bundles ──
        bundles: list[dict] = []
        for r in bun_rows:
            key, name, icon, desc, inc_json, price, price_annual = r
            try:
                includes = json.loads(inc_json or '{"modules":[],"extensions":[]}')
            except Exception:
                includes = {"modules": [], "extensions": []}
            item = {
                "key": key,
                "name": name,
                "icon": icon or "📦",
                "description": desc or "",
                "includes": includes,
                "modules_count": len(includes.get("modules", [])),
                "price_monthly": int(price or 0),
                "price_fmt": f"{int(price or 0):,}".replace(",", "\u00a0") + "\u00a0₽/мес.",
            }
            item.update(_annual_fields(int(price or 0), int(price_annual or 0)))
            bundles.append(item)

        # ── Process extensions ──
        extensions: list[dict] = []
        for r in ext_rows:
            module_key, key, name, icon, desc, price = r
            _icon = icon or "🔧"
            extensions.append({
                "module_key": module_key,
                "key": key,
                "name": name,
                "name_display": _strip_leading_emoji(name, _icon),
                "icon": _icon,
                "description": desc or "",
                "price_monthly": int(price or 0),
                "price_fmt": f"{int(price or 0):,}".replace(",", "\u00a0") + "\u00a0₽/мес.",
            })

        # Requisites
        requisites = req_row[0] if req_row and req_row[0] else ""

        # Module subs with days_remaining (включая недавно истёкшие)
        today = _date.today()
        user_mod_subs: dict = {}
        for r in sub_rows:
            end_str = str(r[2] or "")[:10]
            days_remaining = None
            is_expired = False
            if end_str:
                try:
                    from datetime import datetime as _dt
                    end_d = _dt.strptime(end_str, "%Y-%m-%d").date()
                    diff = (end_d - today).days
                    if diff < 0:
                        is_expired = True
                        days_remaining = diff  # отрицательное
                    else:
                        days_remaining = diff
                except Exception:
                    pass
            user_mod_subs[r[0]] = {
                "item_type": r[1],
                "end_date": _fmt_sub_date(end_str) if end_str else "∞",
                "days_remaining": days_remaining,
                "is_expired": is_expired,
            }

        pending_plan_types: set = {r[0] for r in pending_pt_rows if r[0]}
        has_pending = bool(pending_plan_types)

        # History rows
        _STATUS_LABELS = {
            "pending": ("⏳ Ожидает", "text-amber-600 bg-amber-50"),
            "approved": ("✅ Подтверждено", "text-emerald-600 bg-emerald-50"),
            "rejected": ("❌ Отклонено", "text-red-600 bg-red-50"),
            "cancelled": ("🚫 Отозвано", "text-slate-500 bg-slate-100"),
        }
        history: list[dict] = []
        for row in hist_rows:
            st = row[3] or "pending"
            label, css = _STATUS_LABELS.get(st, (st, "text-slate-500 bg-slate-50"))
            history.append({
                "id": row[0],
                "plan_type": row[1],
                "plan_label": _plan_type_label(row[1] or ""),
                "amount": int(row[2] or 0),
                "status": st,
                "status_label": label,
                "status_css": css,
                "created_at": str(row[4] or "")[:16].replace("T", " "),
                "processed_at": str(row[5] or "")[:16].replace("T", " ") if row[5] else "—",
            })
        history_has_more = hist_total > 10

        # Trial
        trial = None
        if trial_row:
            trial = {"end_date": _fmt_sub_date(str(trial_row[0] or "")[:10]), "is_trial": True}

        monthly_total = _calc_monthly_total(user_mod_subs, modules, extensions, bundles)
        if monthly_total > 0:
            monthly_total_fmt = f"{monthly_total:,}".replace(",", "\u00a0") + "\u00a0₽/мес."
        else:
            monthly_total_fmt = "0\u00a0₽"

        return {
            "user_id": user_id,
            "modules": modules,
            "bundles": bundles,
            "extensions": extensions,
            "mmap": _modules_map(modules, extensions),
            "requisites": requisites,
            "user_mod_subs": user_mod_subs,
            "has_pending": has_pending,
            "pending_plan_types": pending_plan_types,
            "monthly_total": monthly_total,
            "monthly_total_fmt": monthly_total_fmt,
            "history": history,
            "history_has_more": history_has_more,
            "trial": trial,
        }
    except Exception:
        return _EMPTY


@router.get("/subscription")
def subscription_page(request: Request, msg: str = "", tab: str = "modules", need: str = ""):
    from web.auth import get_session_user, get_csrf_token
    from billing_utils import get_active_billing_items

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "super_admin"):
        _dest = f"/dashboard?msg={msg}" if msg else "/dashboard"
        return RedirectResponse(url=_dest, status_code=302)

    telegram_id = int(user["sub"])

    # Все запросы к shop_bot.db в одном соединении
    data = _load_shop_bot_data(telegram_id)

    need = need.strip()[:64]
    need_ext = next((e for e in data["extensions"] if e["key"] == need), None) if need else None

    if user.get("role") == "super_admin":
        return request.app.state.templates.TemplateResponse(
            request,
            "subscription/index.html",
            {
                "request": request,
                "user": user,
                "trial": None,
                "msg": msg,
                "csrf_token": get_csrf_token(request),
                "modules": data["modules"],
                "bundles": data["bundles"],
                "extensions": data["extensions"],
                "modules_map": data["mmap"],
                "user_mod_subs": {"*": {"item_type": "all", "end_date": "∞"}},
                "active_items": {"modules": ["*"], "extensions": ["*"], "bundles": ["*"]},
                "has_pending": False,
                "pending_plan_types": set(),
                "monthly_total": 0,
                "monthly_total_fmt": "0\u00a0₽",
                "history": [],
                "history_has_more": False,
                "requisites": data["requisites"],
                "tariff": None,
                "tariff_plans": [],
                "need_ext": need_ext,
                "addon_options": [],
                "expiring_items": [],
            },
        )

    user_id = data["user_id"]
    trial = data["trial"]
    tariff = _get_tariff_overview(telegram_id)
    tariff_plans = _get_tariff_plans(tariff.get("plan_name") if tariff else None, user_id)
    try:
        active_items = get_active_billing_items(telegram_id)
        active_items = {k: list(v) for k, v in active_items.items()}
    except Exception:
        active_items = {"modules": [], "extensions": [], "bundles": []}

    # Free modules (price_monthly=0) are always accessible without a billing
    # record — ensure they appear as active so their extension groups show
    # "N доступно" instead of "⚠ нет модуля".
    if "*" not in active_items.get("modules", []):
        for _fm in data.get("modules", []):
            if _fm.get("price_monthly", 1) == 0:
                _fk = _fm.get("key", "")
                if _fk and _fk not in active_items["modules"]:
                    active_items["modules"].append(_fk)

    # Expiring items: active billing subs with ≤14 days remaining,
    # excluding items that already have a pending payment request.
    # Hidden when super_admin has wildcard "*" access (no billing needed).
    expiring_items: list[dict] = []
    if not data["has_pending"] and "*" not in active_items.get("modules", []):
        _mod_map = {m["key"]: m for m in data["modules"]}
        _ext_map = {e["key"]: e for e in data["extensions"]}
        _bun_map = {b["key"]: b for b in data["bundles"]}
        for _ek, _sub in data["user_mod_subs"].items():
            _dr = _sub.get("days_remaining")
            # Пропускаем: нет даты, истёк уже (< 0), или ещё далеко (> 14 дн.)
            if _dr is None or _dr < 0 or _dr > 14:
                continue
            _itype = _sub.get("item_type", "")
            _idata = (
                _mod_map.get(_ek) if _itype == "module" else
                _ext_map.get(_ek) if _itype == "extension" else
                _bun_map.get(_ek) if _itype == "bundle" else None
            )
            if not _idata or _idata.get("price_monthly", 0) <= 0:
                continue
            _pt = f"{_itype}_{_ek}"
            if _pt in data["pending_plan_types"]:
                continue
            expiring_items.append({
                "key": _ek,
                "type": _itype,
                "name": _idata.get("name_display") or _idata.get("name", _ek),
                "price_monthly": int(_idata.get("price_monthly", 0)),
                "price_annual": int(_idata.get("price_annual", 0)),
                "has_annual": bool(_idata.get("has_annual", False)),
                "days_remaining": int(_dr),
            })

    is_free_plan = not tariff or tariff.get("plan_name") in (None, "Бесплатный", "")
    addon_all_unlimited = False
    addon_options = []
    if not is_free_plan and tariff:
        _unlimited_dims: set = set()
        _dim_map = {"Товар": "products", "Магазин": "shops", "Продаж": "sales"}
        for _r in tariff.get("rows", []):
            _cap = _r.get("cap")
            try:
                if int(_cap) < 0:
                    for _kw, _dim in _dim_map.items():
                        if _kw in _r.get("label", ""):
                            _unlimited_dims.add(_dim)
            except Exception:
                pass
        addon_all_unlimited = len(_unlimited_dims) >= 3
        if not addon_all_unlimited:
            addon_options = _get_addon_options(_unlimited_dims)

    return request.app.state.templates.TemplateResponse(
        request,
        "subscription/index.html",
        {
            "request": request,
            "user": user,
            "trial": trial,
            "msg": msg,
            "csrf_token": get_csrf_token(request),
            "modules": data["modules"],
            "bundles": data["bundles"],
            "extensions": data["extensions"],
            "modules_map": data["mmap"],
            "user_mod_subs": data["user_mod_subs"],
            "active_items": active_items,
            "has_pending": data["has_pending"],
            "pending_plan_types": data["pending_plan_types"],
            "monthly_total": data["monthly_total"],
            "monthly_total_fmt": data["monthly_total_fmt"],
            "history": data["history"],
            "history_has_more": data["history_has_more"],
            "requisites": data["requisites"],
            "tariff": tariff,
            "tariff_plans": tariff_plans,
            "need_ext": need_ext,
            "addon_options": addon_options,
            "addon_all_unlimited": addon_all_unlimited,
            "expiring_items": expiring_items,
        },
    )


@router.post("/subscription/batch-request")
def subscription_batch_request(
    request: Request,
    plan_types_json: str = Form(...),
    csrf_token: str = Form(default=""),
):
    """Пакетная заявка: несколько модулей/расширений/пакетов за один раз.

    Клиент передаёт JSON-массив plan_type строк в скрытом поле plan_types_json.
    Каждый элемент валидируется и получает отдельную строку в payment_requests,
    но админу уходит одно сводное уведомление."""
    from web.auth import get_session_user, verify_csrf_token

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/subscription?msg=csrf_error", status_code=303)

    try:
        raw_list = json.loads(plan_types_json)
    except Exception:
        return RedirectResponse(url="/subscription?msg=invalid_plan", status_code=303)

    if not isinstance(raw_list, list) or not raw_list:
        return RedirectResponse(url="/subscription?msg=invalid_plan", status_code=303)

    valid_prefixes = ("module_", "bundle_", "extension_", "addon_")
    seen_keys: set = set()
    validated: list = []  # list of (plan_type, amount)

    for pt in raw_list[:20]:  # hard cap — 20 позиций за раз
        pt = str(pt).strip()
        if not any(pt.startswith(p) for p in valid_prefixes):
            continue
        if pt in seen_keys:
            continue
        seen_keys.add(pt)
        amount = _get_item_price(pt)
        if amount is None:
            continue
        # Годовая цена = 0 значит цена не задана → пропускаем
        if "_annual_" in pt and not amount:
            continue
        validated.append((pt, amount))

    if not validated:
        return RedirectResponse(url="/subscription?msg=invalid_plan", status_code=303)

    telegram_id = int(user["sub"])
    user_id = _get_user_id_in_shop_bot(telegram_id)
    if not user_id:
        return RedirectResponse(url="/subscription?msg=user_not_found", status_code=303)

    # Пропускаем позиции, по которым уже есть pending-заявка
    existing_pending = _get_pending_plan_types(user_id)
    to_create = [(pt, amt) for pt, amt in validated if pt not in existing_pending]

    if not to_create:
        # Все выбранные позиции уже ожидают обработки
        return RedirectResponse(url="/subscription?msg=already_pending", status_code=303)

    # Создаём все payment_requests в одной транзакции
    created_ids: list = []
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        try:
            for pt, amt in to_create:
                cur = conn.execute(
                    """INSERT INTO payment_requests (user_id, plan_type, amount, payment_proof_file_id)
                       VALUES (?, ?, ?, 'web_module_request')""",
                    (user_id, pt, amt),
                )
                created_ids.append(cur.lastrowid)
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logging.error("subscription_batch_request error: %s", exc)
        return RedirectResponse(url="/subscription?msg=error", status_code=303)

    # Одно уведомление администратору
    user_display = user.get("first_name", user.get("email", "—"))
    if len(to_create) == 1:
        _notify_admin_async(to_create[0][0], to_create[0][1], user_display, telegram_id)
    else:
        total_amount = sum(amt for _, amt in to_create)
        _notify_admin_batch_async(
            [pt for pt, _ in to_create], total_amount, user_display, telegram_id
        )

    first_req_id = created_ids[0] if created_ids else ""
    batch_count = len(to_create)
    total_amount = sum(amt for _, amt in to_create)
    return RedirectResponse(
        url=f"/subscription?msg=module_request_sent&req_id={first_req_id}&batch_count={batch_count}&total_amount={total_amount}",
        status_code=303,
    )


@router.post("/subscription/cancel-request")
def subscription_cancel_request(
    request: Request,
    plan_type: str = Form(...),
    item_name: str = Form(default=""),
    csrf_token: str = Form(default=""),
):
    """Клиентская заявка на отключение модуля/расширения/пакета."""
    from web.auth import get_session_user, verify_csrf_token

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/subscription?msg=csrf_error", status_code=303)

    telegram_id = int(user["sub"])
    token = os.environ.get("BOT_TOKEN", "")
    admin_id = os.environ.get("ADMIN_CHAT_ID", "")
    if token and admin_id:
        user_display = user.get("first_name", user.get("email", "—"))
        safe_name = item_name or _plan_type_label(plan_type)
        text = (
            "❌ <b>Запрос на отключение!</b>\n\n"
            f"👤 <b>Пользователь:</b> {_html.escape(str(user_display))}\n"
            f"🆔 <b>Telegram ID:</b> {telegram_id}\n"
            f"📦 <b>Позиция:</b> {_html.escape(safe_name)}\n\n"
            "Пожалуйста, отключите доступ вручную в <b>/admin/billing/grants</b>."
        )
        payload = json.dumps({"chat_id": admin_id, "text": text, "parse_mode": "HTML"}).encode()
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=4):
                pass
        except TimeoutError as exc:
            logging.warning("subscription_cancel_request notify timeout: %s", exc)
        except Exception as exc:
            logging.warning("subscription_cancel_request notify: %s", exc)

    tab = "modules"
    if plan_type.startswith("bundle_"):
        tab = "bundles"
    elif plan_type.startswith("extension_"):
        tab = "extensions"
    return RedirectResponse(url=f"/subscription?tab={tab}&msg=cancel_request_sent", status_code=303)


@router.post("/subscription/withdraw-request")
def subscription_withdraw_request(
    request: Request,
    request_id: int = Form(...),
    csrf_token: str = Form(default=""),
):
    """Отзыв pending-заявки владельцем из вкладки История."""
    from web.auth import get_session_user, verify_csrf_token

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/subscription?tab=history&msg=csrf_error", status_code=303)

    telegram_id = int(user["sub"])
    user_id = _get_user_id_in_shop_bot(telegram_id)
    if not user_id:
        return RedirectResponse(url="/subscription?tab=history&msg=user_not_found", status_code=303)

    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        try:
            row = conn.execute(
                "SELECT id, plan_type, user_id FROM payment_requests WHERE id = ? AND status = 'pending'",
                (request_id,),
            ).fetchone()
            if not row or row[2] != user_id:
                return RedirectResponse(url="/subscription?tab=history&msg=not_found", status_code=303)
            plan_type = row[1] or ""
            conn.execute(
                "UPDATE payment_requests SET status = 'cancelled', processed_at = datetime('now') WHERE id = ?",
                (request_id,),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logging.error("subscription_withdraw_request error: %s", exc)
        return RedirectResponse(url="/subscription?tab=history&msg=error", status_code=303)

    # Notify admin
    token = os.environ.get("BOT_TOKEN", "")
    admin_id = os.environ.get("ADMIN_CHAT_ID", "")
    if token and admin_id:
        label = _plan_type_label(plan_type)
        user_display = user.get("first_name", user.get("email", "—"))
        text = (
            "🚫 <b>Заявка отозвана владельцем</b>\n\n"
            f"👤 <b>Пользователь:</b> {_html.escape(str(user_display))}\n"
            f"🆔 <b>Telegram ID:</b> {telegram_id}\n"
            f"📦 <b>Позиция:</b> {_html.escape(label)}\n\n"
            "Заявка больше не требует рассмотрения."
        )
        payload = json.dumps({"chat_id": admin_id, "text": text, "parse_mode": "HTML"}).encode()

        def _send() -> None:
            try:
                req = urllib.request.Request(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=4):
                    pass
            except Exception as exc2:
                logging.warning("subscription_withdraw_request notify: %s", exc2)

        threading.Thread(target=_send, daemon=True).start()

    return RedirectResponse(url="/subscription?tab=history&msg=request_withdrawn", status_code=303)


@router.post("/subscription/module-request")
def subscription_module_request(
    request: Request,
    plan_type: str = Form(...),
    csrf_token: str = Form(default=""),
):
    """Клиентская заявка на подключение модуля, расширения или пакета."""
    from web.auth import get_session_user, verify_csrf_token

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/subscription?msg=csrf_error", status_code=303)

    valid_prefixes = ("module_", "bundle_", "extension_", "addon_")
    if not any(plan_type.startswith(p) for p in valid_prefixes):
        return RedirectResponse(url="/subscription?msg=invalid_plan", status_code=303)

    amount = _get_item_price(plan_type)
    if amount is None:
        return RedirectResponse(url="/subscription?msg=invalid_plan", status_code=303)
    # Годовой период доступен только если у элемента реально задана годовая цена.
    # Иначе amount=0 создал бы заявку, которую можно подтвердить и выдать 365 дней бесплатно.
    if "_annual_" in plan_type and (amount is None or amount <= 0):
        return RedirectResponse(url="/subscription?msg=invalid_plan", status_code=303)

    telegram_id = int(user["sub"])
    user_id = _get_user_id_in_shop_bot(telegram_id)
    if not user_id:
        return RedirectResponse(url="/subscription?msg=user_not_found", status_code=303)

    def _tab_for(pt: str) -> str:
        if pt.startswith("bundle_"):
            return "bundles"
        if pt.startswith("extension_"):
            return "extensions"
        return "modules"

    if _has_pending_request(user_id):
        return RedirectResponse(
            url=f"/subscription?tab={_tab_for(plan_type)}&msg=already_pending",
            status_code=303,
        )

    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        try:
            cur = conn.execute(
                """INSERT INTO payment_requests (user_id, plan_type, amount, payment_proof_file_id)
                   VALUES (?, ?, ?, 'web_module_request')""",
                (user_id, plan_type, amount),
            )
            conn.commit()
            new_req_id = cur.lastrowid
        finally:
            conn.close()
        _notify_admin_async(
            plan_type, amount,
            user.get("first_name", user.get("email", "—")),
            telegram_id,
        )
        return RedirectResponse(
            url=f"/subscription?tab={_tab_for(plan_type)}&msg=module_request_sent&req_id={new_req_id}",
            status_code=303,
        )
    except Exception as exc:
        logging.error("subscription_module_request error: %s", exc)
        return RedirectResponse(url="/subscription?msg=error", status_code=303)


@router.post("/subscription/tariff-request")
def subscription_tariff_request(
    request: Request,
    plan_name: str = Form(...),
    csrf_token: str = Form(default=""),
):
    """Клиентская заявка на смену тарифа (ось «объём»).
    Использует тот же механизм, что и модули: создаёт payment_requests с
    plan_type = имя тарифа. Подтверждение супер-админом (бот/веб) идёт через
    штатный confirm_payment_request → create_subscription, без дублирования логики."""
    from web.auth import get_session_user, verify_csrf_token

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/subscription?msg=csrf_error", status_code=303)

    plan = _get_tariff_plan_by_name(plan_name)
    if not plan:
        return RedirectResponse(url="/subscription?msg=invalid_plan", status_code=303)

    telegram_id = int(user["sub"])
    user_id = _get_user_id_in_shop_bot(telegram_id)
    if not user_id:
        return RedirectResponse(url="/subscription?msg=user_not_found", status_code=303)

    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        try:
            cur = conn.execute(
                """INSERT INTO payment_requests (user_id, plan_type, amount, payment_proof_file_id)
                   VALUES (?, ?, ?, 'web_tariff_request')""",
                (user_id, plan["name"], plan["price"]),
            )
            conn.commit()
            new_req_id = cur.lastrowid
        finally:
            conn.close()
        _notify_admin_async(
            plan["name"], plan["price"],
            user.get("first_name", user.get("email", "—")),
            telegram_id,
        )
        return RedirectResponse(url=f"/subscription?msg=tariff_request_sent&req_id={new_req_id}", status_code=303)
    except Exception as exc:
        logging.error("subscription_tariff_request error: %s", exc)
        return RedirectResponse(url="/subscription?msg=error", status_code=303)


_PROOF_DIR = "data/payment_proofs"
_PROOF_ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".pdf"}
_PROOF_MAX_BYTES = 10 * 1024 * 1024  # 10 MB


def _notify_admin_proof_uploaded(req_id: int, user_display: str, telegram_id: int) -> None:
    """Уведомляет супер-админа о том, что пользователь прикрепил скриншот оплаты."""
    token = os.environ.get("BOT_TOKEN", "")
    admin_id = os.environ.get("ADMIN_CHAT_ID", "")
    if not token or not admin_id:
        return
    domain = (os.environ.get("APP_DOMAIN") or os.environ.get("REPLIT_DEV_DOMAIN") or "")
    proof_link = f"https://{domain}/payment-proof-req/{req_id}" if domain else ""
    text = (
        "📎 <b>Скриншот оплаты прикреплён</b>\n\n"
        f"👤 <b>Пользователь:</b> {_html.escape(str(user_display))}\n"
        f"🆔 <b>Telegram ID:</b> {telegram_id}\n"
        f"🗒 <b>Заявка №:</b> {req_id}\n"
    )
    if proof_link:
        text += f"\n🔗 <a href='{proof_link}'>Открыть скриншот</a>"
    text += "\n\nПосмотреть заявку: /pending_payments"
    payload = json.dumps({"chat_id": admin_id, "text": text, "parse_mode": "HTML"}).encode()
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=4):
            pass
    except Exception as exc:
        logging.warning("_notify_admin_proof_uploaded error: %s", exc)


@router.post("/subscription/upload-proof")
async def subscription_upload_proof(
    request: Request,
    req_id: int = Form(...),
    csrf_token: str = Form(default=""),
    proof_file: UploadFile = File(default=None),
):
    """Загрузка скриншота оплаты к существующей заявке."""
    from web.auth import get_session_user, verify_csrf_token

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/subscription?msg=csrf_error", status_code=303)

    if not proof_file or not proof_file.filename:
        return RedirectResponse(url="/subscription?msg=no_file", status_code=303)

    ext = Path(proof_file.filename).suffix.lower()
    if ext not in _PROOF_ALLOWED_EXTS:
        return RedirectResponse(url="/subscription?msg=bad_file_type", status_code=303)

    telegram_id = int(user["sub"])
    user_id = _get_user_id_in_shop_bot(telegram_id)
    if not user_id:
        return RedirectResponse(url="/subscription?msg=user_not_found", status_code=303)

    # Проверяем, что заявка принадлежит этому пользователю и ещё pending
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        try:
            row = conn.execute(
                "SELECT id, user_id, status FROM payment_requests WHERE id=?",
                (req_id,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        row = None

    if not row or row[1] != user_id or row[2] != "pending":
        return RedirectResponse(url="/subscription?msg=error", status_code=303)

    # Запоминаем старый файл (если был) — удалим после успешной замены
    old_proof = None
    try:
        conn2 = sqlite3.connect(SHOP_BOT_DB)
        try:
            r2 = conn2.execute(
                "SELECT payment_proof_file_id FROM payment_requests WHERE id=?",
                (req_id,),
            ).fetchone()
        finally:
            conn2.close()
        if r2 and r2[0] and r2[0].startswith("web_proof:"):
            old_proof = os.path.join(_PROOF_DIR, r2[0][len("web_proof:"):])
    except Exception:
        pass

    # Читаем файл (с проверкой размера)
    try:
        content = await proof_file.read()
    except Exception as exc:
        logging.error("upload_proof read error: %s", exc)
        return RedirectResponse(url="/subscription?msg=error", status_code=303)

    if len(content) > _PROOF_MAX_BYTES:
        return RedirectResponse(url="/subscription?msg=file_too_large", status_code=303)

    # Сохраняем
    os.makedirs(_PROOF_DIR, exist_ok=True)
    safe_name = f"{req_id}_{uuid.uuid4().hex[:10]}{ext}"
    save_path = os.path.join(_PROOF_DIR, safe_name)
    try:
        with open(save_path, "wb") as fh:
            fh.write(content)
    except Exception as exc:
        logging.error("upload_proof write error: %s", exc)
        return RedirectResponse(url="/subscription?msg=error", status_code=303)

    # Обновляем payment_proof_file_id
    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        try:
            conn.execute(
                "UPDATE payment_requests SET payment_proof_file_id=? WHERE id=?",
                (f"web_proof:{safe_name}", req_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logging.error("upload_proof db error: %s", exc)
        return RedirectResponse(url="/subscription?msg=error", status_code=303)

    # Удаляем старый файл скриншота (если был)
    if old_proof and os.path.isfile(old_proof):
        try:
            os.remove(old_proof)
        except Exception as exc:
            logging.warning("upload_proof: failed to remove old file %s: %s", old_proof, exc)

    # Уведомляем супер-админа
    threading.Thread(
        target=_notify_admin_proof_uploaded,
        args=(req_id, user.get("first_name", user.get("email", "—")), telegram_id),
        daemon=True,
    ).start()

    return RedirectResponse(url="/subscription?msg=proof_uploaded", status_code=303)


@router.get("/payment-proof/{filename}")
def payment_proof_serve(request: Request, filename: str):
    """Отдаёт скриншот оплаты — только владельцу заявки или супер-админу."""
    from web.auth import get_session_user
    from fastapi.responses import Response as _R

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    # Защита от path traversal: только безопасные символы + abspath-проверка
    if not re.match(r"^[\w\-\.]+$", filename) or ".." in filename:
        return _R(status_code=404)

    proof_dir_abs = os.path.abspath(_PROOF_DIR)
    file_path = os.path.abspath(os.path.join(_PROOF_DIR, filename))
    if not file_path.startswith(proof_dir_abs + os.sep):
        return _R(status_code=404)
    if not os.path.isfile(file_path):
        return _R(status_code=404)

    if user.get("role") != "super_admin":
        telegram_id = int(user["sub"])
        uid = _get_user_id_in_shop_bot(telegram_id)
        try:
            conn = sqlite3.connect(SHOP_BOT_DB)
            try:
                row = conn.execute(
                    "SELECT user_id FROM payment_requests WHERE payment_proof_file_id=?",
                    (f"web_proof:{filename}",),
                ).fetchone()
            finally:
                conn.close()
        except Exception:
            row = None
        if not row or row[0] != uid:
            return _R(status_code=403)

    return FileResponse(file_path)


@router.get("/payment-proof-req/{req_id}")
def payment_proof_by_req(request: Request, req_id: int):
    """Редирект на файл скриншота по номеру заявки — удобная ссылка для бота."""
    from web.auth import get_session_user
    from fastapi.responses import Response as _R

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") != "super_admin":
        return _R(status_code=403)

    try:
        conn = sqlite3.connect(SHOP_BOT_DB)
        try:
            row = conn.execute(
                "SELECT payment_proof_file_id FROM payment_requests WHERE id=?",
                (req_id,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        row = None

    if not row or not row[0].startswith("web_proof:"):
        return _R(status_code=404)

    filename = row[0][len("web_proof:"):]
    return RedirectResponse(url=f"/payment-proof/{filename}", status_code=302)
