from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from datetime import date

router = APIRouter()

CONTEST_TYPE_LABELS = {
    "shop": "Магазины", "seller": "Продавцы",
    "shop_product": "Товары по магазину",
}
METRIC_LABELS = {
    "turnover": "Выручка", "quantity": "Количество",
    "product_quantity": "Кол-во товара",
}
REWARD_LABELS = {
    "cash": "Денежный приз", "gift": "Подарок",
    "bonus": "Бонус", "other": "Другое",
}
STATUS_LABELS = {
    "active": ("Активный", "bg-emerald-100 text-emerald-700"),
    "pending": ("Запланирован", "bg-blue-100 text-blue-700"),
    "finished": ("Завершён", "bg-slate-100 text-slate-500"),
    "cancelled": ("Отменён", "bg-red-100 text-red-500"),
}


def _contest_progress(start_date: str, end_date: str, today: date) -> int:
    """0-100 progress of contest timeline."""
    try:
        from datetime import date as _date
        s = _date.fromisoformat(start_date)
        e = _date.fromisoformat(end_date)
        total = (e - s).days
        if total <= 0:
            return 100
        elapsed = (today - s).days
        return max(0, min(100, round(elapsed / total * 100)))
    except Exception:
        return 0


@router.get("/contests")
def contests_page(
    request: Request,
    status_filter: str = "",
    contest_id: int = 0,
):
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    today = date.today()

    ctx: dict = {
        "request": request, "user": user,
        "is_admin": user.get("role") in ("owner", "admin", "super_admin"),
        "contests": [], "status_filter": status_filter,
        "contest_type_labels": CONTEST_TYPE_LABELS,
        "metric_labels": METRIC_LABELS,
        "reward_labels": REWARD_LABELS,
        "status_labels": STATUS_LABELS,
        "selected_contest": None,
        "leaderboard": [], "today": today.isoformat(),
        "error": None,
    }

    try:
        db = get_web_db(telegram_id, org_db)

        status_arg = status_filter if status_filter else None
        raw = db.get_contests(status=status_arg) or []

        # contests cols: 0:id 1:title 2:desc 3:contest_type 4:metric_type 5:target_value
        # 6:reward_type 7:reward_value 8:start_date 9:end_date 10:shop_filter
        # 11:city_filter 12:user_filter 13:product_filter 14:category_filter
        # 15:extra_conditions 16:status 17:notify_on_start 18:notify_on_end
        # 19:created_by 20:created_at
        contests = []
        for c in raw:
            status = c[16] or "pending"
            status_label, status_cls = STATUS_LABELS.get(status, (status, "bg-slate-100 text-slate-500"))
            progress = _contest_progress(c[8] or "", c[9] or "", today)
            contests.append({
                "id": c[0],
                "title": c[1] or "Без названия",
                "desc": c[2] or "",
                "contest_type": c[3] or "shop",
                "metric_type": c[4] or "turnover",
                "target_value": float(c[5] or 0),
                "reward_type": c[6] or "",
                "reward_value": c[7] or "",
                "start_date": c[8] or "",
                "end_date": c[9] or "",
                "status": status,
                "status_label": status_label,
                "status_cls": status_cls,
                "progress": progress,
                "created_at": (c[20] or "")[:10],
            })

        ctx["contests"] = contests

        # If a specific contest selected, load its leaderboard
        if contest_id:
            selected = next((c for c in contests if c["id"] == contest_id), None)
            if selected:
                ctx["selected_contest"] = selected
                start = selected["start_date"]
                end_d = selected["end_date"]
                # Use today as end if contest still active
                if selected["status"] == "active":
                    end_d = min(end_d, today.isoformat()) if end_d else today.isoformat()

                kwargs: dict = {}
                if start:
                    kwargs["start_date"] = start
                if end_d:
                    kwargs["end_date"] = end_d

                if selected["contest_type"] == "shop":
                    raw_lb = db.get_shop_ranking(**kwargs) or []
                    # shop_name[0] total_sold[1] total_revenue[2] active_sellers[3] total_sales[4]
                    max_val = float(raw_lb[0][2] if raw_lb else 1)
                    leaderboard = []
                    for i, r in enumerate(raw_lb[:10]):
                        val = float(r[2] or 0) if selected["metric_type"] == "turnover" else int(r[1] or 0)
                        leaderboard.append({
                            "pos": i + 1,
                            "label": r[0] or "—",
                            "value": val,
                            "pct": round(val / max(max_val, 1) * 100),
                        })
                else:
                    raw_lb = db.get_sales_ranking(**kwargs) or []
                    # first_name[0] last_name[1] shop_name[2] total_sold[3] total_revenue[4]
                    max_val = float(raw_lb[0][4] if raw_lb else 1)
                    leaderboard = []
                    for i, r in enumerate(raw_lb[:10]):
                        val = float(r[4] or 0) if selected["metric_type"] == "turnover" else int(r[3] or 0)
                        name = f"{r[0] or ''} {r[1] or ''}".strip() or f"#{r[7]}"
                        leaderboard.append({
                            "pos": i + 1,
                            "label": name,
                            "sub": r[2] or "—",
                            "value": val,
                            "pct": round(val / max(max_val, 1) * 100),
                        })
                ctx["leaderboard"] = leaderboard

    except Exception as exc:
        ctx["error"] = str(exc)

    return request.app.state.templates.TemplateResponse(
        request, "contests/index.html", ctx
    )
