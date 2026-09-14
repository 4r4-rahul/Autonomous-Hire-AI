#!/usr/bin/env python3
"""Correct the JPMorgan start date across already-generated résumés.

The base résumé carried "Aug 2023 - Present" for JPMorgan Chase while the profile (and reality) say
October 2023. Every tailored résumé inherits the base, so the wrong date propagated to hundreds of
files — and it also created an overlap with Insight Global, which ran to autumn 2023. Overlapping
dates are the kind of thing a recruiter spots instantly and reads as carelessness or padding.

The base is fixed, so new tailorings are correct. This repairs the ones already written, without
paying to re-tailor them: the date is a literal string substitution, and re-running the LLM would
cost money and re-roll every other word on the page for no benefit.

  python scripts/fix_resume_dates.py            # contract-lane résumés (default)
  python scripts/fix_resume_dates.py --all      # every tailored résumé
  python scripts/fix_resume_dates.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_ROOT = Path(__file__).parent.parent
_STATE = _ROOT / "data" / "pipeline_state.json"

# Match the JPMorgan date line in any of the dash/format variants the renderer and the LLM produce.
_WRONG = re.compile(r"(\*\*)?\s*Aug(?:ust)?\s+2023\s*[-–—]\s*(Present|Current)", re.I)


def _targets(contract_only: bool) -> list[Path]:
    state = json.loads(_STATE.read_text())
    jobs = {j["id"]: j for j in state.get("job_listings", [])}
    from core.employment import classify_employment_for_job

    out: list[Path] = []
    for a in state.get("applications", []):
        path = a.get("tailored_resume_path")
        if not path:
            continue
        if contract_only:
            j = jobs.get(a.get("job_id")) or {}
            cls = classify_employment_for_job(
                j.get("source", ""), a.get("company_name", ""), a.get("role_title", ""),
                (j.get("description") or "")[:3000], j.get("employment_type", ""))
            if cls != "contract":
                continue
        p = Path(path)
        if not p.is_absolute():
            p = _ROOT / p
        md = p.with_suffix(".md")
        if md.exists():
            out.append(md)
    return sorted(set(out))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="every tailored résumé, not just contract")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    targets = _targets(contract_only=not args.all)
    fixed = rendered = 0
    for md in targets:
        text = md.read_text()
        if not _WRONG.search(text):
            continue
        new = _WRONG.sub(lambda m: f"{m.group(1) or ''}Oct 2023 - {m.group(2)}", text)
        if args.dry_run:
            fixed += 1
            continue
        md.write_text(new)
        fixed += 1
        # The PDF is what gets attached, so it has to be re-rendered or the fix is cosmetic.
        try:
            from tools.pdf_parser import markdown_to_pdf
            markdown_to_pdf(str(md))
            rendered += 1
        except Exception:
            pass

    print(f"résumés scanned : {len(targets)}")
    print(f"date corrected  : {fixed}{'  (dry run)' if args.dry_run else ''}")
    if not args.dry_run:
        print(f"PDFs re-rendered: {rendered}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
