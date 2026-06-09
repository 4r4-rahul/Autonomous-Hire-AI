"""
PDF utilities:
  - markdown_to_pdf(): convert tailored .md resume → .pdf for ATS upload
  - extract_text():    extract text from an existing PDF (for parsing JD PDFs)

PDF generation priority:
  1. fpdf2  — pure Python, no system deps, works everywhere (primary)
  2. HTML   — fallback if fpdf2 unavailable
"""

import re
from pathlib import Path

from core.logger import get_logger

logger = get_logger("pdf_parser")

_RESUME_DIR = Path("data/resumes")

# Colours & sizes
_BLACK   = (26,  26,  26)
_GREY    = (100, 100, 100)
_RULE    = (180, 180, 180)
_H1_SIZE = 18
_H2_SIZE = 11
_H3_SIZE = 10
_BODY    = 9
_LINE_H  = 4.5   # mm per line


def _parse_md_sections(md_text: str) -> list[tuple[str, str]]:
    """Return list of (tag, text) tuples parsed from markdown. tag ∈ h1/h2/h3/bullet/hr/bold/text."""
    clean = md_text.split("<!-- TAILOR_META")[0].strip()
    items: list[tuple[str, str]] = []
    for line in clean.splitlines():
        if line.startswith("### "):
            items.append(("h3", line[4:].strip()))
        elif line.startswith("## "):
            items.append(("h2", line[3:].strip()))
        elif line.startswith("# "):
            items.append(("h1", line[2:].strip()))
        elif line.startswith("- "):
            items.append(("bullet", line[2:].strip()))
        elif line.strip() == "---":
            items.append(("hr", ""))
        elif line.strip() == "":
            items.append(("blank", ""))
        else:
            items.append(("text", line.strip()))
    return items


def _strip_inline(text: str) -> list[tuple[bool, bool, str, str]]:
    """Parse inline bold/italic/links into (bold, italic, chunk, link_url) tuples.
    link_url is '' for non-link chunks; for a markdown link it carries the URL so the
    PDF can render a CLICKABLE hyperlink (previously the URL was dropped, leaving e.g.
    'LinkedIn | GitHub' as dead plain text with no profile link)."""
    parts: list[tuple[bool, bool, str, str]] = []
    pos = 0
    pat = re.compile(r"\*\*(.+?)\*\*|\*(.+?)\*|\[([^\]]+)\]\(([^)]+)\)")
    for m in pat.finditer(text):
        if m.start() > pos:
            parts.append((False, False, text[pos:m.start()], ""))
        if m.group(1) is not None:
            parts.append((True, False, m.group(1), ""))
        elif m.group(2) is not None:
            parts.append((False, True, m.group(2), ""))
        else:
            parts.append((False, False, m.group(3), m.group(4)))  # link text + URL
        pos = m.end()
    if pos < len(text):
        parts.append((False, False, text[pos:], ""))
    return parts or [(False, False, text, "")]


def _sanitize(text: str) -> str:
    """Normalize to clean, human-looking ASCII for fpdf2 Helvetica \u2014 strips the
    typographic symbols that read as machine-generated (middle dots, bullets, em-dashes,
    arrows, smart quotes) and fixes the spacing they leave behind."""
    t = text or ""
    # Spacing-aware: collapse " <sep> " into a single clean separator.
    t = re.sub(r"\s*[\u00b7\u2022\u2027\u2219\u25aa\u25cf\u2023\u2043\u204c\u2219]\s*", ", ", t)
    t = re.sub(r"\s*\u2014\s*", " - ", t)     # em dash -> spaced hyphen
    t = t.replace("\u2013", "-")               # en dash -> hyphen
    repl = {
        "\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"',
        "\u00a0": " ", "\u200b": "", "\u2009": " ", "\u202f": " ", "\u2007": " ",
        "\u2026": "...", "\u2192": " to ", "\u2190": " ", "\u2191": "^", "\u2193": "v",
        "\u2713": "", "\u2717": "", "\u00d7": "x", "\u00f7": "/",
    }
    for src, dst in repl.items():
        t = t.replace(src, dst)
    t = re.sub(r" {2,}", " ", t)               # collapse double spaces
    t = re.sub(r"\s+([,.;:])", r"\1", t)       # no space before punctuation
    t = re.sub(r",\s*,", ", ", t)              # no double commas
    return t.encode("latin-1", errors="replace").decode("latin-1")

