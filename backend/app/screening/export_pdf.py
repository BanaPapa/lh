"""일괄 심사 결과 PDF — A4 세로. 1쪽 결과표, 2쪽부터 건별 심사표.

배포 이미지(python:3.14-slim)에는 한글 글꼴이 없으므로 OFL 나눔고딕을 app/assets/fonts
에 함께 싣고 reportlab 에 등록한다. 글꼴이 없으면 한글이 네모로 깨진 PDF 가 나가므로
등록 실패는 조용히 넘기지 않고 예외로 낸다.
"""

from __future__ import annotations

import io
from pathlib import Path
from xml.sax.saxutils import escape

from app.screening.export_report import BatchReport, Block, SiteReport

PDF_MEDIA_TYPE = "application/pdf"
FONT_NAME = "NanumGothic"
FONT_BOLD = "NanumGothic-Bold"
FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

_registered = False


def register_fonts() -> None:
    """나눔고딕(OFL)을 reportlab 에 한 번만 등록한다."""

    global _registered
    if _registered:
        return
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    regular = FONT_DIR / "NanumGothic-Regular.ttf"
    bold = FONT_DIR / "NanumGothic-Bold.ttf"
    if not regular.exists() or not bold.exists():
        raise RuntimeError(f"한글 글꼴 파일이 없습니다: {FONT_DIR}")
    pdfmetrics.registerFont(TTFont(FONT_NAME, str(regular)))
    pdfmetrics.registerFont(TTFont(FONT_BOLD, str(bold)))
    pdfmetrics.registerFontFamily(FONT_NAME, normal=FONT_NAME, bold=FONT_BOLD, italic=FONT_NAME, boldItalic=FONT_BOLD)
    _registered = True


def _styles():
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle

    base = ParagraphStyle("base", fontName=FONT_NAME, fontSize=7.6, leading=10, wordWrap="CJK")
    return {
        "title": ParagraphStyle("title", parent=base, fontName=FONT_BOLD, fontSize=15, leading=19, spaceAfter=4),
        "subtitle": ParagraphStyle("subtitle", parent=base, fontSize=8.5, leading=11, textColor=colors.HexColor("#555555")),
        "site": ParagraphStyle("site", parent=base, fontName=FONT_BOLD, fontSize=12, leading=15, spaceAfter=2),
        "h2": ParagraphStyle("h2", parent=base, fontName=FONT_BOLD, fontSize=9.6, leading=13, spaceBefore=6, spaceAfter=2),
        "h3": ParagraphStyle("h3", parent=base, fontName=FONT_BOLD, fontSize=8.4, leading=11, spaceBefore=4, spaceAfter=1),
        "normal": base,
        "muted": ParagraphStyle("muted", parent=base, textColor=colors.HexColor("#555555")),
        "strong": ParagraphStyle("strong", parent=base, fontName=FONT_BOLD),
        "warning": ParagraphStyle("warning", parent=base, fontName=FONT_BOLD, textColor=colors.HexColor("#B45309")),
        "fail": ParagraphStyle("fail", parent=base, fontName=FONT_BOLD, fontSize=9.6, leading=13, spaceBefore=6, textColor=colors.HexColor("#B91C1C")),
        "review": ParagraphStyle("review", parent=base, fontName=FONT_BOLD, fontSize=9.6, leading=13, spaceBefore=6, textColor=colors.HexColor("#B45309")),
        "pass": ParagraphStyle("pass", parent=base, fontName=FONT_BOLD, fontSize=9.6, leading=13, spaceBefore=6, textColor=colors.HexColor("#047857")),
        "cell": ParagraphStyle("cell", parent=base, fontSize=6.9, leading=8.6),
        "cellhead": ParagraphStyle("cellhead", parent=base, fontName=FONT_BOLD, fontSize=6.9, leading=8.6),
        "cellsub": ParagraphStyle("cellsub", parent=base, fontSize=6.5, leading=8.2, textColor=colors.HexColor("#555555")),
        "bullet": ParagraphStyle("bullet", parent=base, leftIndent=8, bulletIndent=0),
    }


def _p(text: str, style) -> object:
    from reportlab.platypus import Paragraph

    return Paragraph(escape(text).replace("\n", "<br/>"), style)


