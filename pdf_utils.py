"""
Генерация PDF-отчётов по продажам.
Использует reportlab для создания профессиональных отчётов.
"""
import os
import logging
import tempfile
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


def generate_pdf_report(
    org_name: str,
    start_date: str,
    end_date: str,
    sales_rows: list,
    summary: dict,
    top_sellers: list,
    top_products: list,
    output_path: Optional[str] = None,
) -> Optional[str]:
    """
    Генерирует PDF-отчёт по продажам.

    Args:
        org_name: Название организации / магазина
        start_date: Дата начала периода (YYYY-MM-DD)
        end_date: Дата конца периода (YYYY-MM-DD)
        sales_rows: Список строк продаж [(date, product, shop, qty, price, total, seller), ...]
        summary: {'total_sales': N, 'total_revenue': F, 'avg_sale': F}
        top_sellers: [(name, total_revenue, sales_count), ...]
        top_products: [(name, qty_sold, total_revenue), ...]
        output_path: Путь для сохранения. Если None — создаётся временный файл.

    Returns:
        Путь к созданному PDF или None при ошибке.
    """
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
        )
        from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
    except ImportError:
        logger.error("reportlab не установлен. Используйте: pip install reportlab")
        return None

    # ── Шрифты ─────────────────────────────────────────────────────────────────
    # Ищем шрифт с поддержкой кириллицы
    font_name = "Helvetica"
    bold_font = "Helvetica-Bold"
    try:
        font_paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
        ]
        for fp in font_paths:
            if os.path.exists(fp):
                pdfmetrics.registerFont(TTFont("CyrFont", fp))
                font_name = "CyrFont"
                # Для bold пробуем соответствующий bold-шрифт
                bold_fp = fp.replace("Regular", "Bold").replace("Sans.ttf", "Sans-Bold.ttf")
                if os.path.exists(bold_fp):
                    pdfmetrics.registerFont(TTFont("CyrFontBold", bold_fp))
                    bold_font = "CyrFontBold"
                else:
                    bold_font = font_name
                break
    except Exception:
        pass

    # ── Путь к файлу ────────────────────────────────────────────────────────────
    if not output_path:
        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False, dir="data")
        output_path = tmp.name
        tmp.close()

    # ── Цветовая схема ──────────────────────────────────────────────────────────
    PRIMARY   = colors.HexColor("#2563EB")    # синий
    ACCENT    = colors.HexColor("#1E40AF")    # тёмно-синий
    LIGHT_BG  = colors.HexColor("#EFF6FF")    # светло-голубой
    GRAY      = colors.HexColor("#6B7280")
    LIGHT_GR  = colors.HexColor("#F9FAFB")
    TABLE_HDR = colors.HexColor("#1D4ED8")

    # ── Стили ───────────────────────────────────────────────────────────────────
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "Title", fontName=bold_font, fontSize=20, textColor=ACCENT,
        alignment=TA_CENTER, spaceAfter=4
    )
    subtitle_style = ParagraphStyle(
        "Subtitle", fontName=font_name, fontSize=11, textColor=GRAY,
        alignment=TA_CENTER, spaceAfter=2
    )
    section_style = ParagraphStyle(
        "Section", fontName=bold_font, fontSize=13, textColor=ACCENT,
        spaceBefore=12, spaceAfter=6
    )
    normal_style = ParagraphStyle(
        "Normal2", fontName=font_name, fontSize=9, textColor=colors.black
    )
    small_gray_style = ParagraphStyle(
        "SmallGray", fontName=font_name, fontSize=8, textColor=GRAY
    )

    # ── Форматирование дат ──────────────────────────────────────────────────────
    def fmt_date(d: str) -> str:
        try:
            return datetime.strptime(d, "%Y-%m-%d").strftime("%d.%m.%Y")
        except Exception:
            return d

    # ── Форматирование суммы ────────────────────────────────────────────────────
    def fmt_money(v) -> str:
        try:
            return f"{float(v):,.0f} ₽".replace(",", " ")
        except Exception:
            return str(v)

    # ── Построение документа ────────────────────────────────────────────────────
    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm,
        topMargin=2 * cm, bottomMargin=2 * cm,
    )
    story = []
    page_width = A4[0] - 4 * cm

    # ── Заголовок ───────────────────────────────────────────────────────────────
    story.append(Paragraph("Отчёт по продажам", title_style))
    story.append(Paragraph(org_name, subtitle_style))
    period_str = fmt_date(start_date) if start_date == end_date else f"{fmt_date(start_date)} — {fmt_date(end_date)}"
    story.append(Paragraph(f"Период: {period_str}", subtitle_style))
    story.append(Spacer(1, 4))
    story.append(HRFlowable(width="100%", thickness=2, color=PRIMARY))
    story.append(Spacer(1, 8))

    # ── Сводка ──────────────────────────────────────────────────────────────────
    total_sales   = summary.get('total_sales', 0)
    total_revenue = summary.get('total_revenue', 0)
    avg_sale      = summary.get('avg_sale', 0)

    story.append(Paragraph("📊 Сводка", section_style))

    summary_data = [
        ["Показатель", "Значение"],
        ["Количество продаж", str(total_sales)],
        ["Общая выручка", fmt_money(total_revenue)],
        ["Средний чек", fmt_money(avg_sale)],
    ]
    s_table = Table(summary_data, colWidths=[page_width * 0.6, page_width * 0.4])
    s_table.setStyle(TableStyle([
        ("BACKGROUND",  (0, 0), (-1, 0), TABLE_HDR),
        ("TEXTCOLOR",   (0, 0), (-1, 0), colors.white),
        ("FONTNAME",    (0, 0), (-1, 0), bold_font),
        ("FONTSIZE",    (0, 0), (-1, -1), 9),
        ("FONTNAME",    (0, 1), (-1, -1), font_name),
        ("BACKGROUND",  (0, 1), (-1, 1), LIGHT_BG),
        ("BACKGROUND",  (0, 3), (-1, 3), LIGHT_BG),
        ("GRID",        (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ("ALIGN",       (1, 0), (1, -1), "RIGHT"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_GR]),
        ("TOPPADDING",  (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(s_table)
    story.append(Spacer(1, 8))

    # ── Топ продавцы ─────────────────────────────────────────────────────────────
    if top_sellers:
        story.append(Paragraph("🏆 Топ продавцов", section_style))
        seller_data = [["Продавец", "Продаж", "Выручка"]]
        for row in top_sellers[:10]:
            name  = str(row[0] or "—")[:30]
            cnt   = str(row[2] if len(row) > 2 else "—")
            rev   = fmt_money(row[1] if len(row) > 1 else 0)
            seller_data.append([name, cnt, rev])

        sel_widths = [page_width * 0.55, page_width * 0.15, page_width * 0.30]
        sel_table = Table(seller_data, colWidths=sel_widths)
        sel_table.setStyle(TableStyle([
            ("BACKGROUND",  (0, 0), (-1, 0), TABLE_HDR),
            ("TEXTCOLOR",   (0, 0), (-1, 0), colors.white),
            ("FONTNAME",    (0, 0), (-1, 0), bold_font),
            ("FONTNAME",    (0, 1), (-1, -1), font_name),
            ("FONTSIZE",    (0, 0), (-1, -1), 9),
            ("GRID",        (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
            ("ALIGN",       (1, 0), (-1, -1), "RIGHT"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_GR]),
            ("TOPPADDING",  (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(sel_table)
        story.append(Spacer(1, 8))

    # ── Топ товары ───────────────────────────────────────────────────────────────
    if top_products:
        story.append(Paragraph("📦 Топ товаров", section_style))
        prod_data = [["Товар", "Кол-во", "Выручка"]]
        for row in top_products[:10]:
            name = str(row[0] or "—")[:30]
            qty  = str(row[1] if len(row) > 1 else "—")
            rev  = fmt_money(row[2] if len(row) > 2 else 0)
            prod_data.append([name, qty, rev])

        prod_widths = [page_width * 0.55, page_width * 0.15, page_width * 0.30]
        prod_table = Table(prod_data, colWidths=prod_widths)
        prod_table.setStyle(TableStyle([
            ("BACKGROUND",  (0, 0), (-1, 0), TABLE_HDR),
            ("TEXTCOLOR",   (0, 0), (-1, 0), colors.white),
            ("FONTNAME",    (0, 0), (-1, 0), bold_font),
            ("FONTNAME",    (0, 1), (-1, -1), font_name),
            ("FONTSIZE",    (0, 0), (-1, -1), 9),
            ("GRID",        (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
            ("ALIGN",       (1, 0), (-1, -1), "RIGHT"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_GR]),
            ("TOPPADDING",  (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(prod_table)
        story.append(Spacer(1, 8))

    # ── Детальная таблица продаж (до 50 строк) ──────────────────────────────────
    if sales_rows:
        story.append(Paragraph(f"📋 Детализация продаж (показано {min(len(sales_rows), 50)} из {len(sales_rows)})", section_style))

        detail_data = [["Дата", "Товар", "Магазин", "Кол-во", "Сумма", "Продавец"]]
        for row in sales_rows[:50]:
            dt      = str(row[0] or "")[:10]
            product = str(row[1] or "—")[:22]
            shop    = str(row[2] or "—")[:15]
            qty     = str(row[3] or "—")
            total   = fmt_money(row[4] if len(row) > 4 else 0)
            seller  = str(row[5] if len(row) > 5 else "—")[:15]
            detail_data.append([dt, product, shop, qty, total, seller])

        col_w = [
            page_width * 0.11,
            page_width * 0.26,
            page_width * 0.18,
            page_width * 0.07,
            page_width * 0.16,
            page_width * 0.22,
        ]
        det_table = Table(detail_data, colWidths=col_w, repeatRows=1)
        det_table.setStyle(TableStyle([
            ("BACKGROUND",  (0, 0), (-1, 0), TABLE_HDR),
            ("TEXTCOLOR",   (0, 0), (-1, 0), colors.white),
            ("FONTNAME",    (0, 0), (-1, 0), bold_font),
            ("FONTNAME",    (0, 1), (-1, -1), font_name),
            ("FONTSIZE",    (0, 0), (-1, -1), 7.5),
            ("GRID",        (0, 0), (-1, -1), 0.3, colors.HexColor("#CBD5E1")),
            ("ALIGN",       (3, 0), (4, -1), "RIGHT"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_GR]),
            ("TOPPADDING",  (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(det_table)

    # ── Футер ────────────────────────────────────────────────────────────────────
    story.append(Spacer(1, 12))
    story.append(HRFlowable(width="100%", thickness=0.5, color=GRAY))
    story.append(Spacer(1, 4))
    generated_at = datetime.now().strftime("%d.%m.%Y %H:%M")
    story.append(Paragraph(
        f"Сформировано: {generated_at}",
        small_gray_style
    ))

    doc.build(story)
    return output_path


async def generate_pdf_for_report(db, start_date: str, end_date: str,
                                   org_name: str = "Организация",
                                   scope_filter: Optional[dict] = None) -> Optional[str]:
    """
    Высокоуровневая функция: собирает данные из БД и генерирует PDF.
    Вызывается из reports_handlers.py.
    """
    import asyncio

    def _build():
        try:
            # Продажи за период
            sales = db.get_sales_by_period(start_date, end_date) or []

            # Сводка
            total_s   = len(sales)
            total_rev = sum(float(r[3] or 0) * int(r[4] or 1) for r in sales) if sales else 0
            avg_sale  = (total_rev / total_s) if total_s > 0 else 0

            summary = {
                'total_sales': total_s,
                'total_revenue': total_rev,
                'avg_sale': avg_sale,
            }

            # Топ продавцы
            from collections import defaultdict
            seller_stats: dict = defaultdict(lambda: [0, 0])  # {name: [revenue, count]}
            for r in sales:
                seller = r[5] if len(r) > 5 else "—"
                rev    = float(r[3] or 0) * int(r[4] or 1)
                seller_stats[seller][0] += rev
                seller_stats[seller][1] += 1
            top_sellers = sorted(
                [(k, v[0], v[1]) for k, v in seller_stats.items()],
                key=lambda x: x[1], reverse=True
            )[:10]

            # Топ товары
            prod_stats: dict = defaultdict(lambda: [0, 0])  # {name: [qty, revenue]}
            for r in sales:
                prod = r[1] if len(r) > 1 else "—"
                qty  = int(r[4] or 1)
                rev  = float(r[3] or 0) * qty
                prod_stats[prod][0] += qty
                prod_stats[prod][1] += rev
            top_products = sorted(
                [(k, v[0], v[1]) for k, v in prod_stats.items()],
                key=lambda x: x[1], reverse=True
            )[:10]

            # Строки детализации: (date, product, shop, qty, total, seller)
            detail_rows = []
            for r in sales:
                date    = str(r[0] or "")[:10] if r else ""
                product = str(r[1] or "—") if len(r) > 1 else "—"
                shop    = str(r[2] or "—") if len(r) > 2 else "—"
                price   = float(r[3] or 0) if len(r) > 3 else 0
                qty     = int(r[4] or 1) if len(r) > 4 else 1
                seller  = str(r[5] or "—") if len(r) > 5 else "—"
                total   = price * qty
                detail_rows.append((date, product, shop, qty, total, seller))

            return generate_pdf_report(
                org_name=org_name,
                start_date=start_date,
                end_date=end_date,
                sales_rows=detail_rows,
                summary=summary,
                top_sellers=top_sellers,
                top_products=top_products,
            )
        except Exception as e:
            logger.error(f"generate_pdf_for_report._build: {e}", exc_info=True)
            return None

    return await asyncio.to_thread(_build)
