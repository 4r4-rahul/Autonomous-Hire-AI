"""Keep the pipeline's JSON store (backend source of truth) and the DB (what the UI renders)
in sync for HUMAN DECISIONS made in the UI.

The gap this closes: the UI's "Mark Applied"/"Skip" writes only to the DB (api/applications.py
decide_application), and the résumé pipeline reads pipeline_state.json — so a submitted app kept
showing `pending_review` in JSON and could be re-queued/re-applied (duplicate risk), and the UI
count (DB) drifted from the pipeline count (JSON).

`sync_to_db_async` already carries JSON -> DB (submitted always wins). This module adds the reverse,
DB -> JSON, for decided statuses, so:
  • write_decision_to_json(...) — called inline by /decide so a UI decision lands in JSON immediately
  • reconcile_db_decisions_to_json() — one-shot back-fill for the historical drift

Both are FORWARD-ONLY (rank-gated): a decision can only advance an app's JSON status, never regress
a submitted/decided app back to pending_review.
"""
from __future__ import annotations

from core.state_store import mutate_state, load_state

# DB enum value (text) -> JSON status vocabulary. Inverse of db.migrate_from_json._map_status.
DB_TO_JSON_STATUS: dict[str, str] = {
    "pending_approval":      "pending_review",
    "reviewing":             "pending_review",
    "submitting":            "awaiting_confirmation",
    "submitted":             "submitted",
    "pending_manual_submit": "pending_manual_submit",
    "blocked":               "blocked",
    "skipped":               "skipped",
    "approved":              "approved",
    "rejected":              "rejected",
    "interview":             "interview",
    "queued":                "pending_review",
}

# Forward-only ordering: a status may only OVERWRITE a strictly-lower-ranked one, so we never
# downgrade a submitted/decided app. Unknown statuses rank 0 (freely overwritable).
_RANK: dict[str, int] = {
    "queued": 0,
    "pending_review": 1,
    "awaiting_confirmation": 2,
    "pending_manual_submit": 2,
    "blocked": 2,
    "rejected": 3,
    "skipped": 3,
    "interview": 4,
    "approved": 4,
    "submitted": 5,
}


def _rank(json_status: str | None) -> int:
    return _RANK.get(json_status or "", 0)


def write_decision_to_json(*, app_id: str = "", job_id: str = "", ats_url: str = "",
                           db_status: str = "", submitted_at: str | None = None) -> bool:
    """Apply one UI decision to the JSON store. Match by app id, then job_id, then apply_url.
    Forward-only. Returns True if the JSON app row was advanced; safe no-op if nothing to change /
    app absent. apply_url is the ONLY key stable across both stores — the DB and JSON were seeded
    separately so ~6% of app/job ids diverge, and an id-only match silently missed those (they stayed
    pending in the backend after 'Mark Applied'). Always pass ats_url so the match can't miss."""
    new_status = DB_TO_JSON_STATUS.get(db_status, db_status)
    if not new_status:
        return False
    with mutate_state() as state:
        apps = state.get("applications", [])
        app = next((a for a in apps if app_id and a.get("id") == app_id), None) \
            or next((a for a in apps if job_id and a.get("job_id") == job_id), None)
        # apply_url fallback: map the url → JSON job id → its application (stable natural key).
        if app is None and ats_url:
            u = ats_url.strip()
            jid = next((j.get("id") for j in state.get("job_listings", [])
                        if (j.get("apply_url") or "").strip() == u), None)
            if jid:
                app = next((a for a in apps if a.get("job_id") == jid), None)
        if app is None:
            return False
        if _rank(new_status) <= _rank(app.get("status")):
            return False  # would not advance — leave as-is (never regress a submitted app)
        app["status"] = new_status
        if new_status == "submitted" and submitted_at and not app.get("submitted_at"):
            app["submitted_at"] = submitted_at
        return True


async def reconcile_db_decisions_to_json() -> dict:
    """One-shot: pull every DECIDED status from the DB into the JSON store (forward-only), so the
    pipeline stops treating already-submitted/skipped apps as pending. Returns a change summary."""
    from db.session import AsyncSessionLocal
    from sqlalchemy import text

    async with AsyncSessionLocal() as s:
        rows = (await s.execute(text(
            "SELECT id::text, job_id::text, status::text, submitted_at::text, ats_url "
            "FROM applications WHERE status::text IN "
            "('submitted','skipped','pending_manual_submit','blocked','interview','approved','rejected','submitting')"
        ))).all()

    # Snapshot before-counts for the report.
    def _counts():
        st = load_state()
        from collections import Counter
        return Counter(a.get("status") for a in st.get("applications", []))

    before = _counts()
    changed = {"submitted": 0, "skipped": 0, "other": 0, "total": 0}
    for app_id, job_id, db_status, submitted_at, ats_url in rows:
        if write_decision_to_json(app_id=app_id, job_id=job_id, ats_url=ats_url or "",
                                  db_status=db_status, submitted_at=submitted_at):
            changed["total"] += 1
            key = DB_TO_JSON_STATUS.get(db_status, db_status)
            changed["submitted" if key == "submitted" else
                    "skipped" if key == "skipped" else "other"] += 1
    after = _counts()
    return {"changed": changed,
            "before": {k: before.get(k, 0) for k in ("pending_review", "submitted", "skipped")},
            "after":  {k: after.get(k, 0) for k in ("pending_review", "submitted", "skipped")},
            "db_decided_rows": len(rows)}
