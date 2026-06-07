import io
import logging
import traceback
from collections import defaultdict
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, StreamingResponse

logger = logging.getLogger(__name__)

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


def _add_abc_badges(groups: list) -> None:
    """Annotate each group dict with abc='A'|'B'|'C' based on cumulative revenue share."""
    total_rev = sum(g["revenue"] for g in groups)
    if not total_rev:
        for g in groups:
            g["abc"] = ""
        return
    cumulative = 0.0
    for g in groups:
        cumulative += g["revenue"]
        pct = cumulative / total_rev
        g["abc"] = "A" if pct <= 0.80 else ("B" if pct <= 0.95 else "C")


def _compute_sparklines(groups: list, all_sales: list, group_by: str) -> list:
    """Add spark (list[float 0..1], last ≤7 dates) to each group. Returns last7 date strings."""
    from collections import defaultdict

    dates_set = sorted({(s[6] or "")[:10] for s in all_sales if s[6]})
    last7 = dates_set[-7:]
    if len(last7) < 2:
        for g in groups:
            g["spark"] = []
        return last7

    key_daily: dict = defaultdict(lambda: defaultdict(float))
    for s in all_sales:
        d = (s[6] or "")[:10]
        if d not in last7:
            continue
        rev = float((s[3] or 0) * (s[4] or 0))
        if group_by == "product":
            k = s[1]
        elif group_by == "category":
            k = s[8] or "Без категории"
        elif group_by == "shop":
            k = s[2] or "—"
        else:
            k = s[5]
        key_daily[k][d] += rev

    for g in groups:
        gk = g["id"] if group_by in ("product", "seller") else g["label"]
        vals = [key_daily[gk].get(d, 0.0) for d in last7]
        mx = max(vals) if vals else 0
        g["spark"] = [round(v / mx, 3) if mx > 0 else 0.0 for v in vals]
    return last7


