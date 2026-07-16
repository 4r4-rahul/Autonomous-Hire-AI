#!/usr/bin/env python3
"""
Generate interview-prep briefs on demand.

The same engine runs automatically inside the email-monitor scheduler job whenever an application
flips to "interview". This script lets you run it by hand:

  # Generate for every application currently in "interview" status (idempotent — skips ones
  # that already have a prep doc), and wire MeetingScheduled.prep_doc_path:
  python -m scripts.generate_interview_prep

  # Force a brief for a specific company/role even if there's no matching application,
  # optionally grounding "why this company" answers in a pasted JD:
  python -m scripts.generate_interview_prep --company Robinhood --role "Senior Software Engineer" \
      --jd-file robinhood_jd.txt

Briefs are written to data/interview_prep/<company>_<role>_prep.md.
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from agents.application.interview_prep import generate_prep, prep_new_interviews
from core.state_store import load_state, save_state


async def _main() -> int:
    ap = argparse.ArgumentParser(description="Generate interview-prep briefs.")
    ap.add_argument("--company", help="Company name (one-off mode; bypasses the application list).")
    ap.add_argument("--role", help="Role title (one-off mode).")
    ap.add_argument("--jd-file", help="Path to a file with the job description (one-off mode).")
    args = ap.parse_args()

    # One-off mode: explicit company/role, no application needed.
    if args.company or args.role:
        if not (args.company and args.role):
            ap.error("--company and --role must be given together")
        jd = ""
        if args.jd_file:
            jd = Path(args.jd_file).read_text(errors="ignore")
        path = await generate_prep({"company": args.company, "title": args.role, "description": jd})
        print(f"✓ prep written → {path}")
        return 0

    # Batch mode: every application in "interview" status that lacks a prep doc.
    state = load_state()
    n = await prep_new_interviews(state)
    if n:
        save_state(state)   # persist the new MeetingScheduled.prep_doc_path records
        print(f"✓ generated {n} prep brief(s) → data/interview_prep/")
    else:
        print("No interviews need a prep brief (none in 'interview' status, or all already prepped).")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
