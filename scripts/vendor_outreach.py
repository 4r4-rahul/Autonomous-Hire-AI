#!/usr/bin/env python3
"""Bench marketing to state-government vendor recruiters.

Two stages, always in this order:

  # 1. refresh the contact list from the inbox (recruiters who sent public-sector reqs)
  python scripts/vendor_outreach.py harvest

  # 2. preview exactly what would go out, to whom — sends nothing
  python scripts/vendor_outreach.py preview --limit 25

  # 3. send, only after reading the preview
  python scripts/vendor_outreach.py send --limit 25 --confirm

`send` refuses to run without --confirm. Bulk sends cap at 400/day with 100 held in reserve for
on-demand replies; every address is recorded and never mailed twice.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# The long-form detailed résumé (6 pages, built 2026-09-09). The C2C/vendor market expects a full
# technical profile — recruiters keyword-match against it and forward it to the client — so depth
# helps here. It is the WRONG shape for a direct ATS application, where two pages is the norm;
# those keep using the tailored per-role résumés the pipeline generates.
DEFAULT_RESUME = "data_engineer_pittsburgh_resume.pdf"
DEFAULT_WORK_AUTH = ("H-1B. Open to W2 or C2C engagements - happy to discuss whichever "
                     "structure works best for you.")
DEFAULT_AVAILABILITY = "2 weeks' notice"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=("harvest", "preview", "send"))
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--resume", default=DEFAULT_RESUME)
    ap.add_argument("--work-auth", default=DEFAULT_WORK_AUTH)
    ap.add_argument("--availability", default=DEFAULT_AVAILABILITY)
    ap.add_argument("--all-vendors", action="store_true",
                    help="include vendors with no public-sector requirement in their history")
    ap.add_argument("--per-domain", type=int, default=2,
                    help="max recruiters per staffing firm in one batch (default 2)")
    ap.add_argument("--source", default="", help='only this provenance, e.g. "web:careers_page"')
    ap.add_argument("--confirm", action="store_true", help="required to actually send")
    args = ap.parse_args()

    if args.stage == "harvest":
        from agents.outreach.vendor_harvest import harvest, merge_past_vendors
        report = harvest()
        merged = merge_past_vendors()
        print(f"merged {merged['merged']} recruiters from past_vendors.json")
        from agents.outreach.vendor_harvest import load_contacts
        contacts = load_contacts()
        print(f"scanned {report['scanned_messages']} messages -> {len(contacts)} vendor recruiters")
        print(f"  with a public-sector requirement: {sum(1 for c in contacts if c['gov_reqs'])}")
        print(f"  not yet contacted:                {sum(1 for c in contacts if not c['already_contacted'])}")
        return 0

    if args.stage == "send" and not args.confirm:
        print("send requires --confirm (run `preview` first and read the sample)")
        return 1

    from agents.outreach.vendor_blast import run
    report = run(resume=args.resume, work_auth=args.work_auth, availability=args.availability,
                 limit=args.limit, gov_only=not args.all_vendors,
                 per_domain=args.per_domain, source=args.source,
                 dry_run=(args.stage == "preview"))

    if report.get("error"):
        print(report["error"])
        return 1

    if args.stage == "preview":
        sample = report.pop("sample", None)
        recipients = report.pop("recipients", [])
        print(json.dumps(report, indent=1))
        print(f"\n--- recipients ({len(recipients)}) ---")
        for r in recipients:
            states = ",".join(r["states"]) or "--"
            print(f"  {r['email'][:44]:46s} gov_reqs={r['gov_reqs']:<3d} {states}")
        if sample:
            print(f"\n--- sample message -> {sample['to']} ---")
            print(f"Subject: {sample['subject']}\n")
            print(sample["body"])
    else:
        print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
