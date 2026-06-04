import logging
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, Response
from datetime import date
from typing import List, Optional

logger = logging.getLogger(__name__)
router = APIRouter()

CONTEST_TYPE_LABELS = {
    "any": "Любые продажи",
    "product": "По товару",
    "category": "По категории",
    "shop": "Магазины",
    "seller": "Продавцы",
    "shop_product": "Товары по магазину",
}
METRIC_LABELS = {
    "turnover": "Выручка", "quantity": "Количество",
    "product_quantity": "Кол-во товара",
}
REWARD_TYPE_LABELS = {
    "fixed": "Фиксированная сумма (₽)",
    "percent": "Процент от оборота (%)",
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
    msg: str = "",
):
    from web.auth import get_session_user
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    today = date.today()

    from web.auth import get_csrf_token
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
        "msg": msg,
        "csrf_token": get_csrf_token(request),
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
                            "shop_name": r[0] or None,
                            "value": val,
                            "pct": round(val / max(max_val, 1) * 100),
                        })
                else:
                    raw_lb = db.get_sales_ranking(**kwargs) or []
                    # first_name[0] last_name[1] shop_name[2] total_sold[3] total_revenue[4] ... user_db_id[7]
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
                            "user_db_id": r[7] if len(r) > 7 else None,
                        })
                ctx["leaderboard"] = leaderboard

    except Exception as exc:
        ctx["error"] = "Произошла внутренняя ошибка. Попробуйте позже."

    return request.app.state.templates.TemplateResponse(
        request, "contests/index.html", ctx
    )


def _load_contest_form_data(db):
    """Load shops, cities, categories, products for the contest form."""
    shops, cities, categories, products = [], [], [], []
    try:
        shops = db.get_all_shops() or []
    except Exception:
        pass
    try:
        cities = db.get_all_cities() or []
    except Exception:
        pass
    try:
        categories = [c for c in (db.get_all_categories() or []) if c]
    except Exception:
        pass
    try:
        raw = db.get_all_products() or []
        products = [
            {"id": p[0], "name": p[1], "category": p[2] or ""}
            for p in raw[:300]
        ]
        products.sort(key=lambda p: (p["category"], p["name"]))
    except Exception:
        pass
    return shops, cities, categories, products


@router.get("/contests/new")
def contests_new(request: Request):
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/contests", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    db = get_web_db(telegram_id, org_db)
    shops, cities, categories, products = _load_contest_form_data(db)

    today = date.today().isoformat()
    ctx = {
        "request": request, "user": user, "is_admin": True,
        "shops": shops, "cities": cities,
        "categories": categories, "products": products,
        "contest_type_labels": CONTEST_TYPE_LABELS,
        "metric_labels": METRIC_LABELS,
        "reward_type_labels": REWARD_TYPE_LABELS,
        "csrf_token": get_csrf_token(request),
        "error": None, "form_data": None,
        "today": today,
    }
    return request.app.state.templates.TemplateResponse(request, "contests/form.html", ctx)


