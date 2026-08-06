"""Review the pending_review queue and flag UNNECESSARY apps for a 7-year Senior Data Engineer.

Removes (marks skipped) on relevance signals, NOT liveness (that's prune_dead_queue.py):
  • seniority overshoot   — Principal/Distinguished/Director/VP/Chief, or Senior/Sr Staff, Staff+
                            (plain "Staff" and "Member of Technical Staff" are KEPT — attainable/normal IC)
  • pre-sales / solutions — Solutions/Sales Engineer, Solution Architect, pre-sales (customer-facing, not IC dev)
  • research-track        — Research Scientist/Engineer, Applied Scientist (PhD/research-oriented)
  • duplicate            — same company + normalized title already kept once

Reversible: sets status=skipped with a reason. Run: PYTHONPATH=. .venv/bin/python scripts/prune_unnecessary_queue.py [--apply]
"""
import json, re, sys
from collections import Counter
from pathlib import Path

STATE = Path("data/pipeline_state.json")

OVERSHOOT = re.compile(r"\bprincipal\b|distinguished|\bdirector\b|head of|\bchief\b|\bvp\b|"
                       r"(senior|sr\.?)\s+staff|staff\s*\+|staff plus", re.I)
MOTS      = re.compile(r"member of technical staff", re.I)   # normal IC title — never overshoot
SALES     = re.compile(r"solutions?\s+engineer|sales\s+engineer|pre-?sales|solution architect|"
                       r"partner .*(solution|technolog)", re.I)
RESEARCH  = re.compile(r"research scientist|applied scientist|research engineer", re.I)


def classify(title: str) -> str | None:
    t = title or ""
    if MOTS.search(t):
        return None
    if OVERSHOOT.search(t):
        return "seniority overshoot (Principal/Sr-Staff/Director+) — above a 7-yr senior"
    if SALES.search(t):
        return "pre-sales/solutions role — not a hands-on data engineering IC role"
    if RESEARCH.search(t):
        return "research-track role — PhD/research-oriented, off-profile"
    return None


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def main(apply: bool):
    state = json.loads(STATE.read_text())
    apps = [a for a in state["applications"] if a.get("status") == "pending_review"]

    reasons = {}          # app_id -> reason
    seen = set()          # (company, norm title) kept so far
    for a in apps:
        r = classify(a.get("role_title", ""))
        if r:
            reasons[a["id"]] = r
            continue
        key = (norm(a.get("company_name")), norm(a.get("role_title")))
        if key in seen:
            reasons[a["id"]] = "duplicate — same company+title already in queue"
        else:
            seen.add(key)

    tally = Counter(v.split(" — ")[0] for v in reasons.values())
    print(f"pending_review: {len(apps)} | flagged unnecessary: {len(reasons)} | keeping: {len(apps)-len(reasons)}\n")
    for k, n in tally.most_common():
        print(f"  {n:4}  {k}")
    print("\nsamples:")
    shown = Counter()
    for a in apps:
        r = reasons.get(a["id"])
        if not r:
            continue
        cat = r.split(" — ")[0]
        if shown[cat] < 6:
            shown[cat] += 1
            print(f"  x {a['company_name']:20.20} | {str(a['role_title']):48.48} | {cat}")

    if apply:
        for a in state["applications"]:
            if a["id"] in reasons:
                a["status"] = "skipped"
                a["skip_reason"] = reasons[a["id"]]
        STATE.write_text(json.dumps(state, indent=2))
        print(f"\nAPPLIED — {len(reasons)} skipped. pending_review now {len(apps)-len(reasons)}.")
    else:
        print("\nDRY RUN — pass --apply to remove these.")


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
