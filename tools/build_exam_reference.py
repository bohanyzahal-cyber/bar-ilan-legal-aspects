"""Render the canonical exam reference HTML to a paginated Hebrew PDF.

Usage: python tools/build_exam_reference.py --html PATH_TO_CHEATSHEET
Requires reportlab, python-bidi and Arial fonts installed with Windows.
"""
from __future__ import annotations

import argparse
import json
import re
from html.parser import HTMLParser
from pathlib import Path

from bidi.algorithm import get_display
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    CondPageBreak, Flowable, LongTable, PageBreak, SimpleDocTemplate, Spacer, TableStyle,
)

ROOT = Path(__file__).resolve().parents[1]
FONT = "ExamArial"
BOLD = "ExamArialBold"
BLUE = colors.HexColor("#183e63")
RULE = colors.HexColor("#c9d3dc")
INK = colors.HexColor("#172331")


class Node:
    def __init__(self, tag="root", attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def text(self):
        parts = []
        for child in self.children:
            if isinstance(child, str): parts.append(child)
            elif child.tag == "br": parts.append("\n")
            elif child.tag not in {"style", "script"}: parts.append(child.text())
        return "".join(parts)

    def find(self, tag):
        found = []
        for child in self.children:
            if isinstance(child, Node):
                if child.tag == tag: found.append(child)
                found.extend(child.find(tag))
        return found


class Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        n = Node(tag, attrs)
        self.stack[-1].children.append(n)
        if tag not in {"br", "hr", "input", "meta", "link", "img"}: self.stack.append(n)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data): self.stack[-1].children.append(data)


def clean(text):
    text = text.replace("—", "-").replace("–", "-").replace("‑", "-")
    text = text.replace("✓", "סימון").replace("✔", "כן").replace("✘", "לא").replace("✗", "לא")
    return re.sub(r"[ \t]+", " ", text).strip()


class HebrewText(Flowable):
    """Wrap logical Hebrew text, then apply bidi separately to each visual line."""
    def __init__(self, text, size=11, bold=False, color=INK, leading=None, lines=None, link=None):
        super().__init__()
        self.text = clean(text)
        self.size, self.font = size, BOLD if bold else FONT
        self.color, self.leading = color, leading or size * 1.38
        self.fixed_lines, self.link = lines, link
        self.spaceAfter = 5

    def wrap(self, availWidth, availHeight):
        self.width = availWidth
        if self.fixed_lines is not None: self.lines = self.fixed_lines
        else:
            self.lines = []
            for paragraph in self.text.split("\n"):
                if not paragraph.strip():
                    self.lines.append("")
                    continue
                line = ""
                for word in paragraph.split():
                    candidate = f"{line} {word}".strip()
                    if line and pdfmetrics.stringWidth(get_display(candidate), self.font, self.size) > availWidth:
                        self.lines.append(line)
                        line = word
                    else: line = candidate
                if line: self.lines.append(line)
        self.height = max(1, len(self.lines)) * self.leading
        return self.width, self.height

    def split(self, availWidth, availHeight):
        self.wrap(availWidth, availHeight)
        count = int(availHeight // self.leading)
        if count < 2 or count >= len(self.lines): return []
        opts = dict(size=self.size, bold=self.font == BOLD, color=self.color, leading=self.leading)
        return [HebrewText("", lines=self.lines[:count], **opts), HebrewText("", lines=self.lines[count:], **opts)]

    def draw(self):
        self.canv.setFillColor(self.color)
        self.canv.setFont(self.font, self.size)
        for i, line in enumerate(self.lines):
            self.canv.drawRightString(self.width, self.height - (i + 1) * self.leading + 3, get_display(line))
        if self.link:
            self.canv.linkRect("", self.link, (0, 0, self.width, self.height), relative=1, thickness=0)


class Bookmark(Flowable):
    def __init__(self, ident, title, mapping):
        super().__init__()
        self.ident, self.title, self.mapping = ident, title, mapping

    def wrap(self, *args): return 0, 0

    def draw(self):
        self.mapping[self.ident] = self.canv.getPageNumber()
        self.canv.bookmarkPage(self.ident)
        self.canv.addOutlineEntry(self.title, self.ident, level=0, closed=False)


def table(rows, widths, index=False, spans=()):
    # Reverse columns so the logical first column appears at the right edge.
    cells = [[HebrewText(c, size=10.5, bold=(i == 0 or j == 0), color=BLUE if i == 0 else INK)
              for j, c in enumerate(row)][::-1] for i, row in enumerate(rows)]
    if index:
        for i, ident in enumerate(index, 1): cells[i][-1].link = ident
    t = LongTable(cells, colWidths=widths[::-1], repeatRows=1, hAlign="RIGHT")
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eaf0f6")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f6f8fa")]),
        ("GRID", (0, 0), (-1, -1), .35, RULE),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ] + [("SPAN", (len(widths)-1-end, row), (len(widths)-1-start, row)) for row,start,end in spans]))
    return t


