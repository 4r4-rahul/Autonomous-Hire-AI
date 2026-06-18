"""Years-of-experience ceiling — drop roles requiring more years than the candidate has.

Rahul has ~6-7 yrs; Staff/Principal reqs of 8/10/12+ are a poor fit and waste an application.
The extractor must read the HARD minimum (ignoring 'preferred'/'a plus') without a later preferred
clause downgrading an earlier required mention, and without false-positives on unrelated numbers.
"""
import pytest

from agents.discovery.fit_analyst import (
    _min_required_years, _MAX_REQUIRED_YEARS, _quick_prefilter,
)


@pytest.mark.parametrize("desc,expected", [
    ("We require 8+ years of experience in software", 8),
    ("Minimum of 10 years of professional experience", 10),
    ("5+ years experience; 10+ years preferred", 5),          # preferred clause ignored
    ("3-5 years of experience required", 3),
    ("12+ years experience building data platforms", 12),
    ("10+ years required. Prior startup experience preferred.", 10),  # later 'preferred' ≠ this one
    ("10+ years experience is a plus", None),                 # whole mention is optional
    ("Bachelor's degree and 2 years experience", 2),
    ("Strong Python background, no specific tenure", None),   # no number
    ("Founded 10 years ago, fast-growing team", None),        # not an experience requirement
])
def test_min_required_years(desc, expected):
    assert _min_required_years(desc) == expected


def test_prefilter_drops_high_experience():
    job = {"title": "Principal Software Engineer", "company_name": "acme",
           "description": "We need 10+ years of experience in distributed systems.",
           "salary_max": None}
    ok, reason = _quick_prefilter(job, {"target": {}})
    assert not ok and "yrs experience" in reason


def test_prefilter_keeps_in_range_experience():
    job = {"title": "Senior Software Engineer, Data Platform", "company_name": "acme",
           "description": "5+ years of experience with Python, AWS, and data pipelines.",
           "salary_max": None}
    ok, _ = _quick_prefilter(job, {"target": {}})
    assert ok


def test_ceiling_is_seven():
    assert _MAX_REQUIRED_YEARS == 7
