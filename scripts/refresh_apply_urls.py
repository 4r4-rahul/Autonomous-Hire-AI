"""CLI: refresh queued applications' apply URLs against the LIVE ATS boards.

Keeps the review queue pointing at currently-open application forms (stale roles 404 / orphans
have no URL → user "opens a general URL"). Core logic lives in agents/application/url_refresher.py
so the scheduler can run the same pass periodically.

Run: PYTHONPATH=. .venv/bin/python scripts/refresh_apply_urls.py [--apply]
(without --apply it's a dry run that only reports counts).
"""
import asyncio
import sys
import json
from pathlib import Path

ROOT = Path(__file__).parent.parent
STATE = ROOT / "data" / "pipeline_state.json"


async def main(apply: bool):
    from agents.application.url_refresher import refresh_pending_urls

    state = json.loads(STATE.read_text())
    state, report = await refresh_pending_urls(state, apply=apply)
    if apply:
        STATE.write_text(json.dumps(state, indent=2))
    print(f"\n{'APPLIED' if apply else 'DRY RUN'} — refresh report: {json.dumps(report, indent=2)}")


if __name__ == "__main__":
    asyncio.run(main(apply="--apply" in sys.argv))
