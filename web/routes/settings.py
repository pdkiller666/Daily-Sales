import logging
import traceback

import hashlib
import os
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from timezone_utils import DEFAULT_TZ

_PROFILE_PHOTO_DIR = Path(__file__).parent.parent / "static" / "profile_photos"
_PROFILE_PHOTO_DIR.mkdir(parents=True, exist_ok=True)
_PHOTO_MAX_BYTES = 5 * 1024 * 1024  # 5 MB

router = APIRouter()
logger = logging.getLogger(__name__)


def _get_user_db_id(db, telegram_id: int) -> int | None:
    """Get internal user id from telegram_id."""
    try:
        import sqlite3
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


def _get_org_id_for_user(telegram_id: int) -> int | None:
    """Retrieve org_id from main.db for an active user."""
    try:
        import sqlite3
        conn = sqlite3.connect("data/main.db")
        try:
            row = conn.execute(
                "SELECT org_id FROM user_org_mapping WHERE telegram_id=? AND is_active=1",
                (telegram_id,)
            ).fetchone()
        finally:
            conn.close()
        return row[0] if row else None
    except Exception:
        return None


_EMAIL_ERROR_MSGS = {
    "invalid_email":       "Некорректный email-адрес.",
    "short_password":      "Пароль должен содержать не менее 8 символов.",
    "passwords_mismatch":  "Пароли не совпадают.",
    "email_taken":         "Этот email уже привязан к другому аккаунту.",
    "wrong_old_password":  "Текущий пароль введён неверно.",
    "no_cred":             "Email-аккаунт не найден. Сначала привяжите email.",
    "server_error":        "Ошибка сервера. Попробуйте позже.",
    "csrf":                "Ошибка безопасности. Обновите страницу.",
    "cannot_unlink":       "Нельзя отвязать email — это единственный способ входа в аккаунт.",
    "smtp_failed":         "Не удалось отправить письмо. Проверьте позже или обратитесь к администратору.",
}


