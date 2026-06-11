"""
Observability — system health checks + a daily digest, so unattended running never
silently breaks (the API-credit outage that quietly turned every email into "other"
is exactly what this catches) and the user stays informed without doing the work.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from core.logger import get_logger

logger = get_logger("health")

_ROOT = Path(__file__).parent.parent.parent
_STATE = _ROOT / "data" / "pipeline_state.json"
_APPLIED = _ROOT / "data" / "applied_jobs.json"
_KILL = _ROOT / "data" / "auto_apply.disabled"
_DIGEST_MD = _ROOT / "data" / "daily_digest.md"
_DIGEST_JSON = _ROOT / "data" / "daily_digest.json"


def _load_state() -> dict:
    try:
        return json.loads(_STATE.read_text())
    except Exception:
        return {}


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


# ── Health checks ────────────────────────────────────────────────────────────
def _check(name: str, fn) -> dict:
    try:
        ok, detail = fn()
        return {"name": name, "status": "ok" if ok else "fail", "detail": detail}
    except Exception as e:
        return {"name": name, "status": "fail", "detail": str(e)[:140]}


def system_health(probe_llm: bool = True) -> dict:
    """Run all health checks. Returns {ok, checks:[...], generated_at}."""
    checks = []

    def _llm():
        if not probe_llm:
            return True, "skipped (set probe_llm=True to check credits)"
        import asyncio
        from core.llm import get_llm
        llm = get_llm("health", model="claude-haiku-4-5")
        # Cheapest possible ping; a credit/auth failure raises (e.g. "credit balance too low").
        asyncio.run(llm.chat("Reply with: ok", [{"role": "user", "content": "ok"}],
                             max_tokens=5, retries=1))
        return True, "Anthropic API reachable (credits OK)"

    def _gmail():
        try:
            from agents.monitoring.email_monitor import _build_gmail_service
            prof = _build_gmail_service().users().getProfile(userId="me").execute()
            return True, f"Gmail read OK ({prof.get('emailAddress','?')})"
        except Exception as e:
            # Self-documenting failure: callback detection is blind until re-auth. Tell the user the
            # exact fix so a stale token never silently swallows interview replies again.
            return False, ("Gmail token DOWN — callback detection blind. "
                           "Fix: python scripts/gmail_reauth.py  "
                           f"({str(e)[:50]})")

    def _linkedin():
        from agents.monitoring.linkedin_health import is_auto_paused
        paused = is_auto_paused()
        return (not paused), ("auto-paused (CAPTCHA/restriction detected)" if paused
                              else "active (not auto-paused)")

    def _store():
        st = _load_state()
        pend = sum(1 for a in st.get("applications", []) if a.get("status") == "pending_review")
        try:
            # SYNC session — system_health is often called from inside a running event loop
            # (the scheduler), where asyncio.run() would raise. The sync engine has no such issue.
            from sqlalchemy import select, func
            from db.session import SyncSessionLocal
            from db.models import Application, ApplicationStatus
            with SyncSessionLocal() as s:
                db_pend = s.execute(
                    select(func.count()).select_from(Application)
                    .where(Application.status == ApplicationStatus.pending_approval)).scalar()
        except Exception as e:
            return False, f"DB unreachable: {str(e)[:60]}"
        # Tolerate small legitimate drift — the sync collapses edge cases (a pending role whose URL
        # collides with an already-decided app is correctly kept as decided). Flag only SIGNIFICANT
        # desync (a wipe or major divergence), which is the failure mode that actually matters.
        drift = abs(pend - db_pend)
        consistent = drift <= max(3, int(pend * 0.05))
        return consistent, (f"parking lot: state={pend} db={db_pend}"
                            + ("" if consistent else " (OUT OF SYNC — run db.migrate_from_json)"))

    def _freshness():
        # Heartbeat: the scheduler touches data/scheduler_heartbeat every health-check tick (30 min)
        # whether or not a job saves state — a true "scheduler alive" signal. Fall back to the
        # state file mtime / last_run_at so this works even before the first heartbeat.
        candidates = []
        hb = Path("data/scheduler_heartbeat")
        if hb.exists():
            candidates.append(datetime.fromtimestamp(hb.stat().st_mtime, tz=timezone.utc))
        sf = Path("data/pipeline_state.json")
        if sf.exists():
            candidates.append(datetime.fromtimestamp(sf.stat().st_mtime, tz=timezone.utc))
        last = (_load_state() or {}).get("last_run_at")
        if last:
            try:
                candidates.append(datetime.fromisoformat(str(last).replace("Z", "+00:00")))
            except Exception:
                pass
        if not candidates:
            return True, "no activity timestamp yet"
        newest = max(candidates)
        age_h = (datetime.now(timezone.utc) - newest).total_seconds() / 3600
        return (age_h < 48), f"last scheduler activity {age_h:.1f}h ago"

    checks.append(_check("anthropic_api", _llm))
    checks.append(_check("gmail", _gmail))
    checks.append(_check("linkedin", _linkedin))
    checks.append(_check("datastore_sync", _store))
    checks.append(_check("pipeline_freshness", _freshness))

    auto_on = os.getenv("AUTONOMOUSHIRE_AUTO_SUBMIT", "0") == "1"
    checks.append({"name": "auto_submit", "status": "ok",
                   "detail": ("ENABLED" if (auto_on and not _KILL.exists())
                              else "kill-switch ON" if _KILL.exists()
                              else "off (opt-in: AUTONOMOUSHIRE_AUTO_SUBMIT=1)")})

    overall_ok = all(c["status"] == "ok" for c in checks if c["name"] != "auto_submit")
    return {"ok": overall_ok, "checks": checks, "generated_at": datetime.now(timezone.utc).isoformat()}


# ── Daily digest ─────────────────────────────────────────────────────────────
def build_digest(write: bool = True) -> dict:
    """Summarize the pipeline + today's activity into a digest (dict + markdown report)."""
    st = _load_state()
    apps = st.get("applications", [])
    today = _today()
    from collections import Counter
    by_status = dict(Counter(a.get("status") for a in apps))

    def _today_count(field):
        return sum(1 for a in apps if (a.get(field) or "").startswith(today))

    _awaiting = [a for a in apps if a.get("status") == "awaiting_confirmation"]
    awaiting_page = sum(1 for a in _awaiting if a.get("submit_confidence") == "page")
    awaiting_unconfirmed = sum(1 for a in _awaiting if a.get("submit_confidence") == "click_only")

    events = st.get("email_events", [])
    ev_today = [e for e in events if (e.get("received_at") or "")[:10] == today]
    ev_counts = Counter(e.get("category") for e in ev_today)

    health = system_health(probe_llm=False)  # cheap for the digest

    # Referral opportunities — for the freshest queued roles, surface the LinkedIn connections
    # search so the user can pursue a warm intro (the single biggest callback lever). Zero LLM.
    referrals = []
    try:
        from agents.outreach.referral import search_urls
        jl = {j.get("id"): j for j in st.get("job_listings", [])}
        fresh = [a for a in apps if a.get("status") == "pending_review"]
        fresh.sort(key=lambda a: (jl.get(a.get("job_id")) or {}).get("posted_at") or "", reverse=True)
        for a in fresh[:5]:
            co, role = a.get("company_name") or "", a.get("role_title") or ""
            if co:
                referrals.append({"company": co, "role": role,
                                  "connections_url": search_urls(co, role)["your_connections"]})
    except Exception as e:
        logger.warning("digest_referrals_failed", error=str(e)[:80])

    digest = {
        "date": today,
        "referrals": referrals,
        "pipeline": {
            "queued_for_review": by_status.get("pending_review", 0),
            "awaiting_confirmation": by_status.get("awaiting_confirmation", 0),
            "awaiting_page_confirmed": awaiting_page,
            "awaiting_unconfirmed": awaiting_unconfirmed,
            "submitted": by_status.get("submitted", 0),
            "interview": by_status.get("interview", 0),
            "offer": by_status.get("offer", 0),
            "rejected": by_status.get("rejected", 0),
            "needs_manual": by_status.get("pending_manual_submit", 0),
            "blocked": by_status.get("blocked", 0),
        },
        "today": {
            "sent": _today_count("sent_at"),
            "confirmed": _today_count("submitted_at"),
            "email_events": dict(ev_counts),
        },
        "response_rate": st.get("response_rate", 0.0),
        "interview_rate": st.get("interview_rate", 0.0),
        "health": {c["name"]: c["status"] for c in health["checks"]},
        "health_ok": health["ok"],
    }
    if write:
        try:
            _DIGEST_JSON.write_text(json.dumps(digest, indent=2))
            _DIGEST_MD.write_text(_render_digest(digest, health))
        except Exception as e:
            logger.warning("digest_write_failed", error=str(e)[:80])
    return digest


