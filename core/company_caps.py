"""Per-company application caps — respect employer application-limit policies.

Some employers auto-close applicants who exceed a rolling-window limit (Roblox: 5 per 30 days), and
many ATSes (Greenhouse/Workday) dedup or flag prolific applicants. We therefore cap how many
DISTINCT roles we apply to at one company within a rolling window. Reach comes from breadth (many
companies), not depth at one employer.

Config: data/company_caps.json — { default_max, window_days, exempt[], overrides{} }.
The cap counts PENDING + SUBMITTED-within-window apps for a company (see count_against_cap).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

_FILE = Path(__file__).parent.parent / "data" / "company_caps.json"

# Fallbacks if the config is missing/corrupt (never crash intake on a bad file). default_max=None
# means NO CAP — we only limit companies with a proven policy (manual override or email-learned).
_FALLBACK = {"default_max": None, "window_days": 30, "exempt": [], "overrides": {}, "learned": {}}

_PENDING_STATUSES = ("pending_review", "pending_manual_submit", "awaiting_confirmation")


def _norm(name: str) -> str:
    return (name or "").strip().lower()


def _load() -> dict:
    try:
        cfg = json.loads(_FILE.read_text())
        return {**_FALLBACK, **cfg}
    except Exception:
        return dict(_FALLBACK)


def window_days() -> int:
    return int(_load().get("window_days", 30))


def is_exempt(company: str) -> bool:
    return _norm(company) in {_norm(c) for c in _load().get("exempt", [])}


def cap_for(company: str) -> float:
    """Max distinct apps allowed at this company within the window. Unlimited (inf) for exempt
    staffing agencies and for any company with no proven limit (default_max=null). A company is
    capped only via a manual `overrides` entry or an email-`learned` one."""
    cfg = _load()
    if is_exempt(company):
        return float("inf")
    # learned (auto-detected from emails) + manual overrides; manual wins on conflict.
    caps = {_norm(k): v for k, v in {**cfg.get("learned", {}), **cfg.get("overrides", {})}.items()}
    val = caps.get(_norm(company), cfg.get("default_max"))
    return float("inf") if val is None else float(val)


def set_cap(company: str, max_apps: int, *, source: str = "email", note: str = "") -> None:
    """Record a proven per-company limit (e.g. auto-learned from a rejection email). Writes to the
    `learned` map so it survives restarts and is visible/auditable. Manual `overrides` still win."""
    company = _norm(company)
    cfg = _load()
    learned = cfg.get("learned", {}) or {}
    prev = learned.get(company)
    new = int(max_apps) if prev is None else min(int(prev), int(max_apps))  # tightest wins
    learned[company] = new
    cfg["learned"] = learned
    notes = cfg.get("_learned_notes", {}) or {}
    notes[company] = f"{source}: max {max_apps}{(' — ' + note) if note else ''}"
    cfg["_learned_notes"] = notes
    try:
        _FILE.write_text(json.dumps(cfg, indent=2))
    except Exception:
        pass


def _within_window(app: dict, now: datetime, win: timedelta) -> bool:
    for k in ("submitted_at", "updated_at", "created_at"):
        ts = app.get(k)
        if ts:
            try:
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return (now - dt) <= win
            except Exception:
                continue
    return True  # unknown date → count it (conservative, never under-count)


def count_against_cap(app: dict, now: datetime | None = None, win_days: int | None = None) -> bool:
    """Does this application count toward its company's rolling-window cap?
    PENDING apps always count (they're a future submission); SUBMITTED apps count only if within the
    window (older submissions age out, matching 'N per any 30-day period' policies)."""
    status = app.get("status")
    if status in _PENDING_STATUSES:
        return True
    if status == "submitted":
        now = now or datetime.now(timezone.utc)
        win = timedelta(days=win_days if win_days is not None else window_days())
        return _within_window(app, now, win)
    return False