@router.post("/contests/create")
def contests_create(
    request: Request,
    csrf_token: str = Form(default=""),
    title: str = Form(...),
    description: str = Form(default=""),
    contest_type: str = Form(default="any"),
    metric_type: str = Form(default="turnover"),
    target_value: str = Form(default=""),
    reward_type: str = Form(default="fixed"),
    reward_value: str = Form(default=""),
    start_date: str = Form(...),
    end_date: str = Form(...),
    shop_filter: str = Form(default=""),
    city_filter: str = Form(default=""),
    filter_categories: List[str] = Form(default=[]),
    filter_products: List[str] = Form(default=[]),
    reward_mode: str = Form(default="total"),
    tier_count: int = Form(default=0),
    tier_pct_1: str = Form(default=""), tier_bonus_1: str = Form(default=""),
    tier_pct_2: str = Form(default=""), tier_bonus_2: str = Form(default=""),
    tier_pct_3: str = Form(default=""), tier_bonus_3: str = Form(default=""),
    indiv_enabled: str = Form(default=""),
    indiv_shops: str = Form(default=""),
    indiv_target_1: str = Form(default=""),
    indiv_target_2: str = Form(default=""),
    indiv_target_3: str = Form(default=""),
    indiv_target_4: str = Form(default=""),
    indiv_target_5: str = Form(default=""),
    indiv_target_6: str = Form(default=""),
    indiv_target_7: str = Form(default=""),
    indiv_target_8: str = Form(default=""),
    indiv_target_9: str = Form(default=""),
    indiv_target_10: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token, get_csrf_token
    from web.deps import get_web_db
    import json as _json

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/contests", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return Response(content="Недействительный CSRF-токен. Обновите страницу и попробуйте снова.",
                        status_code=403)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    db = get_web_db(telegram_id, org_db)
    shops, cities, categories, products = _load_contest_form_data(db)
    error = None

    def _re_render(err):
        ctx = {
            "request": request, "user": user, "is_admin": True,
            "shops": shops, "cities": cities,
            "categories": categories, "products": products,
            "contest_type_labels": CONTEST_TYPE_LABELS,
            "metric_labels": METRIC_LABELS,
            "reward_type_labels": REWARD_TYPE_LABELS,
            "csrf_token": get_csrf_token(request),
            "error": err,
            "today": date.today().isoformat(),
            "form_data": {
                "title": title, "description": description,
                "contest_type": contest_type, "metric_type": metric_type,
                "target_value": target_value, "reward_type": reward_type,
                "reward_value": reward_value, "start_date": start_date,
                "end_date": end_date, "shop_filter": shop_filter,
                "city_filter": city_filter,
                "filter_categories": filter_categories,
                "filter_products": [str(p) for p in filter_products],
            },
        }
        return request.app.state.templates.TemplateResponse(request, "contests/form.html", ctx)

    try:
        title_clean = title.strip()
        if not title_clean:
            return _re_render("Введите название конкурса.")

        if contest_type not in ("any", "product", "category"):
            return _re_render("Выберите тип конкурса.")
        if metric_type not in ("turnover", "quantity"):
            return _re_render("Выберите метрику.")
        if reward_type not in ("fixed", "percent"):
            return _re_render("Выберите тип награды.")

        if not start_date or not end_date:
            return _re_render("Укажите даты начала и конца конкурса.")
        from datetime import date as _date
        sd = _date.fromisoformat(start_date)
        ed = _date.fromisoformat(end_date)
        if ed <= sd:
            return _re_render("Дата окончания должна быть позже даты начала.")

        tv = 0.0
        if target_value.strip():
            try:
                tv = float(target_value.replace(",", ".").strip())
            except ValueError:
                return _re_render("Целевое значение должно быть числом.")

        rv = reward_value.strip() or ""

        category_filter = None
        product_filter = None
        if contest_type == "category":
            if not filter_categories:
                return _re_render("Выберите хотя бы одну категорию.")
            category_filter = _json.dumps(filter_categories, ensure_ascii=False)
        elif contest_type == "product":
            if not filter_products:
                return _re_render("Выберите хотя бы один товар.")
            product_filter = _json.dumps([int(p) for p in filter_products])

        # Build individual_targets JSON (N4)
        individual_targets = None
        if indiv_enabled and indiv_shops.strip():
            shop_list = [s.strip() for s in indiv_shops.split(",") if s.strip()]
            target_vals = [
                indiv_target_1, indiv_target_2, indiv_target_3, indiv_target_4,
                indiv_target_5, indiv_target_6, indiv_target_7, indiv_target_8,
                indiv_target_9, indiv_target_10,
            ]
            targets_dict = {}
            for idx, sh in enumerate(shop_list):
                raw = target_vals[idx] if idx < len(target_vals) else ""
                if raw.strip():
                    try:
                        targets_dict[sh] = float(raw.replace(",", ".").strip())
                    except ValueError:
                        pass
            if targets_dict:
                individual_targets = _json.dumps(targets_dict, ensure_ascii=False)

        # Effective reward_mode (N3)
        eff_reward_mode = reward_mode if reward_mode in ("total", "per_sale") else "total"

        new_id = db.create_contest(
            title=title_clean,
            description=description.strip() or None,
            contest_type=contest_type,
            metric_type=metric_type,
            target_value=tv,
            reward_type=reward_type,
            reward_value=rv,
            start_date=start_date,
            end_date=end_date,
            shop_filter=shop_filter.strip() or None,
            city_filter=city_filter.strip() or None,
            category_filter=category_filter,
            product_filter=product_filter,
            created_by=telegram_id,
            reward_mode=eff_reward_mode,
            individual_targets=individual_targets,
        )
        if not new_id:
            return _re_render("Не удалось создать конкурс. Попробуйте ещё раз.")

        # Save per_sale tiers (N3)
        if eff_reward_mode == "per_sale" and tier_count > 0:
            tier_pcts = [tier_pct_1, tier_pct_2, tier_pct_3]
            tier_bonuses = [tier_bonus_1, tier_bonus_2, tier_bonus_3]
            tiers = []
            for i in range(min(tier_count, 3)):
                pct_raw = tier_pcts[i].strip()
                bon_raw = tier_bonuses[i].strip()
                if pct_raw and bon_raw:
                    try:
                        tiers.append({
                            "min_plan_pct": float(pct_raw.replace(",", ".")),
                            "bonuses": [{"product_id": None, "product_name": "all",
                                         "bonus_per_unit": float(bon_raw.replace(",", "."))}],
                        })
                    except ValueError:
                        pass
            if tiers:
                try:
                    db.save_contest_product_bonuses(new_id, tiers)
                except Exception as tier_exc:
                    logger.warning(f"save tiers failed for contest {new_id}: {tier_exc}")

        return RedirectResponse(url=f"/contests?contest_id={new_id}", status_code=303)

    except Exception as exc:
        logger.error(f"contests_create error: {exc}")
        return _re_render("Ошибка при создании конкурса. Попробуйте ещё раз.")


