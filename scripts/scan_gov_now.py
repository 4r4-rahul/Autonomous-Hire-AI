#!/usr/bin/env python3
"""Run the state/local-government C2C lane once, now.

  python scripts/scan_gov_now.py                  # scan vendor boards, park what fits
  python scripts/scan_gov_now.py --gov-only       # only roles with a detected government client
  python scripts/scan_gov_now.py --employers      # also scan government EMPLOYER boards (W2)
  python scripts/scan_gov_now.py --dry-run        # discover + filter, park nothing
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.state_store import load_state
from agents.discovery.gov_scanner import run


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gov-only", action="store_true")
    ap.add_argument("--employers", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=200)
    args = ap.parse_args()

    _, report = await run(load_state(), limit=args.limit, tailor=False,
                          persist=not args.dry_run, gov_only=args.gov_only,
                          include_employers=args.employers)
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
