"""One-shot: run the production off-LinkedIn apply pipeline (intake) once and report.
Resolves discovered leads → real ATS form + Opus-tailored résumé → parks in the review queue."""
import asyncio
import json
from collections import Counter
from pathlib import Path

from orchestration.scheduler import job_apply_pipeline

SP = Path("data/pipeline_state.json")


def _counts():
    s = json.loads(SP.read_text())
    return dict(Counter(a.get("status") for a in s["applications"])), len(s["applications"])


async def main():
    before, n0 = _counts()
    print("BEFORE:", before, flush=True)
    await job_apply_pipeline()        # production job: intake(limit=12) + save + DB sync
    after, n1 = _counts()
    print("AFTER:", after, flush=True)
    # show the newly parked apps + their resolved ATS URLs
    s = json.loads(SP.read_text())
    jobs = {j["id"]: j for j in s["job_listings"]}
    new = s["applications"][n0:]
    print(f"NEW APPS: {len(new)}", flush=True)
    for a in new:
        j = jobs.get(a.get("job_id"), {})
        print(f"  {a.get('company_name')} | {a.get('role_title')} | {a.get('status')} "
              f"| q={a.get('quality_score')} | {j.get('apply_url','')[:60]}", flush=True)
    print("RUN_DONE", flush=True)


asyncio.run(main())