@router.get("/contests/{contest_id}/edit")
def contests_edit(request: Request, contest_id: int):
    from web.auth import get_session_user, get_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/contests", status_code=302)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    db = get_web_db(telegram_id, org_db)

    try:
        row = db.get_contest(contest_id)
    except Exception:
        row = None

    if not row:
        return RedirectResponse(url="/contests", status_code=302)

    status = row[16] or "pending"
    if status in ("finished", "cancelled"):
        return RedirectResponse(url=f"/contests?contest_id={contest_id}", status_code=302)

    import json as _json
    def _parse_json_list(val):
        if not val:
            return []
        try:
            return _json.loads(val)
        except Exception:
            return []

    shops, cities, categories, products = _load_contest_form_data(db)

    form_data = {
        "title": row[1] or "",
        "description": row[2] or "",
        "contest_type": row[3] or "any",
        "metric_type": row[4] or "turnover",
        "target_value": str(row[5]) if row[5] else "",
        "reward_type": row[6] or "fixed",
        "reward_value": row[7] or "",
        "start_date": row[8] or "",
        "end_date": row[9] or "",
        "shop_filter": row[10] or "",
        "city_filter": row[11] or "",
        "filter_categories": _parse_json_list(row[14]),
        "filter_products": [str(p) for p in _parse_json_list(row[13])],
    }

    ctx = {
        "request": request, "user": user, "is_admin": True,
        "shops": shops, "cities": cities,
        "categories": categories, "products": products,
        "contest_type_labels": CONTEST_TYPE_LABELS,
        "metric_labels": METRIC_LABELS,
        "reward_type_labels": REWARD_TYPE_LABELS,
        "csrf_token": get_csrf_token(request),
        "error": None,
        "form_data": form_data,
        "today": date.today().isoformat(),
        "is_edit": True,
        "edit_id": contest_id,
        "edit_status": status,
    }
    return request.app.state.templates.TemplateResponse(request, "contests/form.html", ctx)