@router.get("/reports")
def reports_page(
    request: Request,
    period: str = "month",
    category: str = "",
    seller_id: int = 0,
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
        "category": category, "seller_id": seller_id, "seller_name": "",
        "shops": [], "summary": (0, 0, 0, 0),
        "groups": [], "all_sales": [], "error": None,
        "chart_labels": [], "chart_data": [], "chart_dates": [],
        "prev_revenue": None, "growth_pct": None,
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
        from web.routes.sales import _get_user_allowed_shops
        allowed_shops = _get_user_allowed_shops(telegram_id, db)
        ctx["shops"] = allowed_shops
        if is_admin and set(allowed_shops) == set(db.get_all_shops() or []):
            # Unrestricted admin — show full shop list
            ctx["shops"] = db.get_all_shops() or []

        all_shops = db.get_all_shops() or []
        _scoped = bool(allowed_shops) and set(allowed_shops) != set(all_shops)

        kwargs: dict = {"start_date": df, "end_date": dt}
        if shop:
            # Validate requested shop against allowed scope for all users (incl. scoped admin)
            if _scoped and shop not in allowed_shops:
                shop = allowed_shops[0] if allowed_shops else shop
                ctx["shop"] = shop
            kwargs["shop_name"] = shop
        elif _scoped:
            # Auto-apply scope restriction (non-admin and scoped admin)
            kwargs["shop_names"] = allowed_shops

        all_sales = db.get_sales_report(**kwargs) or []
        # Category drill-down: filter by selected category
        if category:
            all_sales = [s for s in all_sales if (s[8] or "Без категории") == category]
        # Seller drill-down: filter by user_db_id
        if seller_id:
            all_sales = [s for s in all_sales if int(s[5] or 0) == seller_id]
            if all_sales:
                fname = (all_sales[0][9] or "").strip()
                lname = (all_sales[0][10] or "").strip()
                ctx["seller_name"] = f"{fname} {lname}".strip() or f"Продавец #{seller_id}"
        ctx["all_sales"] = all_sales
        summary_kwargs: dict = {"start_date": df, "end_date": dt}
        if shop:
            summary_kwargs["shop_name"] = shop
        elif not is_admin and "shop_names" in kwargs:
            summary_kwargs["shop_names"] = kwargs["shop_names"]
        ctx["summary"] = db.get_sales_summary(**summary_kwargs) or (0, 0, 0, 0)
        ctx["groups"] = _aggregate(all_sales, group_by)
        _add_abc_badges(ctx["groups"])
        _compute_sparklines(ctx["groups"], all_sales, group_by)

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
            ctx["chart_labels"] = [d[8:10] + '-' + d[5:7] for d in all_days]   # DD-MM
            ctx["chart_data"]   = [int(daily_rev.get(d, 0)) for d in all_days]
            ctx["chart_dates"]  = list(all_days)  # ISO strings for drill-down

            # Previous period revenue for growth indicator
            try:
                from datetime import timedelta
                import datetime as _dt2
                cur_start = _dt2.date.fromisoformat(df)
                cur_end   = _dt2.date.fromisoformat(dt)
                span = (cur_end - cur_start).days
                prev_end   = cur_start - timedelta(days=1)
                prev_start = prev_end - timedelta(days=span)
                prev_kwargs: dict = {"start_date": prev_start.isoformat(), "end_date": prev_end.isoformat()}
                if shop:
                    prev_kwargs["shop_name"] = shop
                elif _scoped:
                    prev_kwargs["shop_names"] = allowed_shops
                prev_summary = db.get_sales_summary(**prev_kwargs) or (0, 0, 0, 0)
                prev_revenue = float(prev_summary[2] or 0)
                cur_revenue  = float(ctx["summary"][2] or 0)
                if prev_revenue > 0:
                    growth_pct = round((cur_revenue - prev_revenue) / prev_revenue * 100, 1)
                elif cur_revenue > 0:
                    growth_pct = 100.0
                else:
                    growth_pct = 0.0
                ctx["prev_revenue"] = int(prev_revenue)
                ctx["growth_pct"]   = growth_pct
            except Exception:
                ctx["prev_revenue"] = None
                ctx["growth_pct"]   = None
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
        from subscription_utils import check_export_permission
        if not check_export_permission(telegram_id, org_db=org_db):
            return RedirectResponse(
                url="/reports?error=Экспорт+отчётов+недоступен+на+вашем+тарифе",
                status_code=302,
            )

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
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
        from openpyxl.chart import BarChart, Reference
        wb = openpyxl.Workbook()

        thin = Side(style="thin", color="D1D5DB")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)
        hdr_fill = PatternFill("solid", fgColor="DC2626")
        hdr_font = Font(color="FFFFFF", bold=True, size=11)
        even_fill = PatternFill("solid", fgColor="FFF5F5")
        tot_fill = PatternFill("solid", fgColor="FEE2E2")

        # ── Лист 1: Сводка ────────────────────────────────────────────────────
        ws1 = wb.active
        ws1.title = "Сводка"
        meta_rows = [
            ("Период", f"{date_from} — {date_to}"),
            ("Магазин", shop or "Все"),
            ("Кол-во продаж", summary[0] or 0),
            ("Общее кол-во ед.", summary[1] or 0),
            ("Выручка (₽)", round(float(summary[2] or 0), 2)),
            ("Средний чек (₽)", round(float(summary[3] or 0), 2)),
        ]
        ws1.append(["Показатель", "Значение"])
        for cell in ws1[1]:
            cell.fill = hdr_fill
            cell.font = hdr_font
            cell.border = border
        for label, val in meta_rows:
            ws1.append([label, val])
        for row in ws1.iter_rows(min_row=2, max_row=ws1.max_row):
            for cell in row:
                cell.border = border
            if isinstance(row[1].value, float):
                row[1].number_format = '#,##0.00 ₽'
                row[1].alignment = Alignment(horizontal="right")
        ws1.column_dimensions["A"].width = 22
        ws1.column_dimensions["B"].width = 22

        # ── Лист 2: По группам (с рамками, заморозкой, чередованием, графиком)
        ws2 = wb.create_sheet("По группам")
        grp_labels = {
            "product": "Товар", "category": "Категория",
            "shop": "Магазин", "seller": "Продавец",
        }
        g2_headers = [grp_labels.get(group_by, "Группа"), "Транзакций", "Кол-во ед.", "Выручка (₽)", "Доля (%)"]
        g2_widths = [30, 14, 14, 18, 12]
        for i, (h, w) in enumerate(zip(g2_headers, g2_widths), 1):
            cell = ws2.cell(row=1, column=i, value=h)
            cell.fill = hdr_fill
            cell.font = hdr_font
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = border
            ws2.column_dimensions[get_column_letter(i)].width = w
        ws2.row_dimensions[1].height = 26
        ws2.freeze_panes = "A2"

        total_rev = sum(g["revenue"] for g in groups)
        for row_idx, g in enumerate(groups, 2):
            row_fill = even_fill if row_idx % 2 == 0 else None
            pct = round(g["revenue"] / total_rev * 100, 1) if total_rev else 0
            for col_idx, val in enumerate(
                [g["label"], g["qty"], g["count"], round(g["revenue"], 2), pct], 1
            ):
                cell = ws2.cell(row=row_idx, column=col_idx, value=val)
                cell.border = border
                if row_fill:
                    cell.fill = row_fill
                if col_idx == 4:
                    cell.number_format = '#,##0.00 ₽'
                    cell.alignment = Alignment(horizontal="right")
                elif col_idx == 5:
                    cell.number_format = '0.0"%"'
                    cell.alignment = Alignment(horizontal="center")
                elif col_idx in (2, 3):
                    cell.alignment = Alignment(horizontal="center")

        # ИТОГО строка
        tr2 = len(groups) + 2
        for col in range(1, 6):
            ws2.cell(row=tr2, column=col).border = border
            ws2.cell(row=tr2, column=col).fill = tot_fill
        ws2.cell(row=tr2, column=1, value="ИТОГО").font = Font(bold=True)
        ws2.cell(row=tr2, column=2, value=sum(g["qty"] for g in groups)).font = Font(bold=True)
        ws2.cell(row=tr2, column=3, value=sum(g["count"] for g in groups)).font = Font(bold=True)
        rev_cell = ws2.cell(row=tr2, column=4, value=round(total_rev, 2))
        rev_cell.font = Font(bold=True)
        rev_cell.number_format = '#,##0.00 ₽'
        rev_cell.alignment = Alignment(horizontal="right")

        # BarChart на листе 2
        if 1 < len(groups) <= 30:
            chart2 = BarChart()
            chart2.type = "col"
            chart2.title = f"Выручка по {grp_labels.get(group_by, 'группам').lower()}"
            chart2.y_axis.title = "Выручка (₽)"
            chart2.style = 10
            chart2.width = 18
            chart2.height = 12
            data_ref2 = Reference(ws2, min_col=4, min_row=1, max_row=tr2 - 1)
            cats_ref2 = Reference(ws2, min_col=1, min_row=2, max_row=tr2 - 1)
            chart2.add_data(data_ref2, titles_from_data=True)
            chart2.set_categories(cats_ref2)
            ws2.add_chart(chart2, "G2")

        # ── Лист 3: Детализация (с рамками, заморозкой, чередованием) ────────
        ws3 = wb.create_sheet("Детализация")
        d3_headers = ["Дата", "Товар", "Категория", "Магазин", "Кол-во", "Цена (₽)", "Сумма (₽)", "Продавец"]
        d3_widths = [12, 28, 18, 20, 8, 12, 12, 22]
        for i, (h, w) in enumerate(zip(d3_headers, d3_widths), 1):
            cell = ws3.cell(row=1, column=i, value=h)
            cell.fill = hdr_fill
            cell.font = hdr_font
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = border
            ws3.column_dimensions[get_column_letter(i)].width = w
        ws3.row_dimensions[1].height = 26
        ws3.freeze_panes = "A2"

        even_fill3 = PatternFill("solid", fgColor="FFF5F5")
        d3_total_qty = 0
        d3_total_rev = 0.0
        for row_idx, s in enumerate(all_sales, 2):
            fname_ = (s[9] or "").strip()
            lname_ = (s[10] or "").strip()
            seller = f"{fname_} {lname_}".strip() or "—"
            qty = int(s[3] or 0)
            price = float(s[4] or 0)
            amount = round(qty * price, 2)
            d3_total_qty += qty
            d3_total_rev += amount
            row_fill3 = even_fill3 if row_idx % 2 == 0 else None
            for col_idx, val in enumerate(
                [str(s[6] or "")[:10], s[7] or "—", s[8] or "—", s[2] or "—",
                 qty, round(price, 2), amount, seller], 1
            ):
                cell = ws3.cell(row=row_idx, column=col_idx, value=val)
                cell.border = border
                if row_fill3:
                    cell.fill = row_fill3
                if col_idx in (6, 7):
                    cell.number_format = '#,##0.00 ₽'
                    cell.alignment = Alignment(horizontal="right")
                elif col_idx == 5:
                    cell.alignment = Alignment(horizontal="center")

        # ИТОГО детализации
        if all_sales:
            tr3 = len(all_sales) + 2
            for col in range(1, 9):
                ws3.cell(row=tr3, column=col).border = border
                ws3.cell(row=tr3, column=col).fill = tot_fill
            ws3.cell(row=tr3, column=1, value="ИТОГО").font = Font(bold=True)
            ws3.cell(row=tr3, column=5, value=d3_total_qty).font = Font(bold=True)
            rc = ws3.cell(row=tr3, column=7, value=round(d3_total_rev, 2))
            rc.font = Font(bold=True)
            rc.number_format = '#,##0.00 ₽'
            rc.alignment = Alignment(horizontal="right")

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
        logger.error(f"reports_export_xlsx error: {exc}\n{traceback.format_exc()}")
        return RedirectResponse(url="/reports?error=Ошибка+при+формировании+отчёта.+Попробуйте+позже.", status_code=302)