def boxed(text, warn=False):
    """A rule or warning in a shaded frame, so it stands out on paper."""
    t = LongTable([[HebrewText(text, size=10.5)]], hAlign="RIGHT")
    edge = colors.HexColor("#806029") if warn else BLUE
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fbf6ec") if warn else colors.HexColor("#f0f4f8")),
        ("BOX", (0, 0), (-1, -1), .5, RULE), ("LINEAFTER", (-1, 0), (-1, -1), 2.2, edge),
        ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def heading_text(h):
    """Heading with its law tag set apart: '9 · חוק התרופות - עץ ההחלטה [תרופות]'."""
    main, tags = [], []
    for c in h.children:
        if isinstance(c, Node) and "law-tag" in c.attrs.get("class", ""): tags.append(clean(c.text()))
        else: main.append(c if isinstance(c, str) else c.text())
    title = clean("".join(main))
    return title + (" [" + ", ".join(tags) + "]" if tags else "")


def statute_tables(node, story, width):
    """One table per law: the section number in its own bold column, and the law's name repeated at the
    top of every page the table runs onto, so that section 14 of one law is never read as the other's."""
    laws, intro = [], []
    for c in node.children:
        if isinstance(c, Node) and c.tag == "b":
            label = clean(c.text())
            if label.startswith("חוק"):
                laws.append([label, []])
                continue
            if laws and re.match(r"^\d", label):
                laws[-1][1].append([label.rstrip("."), ""])
                continue
        text = c if isinstance(c, str) else c.text()
        if laws and laws[-1][1]: laws[-1][1][-1][1] += text
        elif not laws: intro.append(text)
    if "".join(intro).strip(): story.append(HebrewText("".join(intro).strip()))
    for name, rows in laws:
        body = [[num, re.sub(r"\n\s*", "\n", txt.strip())] for num, txt in rows]
        story.extend([CondPageBreak(120), table([["סעיף", name + " - לשון החוק"]] + body, [width * .1, width * .9]), Spacer(1, 7)])


def emit_node(node, story, width):
    if node.tag in {"script", "style", "nav", "form"} or "noprint" in node.attrs.get("class", ""): return
    classes = node.attrs.get("class", "").split()
    if node.tag == "div" and "statute" in classes:
        statute_tables(node, story, width)
        return
    if node.tag == "div" and ("box" in classes or "warn" in classes) and not node.find("table"):
        story.extend([boxed(node.text().strip(), warn="warn" in classes), Spacer(1, 5)])
        return
    if node.tag == "table":
        trs = node.find("tr")
        rows, spans = [], []
        for tr in trs:
            vals = []
            for c in tr.children:
                if not isinstance(c, Node) or c.tag not in {"td", "th"}: continue
                count = int(c.attrs.get("colspan", "1"))
                start = len(vals)
                if count > 1: spans.append((len(rows), start, start+count-1))
                vals.extend([""]*(count-1)+[c.text()])
            if vals: rows.append(vals)
        if not rows: return
        count = max(map(len, rows))
        for row in rows:
            while len(row) < count: row.append("")
        is_cases = "פסק דין" in rows[0][0]
        widths = ([width * .32, width * .23, width * .45] if is_cases else [width * .2, width * .45, width * .35]) if count == 3 else [width * .25, width * .75]
        if count == 2 and rows[0][0].strip() == "✓": widths = [width * .09, width * .91]
        if count == 1: widths = [width]
        story.extend([table(rows, widths, spans=spans), Spacer(1, 7)])
        return
    if node.tag in {"h3", "h4"}:
        h = HebrewText(node.text(), size=12, bold=True, color=BLUE)
        story.extend([CondPageBreak(150), Spacer(1, 5), h])
        return
    if node.tag in {"div", "p", "li"} and not node.find("table") and not node.find("h3"):
        text = node.text().strip()
        for paragraph in re.split(r"\n\s*\n", text):
            if paragraph.strip(): story.append(HebrewText(paragraph))
        return
    for child in node.children:
        if isinstance(child, Node): emit_node(child, story, width)


def footer(canvas, doc):
    canvas.saveState()
    width, height = A4
    canvas.setStrokeColor(RULE)
    canvas.line(38, height - 33, width - 38, height - 33)
    canvas.setFillColor(BLUE)
    canvas.setFont(BOLD, 9)
    canvas.drawRightString(width - 38, height - 25, get_display("היבטים משפטיים בניהול | חומר עזר למסכם | הקורס שלנו"))
    canvas.setFont(FONT, 9)
    canvas.drawRightString(width - 38, 22, get_display("חלק כללי ותרופות הם שני חוקים שונים - ציינו את שם החוק"))
    canvas.drawString(38, 22, get_display(f"עמוד {doc.page}"))
    canvas.restoreState()


