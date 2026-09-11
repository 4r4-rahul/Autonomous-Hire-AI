#!/usr/bin/env python3
"""Render the two working lists: who to CALL, and who to EMAIL.

  python scripts/contact_sheets.py --out-dir .

Produces:
  call_sheet.md      staffing recruiters with live requirements + direct phone numbers
  gov_contacts.md    government hiring contacts attached to in-lane public-sector roles

Separate sheets because they are worked differently. A recruiter who emailed a requirement last week
wants a phone call today. A government hiring contact wants a formal application first, with the
email as a polite follow-up naming the requisition.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# ADA / EEO inboxes appear in public-sector bulletins as the accommodation contact. They are not
# hiring contacts, and mailing one is both useless and slightly rude.
_ADA = re.compile(r"(ada|accommodation|accessibility|eeo|civilrights|disability|relay)", re.I)


def _gov_sheet(roles: list[dict]) -> str:
    rows = [r for r in roles if r.get("contacts") and not _ADA.search(r["contacts"][0])]
    rows.sort(key=lambda r: (r.get("state") or "ZZ", r["title"]))
    out = ["# Government hiring contacts — in-lane public-sector roles", ""]
    out.append(f"_{len(rows)} roles with a usable contact. Apply through the posting FIRST, then "
               f"email the contact naming the requisition — an unsolicited résumé with no "
               f"application on file is discarded in public-sector hiring._")
    out.append("")
    out.append("| State | Role | Where | Contact | Posting |")
    out.append("|-------|------|-------|---------|---------|")
    for r in rows:
        note = " ⚠️residency" if r.get("residency_required") else ""
        out.append(f"| {r.get('state') or '—'} | {r['title'][:52]}{note} | "
                   f"{(r.get('location') or '')[:26]} | {r['contacts'][0]} | [link]({r['url']}) |")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--gov-roles", default="", help="path to a saved gov roles JSON")
    ap.add_argument("--limit", type=int, default=60)
    args = ap.parse_args()
    out_dir = Path(args.out_dir)

    from agents.outreach.active_recruiters import call_sheet, load_active
    if load_active():
        (out_dir / "call_sheet.md").write_text(call_sheet(limit=args.limit) + "\n")
        print(f"wrote {out_dir / 'call_sheet.md'}")
    else:
        print("no active recruiters — run scripts/active_recruiters.py harvest")

    roles: list[dict] = []
    if args.gov_roles and Path(args.gov_roles).exists():
        roles = json.loads(Path(args.gov_roles).read_text())
    else:
        import asyncio
        from agents.discovery.gov_jobs_scanner import scan
        roles = asyncio.run(scan(limit=200))["roles"]
    (out_dir / "gov_contacts.md").write_text(_gov_sheet(roles) + "\n")
    print(f"wrote {out_dir / 'gov_contacts.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
