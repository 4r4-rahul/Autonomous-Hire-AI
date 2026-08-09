"""Dead-link queue sweep — keep the review queue trustworthy as postings close.

Jobs on Greenhouse/Lever/Ashby disappear the moment they're filled (~30-40/day here), so the
pending_review queue steadily accumulates apply URLs that 404. This sweeps them out SAFELY:

  • Liveness is checked against each ATS's authoritative JSON API (reused from scripts.prune_dead_queue),
    NEVER a plain GET — Greenhouse/Ashby bot-block GETs with 403, a false "dead". Only a DEFINITIVE
    404/410 counts; blocked/timeout/unknown are KEPT.
  • Network probing happens OUTSIDE any lock; the status flip is a fast critical section via the atomic
    cross-process-locked store, so it never clobbers the live pipeline.
  • Already-applied protection: a 404 often just means "you applied and the posting then closed". Before
    skipping, we cross-check the DB — any app the DB knows as submitted/manual-submit/rejected is
    RESTORED, never dropped (guards the JSON↔DB lag).
  • Reversible: status → skipped (+skip_reason), never deleted.

Wired into the scheduler (job_dead_link_sweep). Also runnable directly for a one-off.
"""
from __future__ import annotations

import asyncio

import httpx

from core.logger import get_logger
from core.state_store import mutate_state, load_state
from scripts.prune_dead_queue import liveness   # authoritative per-ATS liveness (API, not GET)

logger = get_logger("queue_sweeper")

# DB statuses that mean "already actioned" — a dead posting here is expected (applied, then closed).
_ACTIONED = {"submitted", "pending_manual_submit", "rejected", "interview", "offer"}


async def _probe_pending(concurrency: int = 8) -> dict[str, str]:
    """Return {app_id: 'live'|'dead'|'unknown'} for every pending_review app. Read-only, no lock."""
    state = load_state()
    jl = {j["id"]: j for j in state.get("job_listings", [])}
    pending = [a for a in state.get("applications", []) if a.get("status") == "pending_review"]
    sem = asyncio.Semaphore(concurrency)
    verdict: dict[str, str] = {}

    async def _one(app):
        url = (jl.get(app.get("job_id")) or {}).get("apply_url")
        async with sem:
            verdict[app["id"]] = await liveness(client, url)

    async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0"}) as client:
        await asyncio.gather(*[_one(a) for a in pending])
    return verdict


def _db_actioned(app_ids: set[str]) -> dict[str, str]:
    """DB status for the given app_ids that are already actioned (submitted/etc). Empty on any DB error
    — a DB hiccup must never widen the prune; we simply skip the protection lookup and rely on the
    conservative definitive-dead signal."""
    if not app_ids:
        return {}
    try:
        import os
        from sqlalchemy import create_engine, text
        url = os.getenv("DATABASE_URL", "postgresql+asyncpg://localhost/autonomoushire") \
            .replace("+asyncpg", "+psycopg2").replace("postgresql+psycopg2", "postgresql")
        out: dict[str, str] = {}
        with create_engine(url).connect() as c:
            for i in app_ids:
                st = c.execute(text("SELECT status::text FROM applications WHERE id=:i"), {"i": i}).scalar()
                if st in _ACTIONED:
                    out[i] = st
        return out
    except Exception as e:
        logger.warning("queue_sweep_db_protect_skipped", error=str(e)[:120])
        return {}


async def sweep_dead_queue(apply: bool = True) -> dict:
    """Probe the pending queue and (if apply) skip the definitively-dead postings. Returns a summary."""
    verdict = await _probe_pending()
    dead = {aid for aid, v in verdict.items() if v == "dead"}
    counts = {"pending": len(verdict), "live": sum(v == "live" for v in verdict.values()),
              "dead": len(dead), "unknown": sum(v == "unknown" for v in verdict.values())}

    if not apply or not dead:
        logger.info("queue_sweep_dryrun" if not apply else "queue_sweep_nothing_dead", **counts)
        return {**counts, "skipped": 0, "protected": 0}

    # Protect apps the DB already knows as applied (JSON may lag the DB).
    protected = _db_actioned(dead)
    to_skip = dead - set(protected)

    skipped = restored = 0
    with mutate_state() as state:
        for a in state.get("applications", []):
            aid = a.get("id")
            if aid in to_skip and a.get("status") == "pending_review":
                a["status"] = "skipped"
                a["skip_reason"] = "posting expired — ATS API reports job removed (queue sweep)"
                skipped += 1
            elif aid in protected and a.get("status") == "pending_review":
                a["status"] = protected[aid]          # restore the true applied status; don't drop
                a.pop("skip_reason", None)
                restored += 1

    # Mirror to the DB so the UI reflects it.
    try:
        from db.migrate_from_json import sync_to_db_async
        await sync_to_db_async()
    except Exception as e:
        logger.warning("queue_sweep_db_sync_failed", error=str(e)[:120])

    logger.info("queue_sweep_done", **counts, skipped=skipped, protected=restored)
    return {**counts, "skipped": skipped, "protected": restored}


if __name__ == "__main__":
    import sys
    r = asyncio.run(sweep_dead_queue(apply="--apply" in sys.argv))
    print(r)
