from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

router = APIRouter()

PLAN_TYPE_LABELS = {"weekly": "Недельный", "monthly": "Месячный"}
METRIC_LABELS = {"turnover": "Выручка", "quantity": "Количество"}
TARGET_LABELS = {"seller": "Продавец", "shop": "Магазин"}


@router.get("/plans")
def plans_page(request: Request, active_only: str = "1"):
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
        "plans_data": [], "active_only": active_only,
        "plan_type_labels": PLAN_TYPE_LABELS,
        "metric_labels": METRIC_LABELS,
        "target_labels": TARGET_LABELS,
        "error": None,
    }

    try:
        db = get_web_db(telegram_id, org_db)

        from timezone_utils import get_current_user_time
        tz = db.get_user_timezone(telegram_id)
        today = get_current_user_time(tz).date()

        only_active = active_only != "0"
        # get_plans_progress already calls get_sales_plans(active_only=True) internally
        # For inactive plans, we fetch separately
        if only_active:
            progress_rows = db.get_plans_progress(local_today=today)
        else:
            plans = db.get_sales_plans(active_only=False) or []
            progress_rows = []
            for plan in plans:
                actual = db.calculate_plan_actual(plan, local_today=today)
                target = plan[3]
                pct = round((actual / target * 100) if target > 0 else 0.0, 1)
                progress_rows.append((plan, actual, pct))

        # progress_rows: list of (plan_row, actual, percent)
        # plan_row: id[0] plan_type[1] metric_type[2] target_value[3] target_type[4]
        #           user_id[5] shop_name[6] filter_type[7] filter_value[8] is_active[9]
        #           created_by[10] created_at[11] first_name[12] last_name[13]

        plans_data = []
        for plan_row, actual, pct in progress_rows:
            target = float(plan_row[3] or 0)
            is_metric_revenue = plan_row[2] == "turnover"

            # Who this plan targets
            if plan_row[4] == "seller":
                fname = (plan_row[12] or "").strip()
                lname = (plan_row[13] or "").strip()
                target_who = f"{fname} {lname}".strip() or f"user#{plan_row[5]}"
            else:
                target_who = plan_row[6] or "все магазины"

            # Filter description
            filter_desc = ""
            if plan_row[7] == "category" and plan_row[8]:
                try:
                    import json
                    cats = json.loads(plan_row[8])
                    filter_desc = ", ".join(cats) if isinstance(cats, list) else plan_row[8]
                except Exception:
                    filter_desc = plan_row[8]
                filter_desc = f"Категория: {filter_desc}"
            elif plan_row[7] == "product" and plan_row[8]:
                filter_desc = "По выбранным товарам"

            # Color by pct
            if pct >= 100:
                bar_cls = "bg-emerald-500"
                pct_cls = "text-emerald-600"
            elif pct >= 70:
                bar_cls = "bg-blue-500"
                pct_cls = "text-blue-600"
            elif pct >= 40:
                bar_cls = "bg-amber-400"
                pct_cls = "text-amber-600"
            else:
                bar_cls = "bg-red-400"
                pct_cls = "text-red-600"

            plans_data.append({
                "id": plan_row[0],
                "plan_type": plan_row[1],
                "metric_type": plan_row[2],
                "target_value": target,
                "target_type": plan_row[4],
                "target_who": target_who,
                "filter_desc": filter_desc,
                "is_active": bool(plan_row[9]),
                "created_at": (plan_row[11] or "")[:10],
                "actual": float(actual),
                "pct": min(pct, 100),
                "pct_raw": pct,
                "is_metric_revenue": is_metric_revenue,
                "bar_cls": bar_cls,
                "pct_cls": pct_cls,
            })

        # Sort: active first, then by pct desc
        plans_data.sort(key=lambda p: (0 if p["is_active"] else 1, -p["pct_raw"]))
        ctx["plans_data"] = plans_data

    except Exception as exc:
        ctx["error"] = str(exc)

    return request.app.state.templates.TemplateResponse(
        request, "plans/index.html", ctx
    )
