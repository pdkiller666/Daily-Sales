import io
from collections import defaultdict
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, StreamingResponse

router = APIRouter()


def _period_dates(period: str, today):
    from datetime import timedelta
    if period == "today":
        return today.isoformat(), today.isoformat()
    if period == "week":
        return (today - timedelta(days=6)).isoformat(), today.isoformat()
    if period == "prev_month":
        month_start = today.replace(day=1)
        end = (month_start - timedelta(days=1))
        start = end.replace(day=1)
        return start.isoformat(), end.isoformat()
    # default: month
    return today.replace(day=1).isoformat(), today.isoformat()


def _aggregate(all_sales, group_by: str):
    """Aggregate sales rows by group_by key.
    sales cols: id[0] product_id[1] shop_name[2] qty[3] price[4]
                user_id[5] date[6] product_name[7] category[8] first_name[9] last_name[10]
    """
    groups: dict = {}

    for s in all_sales:
        qty = int(s[3] or 0)
        rev = float((s[3] or 0) * (s[4] or 0))

        if group_by == "product":
            key = s[1]
            if key not in groups:
                groups[key] = {"id": s[1], "label": s[7] or "—", "sub": s[8] or "—", "qty": 0, "revenue": 0.0, "count": 0, "link": f"/products/{s[1]}"}
        elif group_by == "category":
            key = s[8] or "Без категории"
            if key not in groups:
                groups[key] = {"id": None, "label": key, "sub": "", "qty": 0, "revenue": 0.0, "count": 0, "link": None}
        elif group_by == "shop":
            key = s[2] or "—"
            if key not in groups:
                groups[key] = {"id": None, "label": key, "sub": "", "qty": 0, "revenue": 0.0, "count": 0, "link": f"/inventory?shop={key}"}
        else:  # seller
            key = s[5]
            fname = (s[9] or "").strip()
            lname = (s[10] or "").strip()
            name = f"{fname} {lname}".strip() or f"id{key}"
            if key not in groups:
                groups[key] = {"id": key, "label": name, "sub": s[2] or "—", "qty": 0, "revenue": 0.0, "count": 0, "link": f"/staff/{key}"}
            else:
                pass

        groups[key]["qty"] += qty
        groups[key]["revenue"] += rev
        groups[key]["count"] += 1

    result = sorted(groups.values(), key=lambda x: x["revenue"], reverse=True)
    max_rev = result[0]["revenue"] if result else 1.0
    for r in result:
        r["pct"] = round(r["revenue"] / max_rev * 100) if max_rev > 0 else 0
    return result


@router.get("/reports")
def reports_page(
    request: Request,
    period: str = "month",
    date_from: str = "",
    date_to: str = "",
    shop: str = "",
    group_by: str = "product",
):
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
        "period": period, "shop": shop, "group_by": group_by,
        "date_from": date_from, "date_to": date_to,
        "shops": [], "summary": (0, 0, 0, 0),
        "groups": [], "all_sales": [], "error": None,
        "chart_labels": [], "chart_data": [],
    }

    try:
        db = get_web_db(telegram_id, org_db)

        from timezone_utils import get_current_user_time
        tz = db.get_user_timezone(telegram_id)
        today = get_current_user_time(tz).date()

        if period == "custom" and date_from and date_to:
            df, dt = date_from, date_to
        else:
            df, dt = _period_dates(period, today)
            date_from, date_to = df, dt

        ctx["date_from"] = date_from
        ctx["date_to"] = date_to

        is_admin = user.get("role") in ("owner", "admin", "super_admin")
        if is_admin:
            ctx["shops"] = db.get_all_shops() or []
        else:
            from web.routes.sales import _get_user_allowed_shops
            allowed_shops = _get_user_allowed_shops(telegram_id, db)
            ctx["shops"] = allowed_shops

        kwargs: dict = {"start_date": df, "end_date": dt}
        if shop:
            kwargs["shop_name"] = shop

        if not is_admin:
            # Enforce scope: restrict sales to user's allowed shops
            all_shops = db.get_all_shops() or []
            if not shop:
                # No manual shop filter — scope-restrict across all allowed shops
                if set(allowed_shops) != set(all_shops):
                    kwargs.pop("shop_name", None)
                    kwargs["shop_names"] = allowed_shops
            else:
                # Manual shop filter — only allow if it's in the user's scope
                if shop not in allowed_shops:
                    kwargs["shop_name"] = allowed_shops[0] if allowed_shops else shop
                    shop = kwargs["shop_name"]
                    ctx["shop"] = shop

        all_sales = db.get_sales_report(**kwargs) or []
        ctx["all_sales"] = all_sales
        summary_kwargs: dict = {"start_date": df, "end_date": dt}
        if shop:
            summary_kwargs["shop_name"] = shop
        elif not is_admin and "shop_names" in kwargs:
            summary_kwargs["shop_names"] = kwargs["shop_names"]
        ctx["summary"] = db.get_sales_summary(**summary_kwargs) or (0, 0, 0, 0)
        ctx["groups"] = _aggregate(all_sales, group_by)

        # Daily chart: aggregate all_sales by date
        try:
            from collections import defaultdict
            daily_rev: dict = defaultdict(float)
            for s in all_sales:
                d = (s[6] or "")[:10]
                if d:
                    daily_rev[d] += float((s[3] or 0) * (s[4] or 0))
            sorted_days = sorted(daily_rev.keys())
            # Fill date gaps for contiguous range
            if sorted_days:
                from datetime import timedelta
                import datetime as _dt
                start_d = _dt.date.fromisoformat(sorted_days[0])
                end_d   = _dt.date.fromisoformat(sorted_days[-1])
                all_days = []
                cur_d = start_d
                while cur_d <= end_d:
                    all_days.append(cur_d.isoformat())
                    cur_d += timedelta(days=1)
            else:
                all_days = []
            # Limit to max 60 data points to keep chart readable
            if len(all_days) > 60:
                all_days = all_days[-60:]
            ctx["chart_labels"] = [d[5:] for d in all_days]   # MM-DD
            ctx["chart_data"]   = [int(daily_rev.get(d, 0)) for d in all_days]
        except Exception:
            ctx["chart_labels"] = []
            ctx["chart_data"]   = []

    except Exception as exc:
        ctx["error"] = "Произошла внутренняя ошибка. Попробуйте позже."

    return request.app.state.templates.TemplateResponse(
        request, "reports/index.html", ctx
    )


