#!/usr/bin/env python3
"""Recruiters working live requirements right now — email list + phone call sheet.

  python scripts/active_recruiters.py harvest --days 21   # rebuild from recent mail
  python scripts/active_recruiters.py calls --limit 40    # phone list, gov desks first
  python scripts/active_recruiters.py calls --out call_sheet.md
  python scripts/active_recruiters.py bounces             # refresh the suppression list

Ranked by recency, not archive volume: someone who emailed in the last few weeks is provably
deliverable and provably hiring. See agents/outreach/active_recruiters.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=("harvest", "calls", "bounces"))
    ap.add_argument("--days", type=int, default=21)
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--max-messages", type=int, default=700)
    ap.add_argument("--out", default="", help="write the call sheet to this file")
    ap.add_argument("--all", action="store_true", help="don't rank government desks first")
    args = ap.parse_args()

    if args.stage == "bounces":
        from agents.outreach.deliverability import record_bounces
        r = record_bounces(days=365)
        print(f"suppression list: {r['total']} addresses ({r['new']} new) — "
              f"{r['bounce_rate']}% of {r['sent_total']} lifetime sends")
        return 0

    if args.stage == "harvest":
        from agents.outreach.active_recruiters import harvest
        p = harvest(days=args.days, max_messages=args.max_messages)
        cs = p["contacts"]
        print(f"scanned {p['scanned_messages']} messages from the last {args.days} days")
        print(f"  active recruiters : {len(cs)}")
        print(f"  reachable by phone: {sum(1 for c in cs if c['phones'])}")
        print(f"  government desks  : {sum(1 for c in cs if c['gov_reqs'])}")
        return 0

    from agents.outreach.active_recruiters import call_sheet, load_active
    if not load_active():
        print("no active recruiters yet — run `harvest` first")
        return 1
    sheet = call_sheet(gov_first=not args.all, limit=args.limit)
    if args.out:
        Path(args.out).write_text(sheet + "\n")
        print(f"wrote {args.out}")
    else:
        print(sheet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
