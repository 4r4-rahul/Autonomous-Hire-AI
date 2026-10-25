"""
ATS Form Demo — fills and submits one real job per ATS platform.

Jobs are loaded directly from data/pipeline_state.json — no LinkedIn
navigation needed. The browser opens visibly for each form.

Run:
    python scripts/demo_ats.py

Press Ctrl+C at any time to stop after the current job.
"""
from __future__ import annotations

import asyncio
import json
import sys
import yaml
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import get_settings
from core.llm import get_llm
from core.state import JobSearchState
from agents.application.form_filler import detect_ats, submit_application
from agents.application.resume_tailor import tailor_one
from agents.application.app_quality_reviewer import review_application

_STATE_FILE   = Path("data/pipeline_state.json")
_APPLIED_FILE = Path("data/applied_jobs.json")
_PROFILE_PATH = Path("data/user_profile.yaml")

# ── Demo job selection: one per ATS type ──────────────────────────────────────
# Loaded from the existing queue — IDs confirmed present in pipeline_state.json

DEMO_JOBS = {
    "greenhouse": "34a39a36-1f0c-41c9-8e46-85dce1ac3820",  # SoFi | Principal SWE, AI Accelerated SDLC
    "lever":      "102fbb83-d52c-4dee-bcef-c79091bc83c8",  # Veeva | Data Engineer
    # Ashby ✅ already submitted in previous run
    # Generic skipped — remoteOK is an aggregator with no ATS form
}

DEMO_BY_URL: dict = {}


def _load_state() -> dict:
    return json.loads(_STATE_FILE.read_text())


def _applied_urls() -> set[str]:
    if not _APPLIED_FILE.exists():
        return set()
    try:
        return {e.get("apply_url", "") for e in json.loads(_APPLIED_FILE.read_text())}
    except Exception:
        return set()


def _find_job(state: dict, job_id: str | None, apply_url: str | None) -> dict | None:
    jobs = state.get("job_listings", [])
    if job_id:
        for j in jobs:
            if j.get("id") == job_id:
                return dict(j)
    if apply_url:
        for j in jobs:
            if j.get("apply_url", "") == apply_url:
                return dict(j)
    return None