@router.get("/reports/export.xlsx")
def reports_export_xlsx(
    request: Request,
    period: str = "month",
    date_from: str = "",
    date_to: str = "",
    shop: str = "",
    group_by: str = "product",
):
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/dashboard", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")

    try:
        db = get_web_db(telegram_id, org_db)
        from timezone_utils import get_current_user_time
        tz = db.get_user_timezone(telegram_id)
        today = get_current_user_time(tz).date()

        if period == "custom" and date_from and date_to:
            df, dt = date_from, date_to
        else:
            df, dt = _period_dates(period, today)
            date_from, date_to = df, dt

        kwargs: dict = {"start_date": df, "end_date": dt}
        if shop:
            kwargs["shop_name"] = shop

        all_sales = db.get_sales_report(**kwargs) or []
        summary = db.get_sales_summary(
            start_date=df, end_date=dt, shop_name=shop if shop else None
        ) or (0, 0, 0, 0)
        groups = _aggregate(all_sales, group_by)

        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
        wb = openpyxl.Workbook()

        # Sheet 1: Summary
        ws1 = wb.active
        ws1.title = "Сводка"
        hdr_fill = PatternFill("solid", fgColor="DC2626")
        hdr_font = Font(color="FFFFFF", bold=True)
        ws1.append(["Показатель", "Значение"])
        for cell in ws1[1]:
            cell.fill = hdr_fill
            cell.font = hdr_font
        ws1.append(["Период", f"{date_from} — {date_to}"])
        ws1.append(["Магазин", shop or "Все"])
        ws1.append(["Кол-во продаж", summary[0] or 0])
        ws1.append(["Общее кол-во ед.", summary[1] or 0])
        ws1.append(["Выручка (₽)", round(float(summary[2] or 0), 2)])
        ws1.append(["Средний чек (₽)", round(float(summary[3] or 0), 2)])
        ws1.column_dimensions["A"].width = 22
        ws1.column_dimensions["B"].width = 20

        # Sheet 2: Groups
        ws2 = wb.create_sheet("По группам")
        grp_labels = {
            "product": "Товар", "category": "Категория",
            "shop": "Магазин", "seller": "Продавец",
        }
        ws2.append([grp_labels.get(group_by, "Группа"), "Продажи (шт)", "Кол-во", "Выручка (₽)", "Доля (%)"])
        for cell in ws2[1]:
            cell.fill = hdr_fill
            cell.font = hdr_font
        for g in groups:
            ws2.append([g["label"], g["qty"], g["count"], round(g["revenue"], 2), g["pct"]])
        for col in ["A", "B", "C", "D", "E"]:
            ws2.column_dimensions[col].width = 18
        ws2.column_dimensions["A"].width = 30

        # Sheet 3: Raw sales
        ws3 = wb.create_sheet("Детализация")
        ws3.append(["Дата", "Товар", "Категория", "Магазин", "Кол-во", "Цена (₽)", "Сумма (₽)", "Продавец"])
        for cell in ws3[1]:
            cell.fill = hdr_fill
            cell.font = hdr_font
        for s in all_sales:
            fname = (s[9] or "").strip()
            lname = (s[10] or "").strip()
            seller = f"{fname} {lname}".strip() or "—"
            qty = int(s[3] or 0)
            price = float(s[4] or 0)
            ws3.append([
                str(s[6] or "")[:10],
                s[7] or "—", s[8] or "—", s[2] or "—",
                qty, round(price, 2), round(qty * price, 2), seller,
            ])
        for col, w in zip(["A", "B", "C", "D", "E", "F", "G", "H"], [12, 28, 18, 20, 8, 12, 12, 22]):
            ws3.column_dimensions[col].width = w

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        fname_safe = f"reports_{date_from}_{date_to}.xlsx"
        return StreamingResponse(
            buf,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{fname_safe}"'},
        )
    except Exception as exc:
        import logging
        logging.error(f"reports_export_xlsx error: {exc}")
        return RedirectResponse(url="/reports?error=export", status_code=303)
