"""
Single, safe accessor for data/pipeline_state.json.

The state file has multiple concurrent writers — the scheduler process (board scan, apply pipeline,
email monitor) AND the API process (on-demand tailoring). Before this, each did an unlocked
read-modify-write of the whole file, so writes raced (lost updates) and a crash mid-write left a
truncated, unparseable file. This module makes ALL writes:
  • cross-process serialized (filelock on data/pipeline_state.lock)
  • atomic (write tmp + os.replace — readers never see a partial file)
  • deduped on the way out (collapse duplicate jobs/apps that leak branded/foreign URLs)
and distinguishes a MISSING file (fresh start, ok) from a CORRUPT file (raise — never silently
return empty, which would let the DB-sync prune wipe everything).

Readers can call load_state() lock-free: os.replace guarantees they see either the old or the new
complete file, never a partial one. Writers MUST use save_state() or the mutate_state() context.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock

_STATE = Path("data/pipeline_state.json")
_LOCK = Path("data/pipeline_state.lock")
_lock = FileLock(str(_LOCK), timeout=60)


def _fresh() -> dict:
    return {
        "companies": [], "job_listings": [], "fit_scores": [],
        "applications": [], "pending_approvals": [], "recruiter_contacts": [],
        "linkedin_contacts": [], "email_events": [], "meetings_scheduled": [],
        "weekly_quality_score": 0.0, "weekly_quantity_count": 0,
        "response_rate": 0.0, "interview_rate": 0.0,
        "last_run_at": None, "errors": [], "agent_statuses": {}, "scanned_company_ids": [],
    }


class StateCorruptError(RuntimeError):
    """The state file exists but is not parseable — refuse to proceed (never overwrite with empty)."""


def load_state(*, fresh_if_missing: bool = True) -> dict:
    """Read the state. MISSING file → fresh template (if allowed). CORRUPT file → StateCorruptError
    (callers must NOT save/sync off a corrupt read — that's how the DB gets wiped)."""
    if not _STATE.exists():
        if fresh_if_missing:
            return _fresh()
        raise FileNotFoundError(_STATE)
    text = _STATE.read_text()
    if not text.strip():
        raise StateCorruptError(f"{_STATE} is empty")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise StateCorruptError(f"{_STATE} is not valid JSON: {e}") from e


# ── dedup (moved here so save_state can apply it everywhere) ──────────────────

def _url_quality(u: str) -> int:
    """Rank apply-url quality so dedup keeps the best variant of a duplicated job."""
    u = (u or "").lower()
    if not u:
        return -1
    if "greenhouse.io" in u or any(h in u for h in
            ("jobs.lever.co", "jobs.ashbyhq.com", "apply.workable.com")):
        return 5
    if any(p in u for p in ("jobs/search", "open-positions/job?", "/jobs?gh",
                            "careers-list", "/jobs/apply/")):
        return 0
    if "gh_jid" in u:
        return 1
    return 3


_MAX_LEAD_AGE_DAYS = 45   # age out app-less leads older than this to bound the state file size


def _lead_is_stale(job: dict, app_job_ids: set) -> bool:
    """A job_listing with NO application, discovered long ago — safe to drop (keeps the file from
    growing unbounded). Jobs that have an application are NEVER aged out."""
    if job.get("id") in app_job_ids:
        return False
    # Age out by DISCOVERED_AT only (when WE first saw it) — NOT posted_at. A role we just
    # discovered deserves the full window even if its posting date is old, else freshly-added
    # leads with an old posted_at vanish on the very next save.
    ts = job.get("discovered_at")
    if not ts:
        return False   # unknown discovery time → keep (don't over-prune)
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).days > _MAX_LEAD_AGE_DAYS
    except Exception:
        return False


def dedup_state(state: dict) -> dict:
    """Collapse duplicate job_listings (by id and apply_url) and applications (by job_id), keeping
    the best-url / best-status variant, and age out stale app-less leads. Idempotent."""
    app_job_ids = {a.get("job_id") for a in state.get("applications", [])}
    best_by_id: dict[str, dict] = {}
    for j in state.get("job_listings", []):
        if _lead_is_stale(j, app_job_ids):
            continue   # drop old app-less leads to bound file growth
        cur = best_by_id.get(j.get("id"))
        if cur is None or _url_quality(j.get("apply_url")) > _url_quality(cur.get("apply_url")):
            best_by_id[j.get("id")] = j
    seen_urls, jobs = set(), []
    for j in sorted(best_by_id.values(), key=lambda x: -_url_quality(x.get("apply_url"))):
        u = j.get("apply_url")
        if u and u in seen_urls:
            continue
        if u:
            seen_urls.add(u)
        jobs.append(j)
    valid_ids = {j.get("id") for j in jobs}

    _prio = {"submitted": 6, "pending_review": 5, "pending_manual_submit": 4,
             "awaiting_confirmation": 3, "blocked": 2, "rejected": 1, "skipped": 0}
    best_app: dict[str, dict] = {}
    for a in state.get("applications", []):
        jid = a.get("job_id")
        if jid not in valid_ids:
            continue
        cur = best_app.get(jid)
        if cur is None or _prio.get(a.get("status"), 0) > _prio.get(cur.get("status"), 0):
            best_app[jid] = a
    state["job_listings"] = jobs
    state["applications"] = list(best_app.values())
    return state


