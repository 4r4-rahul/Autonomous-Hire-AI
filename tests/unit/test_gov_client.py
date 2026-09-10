"""Gov/C2C classification — the filters that keep the public-sector contract lane safe."""
from core.gov_client import (detect_gov_client, is_federal_client, usc_gc_only,
                             visa_friendly, w2_only)


def test_detects_state_client_and_state_code():
    for text, state in (("Client: State of Texas HHSC, 24 month contract", "TX"),
                        ("NCDOT modernization project", "NC"),
                        ("PennDOT statewide contract", "PA"),
                        ("CalTrans District 4", "CA")):
        got = detect_gov_client("Data Engineer", text)
        assert got and got["level"] == "state", text
        assert got["state"] == state, (text, got)


def test_detects_local_client():
    got = detect_gov_client("ETL Developer", "Supporting the County of Fairfax, Virginia")
    assert got and got["level"] == "local" and got["state"] == "VA"


def test_commercial_roles_are_not_tagged_government():
    assert detect_gov_client("Backend Engineer", "We build payments infrastructure") is None
    # vendor boilerplate ("clients ranging from ... federal, state and local agencies") must not
    # promote a commercial req into the government lane
    assert detect_gov_client("Java Developer", "We have executed 100+ projects for clients "
                             "ranging from small business to federal, state and local agencies") is None


def test_federal_clients_are_flagged_not_tagged():
    assert is_federal_client("Supporting the Department of Homeland Security")
    assert detect_gov_client("Cloud Engineer", "Department of Defense program") is None
    assert not is_federal_client("State of Ohio Medicaid program")


def test_citizenship_bar_read_from_the_title():
    # C2C boards encode the bar as a compact title tag, and a SmartRecruiters listing has no body
    assert usc_gc_only("Sr. .Net Developer/Architect (USC/GC Only)", "")
    assert usc_gc_only("SharePoint Developer (USC/GC/TN/CTH)", "")
    assert usc_gc_only("DevOps Engineer - Green Card/US Citizen Only", "")
    assert usc_gc_only("Programmer Analyst", "Only US citizens and green card holders only")
    assert not usc_gc_only("Senior Java Developer", "12 month contract, C2C available")


def test_w2_only_is_not_a_citizenship_bar():
    # a vendor hiring on its own W2 can still transfer an H1B — this must never block a role
    assert w2_only("Java Developer (W2 Only)", "")
    assert not usc_gc_only("Java Developer (W2 Only)", "")
    assert not usc_gc_only("Data Engineer", "No C2C, W2 candidates only")


def test_visa_friendly_language():
    assert visa_friendly("Java Developer", "H1B transfer welcome, all visas accepted")
    assert not visa_friendly("Java Developer", "US Citizens only")
