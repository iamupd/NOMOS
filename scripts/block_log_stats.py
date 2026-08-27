"""Attribute gate blocks to rules from a gate block log (JSONL).

Each line of a gate block log records one tool call the gate refused, with the
rule id that fired. This reproduces the per-rule block attribution reported in
the paper (e.g. on airline the user-confirmation precondition accounts for the
majority of blocks) directly from the released logs.

Usage:
    python scripts/block_log_stats.py ../logs/tau2_airline_gate.jsonl
"""
import json
import sys
from collections import Counter


def summarize(path):
    by_rule = Counter()
    by_tool = Counter()
    total = 0
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        total += 1
        rid = e.get("rule_id") or e.get("rule") or "unknown"
        by_rule[rid] += 1
        tool = e.get("tool") or e.get("function") or "unknown"
        by_tool[tool] += 1

    print(f"total blocked calls: {total}\n")
    print("by rule:")
    for rid, n in by_rule.most_common():
        print(f"  {rid:20s} {n:4d}  ({n / total:.1%})")
    print("\nby tool:")
    for tool, n in by_tool.most_common(12):
        print(f"  {tool:34s} {n:4d}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    for p in sys.argv[1:]:
        summarize(p)
