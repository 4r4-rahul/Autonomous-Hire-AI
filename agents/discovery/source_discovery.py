"""Source-discovery engine — the MOAT: keep finding NEW ATS boards (sources) posting contract roles
in-lane, before they propagate to the crowded aggregators.

A job posting originates on a company/vendor ATS (Greenhouse/Lever/Ashby/SmartRecruiters/Workday),
THEN syndicates to LinkedIn/Indeed/Dice hours-to-days later. Whoever monitors the most SOURCES catches
roles at origin — first, often exclusively. We scan a curated universe (data/board_sponsors.json); this
grows it by ingesting freshly-DISCOVERED ATS slugs and keeping only the ones with live US contract roles.

Discovery of slugs happens OUTSIDE the pipeline (Claude's WebSearch across ATS domains — the unique
capability; slugs land in data/discovered_sources.json). This module is the INGEST half: probe each
discovered slug, verify a real US contract role in-lane (structured employment flag), add live ones to
the source universe. Re-runnable + idempotent. Wire `discover_and_ingest()` into a periodic job or run
after a fresh WebSearch harvest.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from core.logger import get_logger
from agents.discovery.apply_url_resolver import _fetch_company_postings, loc_is_us_ok
from agents.discovery.board_scanner import _title_ok
from core.employment import classify_employment_for_job

logger = get_logger("source_discovery")

_ROOT = Path(__file__).parent.parent.parent
_REGISTRY = _ROOT / "data" / "discovered_sources.json"     # growing slug registry (from WebSearch harvests)
_BOARD_SPONSORS = _ROOT / "data" / "board_sponsors.json"   # the live scan universe


def _load_registry() -> dict:
    try:
        return json.loads(_REGISTRY.read_text())
    except Exception:
        return {"slugs": [], "ingested": []}


def add_discovered(slugs: list[str]) -> int:
    """Record newly-harvested ATS slugs (from a WebSearch source hunt) into the registry (dedup).
    Returns how many were newly added. The ingest pass then probes + promotes the live contract ones."""
    reg = _load_registry()
    have = {s.strip().lower() for s in reg.get("slugs", [])}
    new = [s for s in slugs if s and s.strip().lower() not in have]
    reg["slugs"] = reg.get("slugs", []) + new
    try:
        _REGISTRY.write_text(json.dumps(reg, indent=2))
    except Exception:
        pass
    return len(new)


async def _probe(slug: str) -> tuple[str, int, int]:
    """(slug, total_postings, us_contract_in_lane). Uses the ATS's structured employment flag."""
    try:
        p = await _fetch_company_postings(slug)
    except Exception:
        return slug, 0, 0
    if not p:
        return slug, 0, 0
    n = 0
    for x in p:
        if not (_title_ok(x.get("title", "")) and loc_is_us_ok(x.get("location", ""))):
            continue
        et = classify_employment_for_job(f"board:{x.get('ats', '')}", "", x.get("title", ""),
                                         x.get("description", ""), x.get("employment_type", ""))
        if et == "contract":
            n += 1
    return slug, len(p), n


async def _egress_ok() -> bool:
    """Guard against poisoning the candidate pool. On a network failure _fetch_company_postings
    returns None indistinguishably from "board exists but has nothing matching", and ingest() below
    marks every probed slug "ingested" (permanently skipped) regardless of outcome — so if this
    environment's egress to the ATS APIs is blocked (org policy 403, proxy tunnel failure, etc.),
    every candidate would be misdiagnosed as a dead board and never get a real chance again, even
    after egress is restored. Confirm reachability against a real endpoint first; if it fails, ingest()
    leaves the candidates unprobed instead of burning them."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.get("https://boards-api.greenhouse.io/v1/boards/anthropic/jobs")
        return r.status_code < 500
    except Exception:
        return False


async def ingest(slugs: list[str] | None = None, *, require_contract: bool = True) -> dict:
    """Probe candidate slugs; add the ones with a live US contract role in-lane (or any live board if
    require_contract=False) to board_sponsors. Returns a summary. Idempotent."""
    reg = _load_registry()
    cand = [s for s in (slugs if slugs is not None else reg.get("slugs", []))]
    already = {c.strip().lower() for c in reg.get("ingested", [])}
    cand = [s for s in cand if s.strip().lower() not in already]
    if not cand:
        return {"probed": 0, "added": 0}

    if not await _egress_ok():
        return {"probed": 0, "added": 0, "blocked": True,
                "note": "ATS egress unreachable from this environment (control probe to "
                        "boards-api.greenhouse.io failed) — candidates left unprobed in the registry "
                        "so they can be retried once access is restored, instead of being marked "
                        "'ingested' on a false negative."}

    sem = asyncio.Semaphore(10)

    async def _one(s):
        async with sem:
            return await _probe(s)

    results = await asyncio.gather(*[_one(s) for s in cand])
    add = [s for s, tot, c in results if (c > 0) or (not require_contract and tot > 0)]

    data = json.loads(_BOARD_SPONSORS.read_text())
    ex = {c.strip().lower() for c in data.get("companies", [])}
    fresh = [s for s in add if s.lower() not in ex]
    if fresh:
        data["companies"].extend(fresh)
        _BOARD_SPONSORS.write_text(json.dumps(data, indent=2))
    reg["ingested"] = reg.get("ingested", []) + [s for s, _, _ in results]
    _REGISTRY.write_text(json.dumps(reg, indent=2))

    logger.info("source_discovery_ingest", probed=len(cand), with_contract=len(add),
                added=len(fresh), universe=len(data["companies"]))
    return {"probed": len(cand), "with_contract": len(add), "added": len(fresh),
            "universe": len(data["companies"]), "new_sources": fresh}


async def discover_and_ingest() -> dict:
    """Periodic entrypoint: ingest whatever slugs the latest WebSearch harvest recorded. (The harvest
    itself is a Claude/WebSearch step — see the module docstring — that appends to the registry.)"""
    return await ingest()


if __name__ == "__main__":
    print(asyncio.run(ingest()))
