"""
Web-маршруты модуля CRM (клиенты).
GET  /clients                        — список клиентов (с LTV/аналитикой)
GET  /clients/new                    — форма добавления
POST /clients/new                    — создать клиента
GET  /clients/duplicates             — поиск дублей по телефону
POST /clients/import                 — импорт из Excel
POST /clients/broadcast              — рассылка по тегу
POST /clients/rfm_refresh            — пересчёт RFM-тегов
GET  /clients/{id}                   — карточка клиента
POST /clients/{id}/edit              — редактировать
POST /clients/{id}/delete            — удалить
POST /clients/{id}/interactions      — добавить запись в лог взаимодействий
POST /clients/{id}/interactions/{iid}/delete — удалить запись лога
GET  /clients/export.xlsx            — экспорт Excel
"""
import io
import json
import logging
import re
from datetime import datetime, date

from fastapi import APIRouter, Form, Request, UploadFile, File
from fastapi.responses import RedirectResponse, Response, StreamingResponse

router = APIRouter()
logger = logging.getLogger(__name__)

_PAGE_SIZE = 50

_APPT_STATUS_RU = {
    "planned": "Запланирована",
    "confirmed": "Подтверждена",
    "completed": "Выполнена",
    "cancelled": "Отменена",
    "no_show": "Не пришёл",
}

_INTERACTION_TYPES = {
    "note": "📝 Заметка",
    "call": "📞 Звонок",
    "meeting": "🤝 Встреча",
    "email": "✉️ Email",
    "other": "💬 Другое",
}


def _get_ctx(request: Request):
    from web.auth import get_session_user, get_csrf_token
    user = get_session_user(request) or {}
    return {
        "user": user,
        "csrf_token": get_csrf_token(request),
    }


def _parse_tags(tags_json: str) -> list:
    try:
        v = json.loads(tags_json or "[]")
        return v if isinstance(v, list) else []
    except Exception:
        return []


def _format_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits[0] in ("7", "8"):
        return f"+7 ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"
    return phone


def _rfm_tag(recency_days: int, order_count: int, ltv: float) -> str:
    """Простая RFM-сегментация → один тег."""
    if order_count == 0:
        return "Спящий"
    if recency_days <= 30 and order_count >= 5:
        return "Чемпион"
    if recency_days <= 60 and order_count >= 3:
        return "Лояльный"
    if recency_days > 180:
        return "Уходящий"
    if order_count <= 1 and recency_days > 90:
        return "Спящий"
    return "Обычный"


@router.get("/clients")
def clients_list(request: Request, q: str = "", page: int = 1, tag: str = ""):
    from web.deps import get_web_db
    from web.auth import get_session_user
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    tg_id = int(user.get("sub", 0))
    if not has_module(tg_id, "crm"):
        return RedirectResponse("/subscription?msg=crm_locked", status_code=302)

    org_db = user.get("org_db", "")
    if not org_db:
        return RedirectResponse("/dashboard", status_code=302)
    db = get_web_db(tg_id, org_db)
    conn = db.get_connection()
    conn.create_function("lower_u", 1, lambda s: s.lower() if s else "")
    try:
        offset = (page - 1) * _PAGE_SIZE
        params: list = []
        where = "1=1"
        if q:
            where += " AND (lower_u(c.first_name||' '||c.last_name) LIKE ? OR c.phone LIKE ? OR c.email LIKE ?)"
            like = f"%{q.lower()}%"
            params += [like, like, like]
        if tag:
            where += " AND c.tags_json LIKE ?"
            params.append(f'%"{tag}"%')

        total = conn.execute(f"SELECT COUNT(*) FROM clients c WHERE {where}", params).fetchone()[0]

        # Аналитика: LTV из продаж + записей, кол-во, последний визит
        rows = conn.execute(
            f"""SELECT c.id, c.first_name, c.last_name, c.phone, c.email,
                       c.birth_date, c.tags_json, c.created_at,
                       COALESCE(s.ltv,0)+COALESCE(a.altv,0),
                       COALESCE(s.cnt,0)+COALESCE(a.acnt,0),
                       MAX(s.last_sale, a.last_appt)
                FROM clients c
                LEFT JOIN (
                    SELECT client_id,
                           SUM(quantity_sold*(sale_price or 0)) ltv,
                           COUNT(*) cnt,
                           MAX(sale_date) last_sale
                    FROM sales WHERE client_id IS NOT NULL GROUP BY client_id
                ) s ON s.client_id=c.id
                LEFT JOIN (
                    SELECT client_id,
                           SUM(price or 0) altv,
                           COUNT(*) acnt,
                           MAX(start_time) last_appt
                    FROM appointments WHERE client_id IS NOT NULL GROUP BY client_id
                ) a ON a.client_id=c.id
                WHERE {where}
                GROUP BY c.id
                ORDER BY c.created_at DESC LIMIT ? OFFSET ?""",
            params + [_PAGE_SIZE, offset],
        ).fetchall()

        today = date.today()
        clients = []
        all_tags = set()
        for r in rows:
            tags = _parse_tags(r[6])
            all_tags.update(tags)
            last_visit_str = r[10]
            days_since = None
            if last_visit_str:
                try:
                    lv = date.fromisoformat(str(last_visit_str)[:10])
                    days_since = (today - lv).days
                except Exception:
                    pass
            clients.append({
                "id": r[0], "first_name": r[1], "last_name": r[2],
                "phone": _format_phone(r[3]), "email": r[4],
                "birth_date": r[5], "tags": tags, "created_at": r[7],
                "ltv": float(r[8] or 0),
                "order_count": int(r[9] or 0),
                "days_since": days_since,
            })

        all_tags_list = sorted(all_tags)
        total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
        is_admin = user.get("role") in ("owner", "admin", "super_admin")

        ctx = _get_ctx(request)
        ctx.update({
            "clients": clients, "total": total, "q": q, "tag": tag,
            "page": page, "total_pages": total_pages,
            "all_tags": all_tags_list, "is_admin": is_admin,
            "flash": request.query_params.get("msg", ""),
        })
        return request.app.state.templates.TemplateResponse(request, "clients/index.html", ctx)
    finally:
        conn.close()


