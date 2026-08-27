"""Summarise a compiled rule set: rule count, type split, and guarded tools.

Reads the JSON rule files under rules/ and prints, per suite, how many rules
were enforced and which tools each guards. This lets a reader confirm the rule
counts reported in the paper (e.g. banking = 3 rules, airline = 10) directly
from the released artifacts.

Usage:
    python scripts/rule_stats.py ../rules/agentdojo/banking_rules.json
    python scripts/rule_stats.py ../rules/tau2/airline_rules.auto.json
"""
import json
import sys
from collections import Counter


def summarize(path):
    d = json.load(open(path, encoding="utf-8"))
    rules = d.get("rules", [])
    types = Counter(r["type"] for r in rules)
    print(f"domain: {d.get('domain')}  |  rules: {len(rules)}  "
          f"(forbidden={types.get('forbidden', 0)}, precondition={types.get('precondition', 0)})")
    for r in rules:
        tools = ", ".join(r.get("tools", []))
        print(f"  {r['id']:20s} [{r['type']:12s}] {r['predicate']}")
        print(f"      guards: {tools}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    for p in sys.argv[1:]:
        summarize(p)
        print()