@router.get("/reports/heatmap")
def reports_heatmap(
    request: Request,
    period: str = "month",
    shop: str = "",
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
        "period": period, "shop": shop,
        "shops": [], "heatmap": {}, "max_revenue": 1,
        "date_from": "", "date_to": "", "error": None,
    }
    try:
        db = get_web_db(telegram_id, org_db)
        from timezone_utils import get_current_user_time
        tz = db.get_user_timezone(telegram_id)
        today = get_current_user_time(tz).date()
        df, dt = _period_dates(period, today)
        ctx["date_from"], ctx["date_to"] = df, dt
        ctx["shops"] = db.get_all_shops() or []

        raw = db.get_sales_heatmap(start_date=df, end_date=dt, shop_name=shop or None)
        # Build dict: {weekday: {hour: {"revenue": x, "count": y}}}
        heatmap: dict = {}
        max_rev = 0.0
        for row in raw:
            wd, hr, rev, cnt = int(row[0]), int(row[1]), float(row[2] or 0), int(row[3] or 0)
            heatmap.setdefault(wd, {})[hr] = {"revenue": rev, "count": cnt}
            if rev > max_rev:
                max_rev = rev
        ctx["heatmap"] = heatmap
        ctx["max_revenue"] = max_rev or 1
    except Exception as exc:
        ctx["error"] = str(exc)

    return request.app.state.templates.TemplateResponse(request, "reports/heatmap.html", ctx)