@router.get("/settings")
def settings_page(request: Request, saved: str = "", profile_saved: str = "",
                  email_saved: str = "", email_error: str = "", email_sent: str = ""):
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    is_admin = user.get("role") in ("owner", "admin", "super_admin")
    is_super = user.get("role") == "super_admin"
    is_owner = user.get("role") in ("owner", "super_admin")

    ctx: dict = {
        "request": request, "user": user,
        "is_admin": is_admin,
        "is_super": is_super,
        "is_owner": is_owner,
        "csrf_token": get_csrf_token(request),
        "org_logo_path": "",
        "first_product_id": None,
        "notif_settings": None,
        "user_db_id": None,
        "saved": saved == "1",
        "profile_saved": profile_saved == "1",
        "email_saved": email_saved == "1",
        "email_sent": email_sent == "1",
        "email_error": _EMAIL_ERROR_MSGS.get(email_error, ""),
        "error": None,
        "scheduled_notifications": [],
        "notification_history": [],
        "chat_min_plan": None,
        "all_orgs": [],
        # invite block
        "org_id": None,
        "invite_code": "",
        "invite_deep_link": "",
        "invite_preset_role": None,
        "invite_preset_shop": None,
        "invite_shops": [],
        # org info (always pre-initialised so template never sees Undefined)
        "org_name": "—",
        "org_plan": "—",
        "org_plan_end": "",
        # timezone
        "tz_choices": {},
        "current_tz": DEFAULT_TZ,
        # profile
        "profile": {},
        # web credentials
        "web_cred": None,
        # AI weekly digest prefs (owners with ai_network_insights)
        "has_network_insights": False,
        "digest_prefs": {"weekday": 0, "hour_msk": 12},
        # tasks_pro — для показа авто-задача переключателя
        "tasks_pro": False,
    }

    try:
        from database import Database as _DB
        _shop_db = _DB('data/shop_bot.db')
        ctx["web_cred"] = _shop_db.get_web_credential_by_telegram_id(telegram_id)
    except Exception:
        pass

    try:
        import sqlite3 as _s3
        _ac = _s3.connect("data/main.db")
        try:
            _ac.execute(
                "CREATE TABLE IF NOT EXISTS apk_notif_prefs "
                "(telegram_id INTEGER PRIMARY KEY, enabled INTEGER DEFAULT 0)"
            )
            _ar = _ac.execute(
                "SELECT enabled FROM apk_notif_prefs WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
        finally:
            _ac.close()
        ctx["apk_notif_enabled"] = bool(_ar[0]) if _ar else False
    except Exception:
        ctx["apk_notif_enabled"] = False

    try:
        db = get_web_db(telegram_id, org_db)
        user_db_id = _get_user_db_id(db, telegram_id)
        ctx["user_db_id"] = user_db_id

        # Current timezone
        try:
            tz = db.get_user_timezone(telegram_id)
            if tz:
                ctx["current_tz"] = tz
        except Exception:
            pass

        # Profile data
        try:
            user_row = db.get_user(telegram_id)
            if user_row:
                ctx["profile"] = {
                    "first_name": user_row[2] or "",
                    "last_name": user_row[3] or "",
                    "middle_name": user_row[4] or "",
                    "phone": user_row[5] or "",
                    "email": user_row[6] or "",
                    "trade_network": user_row[7] or "",
                    "city": user_row[9] or "",
                    "profile_photo": "",
                }
                try:
                    _conn = db.get_connection()
                    try:
                        _r = _conn.execute(
                            "SELECT profile_photo FROM users WHERE telegram_id=?",
                            (telegram_id,)
                        ).fetchone()
                        ctx["profile"]["profile_photo"] = (_r[0] if _r else None) or ""
                    finally:
                        _conn.close()
                except Exception:
                    pass
        except Exception:
            pass

        if user_db_id:
            ctx["notif_settings"] = db.get_notification_settings(user_db_id)
        else:
            ctx["notif_settings"] = {
                "low_stock_alerts": True, "daily_reports": False,
                "sales_alerts": True, "payment_alerts": True,
                "admin_notifications": True, "stock_threshold": 5,
                "notification_time": "09:00", "shift_sale_alerts": True,
                "shift_reminders": True, "shift_remind_minutes": 0,
            }

        # Notification history for current user
        if user_db_id:
            try:
                hist_raw = db.get_notification_history(user_db_id, limit=15) or []
                ctx["notification_history"] = [
                    {
                        "type": h[2] or "",
                        "message": (h[3] or "")[:200],
                        "is_read": bool(h[4]),
                        "created_at": (h[5] or "")[:16].replace("T", " "),
                    }
                    for h in hist_raw
                ]
            except Exception:
                ctx["notification_history"] = []

        # AI weekly digest prefs (owners with ai_network_insights extension)
        if is_owner:
            try:
                from billing_utils import has_extension as _has_ext, has_module as _has_mod
                ctx["tasks_pro"] = _has_mod(telegram_id, "tasks_pro")
                ctx["has_network_insights"] = (
                    _has_mod(telegram_id, "ai_assistant") and
                    _has_ext(telegram_id, "ai_network_insights")
                )
                if ctx["has_network_insights"]:
                    from database import Database as _SBDb
                    _sb = _SBDb("data/shop_bot.db")
                    ctx["digest_prefs"] = _sb.get_network_digest_prefs(telegram_id)
            except Exception:
                ctx["has_network_insights"] = False

        # Email-missing warning for AI alert email toggles (admin only)
        if is_admin and org_db:
            try:
                from billing_utils import has_extension as _has_ext2, has_module as _has_mod2
                if _has_mod2(telegram_id, "ai_assistant") and _has_ext2(telegram_id, "ai_smart_alerts"):
                    from web.routes.ai_insights import _has_verified_admin_email
                    _ais_db = get_web_db(telegram_id, org_db)
                    _ais = _ais_db.get_ai_alert_settings()
                    if _ais and (_ais.get("alert_email_enabled") or _ais.get("digest_email_enabled")):
                        ctx["email_missing_warning"] = not _has_verified_admin_email(org_db, telegram_id)
            except Exception:
                pass

        # Scheduled notifications (admin only)
        if is_admin:
            raw_sched = db.get_scheduled_notifications(status=None) or []
            scheduled = []
            for sn in raw_sched:
                fname = (sn[9] if len(sn) > 9 else "") or ""
                lname = (sn[10] if len(sn) > 10 else "") or ""
                creator = f"{fname} {lname}".strip() or "—"
                scheduled.append({
                    "id": sn[0], "text": (sn[3] or "")[:120],
                    "recipients_type": sn[4] or "",
                    "scheduled_at": (sn[6] or "")[:16].replace("T", " "),
                    "status": sn[7] or "pending",
                    "creator": creator,
                    "created_at": (sn[8] or "")[:10],
                })
            ctx["scheduled_notifications"] = scheduled

        # Org info from main.db
        import sqlite3
        conn = sqlite3.connect("data/main.db")
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT o.id, o.name, o.subscription_plan, o.subscription_end, o.invite_code, "
                "o.invite_preset_role, o.invite_preset_shop "
                "FROM organizations o "
                "JOIN user_org_mapping m ON m.org_id = o.id "
                "WHERE m.telegram_id = ? AND m.is_active = 1 LIMIT 1",
                (telegram_id,)
            )
            org_row = cur.fetchone()
        finally:
            conn.close()

        if org_row:
            org_id = org_row[0]
            ctx["org_name"] = org_row[1] or "—"
            ctx["org_plan"] = org_row[2] or "—"
            ctx["org_plan_end"] = (org_row[3] or "")[:10]
            ctx["org_id"] = org_id

            # Ensure invite code exists (auto-generate if missing)
            invite_code = org_row[4]
            if not invite_code and is_admin:
                from tenant_manager import tenant_manager
                invite_code = tenant_manager.generate_invite_code(org_id)
            ctx["invite_code"] = invite_code or ""

            # Build deep link using bot username
            if invite_code:
                try:
                    import bot_holder
                    bot_uname = bot_holder.get_username() or ""
                except Exception:
                    bot_uname = ""
                if bot_uname:
                    ctx["invite_deep_link"] = f"https://t.me/{bot_uname}?start={invite_code}"

            ctx["invite_preset_role"] = org_row[5]
            ctx["invite_preset_shop"] = org_row[6]

            # Shops list for preset dropdown
            try:
                ctx["invite_shops"] = db.get_all_shops() or []
            except Exception:
                ctx["invite_shops"] = []
        else:
            ctx["org_name"] = "—"
            ctx["org_plan"] = "—"
            ctx["org_plan_end"] = ""

        # Org logo + first product for label design cards (owners only)
        if is_owner:
            try:
                ls = db.get_label_settings()
                ctx["org_logo_path"] = (ls or {}).get("org_logo_path", "")
            except Exception:
                ctx["org_logo_path"] = ""
            try:
                prods = db.get_all_products() or []
                ctx["first_product_id"] = prods[0][0] if prods else None
            except Exception:
                ctx["first_product_id"] = None
        else:
            ctx["org_logo_path"] = ""
            ctx["first_product_id"] = None

        ctx["chat_min_plan"] = None

        # All active orgs list for org switcher (super_admin only)
        if user.get("role") == "super_admin":
            try:
                import sqlite3 as _sqlite3
                _mc = _sqlite3.connect("data/main.db")
                try:
                    _orgs = _mc.execute(
                        "SELECT id, name, db_path FROM organizations WHERE is_active=1 ORDER BY name"
                    ).fetchall()
                finally:
                    _mc.close()
                ctx["all_orgs"] = [
                    {"id": r[0], "name": r[1] or f"Орг #{r[0]}", "db_path": r[2]}
                    for r in _orgs if r[2]
                ]
            except Exception:
                ctx["all_orgs"] = []

        # Beta mode flag (super_admin only)
        if user.get("role") == "super_admin":
            try:
                import sqlite3 as _sqlite3
                _sdb2 = "data/shop_bot.db"
                _c2 = _sqlite3.connect(_sdb2)
                try:
                    _row = _c2.execute(
                        "SELECT value FROM payment_settings WHERE key='beta_mode'"
                    ).fetchone()
                finally:
                    _c2.close()
                ctx["beta_mode_enabled"] = (_row is None or _row[0] != "0")
            except Exception:
                ctx["beta_mode_enabled"] = True
        else:
            ctx["beta_mode_enabled"] = None  # hide toggle

        # Email auth settings (super_admin only)
        if user.get("role") == "super_admin":
            try:
                from web.email_utils import is_configured as _smtp_ok
                ctx["smtp_configured"] = _smtp_ok()
            except Exception:
                ctx["smtp_configured"] = False
            try:
                import sqlite3 as _sqlite3
                _sdb5 = "data/shop_bot.db"
                _c5 = _sqlite3.connect(_sdb5)
                try:
                    _row5 = _c5.execute(
                        "SELECT value FROM payment_settings WHERE key='email_registration_enabled'"
                    ).fetchone()
                finally:
                    _c5.close()
                ctx["email_registration_enabled"] = (_row5 is None or _row5[0] != "0")
            except Exception:
                ctx["email_registration_enabled"] = True
            try:
                from database import Database as _DB
                _edb = _DB("data/shop_bot.db")
                _eu = _edb.get_all_web_credentials()
                ctx["email_users"] = _eu
                ctx["email_users_unverified"] = sum(1 for u in _eu if not u.get("email_verified"))
            except Exception:
                ctx["email_users"] = []
                ctx["email_users_unverified"] = 0
        else:
            ctx["smtp_configured"] = None
            ctx["email_registration_enabled"] = None
            ctx["email_users"] = []
            ctx["email_users_unverified"] = 0

        # Referral stats (from shop_bot.db)
        try:
            import sqlite3 as _sqlite3
            _sdb = "data/shop_bot.db"
            _conn = _sqlite3.connect(_sdb)
            try:
                _cur = _conn.cursor()
                _cur.execute("SELECT COUNT(*) FROM referrals WHERE referrer_telegram_id = ?", (telegram_id,))
                total_ref = (_cur.fetchone() or [0])[0]
                _cur.execute("SELECT COUNT(*) FROM referrals WHERE referrer_telegram_id = ? AND bonus_granted = 1", (telegram_id,))
                paid_ref = (_cur.fetchone() or [0])[0]
            finally:
                _conn.close()
            ctx["referral"] = {
                "total_referred": total_ref,
                "bonus_granted": paid_ref,
                "bonus_days": paid_ref * 30,
            }
        except Exception:
            ctx["referral"] = {"total_referred": 0, "bonus_granted": 0, "bonus_days": 0}

    except Exception as exc:
        ctx["error"] = "Произошла внутренняя ошибка. Попробуйте позже."

    try:
        return request.app.state.templates.TemplateResponse(
            request, "settings/index.html", ctx
        )
    except Exception as tmpl_exc:
        logger.error("settings template error: %s\n%s", tmpl_exc, traceback.format_exc())
        from fastapi.responses import HTMLResponse
        return HTMLResponse(
            f"<h1>Ошибка шаблона</h1><pre>{tmpl_exc}</pre>",
            status_code=500
        )


