#!/usr/bin/env python3
"""Build docs/Beginners-Guide.pdf from docs/user-guide.md.

    pip install reportlab
    python tools/build_guide_pdf.py            # writes docs/Beginners-Guide.pdf

The Markdown file is the single source of truth: it reads fine on GitHub, and this script lays the
same content out as a PDF with a clickable table of contents, bookmarks and page numbers.

It understands only the small Markdown subset the guide uses: headings, paragraphs, **bold**, *italic*,
`code`, [links](url), bullet and numbered lists (which may contain code blocks and nested lists),
fenced code blocks, pipe tables, > call-out boxes, ![images](path) and --- rules.
"""
from __future__ import annotations

import hashlib
import html
import re
import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (BaseDocTemplate, CondPageBreak, Frame, HRFlowable, Image, KeepTogether, ListFlowable,
                                ListItem, PageBreak, PageTemplate, Paragraph, Preformatted, Spacer, Table, TableStyle)
from reportlab.platypus.tableofcontents import TableOfContents

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "docs" / "user-guide.md"
OUT = ROOT / "docs" / "Beginners-Guide.pdf"

NAVY, BLUE, GREY, LIGHT = colors.HexColor("#1f3a5f"), colors.HexColor("#1a56b0"), colors.HexColor("#555555"), colors.HexColor("#f3f4f6")
AMBER, AMBER_BG = colors.HexColor("#d99a00"), colors.HexColor("#fff7df")

PAGE_W, PAGE_H = A4
MARGIN = 18 * mm
AVAIL = PAGE_W - 2 * MARGIN

BODY = ParagraphStyle("Body", fontName="Helvetica", fontSize=10, leading=14.5, textColor=colors.HexColor("#222222"), spaceAfter=6)
TITLE = ParagraphStyle("Title", parent=BODY, fontName="Helvetica-Bold", fontSize=25, leading=30, textColor=NAVY, spaceBefore=18, spaceAfter=6)
SUB = ParagraphStyle("Sub", parent=BODY, fontSize=12.5, leading=17, textColor=GREY, spaceAfter=10)
H2 = ParagraphStyle("H2", parent=BODY, fontName="Helvetica-Bold", fontSize=16, leading=20, textColor=NAVY, spaceBefore=14, spaceAfter=3, keepWithNext=1)
H3 = ParagraphStyle("H3", parent=BODY, fontName="Helvetica-Bold", fontSize=11.5, leading=15, textColor=colors.HexColor("#2c5282"), spaceBefore=8, spaceAfter=3, keepWithNext=1)
CELL = ParagraphStyle("Cell", parent=BODY, fontSize=8.6, leading=11.2, spaceAfter=0)
CELL_H = ParagraphStyle("CellH", parent=CELL, fontName="Helvetica-Bold", textColor=colors.white)
CAP = ParagraphStyle("Cap", parent=BODY, fontName="Helvetica-Oblique", fontSize=8.5, leading=11, textColor=GREY, alignment=TA_CENTER, spaceBefore=3, spaceAfter=10)
CODE = ParagraphStyle("Code", fontName="Courier", fontSize=7.6, leading=10.2, textColor=colors.HexColor("#111111"),
                      backColor=LIGHT, borderPadding=(5, 6, 5, 6), leftIndent=4, rightIndent=4, spaceBefore=3, spaceAfter=9)
CALL = ParagraphStyle("Call", parent=BODY, fontSize=9.6, leading=13.5, spaceAfter=4)
TOC_TITLE = ParagraphStyle("TocTitle", parent=H2, spaceBefore=6, spaceAfter=8)
TOC0 = ParagraphStyle("TOC0", parent=BODY, fontSize=10.5, leading=17, leftIndent=0, spaceAfter=0)

UNI = {"→": "->", "₹": "Rs ", "’": "'", "‘": "'", "“": '"', "”": '"', "✓": "ok", "≥": ">="}


def clean(text: str) -> str:
    for k, v in UNI.items():
        text = text.replace(k, v)
    return text


# ----------------------------------------------------------------------------- inline

def inline(text: str) -> str:
    """Markdown inline -> ReportLab paragraph markup."""
    text = clean(text)
    spans: list[str] = []

    def stash(m: re.Match) -> str:
        spans.append(f'<font face="Courier" size="8.8" color="#a0214a">{html.escape(m.group(1))}</font>')
        return f"\x00{len(spans) - 1}\x00"

    text = re.sub(r"`([^`]+)`", stash, text)
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<![\*\w])\*(?!\s)([^*]+?)(?<!\s)\*(?![\*\w])", r"<i>\1</i>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2" color="#1a56b0"><u>\1</u></a>', text)
    return re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], text)