def build(source, output, pages):
    parsed = Tree()
    parsed.feed(source.read_text(encoding="utf-8"))
    sections = [n for n in parsed.root.find("section") if "data-exam-section" in n.attrs]
    width = A4[0] - 76
    mapping = {}
    story = [HebrewText("דף עזר למבחן - מפתח מהיר", size=19, bold=True, color=BLUE)]
    pg = lambda n: str(pages.get("exam-sec-%02d" % n, 0))
    story += [HebrewText("עודכן 9.10.2026 | רק מחומרי הקורס שלנו בלמדה ומההקלטות שלנו | המבחן: 1.11.2026, 9:00, שעתיים וחצי, ניירות בלבד", size=10),
              Spacer(1, 4),
              boxed("סדר העבודה בכל שאלה בקייס:\n"
                    "1. קוראים קודם את השאלה, ורק אז את הסיפור. מסמנים בסיפור מילים ומספרים.\n"
                    "2. מהמילים שבעובדות לסעיף - עמוד " + pg(13) + " (דגלים אדומים).\n"
                    "3. שלד התשובה - עמוד " + pg(1) + ". בתרופות: עץ ההחלטה - עמוד " + pg(9) + ".\n"
                    "4. משפט מוכן לפתיחה - עמוד " + pg(12) + ". לשון הסעיף - עמוד " + pg(11) + ". פסק דין - עמוד " + pg(10) + ".\n"
                    "5. כותבים: יסוד - יישום על העובדות - מסקנה - סעד, ומספר סעיף עם שם החוק."),
              Spacer(1, 4),
              boxed("כשנתקעים: לא נשארים על שאלה. כותבים את היסודות שכן יודעים, משאירים שלוש שורות, מסמנים בשוליים וממשיכים. "
                    "חוזרים בסוף. אמריקאית - עד 3 דקות; היגד - עד 3 דקות; הניקוד שליד השאלה קובע כמה לכתוב.", warn=True),
              Spacer(1, 8)]
    rows = [["מה מחפשים", "עמוד ב-PDF"]]
    ids = []
    for section in sections:
        heading = section.find("h2")[0]
        title = heading_text(heading)
        ident = section.attrs["id"]
        rows.append([title, str(pages.get(ident, 0)) or "0"])
        ids.append(ident)
    story.append(table(rows, [width * .86, width * .14], index=ids))
    story += [Spacer(1, 8), HebrewText("במיוחד: חלק כללי 14 = טעות; תרופות 14 = הקטנת נזק. חלק כללי 17 = כפייה; תרופות 17 = הפרה צפויה. תרופות 18 = סיכול.", bold=True),
              HebrewText("הדפסה: A4, גודל 100% (גודל אמיתי, לא התאמה לעמוד); שחור-לבן מספיק. עדיף חד-צדדי, כדי לפרוש כמה עמודים זה ליד זה. "
                         "מהדקים לפי מספרי העמודים ומדביקים לשוניות: שלד (עמוד " + pg(1) + "), פגמים (" + pg(5) + "), תרופות (" + pg(9) + "), פסקי דין (" + pg(10) + "), לשון החוק (" + pg(11) + "), דגלים (" + pg(13) + "). "
                         "לבחינה מביאים ניירות בלבד: הדף הזה, קובץ החקיקה של המרצה ותרשימי הזרימה שבלמדה.", size=10), PageBreak()]
    for section in sections:
        if section.attrs["data-exam-section"] in {"13", "15"}: story.append(PageBreak())
        story.append(CondPageBreak(180))
        h2 = section.find("h2")[0]
        story.append(Bookmark(section.attrs["id"], heading_text(h2), mapping))
        heading = HebrewText(heading_text(h2), size=13.2, bold=True, color=BLUE)
        gap = Spacer(1, 3)
        story.extend([Spacer(1, 7), heading, gap])
        for child in section.children:
            if isinstance(child, Node) and child.tag != "h2": emit_node(child, story, width)
    doc = SimpleDocTemplate(str(output), pagesize=A4, rightMargin=38, leftMargin=38, topMargin=43, bottomMargin=40,
                            title="דף עזר למסכם - הקורס שלנו", author="חומר לימוד מעובד לפי קורס נאוה קורמן")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return mapping


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--html", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=ROOT / "output/pdf/exam-reference.pdf")
    args = parser.parse_args()
    for name, filename in [(FONT, "arial.ttf"), (BOLD, "arialbd.ttf")]:
        pdfmetrics.registerFont(TTFont(name, str(Path("C:/Windows/Fonts") / filename)))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    mapping = {}
    for attempt in range(3):
        new_mapping = build(args.html, args.out, mapping)
        if mapping == new_mapping: break
        mapping = new_mapping
    else: raise RuntimeError("PDF page index did not stabilize")
    (args.out.parent / "exam-reference-pages.json").write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Created {args.out}; {len(mapping)} indexed sections")


if __name__ == "__main__": main()
