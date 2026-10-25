"""US-only intake gate. Signal strength is positional: title/location decide, JD text is context.

Both directions were live bugs, so both are pinned here:
  * bare "remote" counted as a US signal, so "Data Engineer ---- CANADA" @ "Remote" was QUEUED;
  * flattening the JD into the same blob let "offices in London" reject real US roles.
"""
import pytest

from agents.application.ats_intake import _is_relevant_location as R


@pytest.mark.parametrize("title,location,context", [
    ("Data Engineer ---- CANADA", "Remote", "Work Arrangement: 100% Remote Canada"),
    ("Senior Data Engineer", "Toronto, Ontario", "remote"),
    ("Data Engineer", "Bangalore, India", "100% remote"),
    ("AI Deployment Strategist - UK", "London", "join our team"),
    # Foreign named as the WORKSITE in the JD, with nothing US up front.
    ("Data Engineer", "Remote", "This is a 100% Remote Canada position"),
    ("Data Engineer", "Remote", "candidates must be based in India"),
    ("Data Engineer", "Remote", "Canada only - no exceptions"),
])
def test_rejects_foreign(title, location, context):
    assert R(title, location, context) is False


@pytest.mark.parametrize("title,location,context", [
    # US cities absent from the short _US list, whose JD mentions a foreign office in passing.
    ("Senior Software Engineer - Reliability", "Miami Remote", "offices in London and Miami"),
    ("Staff Software Engineer, Data", "Palo Alto Remote", "our Bangalore team collaborates"),
    ("Data Engineer", "Austin, TX", ""),
    ("Data Engineer", "US Remote", "remote role"),
    ("Data Engineer", "Remote (US)", "we also have a Toronto office"),
    ("Data Engineer", "Remote", "no country mentioned at all"),
    ("Data Engineer", "Dallas, Texas", "offices in London and Dallas"),
    ("Data Engineer", "Charlotte, NC", "team spread across Europe and the US"),
])
def test_keeps_us(title, location, context):
    assert R(title, location, context) is True
