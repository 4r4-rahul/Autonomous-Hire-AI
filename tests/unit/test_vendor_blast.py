"""Bench-marketing email composition — the constraints that protect long-lived vendor relationships."""
from agents.outreach.vendor_blast import DAILY_CAP, _first_name, compose

_PROFILE = {"personal": {"name": "Jane Doe", "email": "jane.doe@example.com",
                         "phone": "+1 555-010-0100", "location": "Plano, TX",
                         "linkedin_url": "https://linkedin.com/in/x"}}
_KW = {"work_auth": "H-1B. Open to W2 or C2C.", "availability": "2 weeks' notice",
       "resume_name": "resume.pdf"}


def test_never_claims_government_experience():
    """He has none. The public-sector angle is transferable regulated-industry work, and a claim
    of state/agency experience would be caught and would end the relationship."""
    _, body = compose({"email": "a@b.com", "states": ["TX"], "gov_reqs": 3}, _PROFILE, **_KW)
    lowered = body.lower()
    for claim in ("state government experience", "worked for the state",
                  "public sector experience", "state agency experience", "medicaid experience"):
        assert claim not in lowered


def test_work_auth_and_availability_are_verbatim():
    """These two lines are what a C2C recruiter filters on — they must appear exactly as given,
    never paraphrased into something the candidate did not approve."""
    _, body = compose({"email": "a@b.com", "states": [], "gov_reqs": 1}, _PROFILE, **_KW)
    assert _KW["work_auth"] in body
    assert _KW["availability"] in body


def test_opt_out_line_is_always_present():
    _, body = compose({"email": "a@b.com", "states": [], "gov_reqs": 1}, _PROFILE, **_KW)
    assert "remove" in body.lower()


def test_states_personalize_only_when_known():
    _, with_states = compose({"email": "a@b.com", "states": ["MI", "PA"], "gov_reqs": 2},
                             _PROFILE, **_KW)
    assert "MI, PA" in with_states
    _, without = compose({"email": "a@b.com", "states": [], "gov_reqs": 1}, _PROFILE, **_KW)
    # no empty "I've seen  public-sector requirements" stub when we don't know the state
    assert "public-sector requirements come from you" not in without


def test_junk_recruiter_names_fall_back_to_generic_greeting():
    # "Hi Staffing," reads worse than "Hi there,"
    assert _first_name({"name": "US IT Recruiter"}) == "there"
    assert _first_name({"name": ""}) == "there"
    assert _first_name({"name": "Priya Sharma"}) == "Priya"


def test_daily_cap_holds_a_reserve_below_gmails_limit():
    # a 520-send blast once consumed the whole quota and blocked real replies for 24h
    assert DAILY_CAP <= 400