@router.post("/contests/{contest_id}/update")
def contests_update(
    request: Request,
    contest_id: int,
    csrf_token: str = Form(default=""),
    start_date: str = Form(...),
    end_date: str = Form(...),
    target_value: str = Form(default=""),
    reward_value: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token, get_csrf_token
    from web.deps import get_web_db
    import json as _json

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/contests", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return Response(content="Недействительный CSRF-токен. Обновите страницу и попробуйте снова.",
                        status_code=403)

    telegram_id = int(user["sub"])
    org_db = user.get("org_db")
    db = get_web_db(telegram_id, org_db)

    try:
        row = db.get_contest(contest_id)
    except Exception:
        row = None

    if not row:
        return RedirectResponse(url="/contests", status_code=302)

    status = row[16] or "pending"
    if status in ("finished", "cancelled"):
        return RedirectResponse(url=f"/contests?contest_id={contest_id}", status_code=302)

    def _parse_json_list(val):
        if not val:
            return []
        try:
            return _json.loads(val)
        except Exception:
            return []

    shops, cities, categories, products = _load_contest_form_data(db)

    form_data = {
        "title": row[1] or "",
        "description": row[2] or "",
        "contest_type": row[3] or "any",
        "metric_type": row[4] or "turnover",
        "target_value": target_value,
        "reward_type": row[6] or "fixed",
        "reward_value": reward_value,
        "start_date": start_date,
        "end_date": end_date,
        "shop_filter": row[10] or "",
        "city_filter": row[11] or "",
        "filter_categories": _parse_json_list(row[14]),
        "filter_products": [str(p) for p in _parse_json_list(row[13])],
    }

    def _re_render(err):
        ctx = {
            "request": request, "user": user, "is_admin": True,
            "shops": shops, "cities": cities,
            "categories": categories, "products": products,
            "contest_type_labels": CONTEST_TYPE_LABELS,
            "metric_labels": METRIC_LABELS,
            "reward_type_labels": REWARD_TYPE_LABELS,
            "csrf_token": get_csrf_token(request),
            "error": err,
            "today": date.today().isoformat(),
            "form_data": form_data,
            "is_edit": True,
            "edit_id": contest_id,
            "edit_status": status,
        }
        return request.app.state.templates.TemplateResponse(request, "contests/form.html", ctx)

    try:
        if not start_date or not end_date:
            return _re_render("Укажите даты начала и конца конкурса.")
        from datetime import date as _date
        sd = _date.fromisoformat(start_date)
        ed = _date.fromisoformat(end_date)
        if ed <= sd:
            return _re_render("Дата окончания должна быть позже даты начала.")

        tv = 0.0
        if target_value.strip():
            try:
                tv = float(target_value.replace(",", ".").strip())
            except ValueError:
                return _re_render("Целевое значение должно быть числом.")

        rv = reward_value.strip() or ""

        updated = db.update_contest(
            contest_id,
            start_date=start_date,
            end_date=end_date,
            target_value=tv,
            reward_value=rv,
        )
        if not updated:
            return _re_render("Не удалось сохранить изменения. Попробуйте ещё раз.")

        return RedirectResponse(url=f"/contests?contest_id={contest_id}", status_code=303)

    except Exception as exc:
        logger.error(f"contests_update error: {exc}")
        return _re_render("Ошибка при сохранении. Попробуйте ещё раз.")


@router.post("/contests/{contest_id}/finish")
def contests_finish(
    request: Request,
    contest_id: int,
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/contests", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return Response(content="Недействительный CSRF-токен.", status_code=403)

    try:
        telegram_id = int(user["sub"])
        org_db = user.get("org_db")
        db = get_web_db(telegram_id, org_db)
        row = db.get_contest(contest_id)
        if row and row[16] == "active":
            db.update_contest_status(contest_id, "finished")
    except Exception as exc:
        logger.error(f"contests_finish error: {exc}")

    return RedirectResponse(url=f"/contests?contest_id={contest_id}", status_code=303)


@router.post("/contests/{contest_id}/delete")
def contests_delete(
    request: Request,
    contest_id: int,
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/contests", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return Response(content="Недействительный CSRF-токен.", status_code=403)

    try:
        telegram_id = int(user["sub"])
        org_db = user.get("org_db")
        db = get_web_db(telegram_id, org_db)
        row = db.get_contest(contest_id)
        if row and row[16] in ("finished", "cancelled"):
            db.delete_contest(contest_id)
            return RedirectResponse(url="/contests?msg=deleted", status_code=303)
    except Exception as exc:
        logger.error(f"contests_delete error: {exc}")

    return RedirectResponse(url=f"/contests?contest_id={contest_id}", status_code=303)


@router.post("/contests/clear-archive")
def contests_clear_archive(
    request: Request,
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/contests", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return Response(content="Недействительный CSRF-токен.", status_code=403)

    try:
        telegram_id = int(user["sub"])
        org_db = user.get("org_db")
        db = get_web_db(telegram_id, org_db)
        db.clear_contests_archive()
    except Exception as exc:
        logger.error(f"contests_clear_archive error: {exc}")

    return RedirectResponse(url="/contests?msg=archive_cleared", status_code=303)


@router.post("/contests/{contest_id}/cancel")
def contests_cancel(
    request: Request,
    contest_id: int,
    csrf_token: str = Form(default=""),
):
    from web.auth import get_session_user, verify_csrf_token
    from web.deps import get_web_db

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.get("role") not in ("owner", "admin", "super_admin"):
        return RedirectResponse(url="/contests", status_code=302)
    if not verify_csrf_token(request, csrf_token):
        return Response(content="Недействительный CSRF-токен.", status_code=403)

    try:
        telegram_id = int(user["sub"])
        org_db = user.get("org_db")
        db = get_web_db(telegram_id, org_db)
        row = db.get_contest(contest_id)
        if row and row[16] in ("active", "pending"):
            db.update_contest_status(contest_id, "cancelled")
    except Exception as exc:
        logger.error(f"contests_cancel error: {exc}")

    return RedirectResponse(url=f"/contests?contest_id={contest_id}", status_code=303)