# ------------------------------------------------------------------------------ parse

Block = tuple


def parse(md: str) -> list[Block]:
    lines = md.split("\n")
    blocks: list[Block] = []
    i = 0
    marker = re.compile(r"^(\d+)\. (.*)$|^- (.*)$")
    structural = re.compile(r"^(#{1,3} |```|\||>|!\[|---\s*$|\d+\. |- )")

    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
        elif line.startswith("```"):
            i += 1
            code = []
            while i < len(lines) and not lines[i].startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            blocks.append(("code", "\n".join(code)))
        elif re.match(r"^#{1,3} ", line):
            level = len(line) - len(line.lstrip("#"))
            blocks.append((f"h{level}", line[level + 1:].strip()))
            i += 1
        elif re.match(r"^---\s*$", line):
            blocks.append(("rule",))
            i += 1
        elif line.startswith("!["):
            m = re.match(r"!\[(.*?)\]\((.*?)\)", line)
            blocks.append(("image", m.group(1), m.group(2)))
            i += 1
        elif line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                    rows.append(cells)
                i += 1
            blocks.append(("table", rows))
        elif line.startswith(">"):
            quote = []
            while i < len(lines) and lines[i].startswith(">"):
                quote.append(lines[i][1:].lstrip() if lines[i][1:2] == " " else lines[i][1:])
                i += 1
            blocks.append(("quote", parse("\n".join(quote))))
        elif marker.match(line):
            ordered = bool(re.match(r"^\d+\. ", line))
            start = int(re.match(r"^(\d+)\.", line).group(1)) if ordered else 1
            items: list[list[Block]] = []
            while i < len(lines):
                m = marker.match(lines[i])
                if not m or bool(re.match(r"^\d+\. ", lines[i])) != ordered:
                    break
                body = [m.group(2) if ordered else m.group(3)]
                i += 1
                while i < len(lines):
                    nxt = lines[i]
                    if nxt.startswith(" "):
                        body.append(re.sub(r"^ {1,3}", "", nxt))
                        i += 1
                    elif not nxt.strip():
                        j = i
                        while j < len(lines) and not lines[j].strip():
                            j += 1
                        if j < len(lines) and lines[j].startswith(" "):
                            body.extend([""] * (j - i))
                            i = j
                        else:
                            break
                    else:
                        break
                items.append(parse("\n".join(body)))
                while i < len(lines) and not lines[i].strip() and i + 1 < len(lines) and marker.match(lines[i + 1]) \
                        and bool(re.match(r"^\d+\. ", lines[i + 1])) == ordered:
                    i += 1
            blocks.append(("ol" if ordered else "ul", start, items))
        else:
            para = [line.strip()]
            i += 1
            while i < len(lines) and lines[i].strip() and not structural.match(lines[i]):
                para.append(lines[i].strip())
                i += 1
            blocks.append(("p", " ".join(para)))
    return blocks


# ---------------------------------------------------------------------------- render

def table_flowable(rows: list[list[str]], width: float) -> Table:
    ncols = max(len(r) for r in rows)
    rows = [r + [""] * (ncols - len(r)) for r in rows]
    weights = [max(8, min(55, max(len(rows[r][c]) for r in range(len(rows))))) for c in range(ncols)]
    widths = [width * w / sum(weights) for w in weights]
    data = [[Paragraph(inline(c), CELL_H if r == 0 else CELL) for c in row] for r, row in enumerate(rows)]
    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c9ced6")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f8fa")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
    ]))
    return t


