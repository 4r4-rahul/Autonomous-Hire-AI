#!/usr/bin/env python3
"""Immediately scan the newly-added finance/fintech sponsor boards and intake fitting US roles into
the queue — without waiting for the rotation cursor to reach them. Points the cursor at the finance
block, runs one board scan over exactly those companies (persist via locked merge), then restores
the original cursor so the normal rotation is undisturbed.

Run: PYTHONPATH=. .venv/bin/python scripts/scan_finance_now.py
"""
from __future__ import annotations
import asyncio, sys, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from agents.discovery import board_scanner as bs
from core.state_store import load_state

# The finance/fintech names added this session (probe_finance_sponsors.py).
FINANCE = [
    "northwestern mutual", "sixth street", "okx", "figure", "lincoln financial", "elliptic",
    "general atlantic", "dimensional fund advisors", "crypto.com", "blockchain.com", "silver lake",
    "consensys", "blockdaemon", "airwallex", "adyen", "acorns", "iex", "wise", "nium", "payoneer",
    "stash", "visa", "william blair", "stifel", "us bank",
]


async def main():
    rotation = bs._rotation_companies()
    idx = {c.strip().lower(): i for i, c in enumerate(rotation)}
    positions = sorted(idx[n] for n in (f.lower() for f in FINANCE) if n in idx)
    if not positions:
        print("none of the finance names are in the rotation — nothing to scan")
        return
    lo, hi = positions[0], positions[-1]
    span = hi - lo + 1
    print(f"finance block: rotation indices {lo}..{hi} ({span} slots, {len(positions)} finance names)")

    saved = bs._cursor()
    try:
        bs._save_cursor(lo)
        state = load_state()
        _, report = await bs.run(state, max_companies=span, intake_limit=60,
                                 tailor=False, persist=True)
    finally:
        bs._save_cursor(saved)   # restore normal rotation

    print("\nscan report:")
    print(json.dumps({k: report.get(k) for k in
                      ("scanned", "discovered", "queued", "persisted", "created", "skipped", "duplicates")
                      if k in report}, indent=2))
    fresh = sum(1 for a in load_state().get("applications", []) if a.get("status") == "pending_review")
    print(f"\npending_review now: {fresh}")


if __name__ == "__main__":
    asyncio.run(main())
