"""
Retrieve the EXACT tailored résumé submitted for a role — e.g. when a recruiter reaches back.

Usage:
  python scripts/get_resume.py                 # list every application + its résumé
  python scripts/get_resume.py pinecone        # find by company (or role) substring
  python scripts/get_resume.py "data platform" # match on role title too
  python scripts/get_resume.py pinecone --open # also open the PDF

For each match it renders/locates the PDF and copies it to data/submitted_resumes/ with a
human-readable name (Company__Role__date.pdf) so you can attach it straight to a reply.
"""
import sys
import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent.parent
STATE = ROOT / "data" / "pipeline_state.json"
OUT = ROOT / "data" / "submitted_resumes"

# statuses that mean "we actually applied / queued to apply" (most relevant for recruiters)
APPLIED = {"submitted", "awaiting_confirmation", "pending_manual_submit", "applied"}


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", (s or "").strip()).strip("_")[:50] or "Unknown"


def _pdf_for(md_path: Path) -> Path | None:
    """Locate (or render) the PDF for a tailored résumé markdown."""
    pdf = md_path.with_suffix(".pdf")
    if pdf.exists():
        return pdf
    try:
        sys.path.insert(0, str(ROOT))
        from agents.application.form_filler import markdown_to_pdf
        return Path(markdown_to_pdf(md_path))
    except Exception as e:
        print(f"    ! could not render PDF: {str(e)[:80]}")
        return None


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    do_open = "--open" in sys.argv
    query = " ".join(args).lower().strip()

    state = json.loads(STATE.read_text())
    apps = state.get("applications", [])

    def matches(a):
        if not query:
            return True
        hay = f"{a.get('company_name','')} {a.get('role_title','')}".lower()
        return query in hay

    rows = [a for a in apps if matches(a) and a.get("tailored_resume_path")]
    # applied ones first, then by recency
    rows.sort(key=lambda a: (a.get("status") not in APPLIED,
                             -(len(a.get("submitted_at") or a.get("sent_at") or ""))))

    if not rows:
        print(f"No application matched {query!r}. Try a company name (e.g. 'pinecone').")
        return

    OUT.mkdir(parents=True, exist_ok=True)
    print(f"{len(rows)} match(es) for {query or 'ALL'}:\n")
    for a in rows:
        co, role = a.get("company_name", "?"), a.get("role_title") or "role"
        when = (a.get("submitted_at") or a.get("sent_at") or "")[:10]
        md = a.get("tailored_resume_path")
        mdp = Path(md) if Path(md).is_absolute() else ROOT / md
        status = a.get("status")
        print(f"• {co} — {role}")
        print(f"    status: {status}   applied: {when or '—'}")
        if not mdp.exists():
            print(f"    ! résumé markdown missing: {md}")
            continue
        pdf = _pdf_for(mdp)
        if pdf and pdf.exists():
            dest = OUT / f"{_safe(co)}__{_safe(role)}{('__' + when) if when else ''}.pdf"
            shutil.copyfile(pdf, dest)
            print(f"    résumé: {dest}")
            if do_open:
                subprocess.run(["open", str(dest)], check=False)
        print()


if __name__ == "__main__":
    main()
