"""Render a tailored résumé markdown file as .docx.

Vendors want Word, not PDF — they reformat, add a letterhead, and sometimes strip the contact block
before forwarding to the client. A PDF forces them to retype, which is how names and dates get
mangled on the way to a submission. The Snowflake lane has always been .docx for that reason; this
gives the generated (markdown-based) résumés the same option.

Deliberately hand-rolled rather than shelling out: there is no pandoc or LibreOffice on this machine
(checked), and Word itself cannot open markdown. python-docx is already a dependency.

Handles the subset of markdown `resume_tailor` actually emits:
    # H1            -> centred name
    contact lines   -> centred, small
    ## H2           -> section heading, bold + rule
    ### H3          -> role heading, bold
    **bold**        -> bold runs (inline, mid-sentence)
    `code`          -> monospace runs
    - bullet        -> bulleted list item
    ---             -> section break (skipped; headings carry the rule)
    <!-- ... -->    -> dropped, including the TAILOR_META block
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt, RGBColor

_INLINE = re.compile(r"(\*\*.+?\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_COMMENT = re.compile(r"<!--.*?-->", re.S)


def _runs(par, text: str, *, bold=False, size=None, color=None) -> None:
    """Write text into a paragraph, honouring inline **bold**, `code` and [links]."""
    for piece in _INLINE.split(text):
        if not piece:
            continue
        b, mono = bold, False
        if piece.startswith("**") and piece.endswith("**"):
            piece, b = piece[2:-2], True
        elif piece.startswith("`") and piece.endswith("`"):
            piece, mono = piece[1:-1], True
        else:
            m = _LINK.fullmatch(piece)
            if m:
                piece = m.group(1)          # keep the label, drop the URL
        r = par.add_run(piece)
        r.bold = b
        if mono:
            r.font.name = "Consolas"
        if size:
            r.font.size = Pt(size)
        if color:
            r.font.color.rgb = color


def convert(md_path: Path, out_path: Path | None = None) -> Path:
    md = _COMMENT.sub("", md_path.read_text())
    out_path = out_path or md_path.with_suffix(".docx")

    doc = Document()
    st = doc.styles["Normal"]
    st.font.name = "Calibri"
    st.font.size = Pt(10)
    for s in doc.sections:                  # résumés need the page, not Word's default margins
        s.left_margin = s.right_margin = Pt(45)
        s.top_margin = s.bottom_margin = Pt(36)

    lines = md.splitlines()
    for i, raw in enumerate(lines):
        line = raw.rstrip()
        if not line.strip() or line.strip() == "---":
            continue

        if line.startswith("# "):
            p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            _runs(p, line[2:], bold=True, size=20)
            p.paragraph_format.space_after = Pt(2)
        elif line.startswith("## "):
            p = doc.add_paragraph()
            _runs(p, line[3:].upper(), bold=True, size=12,
                  color=RGBColor(0x1F, 0x3B, 0x63))
            p.paragraph_format.space_before = Pt(10)
            p.paragraph_format.space_after = Pt(3)
        elif line.startswith("### "):
            p = doc.add_paragraph()
            _runs(p, line[4:], bold=True, size=11)
            p.paragraph_format.space_before = Pt(8)
            p.paragraph_format.space_after = Pt(1)
        elif line.startswith("- "):
            p = doc.add_paragraph(style="List Bullet")
            _runs(p, line[2:])
            p.paragraph_format.space_after = Pt(2)
        else:
            p = doc.add_paragraph()
            # The two lines under the name are the contact block: centre them.
            centred = i < 5 and ("|" in line or "linkedin" in line.lower())
            if centred:
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            _runs(p, line, size=9 if centred else None)
            p.paragraph_format.space_after = Pt(3)

    doc.save(out_path)
    return out_path


if __name__ == "__main__":
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    print(convert(src, dst))
