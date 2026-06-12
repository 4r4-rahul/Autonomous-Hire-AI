"""
Data-integrity core tests — the load-bearing paths that lose data or money when wrong.

Covers: state_store (atomic write, dedup, corrupt detection, locked round-trip), budget_guard
(daily cap accounting + reset), sponsorship word-boundary matching, posting freshness, and the
dedup ledger title normalization. These had ZERO coverage and are exactly where the system has
hemorrhaged correctness this cycle.
"""
import importlib
import json
from datetime import datetime, timezone, timedelta

import pytest
from filelock import FileLock


# ── state_store ──────────────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path):
    import core.state_store as ss
    ss._STATE = tmp_path / "state.json"
    ss._lock = FileLock(str(tmp_path / "state.lock"), timeout=10)
    return ss


def test_missing_file_returns_fresh(store):
    s = store.load_state()
    assert s["job_listings"] == [] and s["applications"] == []


def test_corrupt_file_raises(store):
    store._STATE.write_text("{ not json")
    with pytest.raises(store.StateCorruptError):
        store.load_state()
    store._STATE.write_text("   ")
    with pytest.raises(store.StateCorruptError):
        store.load_state()


def test_save_is_atomic_no_tmp_left(store):
    store.save_state({"job_listings": [], "applications": []})
    assert store._STATE.exists()
    assert not store._STATE.with_suffix(".json.tmp").exists()


def test_dedup_collapses_duplicate_ids_keeping_canonical(store):
    s = {"job_listings": [
            {"id": "a", "apply_url": "https://www.x.com/jobs/apply/1?gh_jid=1"},   # branded
            {"id": "a", "apply_url": "https://job-boards.greenhouse.io/x/jobs/1"}],  # canonical
         "applications": [{"id": "app1", "job_id": "a", "status": "pending_review"}]}
    store.save_state(s)
    r = store.load_state()
    assert len(r["job_listings"]) == 1
    assert "greenhouse.io" in r["job_listings"][0]["apply_url"]   # kept the canonical variant


def test_dedup_drops_orphan_apps(store):
    s = {"job_listings": [{"id": "a", "apply_url": "u1"}],
         "applications": [{"id": "app1", "job_id": "a", "status": "pending_review"},
                          {"id": "app2", "job_id": "ghost", "status": "pending_review"}]}
    store.save_state(s)
    r = store.load_state()
    assert {a["job_id"] for a in r["applications"]} == {"a"}   # orphan app2 dropped


def test_dedup_keeps_best_status_per_job(store):
    s = {"job_listings": [{"id": "a", "apply_url": "u1"}],
         "applications": [{"id": "x", "job_id": "a", "status": "skipped"},
                          {"id": "y", "job_id": "a", "status": "submitted"}]}
    store.save_state(s)
    r = store.load_state()
    assert len(r["applications"]) == 1 and r["applications"][0]["status"] == "submitted"


def test_save_merges_concurrent_appends(store):
    # Simulate the lost-update race: process A saves an initial state; process B appends a new app
    # to disk; process A (with a STALE snapshot) saves again — B's app must survive.
    store.save_state({"job_listings": [{"id": "a", "apply_url": "u1"}],
                      "applications": [{"id": "app1", "job_id": "a", "status": "pending_review"}]})
    stale = store.load_state()
    # concurrent writer adds a new job+app directly to disk
    store.merge_new([{"id": "b", "apply_url": "u2"}],
                    [{"id": "app2", "job_id": "b", "status": "pending_review"}])
    # stale process saves its old snapshot (missing app2) — union-merge must keep app2
    store.save_state(stale)
    ids = {a["id"] for a in store.load_state()["applications"]}
    assert ids == {"app1", "app2"}, f"lost-update! got {ids}"


def test_save_keeps_more_progressed_status(store):
    store.save_state({"job_listings": [{"id": "a", "apply_url": "u1"}],
                      "applications": [{"id": "app1", "job_id": "a", "status": "pending_review"}]})
    # disk advances to submitted; a stale caller still thinks pending_review
    store.save_state({"job_listings": [{"id": "a", "apply_url": "u1"}],
                      "applications": [{"id": "app1", "job_id": "a", "status": "submitted"}]})
    stale = {"job_listings": [{"id": "a", "apply_url": "u1"}],
             "applications": [{"id": "app1", "job_id": "a", "status": "pending_review"}]}
    store.save_state(stale)
    assert store.load_state()["applications"][0]["status"] == "submitted"   # not reverted


def test_mutate_state_round_trip(store):
    store.save_state({"job_listings": [{"id": "a", "apply_url": "u"}],
                      "applications": [{"id": "x", "job_id": "a", "status": "pending_review"}]})
    with store.mutate_state() as s:
        s["applications"][0]["status"] = "submitted"
    assert store.load_state()["applications"][0]["status"] == "submitted"


# ── budget_guard ─────────────────────────────────────────────────────────────

@pytest.fixture
def budget(tmp_path, monkeypatch):
    import core.budget_guard as bg
    bg._FILE = tmp_path / "spend.json"
    monkeypatch.setenv("AUTONOMOUSHIRE_DAILY_USD_CAP", "4")
    return bg


def test_budget_accumulates_and_caps(budget):
    assert budget.spent_today() == 0
    budget.record(1.5)
    budget.record(1.0)
    assert abs(budget.spent_today() - 2.5) < 1e-6
    assert not budget.over_budget()
    budget.record(2.0)                      # -> 4.5 >= cap 4
    assert budget.over_budget()
    assert budget.remaining() == 0


