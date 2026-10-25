"""
Regression coverage for the extension autofill resolver (api/autofill.py).

Locks in the field→answer behavior across every category + the substring-collision traps
that have bitten us repeatedly (unit↔UNITed, city↔ethniCITY, source↔open source,
gender↔transgender, country↔country-of-citizenship). If a future edit breaks one of these,
the form-fill silently regresses — these tests catch it.
"""
import pytest

from api.autofill import _resolve_one, _load_profile, FieldIn

P = _load_profile()
_PER = P.get("personal", {})
_FIRST, _LAST = (_PER.get("name", "First Last").split() + ["", ""])[:2]
_PHONE_DIGITS = "".join(ch for ch in str(_PER.get("phone", "")) if ch.isdigit())[-10:-7] or "555"
_ADDR = P.get("address", {}) if isinstance(P.get("address"), dict) else {}
_STREET = str(_ADDR.get("street") or _PER.get("street") or "").split(" ")[-1] or ""
_CITY = str(_ADDR.get("city") or _PER.get("city") or _PER.get("location", "").split(",")[0] or "")
_ZIP = str(_ADDR.get("zip") or _PER.get("zip") or "")


def resolve(label, type="text", options=None):
    return _resolve_one(FieldIn(id="x", label=label, type=type, options=options or []), P)


# (label, type, options, expected_kind, expected_value_substring_or_None)
CASES = [
    # ── identity / contact ──
    ("First Name", "text", None, "text", _FIRST),
    ("Last Name", "text", None, "text", _LAST),
    ("Full Name", "text", None, "text", f"{_FIRST} {_LAST}".strip()),
    ("Email", "email", None, "text", "@"),
    ("Phone", "tel", None, "text", _PHONE_DIGITS),
    ("LinkedIn URL", "url", None, "text", "linkedin.com"),
    ("GitHub", "url", None, "text", "github.com"),
    # ── address + collision traps ──
    ("Street Address", "text", None, "text", _STREET),
    ("Address Line 2", "text", None, "text", ""),
    ("City", "text", None, "text", _CITY),
    ("State", "select", ["Texas", "California"], "value", "Texas"),
    ("Zip", "text", None, "text", _ZIP),
    ("Country", "select", ["United States", "India"], "value", "United States"),
    # TRAPS — must NOT answer United States:
    ("Country of Citizenship", "select", ["United States", "India"], "value", "India"),
    ("Country of Birth", "select", ["United States", "India"], "value", "India"),
    ("What is your nationality?", "text", None, "text", "Indian"),
    # ── work authorization / sponsorship ──
    ("Are you legally authorized to work in the US?", "radio", ["Yes", "No"], "value", "Yes"),
    ("Will you now or in the future require sponsorship?", "radio", ["Yes", "No"], "value", "Yes"),
    ("Are you authorized to work without sponsorship?", "radio", ["Yes", "No"], "value", "No"),
    ("Are you a US citizen or permanent resident?", "radio", ["Yes", "No"], "value", "No"),
    # ── application specifics (incl hyphen variant) ──
    ("Ideal start-date?", "text", None, "text", "Immediately"),
    ("When can you start?", "text", None, "text", "Immediately"),
    ("How did you hear about us?", "select", ["LinkedIn", "Indeed"], "value", "LinkedIn"),
    ("Current Company", "text", None, "text", "JPMorgan"),
    ("Pronouns", "select", ["He/Him", "She/Her"], "value", "He/Him"),
    # ── EEO (truthful) — verbose Ashby phrasings ──
    ("Are you a veteran or active member of the United States Armed Forces?", "radio",
     ["I don't wish to answer", "No, I am not a veteran or active member",
      "Yes, I am a veteran or active member"], "value", "No, I am not a veteran"),
    ("Do you identify as transgender?", "radio",
     ["I don't wish to answer", "No", "Yes"], "value", "No"),  # TRAP: contains "gender"
    ("Gender", "radio", ["Man", "Woman", "Non-binary", "I don't wish to answer"], "value", "Man"),
    ("Race / Ethnicity", "radio", ["Asian", "White", "Black", "I don't wish to answer"], "value", "Asian"),
    ("Disability status", "radio", ["No", "Yes", "I don't wish to answer"], "value", "No"),
    ("Sexual orientation (mark all that apply)", "checkbox",
     ["Heterosexual", "Gay", "I don't wish to answer"], "value", "Heterosexual"),
    # EEO decline-fallback: no truthful option present → pick decline, never blank
    ("Gender identity", "radio", ["Genderqueer", "I prefer not to disclose"], "value", "prefer not"),
]


