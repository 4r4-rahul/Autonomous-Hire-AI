"""
Persistent all-time dedup ledger — never tailor (or apply to) the same role twice.

The in-run dedup in ats_intake only checks CURRENTLY-active applications, so a role that gets
re-discovered later under a new ID/URL (or after its app was skipped/rejected) would be tailored
AGAIN — wasting ~$0.085 of Opus each time. This ledger records every role ever tailored, keyed by
both a normalized (company, title) AND its ATS job id (gh_jid / lever-ashby uuid in the URL), and
survives across runs in data/tailored_ledger.json. Intake checks it BEFORE the expensive tailor
step and skips anything already seen.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

_FILE = Path("data/tailored_ledger.json")
_LOCK = Lock()


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _title_key(title: str) -> str:
    """Normalize a title for dedup CONSERVATIVELY — only drop trailing location/remote qualifiers,
    NEVER the team/specialization. 'Senior SWE, AI Studio (Remote)' → 'senior swe, ai studio', but
    'Senior SWE, AI Studio' and 'Senior SWE, Infrastructure' stay DISTINCT (they're different jobs).
    The primary dedup signal is the ATS job id (job_id_from_url); this is the secondary one."""
    t = _norm(title)
    t = re.sub(r"\s*[\(\[][^)\]]*[\)\]]\s*$", "", t)                 # drop a trailing (parenthetical)
    t = re.sub(r"\s*[-–—]\s*(remote|hybrid|onsite|us|usa|united states|new york|"
               r"san francisco|seattle|austin|boston|chicago|remote.*)$", "", t)  # drop trailing "- <location>"
    return re.sub(r"\s+", " ", t).strip()


def job_id_from_url(url: str) -> str | None:
    """Extract the stable ATS job id from an apply URL (gh_jid, /jobs/<id>, lever/ashby uuid)."""
    if not url:
        return None
    m = re.search(r"gh_jid=(\d+)", url) or re.search(r"/jobs/(\d+)", url) \
        or re.search(r"/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", url)
    return m.group(1) if m else None


def _key(company: str, title: str) -> str:
    return f"{_norm(company)}::{_title_key(title)}"


def _load() -> dict:
    try:
        return json.loads(_FILE.read_text())
    except Exception:
        return {"roles": {}, "ids": {}}


class Ledger:
    """In-memory view loaded once per run; check via .seen(), record via .add()."""

    def __init__(self):
        d = _load()
        self._roles: dict = d.get("roles", {})
        self._ids: dict = d.get("ids", {})

    def seen(self, company: str, title: str, url: str = "") -> bool:
        if _key(company, title) in self._roles:
            return True
        jid = job_id_from_url(url)
        return bool(jid and jid in self._ids)

    def add(self, company: str, title: str, url: str = "") -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._roles[_key(company, title)] = {"company": company, "title": title, "at": now}
        jid = job_id_from_url(url)
        if jid:
            self._ids[jid] = now

    def save(self) -> None:
        with _LOCK:
            try:
                _FILE.parent.mkdir(parents=True, exist_ok=True)
                _FILE.write_text(json.dumps({"roles": self._roles, "ids": self._ids}))
            except Exception:
                pass

    def __len__(self) -> int:
        return len(self._roles)


def reconcile_with_state(state: dict) -> int:
    """Rebuild the ledger to EXACTLY the roles present in the current state (queued/applied/skipped).
    Self-heals the ledger getting ahead of reality — e.g. roles recorded as 'seen' during a run
    whose apps were later lost to a clobber would otherwise block re-discovery forever. Returns size."""
    _FILE.unlink(missing_ok=True)
    return backfill_from_state(state)


def backfill_from_state(state: dict) -> int:
    """Seed the ledger from existing applications so we never re-tailor anything already in the
    queue/history. Returns the number of roles recorded."""
    led = Ledger()
    jl = {j.get("id"): j for j in state.get("job_listings", [])}
    n = 0
    for a in state.get("applications", []):
        co, ti = a.get("company_name"), a.get("role_title")
        url = (jl.get(a.get("job_id")) or {}).get("apply_url", "")
        if co and ti and not led.seen(co, ti, url):
            led.add(co, ti, url)
            n += 1
    led.save()
    return n
