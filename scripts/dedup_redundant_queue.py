"""Remove REDUNDANT near-duplicate roles from the review queue: same company + same core role posted
at different levels/locations (e.g. "Data Engineer" AND "Senior Data Engineer" at one company, or
"Senior Data Engineer I" AND "II"). Applying to both is redundant and reads as spray. Keep ONE best
variant per group; skip the rest (reversible status change).

"Best in group" = highest fit; tie → prefer a Senior/Sr title (candidate is a ~7-yr senior); tie →
already-tailored; tie → newest. Run: PYTHONPATH=. .venv/bin/python scripts/dedup_redundant_queue.py [--apply]
"""
import json, re, sys
from collections import defaultdict
from pathlib import Path

STATE = Path("data/pipeline_state.json")


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _core(t):
    """Role identity with level/location/numbering stripped — the redundancy key."""
    t = re.sub(r"\(.*?\)", "", t or "")
    t = re.sub(r"\b(senior|sr|staff|principal|lead|ii|iii|iv|i|2|3|4|remote|us|usa|hybrid|onsite|"
               r"contract|w2|c2c)\b", " ", t.lower())
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def main(apply: bool):
    state = json.loads(STATE.read_text())
    pend = [a for a in state["applications"] if a.get("status") == "pending_review"]
    fits = {f["job_id"]: (f.get("score") or 0) for f in state.get("fit_scores", [])}

    def rank_key(a):
        title = (a.get("role_title") or "").lower()
        return (
            fits.get(a.get("job_id"), 0),                      # highest fit first
            1 if re.search(r"\b(senior|sr|staff)\b", title) else 0,   # prefer senior variant
            1 if a.get("tailored_resume_path") else 0,         # keep already-prepared
            a.get("created_at") or "",                          # newest
        )

    groups = defaultdict(list)
    for a in pend:
        groups[(_norm(a.get("company_name")), _core(a.get("role_title")))].append(a)

    remove = []
    for (co, core), members in groups.items():
        if len(members) < 2:
            continue
        members.sort(key=rank_key, reverse=True)
        keep, drop = members[0], members[1:]
        for d in drop:
            remove.append((d, keep))

    print(f"pending_review: {len(pend)} | redundant groups: "
          f"{sum(1 for m in groups.values() if len(m)>1)} | to remove: {len(remove)}\n")
    for d, keep in sorted(remove, key=lambda x: x[0].get("company_name") or ""):
        print(f"  x {d['company_name']:16.16} | drop: {str(d['role_title']):42.42} "
              f"| keep: {keep['role_title'][:34]}")

    if apply:
        ids = {id(d) for d, _ in remove}
        rm_appids = {d["id"] for d, _ in remove}
        for a in state["applications"]:
            if a.get("id") in rm_appids and a.get("status") == "pending_review":
                a["status"] = "skipped"
                a["skip_reason"] = "redundant — near-duplicate role at same company (kept the best-fit variant)"
        STATE.write_text(json.dumps(state, indent=2))
        print(f"\nAPPLIED — removed {len(remove)} redundant. pending_review now {len(pend)-len(remove)}.")
    else:
        print("\nDRY RUN — pass --apply to remove.")


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