def _table(columns: list[str], rows: list[list[str]], weights: list[float], width: float, styles,
           group_rows: set[int] = frozenset(), sub_rows: set[int] = frozenset()) -> object:
    from reportlab.lib import colors
    from reportlab.platypus import LongTable, TableStyle

    weights = weights or [1.0] * len(columns)
    total = sum(weights)
    col_widths = [width * w / total for w in weights]
    data = [[_p(c, styles["cellhead"]) for c in columns]]
    for index, row in enumerate(rows):
        style = styles["cellhead"] if index in group_rows else (styles["cellsub"] if index in sub_rows else styles["cell"])
        data.append([_p(v, style) for v in row])
    table = LongTable(data, colWidths=col_widths, repeatRows=1, splitByRow=1)
    commands = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DCE6F1")),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#BBBBBB")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
    ]
    for index in group_rows:
        commands.append(("BACKGROUND", (0, index + 1), (-1, index + 1), colors.HexColor("#F2F2F2")))
    table.setStyle(TableStyle(commands))
    return table


def _block_flowables(block: Block, width: float, styles) -> list[object]:
    from reportlab.platypus import ListFlowable, ListItem

    if block.kind == "heading":
        if block.style in ("fail", "review", "pass"):
            return [_p(block.text, styles[block.style])]
        if block.style == "warning":
            return [_p(block.text, styles["warning"])]
        return [_p(block.text, styles["h2"] if block.level <= 2 else styles["h3"])]
    if block.kind == "text":
        return [_p(block.text, styles.get(block.style, styles["normal"]))]
    if block.kind == "kv":
        return [_table(["항목", "값"], block.rows, block.weights or [1.6, 8.4], width, styles)]
    if block.kind == "bullets":
        style = styles["warning"] if block.style == "warning" else styles["normal"]
        items = [ListItem(_p(text, style), leftIndent=8, value="•") for (text,) in block.rows]
        return [ListFlowable(items, bulletType="bullet", start="•", leftIndent=8, bulletFontName=FONT_NAME, bulletFontSize=7)]
    if block.kind == "table":
        return [_table(block.columns, block.rows, block.weights, width, styles, block.group_rows, block.sub_rows)]
    return []


def _site_flowables(site: SiteReport, width: float, styles) -> list[object]:
    from reportlab.platypus import PageBreak

    flow: list[object] = [PageBreak(), _p(site.title, styles["site"])]
    if site.subtitle:
        flow.append(_p(site.subtitle, styles["subtitle"]))
    for block in site.blocks:
        flow += _block_flowables(block, width, styles)
    return flow


def build_batch_pdf(report: BatchReport) -> bytes:
    """A4 세로 PDF. 결과표가 한 쪽을 넘으면 머리글을 반복하며 이어 간다."""

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Spacer

    register_fonts()
    styles = _styles()
    margin = 12 * mm
    page_width = A4[0] - 2 * margin
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=margin, rightMargin=margin, topMargin=margin, bottomMargin=14 * mm,
        title=report.title, author="LH 서류심사", subject="일괄 심사 결과",
    )
    footer_text = f"{report.title} · 생성 {report.info[0][1] if report.info else ''}"

    def on_page(canvas, document) -> None:
        canvas.saveState()
        canvas.setFont(FONT_NAME, 7)
        canvas.setFillGray(0.4)
        canvas.drawString(margin, 8 * mm, footer_text)
        canvas.drawRightString(A4[0] - margin, 8 * mm, f"{document.page} 쪽")
        canvas.restoreState()

    flow: list[object] = [_p(report.title, styles["title"])]
    flow.append(_table(["항목", "값"], [list(pair) for pair in report.info], [1.6, 8.4], page_width, styles))
    flow.append(Spacer(0, 6))
    flow.append(_p("결과표 — 화면의 일괄 심사 표와 같은 열입니다. 2쪽부터 건별 심사표가 이어집니다.", styles["muted"]))
    flow.append(Spacer(0, 3))
    weights = {"접수번호": 1.3, "차수·접수일자·매도자": 2.2, "소재지": 4.6, "분류 · 신청유형": 2.2, "1차 판정": 1.9,
               "2차 점수": 1.2, "교통": 0.8, "주거": 0.8, "교육": 0.8, "가점": 0.8, "필지": 0.8, "상태": 1.9}
    flow.append(_table(report.columns, report.rows, [weights.get(c, 1.0) for c in report.columns], page_width, styles))
    for site in report.sites:
        flow += _site_flowables(site, page_width, styles)
    doc.build(flow, onFirstPage=on_page, onLaterPages=on_page)
    return buffer.getvalue()
