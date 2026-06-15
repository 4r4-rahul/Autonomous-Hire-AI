#!/usr/bin/env python3
"""Summarize autofill failure telemetry → fix CLASSES of forms, not one screenshot at a time.

The extension VERIFIES each field against the live DOM after filling and posts the outcome to
/api/v1/autofill/report, appended to data/autofill_reports.jsonl. This rolls that up so the
recurring failure patterns are obvious: which ATS hosts fail most, which labels never commit,
which widget kinds are weakest, and which labels the resolver has no rule for.

Usage:
  python scripts/autofill_failures.py            # full rollup
  python scripts/autofill_failures.py --recent 20  # only the last N runs
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

_FILE = Path(__file__).parent.parent / "data" / "autofill_reports.jsonl"


def _load(recent: int | None) -> list[dict]:
    if not _FILE.exists():
        return []
    rows = []
    for line in _FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows[-recent:] if recent else rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--recent", type=int, default=None, help="only the last N runs")
    args = ap.parse_args()

    runs = _load(args.recent)
    if not runs:
        print("No autofill reports yet (data/autofill_reports.jsonl is empty).")
        return

    total_fields = sum(r.get("counts", {}).get("total", 0) for r in runs)
    total_filled = sum(r.get("counts", {}).get("filled", 0) for r in runs)
    total_needs = sum(r.get("counts", {}).get("needs_you", 0) for r in runs)
    rate = (total_filled / total_fields * 100) if total_fields else 0.0

    by_host = Counter()           # host -> failures
    host_runs = Counter()         # host -> runs (for per-run failure avg)
    by_label = Counter()          # (label, kind) -> count
    by_kind = Counter()           # widget kind -> failures
    mechanism = Counter()         # value_kind -> count (skip = no resolver rule; value = didn't commit)
    no_rule_labels = Counter()    # labels the resolver has no rule for

    for r in runs:
        host = r.get("host", "?")
        host_runs[host] += 1
        for f in r.get("failures", []):
            by_host[host] += 1
            label = (f.get("label") or "").strip()[:60]
            kind = f.get("kind", "?")
            by_label[(label, kind)] += 1
            by_kind[kind] += 1
            vk = f.get("value_kind", "?")
            mechanism[vk] += 1
            if vk == "skip":
                no_rule_labels[label] += 1

    print(f"\n=== Autofill telemetry — {len(runs)} run(s) "
          f"{'(last %d)' % args.recent if args.recent else '(all)'} ===")
    print(f"Fields filled: {total_filled}/{total_fields} ({rate:.0f}%)   needs-you total: {total_needs}\n")

    print("Worst ATS hosts (failures / runs):")
    for host, n in by_host.most_common(10):
        print(f"  {n:>3}  in {host_runs[host]:>2} run(s)   {host}")
    if not by_host:
        print("  — none — every field committed. 🎉")

    print("\nWidget kinds failing:")
    for kind, n in by_kind.most_common():
        print(f"  {n:>3}  {kind}")

    print("\nFailure mechanism:")
    for vk, n in mechanism.most_common():
        tag = "resolver has NO RULE" if vk == "skip" else "filled but DID NOT COMMIT" if vk in ("value", "text") else vk
        print(f"  {n:>3}  {vk:<6} — {tag}")

    print("\nTop failing fields (label · widget):")
    for (label, kind), n in by_label.most_common(20):
        print(f"  {n:>3}  [{kind}] {label or '(no label)'}")

    if no_rule_labels:
        print("\nLabels the resolver has NO RULE for (add a rule in api/autofill.py):")
        for label, n in no_rule_labels.most_common(15):
            print(f"  {n:>3}  {label or '(no label)'}")
    print()


if __name__ == "__main__":
    main()