@router.get("/reports/abc")
def reports_abc(
    request: Request,
    period: str = "month",
    shop: str = "",
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
        "period": period, "shop": shop,
        "shops": [], "abc_items": [],
        "a_rev": 0, "b_rev": 0, "c_rev": 0,
        "a_count": 0, "b_count": 0, "c_count": 0,
        "date_from": "", "date_to": "", "error": None,
    }
    try:
        db = get_web_db(telegram_id, org_db)
        from timezone_utils import get_current_user_time
        tz = db.get_user_timezone(telegram_id)
        today = get_current_user_time(tz).date()
        df, dt = _period_dates(period, today)
        ctx["date_from"], ctx["date_to"] = df, dt
        ctx["shops"] = db.get_all_shops() or []

        kwargs: dict = {"start_date": df, "end_date": dt}
        if shop:
            kwargs["shop_name"] = shop
        all_sales = db.get_sales_report(**kwargs) or []

        # Aggregate by product
        products: dict = {}
        for s in all_sales:
            pid, name, cat = s[1], s[7] or "—", s[8] or "—"
            rev = float((s[3] or 0) * (s[4] or 0))
            qty = int(s[3] or 0)
            if pid not in products:
                products[pid] = {"id": pid, "name": name, "category": cat, "revenue": 0.0, "qty": 0, "count": 0}
            products[pid]["revenue"] += rev
            products[pid]["qty"] += qty
            products[pid]["count"] += 1

        items = sorted(products.values(), key=lambda x: x["revenue"], reverse=True)
        total_rev = sum(x["revenue"] for x in items) or 1

        # Assign ABC groups
        cumulative = 0.0
        for item in items:
            item["pct"] = round(item["revenue"] / total_rev * 100, 1)
            cumulative += item["revenue"]
            cum_pct = cumulative / total_rev * 100
            item["group"] = "A" if cum_pct <= 80 else ("B" if cum_pct <= 95 else "C")

        ctx["abc_items"] = items
        ctx["a_rev"]   = sum(x["revenue"] for x in items if x["group"] == "A")
        ctx["b_rev"]   = sum(x["revenue"] for x in items if x["group"] == "B")
        ctx["c_rev"]   = sum(x["revenue"] for x in items if x["group"] == "C")
        ctx["a_count"] = sum(1 for x in items if x["group"] == "A")
        ctx["b_count"] = sum(1 for x in items if x["group"] == "B")
        ctx["c_count"] = sum(1 for x in items if x["group"] == "C")
    except Exception as exc:
        ctx["error"] = str(exc)

    return request.app.state.templates.TemplateResponse(request, "reports/abc.html", ctx)