@router.get("/clients/new")
def clients_new_form(request: Request):
    from web.auth import get_session_user
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not has_module(int(user.get("sub", 0)), "crm"):
        return RedirectResponse("/clients", status_code=302)
    ctx = _get_ctx(request)
    ctx.update({"client": None, "error": request.query_params.get("error", "")})
    return request.app.state.templates.TemplateResponse(request, "clients/form.html", ctx)


@router.post("/clients/new")
def clients_create(
    request: Request,
    csrf_token: str = Form(""),
    first_name: str = Form(""),
    last_name: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    birth_date: str = Form(""),
    source: str = Form(""),
    notes: str = Form(""),
    tags: str = Form(""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/clients/new?error=csrf", status_code=302)
    if not has_module(int(user.get("sub", 0)), "crm"):
        return RedirectResponse("/clients", status_code=302)
    if not first_name.strip():
        return RedirectResponse("/clients/new?error=name_required", status_code=302)

    tags_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    tags_json = json.dumps(tags_list, ensure_ascii=False)
    birth_val = birth_date.strip() or None

    tg_id = int(user.get("sub", 0))
    org_db = user.get("org_db", "")
    if not org_db:
        return RedirectResponse("/dashboard", status_code=302)
    db = get_web_db(tg_id, org_db)
    conn = db.get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO clients (first_name, last_name, phone, email, birth_date, source, notes, tags_json, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (first_name.strip(), last_name.strip(), phone.strip(), email.strip(),
             birth_val, source.strip(), notes.strip(), tags_json,
             int(user.get("sub", 0))),
        )
        conn.commit()
        client_id = cur.lastrowid
        return RedirectResponse(f"/clients/{client_id}", status_code=303)
    finally:
        conn.close()


@router.get("/clients/duplicates")
def clients_duplicates(request: Request):
    """Клиенты с одинаковым нормализованным телефоном."""
    from web.auth import get_session_user
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    tg_id = int(user.get("sub", 0))
    if not has_module(tg_id, "crm"):
        return RedirectResponse("/clients", status_code=302)
    is_admin = user.get("role") in ("owner", "admin", "super_admin")

    org_db = user.get("org_db", "")
    if not org_db:
        return RedirectResponse("/dashboard", status_code=302)
    db = get_web_db(tg_id, org_db)
    conn = db.get_connection()
    conn.create_function("digits_only", 1, lambda s: re.sub(r"\D", "", s or "")[-10:] if s else "")
    try:
        rows = conn.execute(
            """SELECT digits_only(phone) dph, COUNT(*) cnt,
                      GROUP_CONCAT(id) ids,
                      GROUP_CONCAT(first_name||' '||COALESCE(last_name,'')) names
               FROM clients WHERE phone != '' AND phone IS NOT NULL
               GROUP BY dph HAVING cnt > 1 ORDER BY cnt DESC"""
        ).fetchall()
        groups = []
        for r in rows:
            ids = [int(x) for x in r[2].split(",")]
            names = r[3].split(",")
            clients = []
            for cid, cname in zip(ids, names):
                cr = conn.execute(
                    "SELECT id, first_name, last_name, phone, email, created_at FROM clients WHERE id=?",
                    (cid,)
                ).fetchone()
                if cr:
                    clients.append({
                        "id": cr[0], "name": f"{cr[1]} {cr[2] or ''}".strip(),
                        "phone": _format_phone(cr[3]), "email": cr[4] or "",
                        "created_at": str(cr[5] or "")[:10],
                    })
            groups.append({"phone_digits": r[0], "count": r[1], "clients": clients})
    finally:
        conn.close()

    ctx = _get_ctx(request)
    ctx.update({"groups": groups, "is_admin": is_admin})
    return request.app.state.templates.TemplateResponse(request, "clients/duplicates.html", ctx)


@router.post("/clients/import")
async def clients_import(
    request: Request,
    csrf_token: str = Form(""),
    file: UploadFile = File(...),
):
    """Массовый импорт клиентов из Excel (.xlsx)."""
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/clients?msg=csrf_error", status_code=302)
    tg_id = int(user.get("sub", 0))
    if not has_module(tg_id, "crm"):
        return RedirectResponse("/clients", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse("/clients", status_code=302)

    org_db = user.get("org_db", "")
    if not org_db:
        return RedirectResponse("/dashboard", status_code=302)

    try:
        import openpyxl
    except ImportError:
        return RedirectResponse("/clients?msg=no_openpyxl", status_code=302)

    content = await file.read()
    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        ws = wb.active
    except Exception:
        return RedirectResponse("/clients?msg=bad_file", status_code=302)

    db = get_web_db(tg_id, org_db)
    conn = db.get_connection()
    imported = 0
    skipped = 0
    try:
        headers = []
        for ri, row in enumerate(ws.iter_rows(values_only=True)):
            if ri == 0:
                headers = [str(c or "").strip().lower() for c in row]
                continue
            if not any(row):
                continue
            def _col(names):
                for n in names:
                    for hi, h in enumerate(headers):
                        if n in h and hi < len(row):
                            return str(row[hi] or "").strip()
                return ""
            first_name = _col(["имя", "first_name", "name"])
            last_name  = _col(["фамилия", "last_name"])
            phone      = _col(["телефон", "phone"])
            email      = _col(["email", "почта"])
            birth_date = _col(["рождени", "birth"])
            source     = _col(["источник", "source"])
            notes      = _col(["заметки", "notes", "примечани"])
            tags_raw   = _col(["теги", "tags"])

            if not first_name:
                skipped += 1
                continue
            tags_list = [t.strip() for t in tags_raw.split(",") if t.strip()]
            tags_json = json.dumps(tags_list, ensure_ascii=False)
            birth_val = birth_date[:10] if len(birth_date) >= 10 else (birth_date or None)
            try:
                conn.execute(
                    "INSERT INTO clients (first_name, last_name, phone, email, birth_date, source, notes, tags_json, created_by) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (first_name, last_name, phone, email, birth_val, source, notes, tags_json, tg_id),
                )
                imported += 1
            except Exception:
                skipped += 1
        conn.commit()
    finally:
        conn.close()

    return RedirectResponse(f"/clients?msg=import_ok_{imported}_{skipped}", status_code=303)


@router.post("/clients/broadcast")
def clients_broadcast(
    request: Request,
    csrf_token: str = Form(""),
    tag: str = Form(""),
    text: str = Form(""),
):
    """Рассылка Telegram-сообщения клиентам с нужным тегом (у кого есть telegram_id)."""
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/clients?msg=csrf_error", status_code=302)
    tg_id = int(user.get("sub", 0))
    if not has_module(tg_id, "crm"):
        return RedirectResponse("/clients", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse("/clients", status_code=302)
    if not text.strip():
        return RedirectResponse(f"/clients?msg=empty_text&tag={tag}", status_code=302)

    org_db = user.get("org_db", "")
    if not org_db:
        return RedirectResponse("/dashboard", status_code=302)
    db = get_web_db(tg_id, org_db)
    conn = db.get_connection()
    try:
        if tag:
            rows = conn.execute(
                "SELECT telegram_id, first_name FROM clients WHERE telegram_id IS NOT NULL AND tags_json LIKE ?",
                (f'%"{tag}"%',)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT telegram_id, first_name FROM clients WHERE telegram_id IS NOT NULL"
            ).fetchall()
    finally:
        conn.close()

    import os, threading, urllib.request as _ureq
    bot_token = os.environ.get("BOT_TOKEN", "")
    sent = 0
    if bot_token:
        def _send_all(rows, text, token):
            for row in rows:
                try:
                    payload = json.dumps({
                        "chat_id": row[0],
                        "text": text,
                        "parse_mode": "HTML",
                    }).encode()
                    req = _ureq.Request(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        data=payload,
                        headers={"Content-Type": "application/json"},
                    )
                    _ureq.urlopen(req, timeout=10)
                except Exception:
                    pass
        threading.Thread(target=_send_all, args=(rows, text.strip(), bot_token), daemon=True).start()
        sent = len(rows)
    return RedirectResponse(f"/clients?msg=broadcast_ok_{sent}", status_code=303)


@router.post("/clients/rfm_refresh")
def clients_rfm_refresh(request: Request, csrf_token: str = Form("")):
    """Пересчитать RFM-теги для всех клиентов."""
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse("/clients?msg=csrf_error", status_code=302)
    tg_id = int(user.get("sub", 0))
    if not has_module(tg_id, "crm"):
        return RedirectResponse("/clients", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse("/clients", status_code=302)

    org_db = user.get("org_db", "")
    if not org_db:
        return RedirectResponse("/dashboard", status_code=302)
    db = get_web_db(tg_id, org_db)
    conn = db.get_connection()
    today = date.today()
    updated = 0
    try:
        clients = conn.execute(
            "SELECT id, tags_json FROM clients"
        ).fetchall()
        for cid, tags_json in clients:
            tags = _parse_tags(tags_json)
            # Remove old RFM tags
            rfm_labels = {"Чемпион", "Лояльный", "Уходящий", "Спящий", "Обычный"}
            tags = [t for t in tags if t not in rfm_labels]

            stats = conn.execute(
                """SELECT COUNT(*), MAX(sale_date),
                          SUM(quantity_sold*(sale_price or 0))
                   FROM sales WHERE client_id=?""", (cid,)
            ).fetchone()
            acnt = conn.execute(
                "SELECT COUNT(*), MAX(start_time), SUM(price or 0) FROM appointments WHERE client_id=?",
                (cid,)
            ).fetchone()
            order_count = (stats[0] or 0) + (acnt[0] or 0)
            ltv = float(stats[2] or 0) + float(acnt[2] or 0)
            last_dates = [d for d in [stats[1], acnt[1]] if d]
            if last_dates:
                last_str = max(str(d)[:10] for d in last_dates)
                try:
                    recency_days = (today - date.fromisoformat(last_str)).days
                except Exception:
                    recency_days = 999
            else:
                recency_days = 999

            rfm = _rfm_tag(recency_days, order_count, ltv)
            tags.append(rfm)
            new_json = json.dumps(list(dict.fromkeys(tags)), ensure_ascii=False)
            conn.execute("UPDATE clients SET tags_json=? WHERE id=?", (new_json, cid))
            updated += 1
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(f"/clients?msg=rfm_ok_{updated}", status_code=303)


@router.get("/clients/{client_id}")
def client_detail(request: Request, client_id: int):
    from web.auth import get_session_user
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not has_module(int(user.get("sub", 0)), "crm"):
        return RedirectResponse("/dashboard", status_code=302)

    tg_id = int(user.get("sub", 0))
    org_db = user.get("org_db", "")
    if not org_db:
        return RedirectResponse("/dashboard", status_code=302)
    db = get_web_db(tg_id, org_db)
    conn = db.get_connection()
    try:
        r = conn.execute(
            "SELECT id, first_name, last_name, phone, email, birth_date, source, notes, tags_json, telegram_id, created_at "
            "FROM clients WHERE id=?", (client_id,)
        ).fetchone()
        if not r:
            return RedirectResponse("/clients", status_code=302)

        client = {
            "id": r[0], "first_name": r[1], "last_name": r[2],
            "phone": r[3], "phone_fmt": _format_phone(r[3]),
            "email": r[4], "birth_date": r[5], "source": r[6],
            "notes": r[7], "tags": _parse_tags(r[8]), "tags_json": r[8],
            "telegram_id": r[9], "created_at": r[10],
        }

        # История продаж
        sales_history = conn.execute(
            "SELECT s.id, p.name, s.quantity_sold, s.sale_price, s.sale_date, s.shop_name "
            "FROM sales s LEFT JOIN products p ON s.product_id=p.id "
            "WHERE s.client_id=? ORDER BY s.sale_date DESC LIMIT 50",
            (client_id,),
        ).fetchall()
        sales = [{"id": r[0], "product": r[1] or "—", "qty": r[2],
                  "price": r[3], "date": r[4], "shop": r[5]} for r in sales_history]
        sales_total = sum((r[2] or 0) * ((r[3]) or 0) for r in sales_history)

        # История записей
        appts_history = conn.execute(
            "SELECT a.id, sv.name, a.start_time, a.end_time, a.status, a.price "
            "FROM appointments a LEFT JOIN services sv ON a.service_id=sv.id "
            "WHERE a.client_id=? ORDER BY a.start_time DESC LIMIT 50",
            (client_id,),
        ).fetchall()
        appts = [{"id": r[0], "service": r[1] or "—", "start": r[2],
                  "end": r[3], "status": r[4],
                  "status_ru": _APPT_STATUS_RU.get(r[4], r[4] or "—"),
                  "price": r[5]} for r in appts_history]
        appts_total = sum((r[5] or 0) for r in appts_history)

        ltv = sales_total + appts_total

        # Лог взаимодействий
        interactions = []
        try:
            inter_rows = conn.execute(
                "SELECT id, type, text, author_name, created_at FROM client_interactions "
                "WHERE client_id=? ORDER BY created_at DESC LIMIT 100",
                (client_id,)
            ).fetchall()
            interactions = [
                {"id": ir[0], "type": ir[1],
                 "type_label": _INTERACTION_TYPES.get(ir[1], ir[1]),
                 "text": ir[2], "author": ir[3] or "", "created_at": str(ir[4] or "")[:16]}
                for ir in inter_rows
            ]
        except Exception:
            pass

        is_admin = user.get("role") in ("owner", "admin", "super_admin")

        # Имя автора для нового взаимодействия
        try:
            author_row = conn.execute(
                "SELECT first_name, last_name FROM users WHERE telegram_id=?", (tg_id,)
            ).fetchone()
            author_name = f"{author_row[0] or ''} {author_row[1] or ''}".strip() if author_row else ""
        except Exception:
            author_name = ""

        # Абонементы клиента
        client_packages = []
        try:
            # auto-expire
            try:
                conn.execute(
                    "UPDATE client_packages SET status='expired' "
                    "WHERE status='active' AND expires_at IS NOT NULL AND expires_at < datetime('now')"
                )
                conn.execute(
                    "UPDATE client_packages SET status='exhausted' "
                    "WHERE status IN ('active','frozen') AND visits_used >= visits_total"
                )
                conn.commit()
            except Exception:
                pass
            cp_rows = conn.execute(
                "SELECT cp.id, sp.name, cp.visits_total, cp.visits_used, "
                "cp.price_paid, cp.purchased_at, cp.expires_at, cp.status, cp.notes "
                "FROM client_packages cp "
                "JOIN service_packages sp ON sp.id=cp.package_id "
                "WHERE cp.client_id=? ORDER BY cp.purchased_at DESC",
                (client_id,),
            ).fetchall()
            _pkg_status_ru = {
                "active": "Активен", "frozen": "Заморожен",
                "exhausted": "Исчерпан", "expired": "Истёк",
            }
            client_packages = [
                {
                    "id": cp[0], "name": cp[1],
                    "visits_total": cp[2], "visits_used": cp[3],
                    "visits_left": cp[2] - cp[3],
                    "price_paid": cp[4], "purchased_at": str(cp[5] or "")[:10],
                    "expires_at": str(cp[6] or "")[:10],
                    "status": cp[7], "status_label": _pkg_status_ru.get(cp[7], cp[7]),
                    "notes": cp[8] or "",
                    "pct": int((cp[3] / cp[2]) * 100) if cp[2] > 0 else 0,
                }
                for cp in cp_rows
            ]
            # Добавляем стоимость активных абонементов в LTV
            ltv += sum(float(cp[4] or 0) for cp in cp_rows)
        except Exception:
            pass

        ctx = _get_ctx(request)
        ctx.update({
            "client": client, "sales": sales, "sales_total": sales_total,
            "appts": appts, "appts_total": appts_total, "ltv": ltv,
            "interactions": interactions,
            "interaction_types": _INTERACTION_TYPES,
            "author_name": author_name,
            "is_admin": is_admin,
            "has_services": has_module(int(user.get("sub", 0)), "services"),
            "client_packages": client_packages,
            "flash": request.query_params.get("msg", ""),
            "error": request.query_params.get("error", ""),
            "tab_sold": request.query_params.get("sold", ""),
        })
        return request.app.state.templates.TemplateResponse(request, "clients/detail.html", ctx)
    finally:
        conn.close()


@router.post("/clients/{client_id}/edit")
def client_edit(
    request: Request,
    client_id: int,
    csrf_token: str = Form(""),
    first_name: str = Form(""),
    last_name: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    birth_date: str = Form(""),
    source: str = Form(""),
    notes: str = Form(""),
    tags: str = Form(""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(f"/clients/{client_id}?error=csrf", status_code=302)
    if not has_module(int(user.get("sub", 0)), "crm"):
        return RedirectResponse("/clients", status_code=302)
    if not first_name.strip():
        return RedirectResponse(f"/clients/{client_id}?error=name_required", status_code=302)

    tags_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    tags_json = json.dumps(tags_list, ensure_ascii=False)
    birth_val = birth_date.strip() or None

    tg_id = int(user.get("sub", 0))
    org_db = user.get("org_db", "")
    if not org_db:
        return RedirectResponse("/dashboard", status_code=302)
    db = get_web_db(tg_id, org_db)
    conn = db.get_connection()
    try:
        conn.execute(
            "UPDATE clients SET first_name=?, last_name=?, phone=?, email=?, birth_date=?, "
            "source=?, notes=?, tags_json=?, updated_at=datetime('now') WHERE id=?",
            (first_name.strip(), last_name.strip(), phone.strip(), email.strip(),
             birth_val, source.strip(), notes.strip(), tags_json, client_id),
        )
        conn.commit()
        return RedirectResponse(f"/clients/{client_id}", status_code=303)
    finally:
        conn.close()


@router.post("/clients/{client_id}/delete")
def client_delete(request: Request, client_id: int, csrf_token: str = Form("")):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(f"/clients/{client_id}?error=csrf", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(f"/clients/{client_id}", status_code=302)
    if not has_module(int(user.get("sub", 0)), "crm"):
        return RedirectResponse("/clients", status_code=302)

    tg_id = int(user.get("sub", 0))
    org_db = user.get("org_db", "")
    if not org_db:
        return RedirectResponse("/dashboard", status_code=302)
    db = get_web_db(tg_id, org_db)
    conn = db.get_connection()
    try:
        conn.execute("UPDATE sales SET client_id=NULL WHERE client_id=?", (client_id,))
        conn.execute("DELETE FROM clients WHERE id=?", (client_id,))
        conn.commit()
        return RedirectResponse("/clients", status_code=303)
    finally:
        conn.close()


@router.post("/clients/{client_id}/interactions")
def client_interaction_add(
    request: Request,
    client_id: int,
    csrf_token: str = Form(""),
    itype: str = Form("note"),
    text: str = Form(""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(f"/clients/{client_id}?error=csrf", status_code=302)
    if not has_module(int(user.get("sub", 0)), "crm"):
        return RedirectResponse("/clients", status_code=302)
    if not text.strip():
        return RedirectResponse(f"/clients/{client_id}?tab=log", status_code=302)

    tg_id = int(user.get("sub", 0))
    org_db = user.get("org_db", "")
    if not org_db:
        return RedirectResponse("/dashboard", status_code=302)
    db = get_web_db(tg_id, org_db)
    conn = db.get_connection()
    try:
        # Get author name
        author_row = conn.execute(
            "SELECT first_name, last_name FROM users WHERE telegram_id=?", (tg_id,)
        ).fetchone()
        author_name = f"{author_row[0] or ''} {author_row[1] or ''}".strip() if author_row else str(tg_id)
        conn.execute(
            "INSERT INTO client_interactions (client_id, type, text, author_id, author_name) VALUES (?,?,?,?,?)",
            (client_id, itype, text.strip(), tg_id, author_name),
        )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(f"/clients/{client_id}?tab=log", status_code=303)


@router.post("/clients/{client_id}/interactions/{inter_id}/delete")
def client_interaction_delete(
    request: Request,
    client_id: int,
    inter_id: int,
    csrf_token: str = Form(""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(f"/clients/{client_id}?error=csrf", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(f"/clients/{client_id}", status_code=302)

    tg_id = int(user.get("sub", 0))
    org_db = user.get("org_db", "")
    if not org_db:
        return RedirectResponse("/dashboard", status_code=302)
    db = get_web_db(tg_id, org_db)
    conn = db.get_connection()
    try:
        conn.execute(
            "DELETE FROM client_interactions WHERE id=? AND client_id=?",
            (inter_id, client_id)
        )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(f"/clients/{client_id}?tab=log", status_code=303)


@router.get("/clients/export.xlsx")
def clients_export(request: Request):
    from web.auth import get_session_user
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not has_module(int(user.get("sub", 0)), "crm"):
        return RedirectResponse("/clients", status_code=302)

    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
    except ImportError:
        return Response("openpyxl not installed", status_code=500)

    tg_id = int(user.get("sub", 0))
    org_db = user.get("org_db", "")
    if not org_db:
        return RedirectResponse("/dashboard", status_code=302)
    db = get_web_db(tg_id, org_db)
    conn = db.get_connection()
    try:
        rows = conn.execute(
            """SELECT c.first_name, c.last_name, c.phone, c.email,
                      c.birth_date, c.source, c.tags_json, c.created_at,
                      COALESCE(s.ltv,0)+COALESCE(a.altv,0),
                      COALESCE(s.cnt,0)+COALESCE(a.acnt,0)
               FROM clients c
               LEFT JOIN (
                   SELECT client_id, SUM(quantity_sold*(sale_price or 0)) ltv, COUNT(*) cnt
                   FROM sales WHERE client_id IS NOT NULL GROUP BY client_id
               ) s ON s.client_id=c.id
               LEFT JOIN (
                   SELECT client_id, SUM(price or 0) altv, COUNT(*) acnt
                   FROM appointments WHERE client_id IS NOT NULL GROUP BY client_id
               ) a ON a.client_id=c.id
               ORDER BY c.created_at DESC"""
        ).fetchall()
    finally:
        conn.close()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Клиенты"
    headers = ["Имя", "Фамилия", "Телефон", "Email", "Дата рождения", "Источник",
               "Теги", "Добавлен", "LTV (₽)", "Покупок"]
    hdr_fill = PatternFill("solid", fgColor="4F46E5")
    hdr_font = Font(color="FFFFFF", bold=True)
    for ci, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=ci, value=h)
        cell.fill = hdr_fill
        cell.font = hdr_font
        cell.alignment = Alignment(horizontal="center")

    for ri, r in enumerate(rows, 2):
        tags = ", ".join(_parse_tags(r[6]))
        ws.append([r[0], r[1], r[2], r[3], r[4] or "", r[5] or "", tags, r[7],
                   round(float(r[8] or 0), 2), int(r[9] or 0)])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=clients.xlsx"},
    )


# ─── API: поиск клиентов (для модала продажи абонемента) ─────────────────────

@router.get("/api/clients/search")
def api_clients_search(request: Request, q: str = ""):
    from web.auth import get_session_user
    from web.deps import get_web_db
    from billing_utils import has_module
    user = get_session_user(request)
    if not user:
        return JSONResponse({"clients": []})
    if not has_module(int(user.get("sub", 0)), "crm"):
        return JSONResponse({"clients": []})

    org_db = user.get("org_db", "")
    if not org_db or not q.strip():
        return JSONResponse({"clients": []})

    db = get_web_db(int(user.get("sub", 0)), org_db)
    conn = db.get_connection()
    try:
        like = f"%{q.strip()}%"
        rows = conn.execute(
            "SELECT id, first_name, last_name, phone FROM clients "
            "WHERE (first_name LIKE ? OR last_name LIKE ? OR phone LIKE ?) "
            "ORDER BY first_name LIMIT 20",
            (like, like, like),
        ).fetchall()
        clients = [
            {
                "id": r[0],
                "name": f"{r[1] or ''} {r[2] or ''}".strip(),
                "phone": r[3] or "",
            }
            for r in rows
        ]
    finally:
        conn.close()
    return JSONResponse({"clients": clients})