def test_budget_isolated_by_day(budget):
    from datetime import date
    budget.record(3.9)
    d = json.loads(budget._FILE.read_text())
    # backdate today's key to yesterday → today reads 0 again. Use the SAME clock the code uses
    # (budget._today() -> local date.today()); computing 'yesterday' off UTC flakes near midnight
    # UTC, when local date and UTC date differ and the backdated key collides with today's.
    y = str(date.today() - timedelta(days=1))
    budget._FILE.write_text(json.dumps({y: 3.9}))
    assert budget.spent_today() == 0 and not budget.over_budget()


# ── sponsorship word-boundary matching ───────────────────────────────────────

def test_sponsorship_no_substring_false_positives():
    from agents.discovery.targeting import _word_match
    assert not _word_match("citi", "velocity global")
    assert not _word_match("box", "dropbox")
    assert not _word_match("block", "blockdaemon")
    assert _word_match("box", "box inc")
    assert _word_match("meta", "meta platforms")
    assert _word_match("stripe", "stripe")


# ── posting freshness ────────────────────────────────────────────────────────

def test_posting_age_days():
    from agents.discovery.apply_url_resolver import posting_age_days
    assert posting_age_days("") is None
    assert posting_age_days("garbage") is None
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    assert posting_age_days(recent) < 3
    assert posting_age_days(old) > 39


# ── dedup ledger title normalization (must keep distinct roles distinct) ──────

def test_ledger_title_key_distinguishes_real_roles():
    from agents.application.dedup_ledger import _title_key
    assert _title_key("Senior SWE, AI Studio") != _title_key("Senior SWE, Infrastructure")
    assert _title_key("Data Engineer") != _title_key("Data Engineer, People Analytics")
    # only location variants collapse
    assert _title_key("Senior SWE, AI Studio (Remote)") == _title_key("Senior SWE, AI Studio")


def test_prune_tripwire():
    from db.migrate_from_json import _should_skip_prune
    # normal sync — no drop → prune allowed
    assert not _should_skip_prune(5000, 5000, 100, 100)
    # job count collapsed (truncated state) → skip
    assert _should_skip_prune(5000, 10, 100, 100)
    # apps collapsed but jobs intact (the queue-wipe scenario) → skip
    assert _should_skip_prune(5000, 5000, 100, 5)
    # tiny DB (below thresholds) → don't trip on normal early-state churn
    assert not _should_skip_prune(10, 1, 5, 1)


def test_lead_age_out_uses_discovered_at_only():
    from core.state_store import _lead_is_stale, _MAX_LEAD_AGE_DAYS
    old = (datetime.now(timezone.utc) - timedelta(days=_MAX_LEAD_AGE_DAYS + 10)).isoformat()
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    # app-backed job is NEVER stale
    assert not _lead_is_stale({"id": "a", "discovered_at": old}, {"a"})
    # freshly discovered but OLD posting must NOT be aged out (posted_at ignored)
    assert not _lead_is_stale({"id": "b", "discovered_at": recent, "posted_at": old}, set())
    # genuinely old discovery, no app → stale
    assert _lead_is_stale({"id": "c", "discovered_at": old}, set())
    # unknown discovery time → keep
    assert not _lead_is_stale({"id": "d", "posted_at": old}, set())


def test_newer_iso_picks_most_recent():
    from agents.discovery.apply_url_resolver import _newer_iso
    assert _newer_iso("2026-03-01T00:00:00Z", "2026-05-01T00:00:00Z").startswith("2026-05-01")
    assert _newer_iso("", "2026-05-01T00:00:00Z").startswith("2026-05-01")
    assert _newer_iso("", "", None) == ""


def test_ats_coverage():
    from agents.application.resume_tailor import _ats_coverage
    md = "Senior Data Engineer — Python, AWS, Spark, Airflow"
    assert _ats_coverage(md, ["python", "aws", "spark"]) == 1.0
    assert _ats_coverage(md, ["python", "kubernetes"]) == 0.5
    assert _ats_coverage(md, []) == 1.0


def test_ledger_job_id_extraction():
    from agents.application.dedup_ledger import job_id_from_url
    assert job_id_from_url("https://job-boards.greenhouse.io/x/jobs/7964335") == "7964335"
    assert job_id_from_url("https://x.com/apply?gh_jid=12345") == "12345"
    assert job_id_from_url("https://jobs.lever.co/x/abcdef01-2345-6789-abcd-ef0123456789") \
        == "abcdef01-2345-6789-abcd-ef0123456789"


def test_loc_class_foreign_cities_not_unknown():
    """Regression: foreign CITY names (no country word) must classify 'foreign', not leak as
    'unknown' (kept by the US-only filter). 'Gurugram' once slipped a real India role into the
    queue because the list had only the old 'gurgaon' spelling and was missing most India hubs."""
    from agents.discovery.apply_url_resolver import _loc_class
    for foreign in ("Gurugram", "Mumbai", "Chennai", "Noida", "New Delhi", "Bengaluru",
                    "Kolkata", "Frankfurt", "Bangkok", "Taipei", "Ottawa"):
        assert _loc_class(foreign) == "foreign", f"{foreign} should be foreign"
    # US signal still wins, and real US hubs/states stay US
    for us in ("Austin, TX", "New York", "San Francisco", "Dallas, Texas", "Remote - US"):
        assert _loc_class(us) == "us", f"{us} should be us"
    assert _loc_class("") == "unknown"