def _md_to_fpdf(md_text: str) -> bytes:
    """Render markdown resume to PDF bytes using fpdf2."""
    from fpdf import FPDF

    pdf = FPDF(format="Letter")
    pdf.set_margins(18, 16, 18)
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=14)

    sections = _parse_md_sections(md_text)

    _LINK_BLUE = (0, 102, 204)

    def _write_inline(text: str, size: int, default_bold: bool = False,
                      color: tuple = _BLACK) -> None:
        pdf.set_text_color(*color)
        for bold, italic, chunk, link in _strip_inline(_sanitize(text)):
            style = ""
            if bold or default_bold: style += "B"
            if italic:               style += "I"
            if link:                 style += "U"   # underline hyperlinks
            pdf.set_font("Helvetica", style=style, size=size)
            if link:
                # Show the readable URL as the visible (clickable) text so ATS parsers —
                # which read text, not click annotations — actually capture the profile.
                # e.g. [LinkedIn](https://linkedin.com/in/x) -> "linkedin.com/in/x".
                display = re.sub(r"^https?://(www\.)?", "", _sanitize(link)).rstrip("/")
                if not display or len(display) > 60:
                    display = chunk  # fall back to the label for long/odd URLs
                pdf.set_text_color(*_LINK_BLUE)
                pdf.write(h=_LINE_H, txt=display, link=_sanitize(link))
                pdf.set_text_color(*color)
            else:
                pdf.write(h=_LINE_H, txt=chunk)

    for tag, text in sections:
        if tag == "h1":
            pdf.set_font("Helvetica", style="B", size=_H1_SIZE)
            pdf.set_text_color(*_BLACK)
            pdf.ln(1)
            pdf.cell(0, 8, txt=_sanitize(text), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(1)

        elif tag == "h2":
            pdf.ln(3)
            pdf.set_font("Helvetica", style="B", size=_H2_SIZE)
            pdf.set_text_color(*_BLACK)
            pdf.cell(0, 5, txt=_sanitize(text).upper(), new_x="LMARGIN", new_y="NEXT")
            # Underline rule
            lm, rm = pdf.l_margin, pdf.r_margin
            y = pdf.get_y()
            pdf.set_draw_color(*_RULE)
            pdf.line(lm, y, pdf.w - rm, y)
            pdf.ln(2)

        elif tag == "h3":
            pdf.ln(2)
            pdf.set_text_color(*_BLACK)
            _write_inline(text, _H3_SIZE, default_bold=True)
            pdf.ln(_LINE_H + 1)

        elif tag == "bullet":
            pdf.set_text_color(*_BLACK)
            pdf.set_x(pdf.l_margin + 4)
            pdf.set_font("Helvetica", size=_BODY)
            pdf.write(h=_LINE_H, txt="- ")
            _write_inline(text, _BODY)
            pdf.ln(_LINE_H)

        elif tag == "hr":
            pdf.ln(2)
            lm, rm = pdf.l_margin, pdf.r_margin
            y = pdf.get_y()
            pdf.set_draw_color(*_RULE)
            pdf.line(lm, y, pdf.w - rm, y)
            pdf.ln(3)

        elif tag == "blank":
            pdf.ln(1.5)

        else:  # text
            _write_inline(text, _BODY, color=_GREY)
            pdf.ln(_LINE_H)

    return bytes(pdf.output())


def _md_to_html(md_text: str) -> str:
    """Fallback: minimal markdown → HTML for ATS portals that accept HTML."""
    html = md_text.split("<!-- TAILOR_META")[0].strip()
    html = re.sub(r"^# (.+)$",   r"<h1>\1</h1>",   html, flags=re.MULTILINE)
    html = re.sub(r"^## (.+)$",  r"<h2>\1</h2>",   html, flags=re.MULTILINE)
    html = re.sub(r"^### (.+)$", r"<h3>\1</h3>",   html, flags=re.MULTILINE)
    html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html)
    html = re.sub(r"\*(.+?)\*",     r"<em>\1</em>",         html)
    html = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', html)
    lines = html.split("\n")
    result, in_list = [], False
    for line in lines:
        if line.startswith("- "):
            if not in_list:
                result.append("<ul>"); in_list = True
            result.append(f"<li>{line[2:].strip()}</li>")
        else:
            if in_list:
                result.append("</ul>"); in_list = False
            result.append(line)
    if in_list:
        result.append("</ul>")
    html = "\n".join(result)
    html = re.sub(r"^---$", "<hr>", html, flags=re.MULTILINE)
    lines = html.split("\n")
    result = []
    for line in lines:
        s = line.strip()
        result.append(f"<p>{s}</p>" if s and not s.startswith("<") else line)
    html = "\n".join(result)
    return (
        '<!DOCTYPE html><html><head><meta charset="UTF-8"><style>'
        'body{font-family:Arial,sans-serif;font-size:11px;margin:20px 30px;color:#1a1a1a;line-height:1.4}'
        'h1{font-size:20px;margin:0 0 2px 0}h2{font-size:13px;border-bottom:1px solid #999;'
        'margin:14px 0 6px 0;text-transform:uppercase}h3{font-size:11px;margin:10px 0 2px 0}'
        'ul{margin:4px 0 4px 16px;padding:0}li{margin:2px 0}p{margin:3px 0}'
        'strong{font-weight:600}a{color:#1a1a1a;text-decoration:none}'
        f'</style></head><body>{html}</body></html>'
    )


def markdown_to_pdf(md_path: str | Path) -> Path:
    """
    Convert a markdown resume to PDF using fpdf2 (pure Python).
    Falls back to HTML if fpdf2 unavailable.
    Returns path to the generated file.
    """
    md_path  = Path(md_path)
    pdf_path = md_path.with_suffix(".pdf")
    md_text  = md_path.read_text(encoding="utf-8")

    try:
        pdf_bytes = _md_to_fpdf(md_text)
        pdf_path.write_bytes(pdf_bytes)
        logger.info("pdf_generated", path=str(pdf_path), method="fpdf2")
        return pdf_path
    except Exception as e:
        logger.warning("fpdf2_failed", error=str(e), fallback="html")
        html_path = md_path.with_suffix(".html")
        html_path.write_text(_md_to_html(md_text), encoding="utf-8")
        logger.info("pdf_fallback_html", path=str(html_path))
        return html_path


def extract_text(pdf_path: str | Path) -> str:
    """Extract plain text from a PDF file (for parsing uploaded JDs)."""
    try:
        import PyPDF2
        with open(pdf_path, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as e:
        logger.error("pdf_extract_failed", path=str(pdf_path), error=str(e))
        return ""
