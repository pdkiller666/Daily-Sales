import logging
from typing import Annotated
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse

router = APIRouter()

# Модули, доступ к которым можно настраивать в кастомных ролях / per-user
MODULE_OPTIONS = [
    ("analytics", "📊 Аналитика"),
    ("team", "👥 Команда"),
    ("plans_motivation", "🎯 Планы и мотивация"),
    ("chat", "💬 Чат"),
    ("integrations", "🔗 Интеграции"),
    ("notifications", "🔔 Уведомления"),
    ("ai_assistant", "🤖 AI-ассистент"),
]
MODULE_LABELS = dict(MODULE_OPTIONS)

DEPT_TYPES = [
    ("region", "🏙️ Регион"),
    ("department", "🏢 Отдел"),
    ("shop", "🏪 Магазин"),
]
DEPT_TYPE_LABELS = dict(DEPT_TYPES)

ROLE_ICONS = ["👑", "🏙️", "🏢", "🏪", "👔", "💼", "📦", "🛒", "📊", "🔧"]

BASE_ROLE_OPTIONS = [
    ("admin", "Администратор (доступ к управлению)"),
    ("user", "Сотрудник (базовый доступ)"),
]


def _redirect(tab: str = "depts", msg: str = "") -> RedirectResponse:
    url = f"/org-structure?tab={tab}"
    if msg:
        url += f"&msg={msg}"
    return RedirectResponse(url=url, status_code=302)


@router.get("/org-structure")
def org_structure_page(request: Request, tab: str = "depts", msg: str = ""):
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db
    from db_utils import org_structure_level, MINIMAL_DEPT_LIMIT

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    ctx: dict = {
        "request": request, "user": user,
        "tab": tab if tab in ("depts", "roles", "info") else "depts",
        "msg": msg,
        "csrf_token": get_csrf_token(request),
        "departments": [], "dept_tree": [], "roles": [],
        "module_options": MODULE_OPTIONS,
        "dept_types": DEPT_TYPES, "dept_type_labels": DEPT_TYPE_LABELS,
        "role_icons": ROLE_ICONS, "base_role_options": BASE_ROLE_OPTIONS,
        "level": "minimal", "minimal_limit": MINIMAL_DEPT_LIMIT,
        "error": None,
    }

    try:
        level = org_structure_level(telegram_id)
        ctx["level"] = level

        db = get_web_db(telegram_id, org_db)
        depts = db.get_departments() or []
        ctx["departments"] = depts
        ctx["dept_tree"] = _build_dept_tree(depts)
        ctx["dept_count"] = len(depts)
        ctx["can_add_dept"] = (level == "full") or (len(depts) < MINIMAL_DEPT_LIMIT)

        if level == "full":
            ctx["roles"] = db.get_org_roles() or []
    except Exception as exc:
        logging.error(f"org_structure_page error: {exc}")
        ctx["error"] = "Произошла внутренняя ошибка. Попробуйте позже."

    return request.app.state.templates.TemplateResponse(
        request, "org_structure/index.html", ctx
    )


def _build_dept_tree(depts: list) -> list:
    """Плоский упорядоченный список с уровнем вложенности (depth) по parent_id."""
    by_id = {d["id"]: dict(d, children=[]) for d in depts}
    roots = []
    for d in by_id.values():
        pid = d.get("parent_id")
        if pid and pid in by_id:
            by_id[pid]["children"].append(d)
        else:
            roots.append(d)

    flat = []

    def _walk(node, depth):
        flat.append({
            "id": node["id"], "name": node["name"],
            "type": node.get("type", "department"), "depth": depth,
        })
        for ch in node["children"]:
            _walk(ch, depth + 1)

    for r in roots:
        _walk(r, 0)
    return flat


@router.post("/org-structure/dept/add")
def dept_add(
    request: Request,
    name: Annotated[str, Form()],
    type: str = Form(default="department"),
    parent_id: str = Form(default=""),
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from db_utils import org_structure_level, MINIMAL_DEPT_LIMIT

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return _redirect("depts", "csrf_error")

    name = (name or "").strip()
    if not name or len(name) > 80:
        return _redirect("depts", "invalid_name")

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    try:
        level = org_structure_level(telegram_id)
        db = get_web_db(telegram_id, org_db)

        if level != "full":
            # Минимальный режим: только плоские отделы до лимита
            if db.count_departments() >= MINIMAL_DEPT_LIMIT:
                return _redirect("depts", "limit")
            type = "department"
            parent_id = ""

        pid = int(parent_id) if parent_id.strip().isdigit() else None
        if type not in DEPT_TYPE_LABELS:
            type = "department"
        db.add_department(name, type=type, parent_id=pid)
    except Exception as e:
        logging.error(f"dept_add error: {e}")
        return _redirect("depts", "error")

    return _redirect("depts", "dept_created")


@router.post("/org-structure/dept/{dept_id}/delete")
def dept_delete(request: Request, dept_id: int, csrf_token: str = Form(default="")):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return _redirect("depts", "csrf_error")

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    try:
        db = get_web_db(telegram_id, org_db)
        db.delete_department(dept_id)
    except Exception as e:
        logging.error(f"dept_delete error: {e}")
        return _redirect("depts", "error")

    return _redirect("depts", "dept_deleted")


@router.post("/org-structure/role/add")
def role_add(
    request: Request,
    name: Annotated[str, Form()],
    icon: str = Form(default="👔"),
    base_role: str = Form(default="user"),
    scope_type: str = Form(default=""),
    modules: list[str] = Form(default=[]),
    can_manage_users: str = Form(default=""),
    can_view_reports: str = Form(default=""),
    can_manage_products: str = Form(default=""),
    can_view_salary: str = Form(default=""),
    can_manage_plans: str = Form(default=""),
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db
    from db_utils import org_structure_level

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return _redirect("roles", "csrf_error")

    name = (name or "").strip()
    if not name or len(name) > 60:
        return _redirect("roles", "invalid_name")

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    try:
        if org_structure_level(telegram_id) != "full":
            return _redirect("roles", "paid_only")

        db = get_web_db(telegram_id, org_db)
        if base_role not in ("admin", "user"):
            base_role = "user"
        if icon not in ROLE_ICONS:
            icon = "👔"

        perms = {
            "can_manage_users": 1 if can_manage_users else 0,
            "can_view_reports": 1 if can_view_reports else 0,
            "can_manage_products": 1 if can_manage_products else 0,
            "can_view_salary": 1 if can_view_salary else 0,
            "can_manage_plans": 1 if can_manage_plans else 0,
        }
        mods = [m for m in (modules or []) if m in MODULE_LABELS]

        st = scope_type if scope_type in ("city", "shop", "network") else None
        db.add_org_role(
            name, icon=icon, base_role=base_role,
            scope_type=st, scope_values=None,
            perms=perms, modules=mods,
        )
    except Exception as e:
        logging.error(f"role_add error: {e}")
        return _redirect("roles", "error")

    return _redirect("roles", "role_created")


@router.post("/org-structure/role/{role_id}/delete")
def role_delete(request: Request, role_id: int, csrf_token: str = Form(default="")):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return _redirect("roles", "csrf_error")

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    try:
        db = get_web_db(telegram_id, org_db)
        db.delete_org_role(role_id)
    except Exception as e:
        logging.error(f"role_delete error: {e}")
        return _redirect("roles", "error")

    return _redirect("roles", "role_deleted")