@router.post("/settings")
async def settings_save(
    request: Request,
    csrf_token: str = Form(default=""),
    low_stock_alerts: str = Form(default=""),
    daily_reports: str = Form(default=""),
    sales_alerts: str = Form(default=""),
    payment_alerts: str = Form(default=""),
    admin_notifications: str = Form(default=""),
    shift_sale_alerts: str = Form(default=""),
    shift_reminders: str = Form(default=""),
    shift_remind_minutes: int = Form(default=0),
    stock_threshold: int = Form(default=5),
    notification_time: str = Form(default="09:00"),
    auto_tasks_low_stock: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/settings", status_code=303)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    try:
        db = get_web_db(telegram_id, org_db)
        user_db_id = _get_user_db_id(db, telegram_id)
        if user_db_id:
            db.update_notification_settings(
                user_db_id,
                low_stock_alerts=1 if low_stock_alerts == "on" else 0,
                daily_reports=1 if daily_reports == "on" else 0,
                sales_alerts=1 if sales_alerts == "on" else 0,
                payment_alerts=1 if payment_alerts == "on" else 0,
                admin_notifications=1 if admin_notifications == "on" else 0,
                shift_sale_alerts=1 if shift_sale_alerts == "on" else 0,
                shift_reminders=1 if shift_reminders == "on" else 0,
                shift_remind_minutes=shift_remind_minutes if shift_remind_minutes in (0, 15, 30, 60) else 0,
                stock_threshold=max(0, stock_threshold),
                notification_time=notification_time or "09:00",
                auto_tasks_low_stock=1 if auto_tasks_low_stock == "on" else 0,
            )
    except Exception:
        pass

    return RedirectResponse(url="/settings?saved=1", status_code=303)


@router.post("/settings/timezone")
async def settings_timezone(
    request: Request,
    csrf_token: str = Form(default=""),
    timezone: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from timezone_utils import validate_timezone

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/settings", status_code=303)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    tz = timezone.strip()
    if tz and validate_timezone(tz):
        try:
            db = get_web_db(telegram_id, org_db)
            db.set_user_timezone(telegram_id, tz)
        except Exception:
            pass

    return RedirectResponse(url="/settings?saved=1", status_code=303)


@router.post("/settings/rotate_invite")
async def rotate_invite(request: Request, csrf_token: str = Form(default="")):
    from web.auth import get_session_user, verify_csrf_token

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/settings#invite", status_code=303)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/settings#invite", status_code=303)

    telegram_id = int(user["sub"])
    org_id = _get_org_id_for_user(telegram_id)
    if org_id:
        from tenant_manager import tenant_manager
        tenant_manager.rotate_invite_code(org_id)

    return RedirectResponse(url="/settings#invite", status_code=303)


@router.post("/settings/profile")
async def settings_profile(
    request: Request,
    csrf_token: str = Form(default=""),
    first_name: str = Form(default=""),
    last_name: str = Form(default=""),
    middle_name: str = Form(default=""),
    phone: str = Form(default=""),
    email: str = Form(default=""),
    trade_network: str = Form(default=""),
    city: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/settings#profile", status_code=303)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    try:
        db = get_web_db(telegram_id, org_db)
        db.update_user(
            telegram_id,
            first_name=first_name.strip() or None,
            last_name=last_name.strip() or None,
            middle_name=middle_name.strip() or None,
            phone=phone.strip() or None,
            email=email.strip() or None,
            trade_network=trade_network.strip() or None,
            city=city.strip() or None,
        )
    except Exception:
        pass

    return RedirectResponse(url="/settings?profile_saved=1#profile", status_code=303)


@router.post("/settings/profile-photo")
async def settings_profile_photo_upload(
    request: Request,
    csrf_token: str = Form(default=""),
    photo: UploadFile = File(default=None),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/settings?error=csrf#profile", status_code=303)
    if not photo or not photo.filename:
        return RedirectResponse(url="/settings?profile_saved=1#profile", status_code=303)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db") or ""
    try:
        data = await photo.read()
        if not data or len(data) > _PHOTO_MAX_BYTES:
            return RedirectResponse(url="/settings?profile_saved=1#profile", status_code=303)
        content_type = photo.content_type or ""
        if not content_type.startswith("image/"):
            return RedirectResponse(url="/settings?profile_saved=1#profile", status_code=303)
        ext = ".jpg"
        if "png" in content_type:
            ext = ".png"
        elif "webp" in content_type:
            ext = ".webp"
        fname = f"{telegram_id}{ext}"
        dest = _PROFILE_PHOTO_DIR / fname
        dest.write_bytes(data)
        photo_url = f"/static/profile_photos/{fname}"
        db = get_web_db(telegram_id, org_db)
        db.update_user_profile_photo(telegram_id, photo_url)
    except Exception as e:
        logger.error("profile_photo_upload: %s", e)

    return RedirectResponse(url="/settings?profile_saved=1#profile", status_code=303)


@router.post("/settings/profile-photo/delete")
async def settings_profile_photo_delete(
    request: Request,
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/settings?error=csrf#profile", status_code=303)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db") or ""
    try:
        db = get_web_db(telegram_id, org_db)
        row = db.get_connection()
        try:
            cur = row.execute("SELECT profile_photo FROM users WHERE telegram_id=?", (telegram_id,))
            rec = cur.fetchone()
        finally:
            row.close()
        if rec and rec[0]:
            old_path = _PROFILE_PHOTO_DIR.parent.parent / rec[0].lstrip("/")
            try:
                old_path.unlink(missing_ok=True)
            except Exception:
                pass
        db.update_user_profile_photo(telegram_id, None)
    except Exception as e:
        logger.error("profile_photo_delete: %s", e)

    return RedirectResponse(url="/settings?profile_saved=1#profile", status_code=303)


@router.post("/settings/save_invite_preset")
async def save_invite_preset(
    request: Request,
    csrf_token: str = Form(default=""),
    preset_role: str = Form(default=""),
    preset_shop: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/settings#invite", status_code=303)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/settings#invite", status_code=303)

    telegram_id = int(user["sub"])
    org_id = _get_org_id_for_user(telegram_id)
    if org_id:
        from tenant_manager import tenant_manager
        role_val = preset_role if preset_role else None
        shop_val = preset_shop if preset_shop else None
        tenant_manager.set_invite_preset(org_id, role_val, shop_val)

    return RedirectResponse(url="/settings#invite", status_code=303)



@router.post("/settings/apk-notif")
async def settings_apk_notif(
    request: Request,
    csrf_token: str = Form(default=""),
    apk_notif: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/settings", status_code=303)
    telegram_id = int(user["sub"])
    try:
        import sqlite3 as _s3
        c = _s3.connect("data/main.db")
        try:
            c.execute(
                "CREATE TABLE IF NOT EXISTS apk_notif_prefs "
                "(telegram_id INTEGER PRIMARY KEY, enabled INTEGER DEFAULT 0)"
            )
            c.execute(
                "INSERT OR REPLACE INTO apk_notif_prefs (telegram_id, enabled) VALUES (?, ?)",
                (telegram_id, 1 if apk_notif == "on" else 0),
            )
            c.commit()
        finally:
            c.close()
    except Exception:
        pass
    return RedirectResponse(url="/settings?saved=1", status_code=303)


@router.post("/settings/beta-mode")
async def settings_beta_mode(
    request: Request,
    csrf_token: str = Form(default=""),
    enabled: str = Form(default="0"),
):
    from web.auth import get_session_user, verify_csrf_token
    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/settings#system", status_code=303)
    if user.get("role") != "super_admin":
        return RedirectResponse(url="/settings", status_code=303)

    import sqlite3 as _sqlite3
    _sdb = "data/shop_bot.db"
    _conn = _sqlite3.connect(_sdb)
    try:
        _conn.execute(
            "INSERT OR REPLACE INTO payment_settings (key, value, updated_at) "
            "VALUES ('beta_mode', ?, datetime('now'))",
            ("1" if enabled == "1" else "0",),
        )
        _conn.commit()
    finally:
        _conn.close()
    return RedirectResponse(url="/settings#system", status_code=303)


@router.post("/settings/email-registration")
async def settings_email_registration(
    request: Request,
    csrf_token: str = Form(default=""),
    enabled: str = Form(default="0"),
):
    from web.auth import get_session_user, verify_csrf_token
    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/settings#email-auth", status_code=303)
    if user.get("role") != "super_admin":
        return RedirectResponse(url="/settings", status_code=303)

    import sqlite3 as _sqlite3
    _sdb = "data/shop_bot.db"
    _conn = _sqlite3.connect(_sdb)
    try:
        _conn.execute(
            "INSERT OR REPLACE INTO payment_settings (key, value, updated_at) "
            "VALUES ('email_registration_enabled', ?, datetime('now'))",
            ("1" if enabled == "1" else "0",),
        )
        _conn.commit()
    finally:
        _conn.close()
    return RedirectResponse(url="/settings#email-auth", status_code=303)


@router.post("/settings/ai-alerts")
async def settings_ai_alerts(
    request: Request,
    csrf_token: str = Form(default=""),
    enabled: str = Form(default=""),
    threshold_pct: int = Form(default=35),
    alert_hour_msk: int = Form(default=10),
    metric_revenue: str = Form(default=""),
    metric_avg_check: str = Form(default=""),
    metric_transactions: str = Form(default=""),
    context_products: str = Form(default=""),
    context_sellers: str = Form(default=""),
    context_plans: str = Form(default=""),
    digest_enabled: str = Form(default=""),
    digest_day_of_week: int = Form(default=0),
    digest_hour_msk: int = Form(default=9),
    digest_push_enabled: str = Form(default=""),
    alert_push_enabled: str = Form(default=""),
    alert_email_enabled: str = Form(default=""),
    digest_email_enabled: str = Form(default=""),
    next_url: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/settings", status_code=303)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/settings?error=csrf", status_code=303)

    threshold_pct = max(5, min(90, threshold_pct))
    alert_hour_msk = max(0, min(23, alert_hour_msk))
    digest_day_of_week = max(0, min(6, digest_day_of_week))
    digest_hour_msk = max(0, min(23, digest_hour_msk))
    metrics = []
    if metric_revenue:
        metrics.append("revenue")
    if metric_avg_check:
        metrics.append("avg_check")
    if metric_transactions:
        metrics.append("transactions")
    if not metrics:
        metrics = ["revenue"]

    digest_context = []
    if context_products:
        digest_context.append("products")
    if context_sellers:
        digest_context.append("sellers")
    if context_plans:
        digest_context.append("plans")
    if not digest_context:
        digest_context = ["products", "sellers", "plans"]

    try:
        db = get_web_db(int(user["sub"]), user.get("org_db"))
        db.save_ai_alert_settings(
            enabled=bool(enabled),
            threshold_pct=threshold_pct,
            alert_hour_msk=alert_hour_msk,
            metrics=metrics,
            digest_context=digest_context,
            digest_enabled=bool(digest_enabled),
            digest_day_of_week=digest_day_of_week,
            digest_hour_msk=digest_hour_msk,
            digest_push_enabled=bool(digest_push_enabled),
            alert_push_enabled=bool(alert_push_enabled),
            alert_email_enabled=bool(alert_email_enabled),
            digest_email_enabled=bool(digest_email_enabled),
        )
    except Exception:
        pass

    return RedirectResponse(url="/ai-insights?saved=1", status_code=303)


@router.post("/settings/ai-digest")
async def settings_ai_digest(
    request: Request,
    csrf_token: str = Form(default=""),
    digest_weekday: int = Form(default=0),
    digest_hour_msk: int = Form(default=12),
):
    from web.auth import get_session_user, verify_csrf_token
    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "super_admin"):
        return RedirectResponse(url="/settings", status_code=303)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url="/settings?error=csrf", status_code=303)

    digest_weekday = max(0, min(6, digest_weekday))
    digest_hour_msk = max(0, min(23, digest_hour_msk))

    try:
        from database import Database as _SBDb
        _sb = _SBDb("data/shop_bot.db")
        _sb.save_network_digest_prefs(int(user["sub"]), digest_weekday, digest_hour_msk)
    except Exception:
        pass

    return RedirectResponse(url="/settings?saved=1#ai-digest", status_code=303)


@router.get("/settings/backup")
def settings_backup(request: Request):
    """Download current org database file."""
    from web.auth import get_session_user
    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/settings", status_code=302)

    import os
    from fastapi.responses import FileResponse
    org_db = user.get("org_db") or ""
    if not org_db or not os.path.isfile(org_db):
        return RedirectResponse(url="/settings?error=backup_not_found", status_code=302)
    fname = os.path.basename(org_db)
    return FileResponse(org_db, media_type="application/octet-stream", filename=fname)
