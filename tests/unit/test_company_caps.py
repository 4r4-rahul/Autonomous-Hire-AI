"""Per-company rolling-window application caps (core.company_caps).

Respects employer application-limit policies (e.g. Roblox auto-closes >5 in 30 days). The key
invariant that broke before: a SUBMITTED app must still count toward the window cap — counting only
pending let us submit 6× to Roblox.
"""
from datetime import datetime, timezone, timedelta

from core.company_caps import (
    cap_for, is_exempt, window_days, count_against_cap,
)

NOW = datetime(2026, 6, 18, tzinfo=timezone.utc)


def _iso(days_ago):
    return (NOW - timedelta(days=days_ago)).isoformat()


def test_default_and_override_and_exempt():
    # POLICY: no cap by default — only proven-limit companies are capped.
    assert cap_for("some-random-co") == float("inf")
    assert cap_for("databricks") == float("inf")   # no known limit → unlimited
    assert cap_for("roblox") == 4                   # override (their hard limit is 5; margin of 1)
    assert is_exempt("kforce") and cap_for("kforce") == float("inf")
    assert not is_exempt("roblox")
    assert window_days() == 30


def test_pending_always_counts():
    for status in ("pending_review", "pending_manual_submit", "awaiting_confirmation"):
        assert count_against_cap({"status": status}, NOW) is True


def test_submitted_counts_only_within_window():
    # within 30d → counts (this is the bug that auto-closed Roblox)
    assert count_against_cap({"status": "submitted", "submitted_at": _iso(10)}, NOW) is True
    # older than 30d → aged out, does not count
    assert count_against_cap({"status": "submitted", "submitted_at": _iso(45)}, NOW) is False


def test_terminal_statuses_dont_count():
    assert count_against_cap({"status": "skipped"}, NOW) is False
    assert count_against_cap({"status": "rejected"}, NOW) is False


def test_unknown_date_counts_conservatively():
    # No usable timestamp on a submitted app → count it (never under-count toward the cap).
    assert count_against_cap({"status": "submitted"}, NOW) is True


def test_email_cap_detector():
    from agents.monitoring.email_monitor import _detect_application_limit, _company_from_sender
    roblox = ("we limit candidates to a maximum of five applications within any 30-day period. "
              "Because we received more than five applications from you, this application was "
              "automatically closed under our application limit policy.")
    assert _detect_application_limit("Your application", roblox) == 5
    assert _detect_application_limit("x", "maximum of 3 applications per month") == 3
    assert _detect_application_limit("Interview", "We'd love to chat about the role") is None
    assert _company_from_sender("no-reply@roblox.com") == "roblox"
    assert _company_from_sender("jobs@greenhouse.io") is None   # ATS domain, not the company
