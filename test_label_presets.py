"""Регресс-тесты библиотеки пресетов дизайна ценников.

Покрывает CRUD-методы пресетов (create/list/get/rename/update/delete) и
lossless-roundtrip: сохранённый пресет (включая logo_path/шрифт/тему/JSON)
после «применения» через save_label_settings полностью восстанавливает
активные настройки get_label_settings.
"""
import os
import tempfile

from database import Database

PASS, FAIL = "✅", "❌"
results = []


def check(label, condition, detail=""):
    status = PASS if condition else FAIL
    results.append((status, label, detail))
    if not condition:
        print(f"  {FAIL} ПРОВАЛ: {label}" + (f" | {detail}" if detail else ""))


def _make_design(**over):
    d = {
        "bg_color": "#101010",
        "text_color": "#fefefe",
        "price_color": "#ff2200",
        "logo_path": "/static/product_photos/abc123/logo.png",
        "font_size": "large",
        "font_family": "Georgia, 'Times New Roman', serif",
        "border_color": "#000000",
        "border_width": "3",
        "label_theme": "dark",
        "element_order": '["name", "price", "barcode"]',
        "visible_elements": '{"name": true, "price": true, "barcode": false}',
        "sale_badge": "АКЦИЯ",
        "qr_content": "https://shop.ru/p/{article}",
    }
    d.update(over)
    return d


def main():
    path = tempfile.mktemp(suffix=".db")
    db = Database(path)
    db.create_tables()
    try:
        # ── create / list / get ──
        des = _make_design()
        pid = db.create_label_preset("Чёрная пятница", des)
        check("create возвращает id", bool(pid), f"id={pid}")

        lst = db.list_label_presets()
        check("list содержит 1 пресет", len(lst) == 1, f"len={len(lst)}")
        check("list имеет name", lst and lst[0]["name"] == "Чёрная пятница")

        got = db.get_label_preset(pid)
        for k, v in des.items():
            check(f"get сохранил поле {k}", got.get(k) == v, f"{got.get(k)!r} != {v!r}")

        # ── lossless roundtrip: apply (save_label_settings) → get_label_settings ──
        p = db.get_label_preset(pid)
        db.save_label_settings(
            bg_color=p["bg_color"], text_color=p["text_color"],
            price_color=p["price_color"], logo_path=p.get("logo_path") or "",
            font_size=p["font_size"], font_family=p["font_family"],
            border_color=p["border_color"], border_width=p["border_width"],
            label_theme=p["label_theme"], element_order=p["element_order"],
            visible_elements=p["visible_elements"], sale_badge=p["sale_badge"],
            qr_content=p.get("qr_content") or "",
        )
        act = db.get_label_settings()
        # get_label_settings парсит JSON-поля в объекты — сверяем их отдельно.
        import json as _json
        for k in ("bg_color", "text_color", "price_color", "logo_path",
                  "font_size", "font_family", "border_color", "border_width",
                  "label_theme", "sale_badge", "qr_content"):
            check(f"roundtrip активных настроек: {k}",
                  act.get(k) == des[k], f"{act.get(k)!r} != {des[k]!r}")
        check("roundtrip element_order (parsed)",
              act.get("element_order") == _json.loads(des["element_order"]),
              f"{act.get('element_order')!r}")
        check("roundtrip visible_elements (parsed)",
              act.get("visible_elements") == _json.loads(des["visible_elements"]),
              f"{act.get('visible_elements')!r}")

        # ── rename ──
        db.rename_label_preset(pid, "БлэкФрайдей")
        check("rename применён", db.get_label_preset(pid)["name"] == "БлэкФрайдей")

        # ── update ──
        db.update_label_preset(pid, _make_design(bg_color="#222222", sale_badge="-50%"))
        upd = db.get_label_preset(pid)
        check("update bg_color", upd["bg_color"] == "#222222")
        check("update sale_badge", upd["sale_badge"] == "-50%")
        check("update сохранил logo_path",
              upd["logo_path"] == des["logo_path"], upd["logo_path"])

        # ── второй пресет + сортировка/изоляция ──
        pid2 = db.create_label_preset("Минимал", _make_design(label_theme="standard"))
        check("два пресета в list", len(db.list_label_presets()) == 2)

        # ── delete ──
        db.delete_label_preset(pid2)
        names = [x["name"] for x in db.list_label_presets()]
        check("delete убрал пресет", "Минимал" not in names, str(names))
        check("delete не задел другой", "БлэкФрайдей" in names, str(names))

        # ── get несуществующего ──
        check("get несуществующего → {}", db.get_label_preset(999999) == {})
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    fails = sum(1 for s, _, _ in results if s == FAIL)
    print("=" * 50)
    print(f"Тестов: {len(results)} | Провалов: {fails}")
    print(f"{PASS} ВСЕ ТЕСТЫ ПРОЙДЕНЫ" if fails == 0 else f"{FAIL} ЕСТЬ ПРОВАЛЫ")
    return fails


if __name__ == "__main__":
    raise SystemExit(1 if main() else 0)
