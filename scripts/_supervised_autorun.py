"""Supervised live auto-apply run (cap=3). Email-as-truth verification.
Writes results to /tmp/ah_autorun.log. Persists state after each submit."""
import asyncio, json, os
from pathlib import Path
import yaml

# Do NOT silently force auto-submit on. Require the operator to opt in explicitly before running
# this supervised harness (it submits real applications): AUTONOMOUSHIRE_AUTO_SUBMIT=1 python -m ...
if os.environ.get("AUTONOMOUSHIRE_AUTO_SUBMIT") != "1":
    raise SystemExit("Refusing to run: set AUTONOMOUSHIRE_AUTO_SUBMIT=1 to confirm live auto-submit.")

from agents.application.auto_apply import select_auto_submit, auto_submit_enabled
from agents.application.form_filler import submit_application
from core.llm import get_llm


def _log(msg):
    print(msg, flush=True)


async def main():
    from core.state_store import load_state, save_state
    state = load_state()
    profile = yaml.safe_load(Path("data/user_profile.yaml").read_text())
    _log(f"auto_submit_enabled: {auto_submit_enabled()}")
    split = select_auto_submit(state, profile, 3)
    _log(f"submit={len(split['submit'])} queue={len(split['queue'])}")

    llm = get_llm("auto_apply")
    apps_by_id = {a["id"]: a for a in state["applications"]}
    # select_auto_submit returns JOB dicts; resolve each back to its pending_review app.
    apps_by_job = {a.get("job_id"): a for a in state["applications"]
                   if a.get("status") == "pending_review"}

    results = []
    for job in split["submit"]:
        app = apps_by_job.get(job.get("id"))
        if not app:
            _log(f"SKIP {job.get('company_name')}: no pending_review app for job {job.get('id')}")
            continue
        title = job.get("title") or app.get("role_title")
        _log(f"\n--- {job.get('company_name')} | {title} ---")
        try:
            updated = await submit_application(
                app, job, profile, llm, headless=True, auto_safe=True
            )
        except Exception as e:
            _log(f"ERROR: {type(e).__name__}: {str(e)[:200]}")
            continue
        # merge back + persist (locked + atomic + union-merge — no scheduler clobber)
        apps_by_id[app["id"]].update(updated)
        save_state(state)
        conf = updated.get("submit_confidence")
        _log(f"RESULT: {updated.get('status')} | confidence={conf} | "
             f"{updated.get('rejection_reason') or 'sent'}")
        results.append({
            "company": job.get("company_name"), "role": title,
            "status": updated.get("status"), "confidence": conf,
            "reason": updated.get("rejection_reason"),
        })

    from collections import Counter
    by_status = Counter(r["status"] for r in results)
    _log(f"\nDONE: {json.dumps({'count': len(results), 'by_status': dict(by_status), 'results': results})}")


asyncio.run(main())