async def run_demo() -> None:
    cfg = get_settings()
    state = _load_state()
    applied = _applied_urls()
    profile = yaml.safe_load(_PROFILE_PATH.read_text())

    llm_score  = get_llm("demo", model="claude-haiku-4-5")
    llm_tailor = get_llm("demo")          # uses default (opus-4-8)
    llm_review = get_llm("demo", model="claude-haiku-4-5")
    llm_cheap  = get_llm("demo", model="claude-haiku-4-5")

    print("\n" + "═" * 72)
    print("  AutonomousHire — ATS Form Demo")
    print("  One live submission per ATS platform, browser visible")
    print("═" * 72 + "\n")

    # Build ordered demo list
    demo_items: list[tuple[str, dict]] = []
    for ats_type, job_id in DEMO_JOBS.items():
        apply_url = DEMO_BY_URL.get(ats_type)
        job = _find_job(state, job_id, apply_url)
        if job is None:
            print(f"  ⚠  {ats_type}: job not found in state — skipping")
            continue
        if job.get("apply_url", "") in applied:
            print(f"  ✓  {ats_type}: {job['company_name']} already applied — skipping")
            continue
        demo_items.append((ats_type, job))

    if not demo_items:
        print("  All demo jobs already applied or missing.")
        return

    print(f"  Demo queue ({len(demo_items)} forms):")
    for ats_type, job in demo_items:
        print(f"    {ats_type:<18} {job['company_name']:<30} {job['title'][:40]}")
    print()

    results = []
    app_state: JobSearchState = {
        "job_listings": [],
        "fit_scores": [],
        "applications": [],
        "outreach_records": [],
        "pipeline_runs": [],
    }

    for idx, (ats_type, job) in enumerate(demo_items):
        print(f"\n{'─'*72}")
        print(f"  [{idx+1}/{len(demo_items)}] {ats_type.upper()} — {job['company_name']} | {job['title']}")
        print(f"  URL: {job['apply_url'][:70]}")
        print(f"{'─'*72}")

        # ── Step 1: Fit score ────────────────────────────────────────────
        print("  → Scoring fit…")
        from core.state import FitScore
        import uuid as _uuid
        try:
            result = await llm_score.chat_json(
                "Score candidate fit 0-100. Return JSON: "
                "{\"score\":0-100,\"passed\":bool,\"highlights\":[str],\"missing\":[str]}",
                [{"role":"user","content":(
                    f"Candidate: {profile name}, Senior Data/Platform Engineer, "
                    f"6+ yrs, Python/AWS/Spark/MLOps\n\n"
                    f"Job: {job['title']} at {job['company_name']}\n"
                    f"Description:\n{job.get('description','')[:1500]}"
                )}],
                max_tokens=512,
            )
            score = int(result.get("score", 0))
            fit_score: FitScore = {
                "id": str(_uuid.uuid4()),
                "job_id": job["id"],
                "score": score,
                "passed_threshold": score >= cfg.min_fit_score,
                "highlights": result.get("highlights", []),
                "missing_keywords": result.get("missing", []),
                "model": "claude-haiku-4-5",
                "scored_at": datetime.now(timezone.utc).isoformat(),
            }
            print(f"  → Score: {score}/100  {'✓ passed' if fit_score['passed_threshold'] else '✗ below threshold'}")
        except Exception as e:
            print(f"  → Score failed: {e} — using default 75")
            fit_score = {"id": str(_uuid.uuid4()), "job_id": job["id"],
                         "score": 75, "passed_threshold": True,
                         "highlights": [], "missing_keywords": [],
                         "model": "claude-haiku-4-5",
                         "scored_at": datetime.now(timezone.utc).isoformat()}

        # Demo continues even below threshold so we show every form type
        if not fit_score["passed_threshold"]:
            print(f"  ℹ  Below threshold but continuing for demo purposes")

        # ── Step 2: Tailor resume ────────────────────────────────────────
        print("  → Tailoring resume…")
        try:
            app = await tailor_one(job, fit_score, llm_tailor, profile, llm_cheap=llm_cheap)
        except Exception as e:
            print(f"  → Tailor failed: {e}")
            results.append({"ats": ats_type, "company": job["company_name"],
                             "role": job["title"], "status": "tailor_failed"})
            continue

        if not app:
            print("  → tailor_one returned None")
            results.append({"ats": ats_type, "company": job["company_name"],
                             "role": job["title"], "status": "tailor_none"})
            continue

        print(f"  → Resume tailored ✓  (quality_score={app.get('quality_score','?')})")

        # ── Step 3: Quality review ───────────────────────────────────────
        print("  → Quality review…")
        try:
            app, _report = await review_application(app, job, llm_review)
        except Exception as e:
            print(f"  → Review failed: {e} — continuing")

        quality = app.get("quality_score", 0)
        print(f"  → Quality: {quality}/100  status={app.get('status')}")
        if app.get("status") == "blocked":
            print("  ✗ Blocked by quality review — skipping")
            results.append({"ats": ats_type, "company": job["company_name"],
                             "role": job["title"], "status": "blocked"})
            continue

        # ── Step 4: Submit (browser opens here) ─────────────────────────
        print(f"  → Opening browser for {ats_type.upper()} form…")
        try:
            submitted_app = await submit_application(
                app, job, profile, llm_tailor, headless=False
            )
        except Exception as e:
            print(f"  → Submit error: {e}")
            results.append({"ats": ats_type, "company": job["company_name"],
                             "role": job["title"], "status": "error"})
            continue

        status = submitted_app.get("status", "unknown")
        conf   = submitted_app.get("confirmation_url", "")
        icon   = "✅" if status == "submitted" else ("⚠️ " if "manual" in status else "❌")
        print(f"  {icon} {status}  {conf[:60] if conf else ''}")
        results.append({"ats": ats_type, "company": job["company_name"],
                        "role": job["title"], "status": status,
                        "confirmation": conf})

        # Human-like pause between forms
        if idx < len(demo_items) - 1:
            print(f"\n  Pausing 10s before next form…")
            await asyncio.sleep(10)

    # ── Final summary ─────────────────────────────────────────────────────────
    print("\n\n" + "═" * 72)
    print("  Demo Complete — Results")
    print("═" * 72)
    print(f"  {'ATS':<18} {'Company':<28} {'Status'}")
    print(f"  {'─'*17} {'─'*27} {'─'*20}")
    for r in results:
        icon = "✅" if r["status"] == "submitted" else ("⚠️ " if "manual" in r["status"] else "❌")
        print(f"  {r['ats']:<18} {r['company']:<28} {icon} {r['status']}")
    submitted_n = sum(1 for r in results if r["status"] == "submitted")
    print(f"\n  {submitted_n}/{len(results)} submitted automatically")
    print()


if __name__ == "__main__":
    asyncio.run(run_demo())