CASES += [
    # ── Export-compliance / sanctions (Databricks two-part pattern) — pick the safe option,
    #    never a sanctioned-country box; a long label with options is NOT an essay. ──
    ("Please confirm whether any of the below applies to you. Select all that apply. Note: "
     "compliance with U.S. sanctions and export controls.", "checkbox",
     ["Citizen or permanent resident of Cuba, Iran, North Korea, or Syria",
      "Ordinarily a resident of Russia or Belarus and not willing to relocate",
      "None of the above"], "value", "None of the above"),
    ("If you selected a response to the prior question other than “none of the above,” please "
     "confirm whether any of the following also applies to you. Select all that apply.", "checkbox",
     ["U.S. citizen", "U.S. non-citizen national", "None of these apply to me",
      "Not applicable (i.e., I selected “none of the above” for the prior question)"],
     "value", "Not applicable"),
    # prior employment at THIS company (long label + options → must pick "No", not essay)
    ("Do you currently or have you previously worked for Databricks in the past?", "select",
     ["No", "Yes - I currently work at Databricks", "Yes - Previous Full-Time Employee"],
     "value", "No"),
    ("Have you been employed by Upstart before?", "select", ["No", "Yes"], "value", "No"),
    # education widget (must not collide with job "start date → Immediately")
    ("School", "select", ["New England College", "Other"], "value", "New England College"),
    ("Degree", "select", ["Bachelor's Degree", "Master's Degree"], "value", "Master's Degree"),
    ("Discipline", "select", ["Computer Science", "Biology"], "value", "Computer Science"),
    ("Discipline", "select", ["Computer Information Systems", "Other"], "value", "Computer Information Systems"),
    ("Start date month", "select", ["August", "July"], "value", "August"),
    ("End date year", "text", None, "text", "2022"),
    ("When can you start?", "text", None, "text", "Immediately"),  # job availability, NOT education
    # Greenhouse appends "(required)" + a field-id hash — must still match the email/phone rule
    ("Email (required) 1fb4ed12", "email", None, "text", "@"),
    ("Phone Number (required) 291e4100", "tel", None, "text", "409"),
    ("First Name (required) d70cf5ba", "text", None, "text", _FIRST),
]


@pytest.mark.parametrize("label,ftype,options,exp_kind,exp_val", CASES)
def test_resolve(label, ftype, options, exp_kind, exp_val):
    r = resolve(label, ftype, options)
    assert r.kind == exp_kind, f"{label!r} → kind {r.kind} (want {exp_kind}); value={r.value!r}"
    if exp_val is not None:
        assert exp_val.lower() in (r.value or "").lower(), \
            f"{label!r} → {r.value!r} missing {exp_val!r}"


# These must be left for the human — NOT auto-answered with a wrong value.
SKIP_OR_GENERATE = [
    ("Do you have experience with our open source tools?", "radio", ["Yes", "No"], "skip"),  # TRAP: source
    ("Have you ever worked with Python at scale?", "radio", ["Yes", "No"], "skip"),          # TRAP: ever worked
    ("Why are you interested in this company?", "textarea", None, "generate"),
    ("Cover Letter", "textarea", None, "generate"),
    ("Tell us about a challenging project", "textarea", None, "generate"),
]


@pytest.mark.parametrize("label,ftype,options,exp_kind", SKIP_OR_GENERATE)
def test_skip_or_generate(label, ftype, options, exp_kind):
    r = resolve(label, ftype, options)
    assert r.kind == exp_kind, f"{label!r} → {r.kind} (want {exp_kind}); value={r.value!r}"
