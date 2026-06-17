#!/usr/bin/env python3
"""Expedite the review queue the expert way: PRIORITIZE (fit x sponsorship x recency) -> PRE-TAILOR
the top wave so each form opens instantly (no 20-30s on-demand wait) -> hand you a ranked worklist
with apply URLs to blitz. Quality stays in the loop; you just work the best roles first.

Usage:
  python scripts/expedite_queue.py                  # rank + print today's worklist (free, instant)
  python scripts/expedite_queue.py --top 25         # worklist size
  python scripts/expedite_queue.py --pretailor 15   # ALSO pre-tailor the top 15 that need it (Opus)
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml
from agents.discovery.targeting import rank_for_application
from core.state_store import load_state

_PEND = ("pending_review", "pending_manual_submit")


def _worklist(top: int):
    state = load_state()
    jl = {j["id"]: j for j in state.get("job_listings", [])}
    apps_by_job = {a.get("job_id"): a for a in state.get("applications", [])
                   if a.get("status") in _PEND}
    jobs = [jl[jid] for jid in apps_by_job if jid in jl]
    try:
        profile = yaml.safe_load(Path("data/user_profile.yaml").read_text())
    except Exception:
        profile = {}
    fit_by_id = {f.get("job_id"): f for f in state.get("fit_scores", [])}
    ranked = rank_for_application(jobs, fit_by_id, profile, state)
    rows = [r for r in ranked if r["job"].get("id") in apps_by_job]
    return rows[:top], apps_by_job


def _print(rows, apps_by_job):
    print(f"\n=== Today's apply worklist — top {len(rows)} (best first) ===\n")
    print(f"  {'#':>2}  {'fit':>3}  {'sponsor':<9} {'tailored':<8} company / title")
    print("  " + "-" * 96)
    for i, r in enumerate(rows, 1):
        j = r["job"]; a = apps_by_job.get(j.get("id"), {})
        ready = "ready" if a.get("tailored_resume_path") else "PENDING"
        co = (j.get("company_name") or "")[:24]
        ti = (j.get("title") or "")[:42]
        print(f"  {i:>2}  {int(r['fit']):>3}  {r['sponsor_label']:<9} {ready:<8} {co} — {ti}")
        print(f"        {j.get('apply_url','')}")


async def _pretailor(rows, apps_by_job, n: int):
    from agents.application.on_demand_tailor import tailor_application
    targets = [r for r in rows if not apps_by_job.get(r["job"].get("id"), {}).get("tailored_resume_path")][:n]
    if not targets:
        print("\n(nothing to pre-tailor — top wave already tailored)")
        return
    print(f"\n=== Pre-tailoring top {len(targets)} (Opus, ~20-30s each, concurrency 3) ===")
    sem = asyncio.Semaphore(3)

    async def _one(r):
        jid = r["job"].get("id")
        async with sem:
            try:
                res = await tailor_application(job_id=jid)
                ok = res.get("status") in ("tailored", "already_tailored")
                print(f"  {'OK ' if ok else '.. '} {r['job'].get('company_name','')[:24]:24} "
                      f"{r['job'].get('title','')[:34]:34} {res.get('status') or res.get('error','')}")
            except Exception as e:
                print(f"  ERR {r['job'].get('company_name','')[:24]:24} {str(e)[:50]}")

    await asyncio.gather(*[_one(r) for r in targets])
    print("Pre-tailor wave done — those forms now open instantly.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=25, help="worklist size (best first)")
    ap.add_argument("--pretailor", type=int, default=0, metavar="N",
                    help="also pre-tailor the top N that aren't tailored yet (Opus cost)")
    args = ap.parse_args()

    rows, apps_by_job = _worklist(args.top)
    if not rows:
        print("Queue is empty — nothing to expedite.")
        return
    _print(rows, apps_by_job)
    n_pending = sum(1 for r in rows if not apps_by_job.get(r["job"].get("id"), {}).get("tailored_resume_path"))
    print(f"\n  {n_pending}/{len(rows)} in this wave still need tailoring "
          f"(run with --pretailor {n_pending} to make them instant-open).")
    if args.pretailor:
        asyncio.run(_pretailor(rows, apps_by_job, args.pretailor))


if __name__ == "__main__":
    main()