def _atomic_write(state: dict) -> None:
    _STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    os.replace(tmp, _STATE)     # atomic on POSIX — readers see old-or-new, never partial


_APP_PRIO = {"submitted": 6, "pending_review": 5, "pending_manual_submit": 4,
             "awaiting_confirmation": 3, "blocked": 2, "rejected": 1, "skipped": 0}


def _merge_states(disk: dict, caller: dict) -> dict:
    """Union the caller's (possibly stale) state with what's currently on disk, so a blind full-state
    save can't clobber rows another process appended/advanced meanwhile.
      • job_listings: union by id; caller's version wins for shared ids (URL canonicalization etc).
      • applications: union by id; for a shared id keep the MORE-PROGRESSED status (submitted beats a
        stale pending_review) so concurrent decisions/submits aren't reverted.
      • fit_scores: union by job_id, caller wins.
    Disk-only rows (e.g. apps a concurrent scan just added) are always retained.
    """
    out = dict(caller)
    # job_listings + fit_scores: caller wins, keep disk-only.
    for coll, key in (("job_listings", "id"), ("fit_scores", "job_id")):
        seen = {x.get(key) for x in caller.get(coll, [])}
        out[coll] = list(caller.get(coll, [])) + [x for x in disk.get(coll, []) if x.get(key) not in seen]
    # applications: union by id, keep the higher-priority status on conflict.
    by_id: dict = {a.get("id"): a for a in disk.get("applications", [])}
    for a in caller.get("applications", []):
        prev = by_id.get(a.get("id"))
        if prev is None or _APP_PRIO.get(a.get("status"), 0) >= _APP_PRIO.get(prev.get("status"), 0):
            by_id[a.get("id")] = a
    out["applications"] = list(by_id.values())
    return out


def save_state(state: dict, *, dedup: bool = True, merge: bool = True) -> None:
    """Serialize state to disk — cross-process locked + atomic + deduped. By default UNION-MERGES
    with the current disk state so a stale snapshot can't clobber concurrent appends/decisions."""
    with _lock:
        if merge and _STATE.exists():
            try:
                state = _merge_states(load_state(), state)
            except StateCorruptError:
                pass   # disk unreadable → just write the caller's state (better than nothing)
        if dedup:
            state = dedup_state(state)
        _atomic_write(state)


def merge_new(new_jobs: list[dict], new_apps: list[dict]) -> int:
    """Append freshly-produced job_listings + applications to the CURRENT on-disk state under the
    lock (re-reads first), so a long-running scan/intake can't clobber concurrent writes by saving
    a stale full-state snapshot. Skips items that already exist (by job id / apply_url / app job_id).
    Returns the number of applications actually added."""
    with _lock:
        state = load_state()
        job_ids = {j.get("id") for j in state.get("job_listings", [])}
        urls = {j.get("apply_url") for j in state.get("job_listings", []) if j.get("apply_url")}
        app_jobs = {a.get("job_id") for a in state.get("applications", [])}
        for j in new_jobs:
            if j.get("id") in job_ids or (j.get("apply_url") and j.get("apply_url") in urls):
                continue
            state.setdefault("job_listings", []).append(j)
            job_ids.add(j.get("id")); urls.add(j.get("apply_url"))
        added = 0
        for a in new_apps:
            if a.get("job_id") in app_jobs:
                continue
            state.setdefault("applications", []).append(a)
            app_jobs.add(a.get("job_id")); added += 1
        state = dedup_state(state)
        _atomic_write(state)
        return added


@contextmanager
def mutate_state(*, dedup: bool = True):
    """Locked read-modify-write: re-reads the state UNDER the lock, yields it for mutation, then
    atomically writes it back. Use this for any change that must not race a concurrent writer."""
    with _lock:
        state = load_state()
        yield state
        if dedup:
            state = dedup_state(state)
        _atomic_write(state)