def _render_digest(d: dict, health: dict) -> str:
    p = d["pipeline"]
    L = [f"# AutonomousHire — Daily Digest ({d['date']})", ""]
    # Callback detection (Gmail) — always shown, because a blind inbox silently hides interview
    # replies. When down, the line carries the one-command fix.
    _gm = next((c for c in health["checks"] if c["name"] == "gmail"), None)
    if _gm is not None:
        L += [(f"Callback detection (Gmail): 🟢 live"
               if _gm["status"] == "ok"
               else "Callback detection (Gmail): 🔴 DOWN — run `python scripts/gmail_reauth.py`"), ""]
    warn = [c for c in health["checks"] if c["status"] != "ok"]
    if warn:
        L += ["## ⚠️ Needs attention"]
        L += [f"- **{c['name']}**: {c['detail']}" for c in warn]
        L += [""]
    L += [
        "## Pipeline",
        f"- Queued for your review: **{p['queued_for_review']}**",
        f"- Sent today / awaiting confirmation: **{d['today']['sent']}** / {p['awaiting_confirmation']}",
        f"    · page-confirmed (sent, waiting on email): {p.get('awaiting_page_confirmed', 0)}  ·  "
        f"unconfirmed (will re-check): {p.get('awaiting_unconfirmed', 0)}",
        f"- Submitted (email-confirmed = applied): **{p['submitted']}**",
        f"- Interviews: **{p['interview']}**  ·  Offers: **{p['offer']}**  ·  Rejections: {p['rejected']}",
        f"- Needs manual submit: {p['needs_manual']}  ·  Blocked: {p['blocked']}",
        "",
        f"Response rate: {d['response_rate']:.0%}  ·  Interview rate: {d['interview_rate']:.0%}",
        "",
    ]
    refs = d.get("referrals") or []
    if refs:
        L += ["## 🤝 Referral opportunities (biggest callback lever — ask before applying cold)"]
        L += [f"- **{r['company']}** — {r['role']}: [find your connections]({r['connections_url']})"
              for r in refs]
        L += [""]
    L += [
        "## Today's email events",
    ]
    L += ([f"- {k}: {v}" for k, v in d["today"]["email_events"].items()] or ["- (none)"])
    L += ["", "## Health", *[f"- {c['name']}: {c['status']} — {c['detail']}" for c in health["checks"]]]
    return "\n".join(L)