def flow(blocks: list[Block], width: float, base: Path, in_list: bool = False) -> list:
    out: list = []
    for b in blocks:
        kind = b[0]
        if kind == "p":
            out.append(Paragraph(inline(b[1]), ParagraphStyle("P", parent=BODY, spaceAfter=3 if in_list else 6)))
        elif kind == "code":
            out.append(Preformatted(clean(b[1]), CODE))
        elif kind == "table":
            out += [table_flowable(b[1], width), Spacer(1, 8)]
        elif kind == "quote":
            inner = flow(b[1], width - 22, base)
            for f in inner:
                if isinstance(f, Paragraph):
                    f.style = CALL
            box = Table([[inner]], colWidths=[width])
            box.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), AMBER_BG), ("LINEBEFORE", (0, 0), (0, -1), 3.5, AMBER),
                ("LEFTPADDING", (0, 0), (-1, -1), 11), ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
            out += [box, Spacer(1, 9)]
        elif kind in ("ul", "ol"):
            items = [ListItem(flow(item, width - 18, base, in_list=True), leftIndent=16) for item in b[2]]
            out.append(ListFlowable(items, bulletType="bullet" if kind == "ul" else "1", bulletFormat=None if kind == "ul" else "%s.",
                                    start="•" if kind == "ul" else b[1],
                                    bulletFontName="Helvetica", bulletFontSize=9.5, leftIndent=16, bulletDedent=12,
                                    spaceAfter=6))
        elif kind == "image":
            path = (base / b[2]).resolve()
            img = Image(str(path))
            w, h = img.imageWidth, img.imageHeight
            scale = min(AVAIL * 0.86 / w, (82 if h > w else 108) * mm / h)   # tall phone-shaped pictures get a smaller cap
            img.drawWidth, img.drawHeight = w * scale, h * scale
            img.hAlign = "CENTER"
            out.append(KeepTogether([img, Paragraph(inline(b[1]), CAP)]))
        elif kind == "rule":
            out.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#d5d9e0"), spaceBefore=4, spaceAfter=4))
        elif kind == "h3":
            out += [CondPageBreak(32 * mm), Paragraph(inline(b[1]), H3)]   # never leave a heading stranded at the page bottom
    return out


class GuideDoc(BaseDocTemplate):
    def afterFlowable(self, flowable):
        if isinstance(flowable, Paragraph) and flowable.style.name == "H2":
            text = flowable.getPlainText()
            # The key must be the SAME on every layout pass (a counter would change it and the contents would never settle).
            key = "part-" + hashlib.md5(text.encode()).hexdigest()[:10]
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(text, key, 0, 0)
            self.notify("TOCEntry", (0, text, self.page, key))


def footer(canvas, doc) -> None:
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#d5d9e0"))
    canvas.line(MARGIN, 14 * mm, PAGE_W - MARGIN, 14 * mm)
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(GREY)
    canvas.drawString(MARGIN, 9.5 * mm, "Optix (NIFTY Options Trading Assistant) - Beginner's Guide  |  open-source, paper-first, not investment advice")
    canvas.drawRightString(PAGE_W - MARGIN, 9.5 * mm, f"Page {doc.page}")
    canvas.restoreState()


def build(src: Path = SRC, out: Path = OUT) -> Path:
    text = src.read_text(encoding="utf-8")
    fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]  # lets a test prove the PDF is not stale
    blocks = parse(text)
    story: list = []
    toc = TableOfContents()
    toc.levelStyles = [TOC0]
    toc.dotsMinLevel = 0
    seen_h2 = False
    first_h1 = True
    for b in blocks:
        if b[0] == "h1":
            story.append(Paragraph(inline(b[1]), TITLE if first_h1 else H2))
            first_h1 = False
        elif b[0] == "h2":
            if not seen_h2:
                story += [HRFlowable(width="100%", thickness=0.8, color=NAVY, spaceBefore=6, spaceAfter=10),
                          Paragraph("Contents", TOC_TITLE), toc, PageBreak()]
                seen_h2 = True
            story += [CondPageBreak(55 * mm), Paragraph(inline(b[1]), H2),
                      HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#c7d0de"), spaceBefore=0, spaceAfter=7)]
        elif b[0] == "p" and not seen_h2 and b[1].startswith("**") and len(story) == 1:
            story.append(Paragraph(inline(b[1]), SUB))  # the bold line under the title
        elif b[0] == "rule" and not seen_h2:
            continue
        else:
            story += flow([b], AVAIL, src.parent)
    doc = GuideDoc(str(out), pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=16 * mm, bottomMargin=20 * mm,
                   title="Optix - Beginner's Guide", author="Niranjan K",
                   subject=f"From GitHub download to your first paper trade [source {fingerprint}]", keywords="options, NIFTY, Angel One, SmartAPI, paper trading")
    doc.addPageTemplates([PageTemplate(id="main", frames=[Frame(MARGIN, 20 * mm, AVAIL, PAGE_H - 36 * mm, id="f")], onPage=footer)])
    doc.multiBuild(story)
    return out


if __name__ == "__main__":
    path = build()
    print(f"Wrote {path.relative_to(ROOT)}")
    sys.exit(0)
