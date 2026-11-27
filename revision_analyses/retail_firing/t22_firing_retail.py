# Reference copy published with NOMOS artifacts v1.2. Local paths are replaced by placeholders
# (<paper>, <tau2-bench>, <agentdojo>); the script reads raw run records that are not in this package
# (available from the corresponding author on reasonable request), so it documents the computation
# rather than running as is.
# -*- coding: utf-8 -*-
"""T22 (Reviewer 4 minor 5, Reviewer 3): firing/non-firing split on tau2-bench
airline (gemma, compiled gate) and retail, and a decomposition of the retail
pass^k deltas, including the one-modification-per-order interaction.

Offline only: reads the canonical run records, the runtime gate-event logs and
the task definitions. No LLM call, no simulation, no server. The only code
execution beyond counting is (a) the deterministic NOMOS gate replayed over
recorded transcripts (same gate code and rule files as the runs) and (b) a
deterministic replay of each retail task's golden actions on the retail DB
(the same replay the tau2 scorer performs to build its gold DB).

Run with the tau2-bench virtualenv:

    PYTHONIOENCODING=utf-8 <tau2-bench>/.venv/Scripts/python.exe \
        <paper>/IEEE/analysis_r1/T22_firing_retail/t22_firing_retail.py

Scoring follows T03 / audit_recompute.py: reward >= 1.0 is a pass (reward None
counts as a fail), pass^k = mean over tasks of C(c,k)/C(n,k), deltas use a
task-paired bootstrap (10,000 resamples, random.Random(0), tasks sorted by id
string, percentile interval boots[int(0.025*B)], boots[int(0.975*B)-1]).

Joining gate events to simulations. The runtime gate never writes a refused call
into the transcript (the agent re-plans inside generate_next_message) and the
side-channel log has no simulation id, trial or timestamp: each line is
{rule_id, tool, arguments, rules_file}, appended in real time by 8 concurrent
workers. An event is therefore located by three constraints:
  (1) entity: every identifier argument of the refused call (order_id,
      reservation_id, user_id, payment ids, item ids) occurs in the transcript
      before the candidate position;
  (2) gate replay: the deterministic gate, fed the transcript up to the
      candidate assistant message (the refused attempt and its denial do not
      update the fact store at runtime), refuses exactly that call with exactly
      the logged rule id;
  (3) time order: the log is append-only, so the assigned positions must admit
      non-decreasing times, each inside its window (timestamp of the message
      before the candidate assistant message, timestamp of that message).
Constraint (3) is solved exactly with a forward (earliest time) and backward
(latest time) pass over interval candidates.
"""
import contextlib
import copy
import hashlib
import io
import json
import random
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from math import comb

PAPER = "<paper>"
RUNS = f"{PAPER}/runs/tau2"
LOGS = f"{PAPER}/runs/tau2-gate-logs"
TAU2 = "<tau2-bench>"
TAU2_SRC = f"{TAU2}/src"
OUT_DIR = f"{PAPER}/IEEE/analysis_r1/T22_firing_retail"
OUT_JSON = f"{OUT_DIR}/results.json"

DOMAINS = {
    "airline": {"undef": "airline_base_k4", "gate": "airline_gate_gemma_v2_k5",
                "log": "gate_events_airline_gemma_v2_k5.jsonl", "k": 5,
                "rules": f"{TAU2}/data/tau2/domains/airline/nomos_rules.auto.json",
                "tasks": f"{TAU2}/data/tau2/domains/airline/tasks.json",
                "paper_refusals": 226},
    "retail": {"undef": "retail_base_k4", "gate": "retail_gate_auto_k4",
               "log": "gate_events_retail_auto.jsonl", "k": 4,
               "rules": f"{TAU2}/data/tau2/domains/retail/nomos_rules.auto.json",
               "tasks": f"{TAU2}/data/tau2/domains/retail/tasks.json",
               "paper_refusals": 208},
}
# manuscript tab:passk (undef, gate, delta, ci_low, ci_high); None = not stated
PAPER_PASSK = {
    "airline": {1: (39.2, 47.6, 8.4, -1.2, 18.0), 2: (27.4, 40.6, 13.2, 2.6, 24.0),
                3: (23.0, 37.0, 14.0, 2.6, 25.4), 4: (21.2, 34.4, 13.2, 0.8, 25.6),
                5: (20.0, 32.0, 12.0, -2.0, 26.0)},
    "retail": {1: (52.4, 50.2, -2.2, -7.9, 3.5), 4: (24.6, 22.8, -1.8, -9.6, 6.1)},
}
# RUN_MANIFEST.md row "Firing/non-firing split (V-B)" (airline only)
HIDDEN_MIN = 50.0   # excess prompt tokens preferred for a candidate position
HIDDEN_HIGH = 80.0  # above the airline baseline maximum (77)
MANIFEST_SPLIT = "quiet tasks pass^1 +3.6 (ns), pass^5 +0.0; firing-prone significant at k=2,4,5"
TOL = 0.15
ITERS = 10000
SEED = 0
ID_KEYS = ("order_id", "reservation_id", "user_id", "payment_method_id", "payment_id",
           "item_ids", "new_item_ids")
ORDER_WRITES = ("modify_pending_order_items", "modify_pending_order_address",
                "modify_pending_order_payment", "cancel_pending_order",
                "exchange_delivered_order_items", "return_delivered_order_items")
PENDING_MODS = ("modify_pending_order_items", "modify_pending_order_address",
                "modify_pending_order_payment")

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, TAU2_SRC)


# ---------------------------------------------------------------- helpers
def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def passed(sim):
    r = (sim.get("reward_info") or {}).get("reward")
    return 1 if (r is not None and r >= 1.0) else 0


def per_task(sims):
    per = defaultdict(list)
    for s in sims:
        per[str(s["task_id"])].append(passed(s))
    return per


def pk(c, n, k):
    return comb(c, k) / comb(n, k)


def passk(per, k, tasks):
    keys = [t for t in tasks if len(per[t]) >= k]
    if not keys:
        return float("nan")
    return 100 * sum(pk(sum(per[t]), len(per[t]), k) for t in keys) / len(keys)


def task_deltas(a, b, k, tasks):
    ts = sorted(t for t in tasks if len(a[t]) >= k and len(b[t]) >= k)
    return ts, [pk(sum(b[t]), len(b[t]), k) - pk(sum(a[t]), len(a[t]), k) for t in ts]


def boot_mean(du, iters=ITERS, seed=SEED):
    if not du:
        return float("nan"), float("nan"), float("nan")
    rng = random.Random(seed)
    boots = []
    for _ in range(iters):
        s = [du[rng.randrange(len(du))] for _ in du]
        boots.append(100 * sum(s) / len(s))
    boots.sort()
    return 100 * sum(du) / len(du), boots[int(0.025 * iters)], boots[int(0.975 * iters) - 1]


def split_rows(pu, pg, tasks, kmax, n_all):
    rows = []
    for k in range(1, kmax + 1):
        ts, du = task_deltas(pu, pg, k, tasks)
        d, lo, hi = boot_mean(du)
        rows.append({"k": k, "n_tasks": len(ts), "undef": r2(passk(pu, k, tasks)), "gate": r2(passk(pg, k, tasks)),
                     "delta": r2(d), "ci_low": r2(lo), "ci_high": r2(hi),
                     "sig": bool(du) and bool(lo > 0 or hi < 0),
                     "contribution_to_all_delta": r2(100 * sum(du) / n_all) if du else 0.0})
    return rows


def fs(x):
    return "NA" if x is None else f"{x:+.2f}"


def r2(x):
    return None if x != x else round(x, 2)


def sign_test(up, down):
    """Exact two-sided binomial sign test on non-tied tasks."""
    n = up + down
    if n == 0:
        return 1.0
    m = min(up, down)
    p = sum(comb(n, i) for i in range(0, m + 1)) / 2 ** n
    return min(1.0, 2 * p)


def ts(s):
    return datetime.fromisoformat(s)


def flat_strings(v):
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        out = []
        for x in v:
            out += flat_strings(x)
        return out
    if isinstance(v, dict):
        out = []
        for x in v.values():
            out += flat_strings(x)
        return out
    return []


def event_keys(args, tier="strict"):
    """Strings of the refused call that must already occur in the transcript.
    strict: every string argument value (nested) of length >= 4 that contains a digit
            (ids, item ids, payment ids, flight numbers, dates as the agent wrote them);
    ids:    only the identifier arguments in ID_KEYS (fallback when strict finds no position)."""
    if tier == "strict":
        keys = [x for x in flat_strings(args) if len(x) >= 4 and any(ch.isdigit() for ch in x)]
    else:
        keys = []
        for k in ID_KEYS:
            if k in args:
                keys += flat_strings(args[k])
    if not keys:  # e.g. get_product_details(product_id)
        keys = [x for x in flat_strings(args) if len(x) >= 4]
    return sorted(set(keys))


def call_sig(tool, args):
    return tool + "|" + json.dumps(args, sort_keys=True)


# ---------------------------------------------------------------- gate
def make_gate(domain, rules_path):
    from loguru import logger
    logger.remove()
    from tau2.agent.nomos_gate import NomosGate
    from tau2.environment.toolkit import ToolType, get_tool_types
    from tau2.registry import registry
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        env = registry.get_env_constructor(domain)()
        types = get_tool_types(env.tools)
        known = {t.name for t in env.get_tools()}
    write = {n for n, t in types.items() if t == ToolType.WRITE}
    gate_rt = NomosGate.from_file(rules_path, known_tools=known, write_tools=write)  # as GatedLLMAgent
    gate_orig = NomosGate.from_file(rules_path)  # as scripts/nomos_firing_split.py
    return gate_rt, gate_orig, write


def guarded(gate, tool):
    return any(r["predicate"] == "user_confirmed" and tool in r["tools"] for r in gate.rules)


def walk(sim, gate, consume=True):
    """Yield (msg_index, facts_before, prior_text) for every assistant message, with the
    fact store evolving exactly as in GatedLLMAgent (user turns, tool results, and
    consumption of a confirmation by an admitted guarded write)."""
    from tau2.agent.nomos_gate import update_facts_from_tool_result, update_facts_from_user_turn
    facts = gate.new_facts()
    pending = {}
    text = []
    for i, m in enumerate(sim.get("messages") or []):
        role = m.get("role")
        if role == "assistant":
            yield i, facts, "\n".join(text)
            tcs = m.get("tool_calls") or []
            for c in tcs:
                pending[c["id"]] = c["name"]
                text.append(json.dumps(c.get("arguments") or {}, sort_keys=True))
            if consume and any(guarded(gate, c["name"]) for c in tcs):
                facts.user_confirmed = False
            if m.get("content"):
                text.append(m["content"])
        elif role == "user":
            update_facts_from_user_turn(facts, m.get("content"))
            text.append(m.get("content") or "")
        elif role == "tool":
            update_facts_from_tool_result(facts, m.get("content"), pending.pop(m.get("id"), None))
            text.append(m.get("content") or "")


def replay_blocks(sims, gate, consume):
    """Baseline replay (pre-treatment split): calls in recorded transcripts the gate would refuse."""
    out = defaultdict(int)
    for s in sims:
        n = 0
        for i, facts, _ in walk(s, gate, consume):
            for c in s["messages"][i].get("tool_calls") or []:
                if not gate.check(c["name"], c.get("arguments") or {}, facts).allowed:
                    n += 1
        out[s["id"]] = n
    return out


# ---------------------------------------------------------------- hidden-context residual
def _residual_rows(sims):
    """Per assistant message with usage: excess prompt tokens over what the recorded
    transcript explains (previous prompt + previous completion + user-turn tokens),
    with the tool-result share modelled linearly. A refused attempt and its denial sit
    in the agent's context but not in the transcript, so they appear as excess."""
    rows = []
    for s in sims:
        prev = None
        inter = []
        for i, m in enumerate(s.get("messages") or []):
            if m.get("role") == "assistant":
                u = m.get("usage")
                if isinstance(u, dict) and u.get("prompt_tokens") is not None:
                    if prev is not None:
                        pu = prev["usage"]
                        uc = sum((x.get("usage") or {}).get("completion_tokens", 0) for x in inter if x.get("role") == "user")
                        r = u["prompt_tokens"] - pu["prompt_tokens"] - pu["completion_tokens"] - uc
                        x = (1.0, sum(len(y.get("content") or "") for y in inter if y.get("role") == "tool"),
                             sum(1 for y in inter if y.get("role") == "tool"), sum(1 for y in inter if y.get("role") == "user"))
                        rows.append((s["id"], i, r, x))
                    prev = m
                inter = []
            else:
                inter.append(m)
    return rows


def fit_residual(baseline_sims):
    import numpy as np
    rows = _residual_rows(baseline_sims)
    X = np.array([r[3] for r in rows], float)
    y = np.array([r[2] for r in rows], float)
    coef = np.linalg.lstsq(X, y, rcond=None)[0]
    res = y - X @ coef
    return coef, res


def residuals(sims, coef):
    out = {}
    for sid, i, r, x in _residual_rows(sims):
        out[(sid, i)] = r - sum(a * b for a, b in zip(coef, x))
    return out


# ---------------------------------------------------------------- event join
def _candidates(events, sims, gate, tier, only=None):
    cands = defaultdict(list)
    keys = {ei: event_keys(e["arguments"], tier) for ei, e in enumerate(events) if only is None or ei in only}
    for s in sims:
        msgs = s["messages"]
        for i, facts, prior in walk(s, gate, consume=True):
            lo = ts(msgs[i - 1]["timestamp"]) if i > 0 else ts(s["start_time"])
            hi = ts(msgs[i]["timestamp"])
            for ei, ks in keys.items():
                if not all(k in prior for k in ks):
                    continue
                e = events[ei]
                v = gate.check(e["tool"], e["arguments"], facts)
                if v.allowed or v.rule_id != e["rule_id"]:
                    continue
                cands[ei].append((s["id"], i, lo, hi))
    return cands


def _max_chain(cands_list):
    """Longest subsequence of events (in log order) admitting non-decreasing times, each
    inside one of its candidate windows. Patience-style DP over (end time) with back
    pointers; returns the set of event indices on one maximum chain."""
    import bisect
    ends, nodes = [], []  # ends[L-1] = minimal end time of a chain of length L; nodes[L-1] = (event, parent node)
    for ei, cs in enumerate(cands_list):
        upd = {}
        for (_, _, lo, hi) in cs:
            L = bisect.bisect_right(ends, hi)
            if L == 0:
                new_end, parent = lo, None
            else:
                new_end, parent = max(lo, ends[L - 1]), nodes[L - 1]
            # chain of length L+1 ending at new_end
            if L not in upd or new_end < upd[L][0]:
                upd[L] = (new_end, (ei, parent))
        for L, (new_end, node) in sorted(upd.items()):
            if L < len(ends):
                if new_end < ends[L]:
                    ends[L], nodes[L] = new_end, node
            elif L == len(ends):
                ends.append(new_end)
                nodes.append(node)
    chain = set()
    node = nodes[-1] if nodes else None
    while node is not None:
        chain.add(node[0])
        node = node[1]
    return chain


def locate_events(events, sims, gate, resid, hidden_min):
    sim_by_id = {s["id"]: s for s in sims}
    strict = _candidates(events, sims, gate, "strict")
    need = {ei for ei in range(len(events)) if not strict.get(ei)}
    loose = _candidates(events, sims, gate, "ids", only=need) if need else {}
    cands, tier = [], []
    for ei in range(len(events)):
        if strict.get(ei):
            cands.append(strict[ei]); tier.append("strict")
        elif loose.get(ei):
            cands.append(loose[ei]); tier.append("ids")
        else:
            cands.append([]); tier.append("none")
    # Step 1: a maximum time-consistent chain under the hard constraints only.
    chain = _max_chain(cands)
    n = len(events)
    idx = [i for i in range(n) if i in chain]

    def bounds(sets):
        NEG, POS = datetime.min, datetime.max
        E, U = {}, {}
        t = NEG
        for i in idx:
            f = [max(lo, t) for (_, _, lo, hi) in sets[i] if hi >= t]
            if not f:
                return None
            E[i] = t
            t = min(f)
        t = POS
        for i in reversed(idx):
            f = [min(hi, t) for (_, _, lo, hi) in sets[i] if lo <= t]
            if not f:
                return None
            U[i] = t
            t = max(f)
        return E, U

    def feasible(sets, EU):
        E, U = EU
        return {i: [c for c in sets[i] if max(c[2], E[i]) <= min(c[3], U[i])] for i in idx}

    EU = bounds(cands)
    assert EU is not None, "chain must be feasible"
    feas0 = feasible(cands, EU)
    # Step 2 (tie-break only): among feasible positions prefer those whose prompt shows
    # hidden context (excess >= hidden_min); keep the preference only where the joint
    # assignment stays feasible.
    pref = {i: list(feas0[i]) for i in idx}
    for i in idx:  # greedy in log order: keep a narrowing only if the joint assignment stays feasible
        hc = [c for c in pref[i] if resid.get((c[0], c[1]), -1e9) >= hidden_min]
        if hc and len(hc) < len(pref[i]):
            old = pref[i]
            pref[i] = hc
            if bounds(pref) is None:
                pref[i] = old
    final = feasible(pref, bounds(pref))
    tie_broken = sum(1 for i in idx if len(final[i]) < len(feas0[i]))
    loc = []
    for i in range(n):
        if i in chain:
            feas = [(sid, j) for (sid, j, lo, hi) in final[i]]
            feas_hard = sorted({sid for (sid, j, lo, hi) in feas0[i]})
        else:
            feas, feas_hard = [], []
        sims_f = sorted({sid for sid, _ in feas})
        cand_tasks = sorted({str(sim_by_id[c[0]]["task_id"]) for c in cands[i]}, key=int)
        loc.append({"tier": tier[i], "n_candidates_entity_and_replay": len(cands[i]),
                    "candidate_tasks_no_time": cand_tasks,
                    "feasible_sims_hard_constraints_only": feas_hard,
                    "on_max_chain": i in chain, "feasible_positions": len(feas),
                    "feasible_sims": sims_f,
                    "feasible_tasks": sorted({str(sim_by_id[sid]["task_id"]) for sid in sims_f}, key=int),
                    "positions": sorted(set(feas)),
                    "residual_at_positions": sorted({round(resid.get(p, float('nan')), 1) for p in set(feas)})})
    return loc, len(chain), tie_broken


# ---------------------------------------------------------------- retail helpers
def tool_events(sim):
    """Executed calls in order: (msg_index, name, args, ok)."""
    res = {}
    for m in sim.get("messages") or []:
        if m.get("role") == "tool":
            res[m.get("id")] = (not m.get("error"), m.get("content") or "")
    out = []
    for i, m in enumerate(sim.get("messages") or []):
        if m.get("role") == "assistant":
            for c in m.get("tool_calls") or []:
                ok, content = res.get(c["id"], (False, ""))
                out.append((i, c["name"], c.get("arguments") or {}, ok, content))
    return out


def gold_replay_retail(tasks):
    from tau2.registry import registry
    ctor = registry.get_env_constructor("retail")
    out = {}
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        for t in tasks:
            if t.get("initial_state") is not None:
                raise RuntimeError(f"task {t['id']} has initial_state")
            g = ctor()
            g.set_state(initialization_data=None, initialization_actions=None, message_history=[])
            errs = []
            for a in t["evaluation_criteria"].get("actions") or []:
                try:
                    g.make_tool_call(tool_name=a["name"], requestor=a.get("requestor") or "assistant", **a["arguments"])
                except Exception as ex:
                    errs.append(f"{a['name']}({a['arguments'].get('order_id')}): {ex}")
            out[str(t["id"])] = errs
    return out


# ---------------------------------------------------------------- main
def main():
    res = {"method": __doc__.strip(), "inputs": {}, "reproduction": {}}
    gates = {}
    runs = {}
    for dom, cfg in DOMAINS.items():
        gate_rt, gate_orig, write = make_gate(dom, cfg["rules"])
        gates[dom] = (gate_rt, gate_orig, write)
        for cond in ("undef", "gate"):
            p = f"{RUNS}/{cfg[cond]}/results.json"
            j = json.load(open(p, encoding="utf-8"))
            runs[(dom, cond)] = j
            sims = j["simulations"]
            res["inputs"][f"{dom}_{cond}"] = {
                "path": p, "md5": md5(p), "n_sims": len(sims),
                "agent": j["info"]["agent_info"].get("implementation"),
                "trials_per_task": dict(Counter(Counter(str(s["task_id"]) for s in sims).values())),
                "reward_none": sum(1 for s in sims if (s.get("reward_info") or {}).get("reward") is None),
                "termination": dict(Counter(s["termination_reason"] for s in sims))}
        lp = f"{LOGS}/{cfg['log']}"
        res["inputs"][f"{dom}_log"] = {"path": lp, "md5": md5(lp)}
        res["inputs"][f"{dom}_rules"] = {"path": cfg["rules"], "md5": md5(cfg["rules"]),
                                         "rules_loaded_runtime": [r["id"] for r in gate_rt.rules]}
        res["inputs"][f"{dom}_tasks"] = {"path": cfg["tasks"], "md5": md5(cfg["tasks"])}
    for f in ("src/tau2/agent/nomos_gate.py", "src/tau2/agent/gated_llm_agent.py"):
        res["inputs"][f] = {"path": f"{TAU2}/{f}", "md5": md5(f"{TAU2}/{f}")}
    print("inputs:")
    for k, v in res["inputs"].items():
        print(f"  {k:32s} md5={v['md5'][:8]} " + " ".join(f"{a}={b}" for a, b in v.items() if a in ("n_sims", "trials_per_task", "reward_none")))

    res["domains"] = {}
    for dom, cfg in DOMAINS.items():
        print("=" * 76)
        print(f"{dom.upper()}")
        print("=" * 76)
        gate_rt, gate_orig, write = gates[dom]
        K = cfg["k"]
        su, sg = runs[(dom, "undef")]["simulations"], runs[(dom, "gate")]["simulations"]
        pu, pg = per_task(su), per_task(sg)
        ALL = sorted(set(pu) | set(pg))
        assert set(pu) == set(pg)
        D = res["domains"][dom] = {"n_tasks": len(ALL), "k": K}

        # ---- 0. reproduction of tab:passk
        full = split_rows(pu, pg, ALL, K, len(ALL))
        D["full"] = full
        rep = []
        for k, vals in PAPER_PASSK[dom].items():
            r = full[k - 1]
            for lbl, c, pv in zip(("undef", "gate", "delta", "ci_low", "ci_high"),
                                  (r["undef"], r["gate"], r["delta"], r["ci_low"], r["ci_high"]), vals):
                rep.append({"k": k, "quantity": lbl, "computed": c, "paper": pv, "match": abs(c - pv) <= TOL})
        res["reproduction"][f"{dom}_passk"] = rep
        print("  reproduction tab:passk:", "ALL MATCH" if all(x["match"] for x in rep) else
              "MISMATCH " + str([x for x in rep if not x["match"]]))
        for r in full:
            print(f"    pass^{r['k']}: {r['undef']:5.1f} -> {r['gate']:5.1f} d={r['delta']:+5.1f} [{r['ci_low']:+.1f},{r['ci_high']:+.1f}]")

        # ---- 1. gate-event join
        events = [json.loads(l) for l in open(f"{LOGS}/{cfg['log']}", encoding="utf-8") if l.strip()]
        rf = Counter(e["rules_file"] for e in events)
        coef, base_res = fit_residual(su)
        resid = residuals(sg, coef)
        resid_u = residuals(su, coef)
        loc, chain_len, tie_broken = locate_events(events, sg, gate_rt, resid, HIDDEN_MIN)
        sim_by_id = {s["id"]: s for s in sg}
        n_unique_sim = sum(1 for x in loc if len(x["feasible_sims"]) == 1)
        n_unique_task = sum(1 for x in loc if len(x["feasible_tasks"]) == 1)
        n_entity_replay_none = sum(1 for x in loc if x["n_candidates_entity_and_replay"] == 0)
        assigned_pos = {x["positions"][0] for x in loc if len(x["positions"]) == 1}
        assigned_sim_pos = set()
        for x in loc:
            if len(x["feasible_sims"]) == 1:
                assigned_sim_pos |= set(x["positions"])
        res_assigned = [resid.get(p) for p in assigned_pos if resid.get(p) is not None]
        high_unexpl = sorted(p for p, r in resid.items() if r >= HIDDEN_HIGH and p not in assigned_sim_pos)
        import numpy as np
        D["join"] = {"events": len(events), "paper_refused_attempts": cfg["paper_refusals"],
                     "match_paper": len(events) == cfg["paper_refusals"],
                     "rules_file_field": dict(rf), "by_rule": dict(Counter(e["rule_id"] for e in events)),
                     "by_tool": dict(Counter(e["tool"] for e in events)),
                     "tier_counts": dict(Counter(x["tier"] for x in loc)),
                     "no_candidate_after_entity_and_replay": n_entity_replay_none,
                     "candidate_task_unique_without_time": sum(1 for x in loc if len(x["candidate_tasks_no_time"]) == 1),
                     "max_time_consistent_chain": chain_len, "off_chain_events": len(events) - chain_len,
                     "events_whose_position_set_was_narrowed_by_hidden_context_tiebreak": tie_broken,
                     "located_to_unique_simulation_hard_constraints_only": sum(1 for x in loc if len(x["feasible_sims_hard_constraints_only"]) == 1),
                     "located_to_unique_simulation": n_unique_sim, "located_to_unique_task": n_unique_task,
                     "located_to_unique_position": len([x for x in loc if len(x["positions"]) == 1]),
                     "hidden_context_model": {"coef_const_toolchars_ntool_nuser": [round(float(c), 4) for c in coef],
                                              "baseline_residual_pct_50_95_99_max": [round(float(v), 1) for v in np.percentile(base_res, [50, 95, 99]).tolist() + [base_res.max()]],
                                              "baseline_positions_ge_high": int((base_res >= HIDDEN_HIGH).sum()),
                                              "gated_positions_ge_high": int(sum(1 for r in resid.values() if r >= HIDDEN_HIGH)),
                                              "hidden_min_preference": HIDDEN_MIN, "high_threshold": HIDDEN_HIGH,
                                              "uniquely_assigned_positions": len(assigned_pos),
                                              "assigned_positions_residual_ge_min": sum(1 for r in res_assigned if r >= HIDDEN_MIN),
                                              "assigned_positions_residual_pct_5_50": [round(float(v), 1) for v in np.percentile(res_assigned, [5, 50])] if res_assigned else None,
                                              "gated_high_residual_positions_without_located_event": len(high_unexpl)}}
        print(f"  events={len(events)} (paper {cfg['paper_refusals']}) rules_file={dict(rf)}")
        print(f"  join: {D['join']['tier_counts']} no candidate={n_entity_replay_none} chain={chain_len}/{len(events)} "
              f"tie-broken {tie_broken} unique sim={n_unique_sim} unique task={n_unique_task}")
        print(f"  hidden-context: {D['join']['hidden_context_model']}")
        amb = [(i, x["feasible_sims"], x["feasible_tasks"], x["candidate_tasks_no_time"]) for i, x in enumerate(loc) if len(x["feasible_sims"]) != 1]
        D["join"]["not_uniquely_located_events"] = [{"event_index": i, "rule_id": events[i]["rule_id"], "tool": events[i]["tool"],
                                                     "feasible_sims": s_, "feasible_tasks": t_, "candidate_tasks_no_time": c_}
                                                    for i, s_, t_, c_ in amb]
        print(f"  not uniquely located: {len(amb)}; e.g. {[(i, t_, c_) for i, s_, t_, c_ in amb[:12]]}")

        # per-sim refusal counts (unique assignments; ambiguous ones listed separately)
        ref_sim = Counter()
        ref_sim_events = defaultdict(list)
        for i, (e, x) in enumerate(zip(events, loc)):
            if len(x["feasible_sims"]) == 1:
                sid = x["feasible_sims"][0]
                ref_sim[sid] += 1
                ref_sim_events[sid].append({"event_index": i, "rule_id": e["rule_id"], "tool": e["tool"],
                                            "order_or_res": e["arguments"].get("order_id") or e["arguments"].get("reservation_id"),
                                            "msg_index": [j for (s_, j) in x["positions"]]})
        # tasks with an ambiguous event: credit the task when all feasible sims share it
        # (off-chain events: credit the task when the entity+replay candidates, without time, name one task)
        amb_task_only = defaultdict(int)
        unplaced_multi_task = []
        for i, x in enumerate(loc):
            if len(x["feasible_sims"]) > 1 and len(x["feasible_tasks"]) == 1:
                amb_task_only[x["feasible_tasks"][0]] += 1
            elif not x["feasible_sims"] and len(x["candidate_tasks_no_time"]) == 1:
                amb_task_only[x["candidate_tasks_no_time"][0]] += 1
            elif len(x["feasible_sims"]) != 1:
                unplaced_multi_task.append(i)

        trials = defaultdict(list)  # task -> [(trial, sim_id, refusals, passed)]
        for s in sg:
            trials[str(s["task_id"])].append((s["trial"], s["id"], ref_sim[s["id"]], passed(s)))
        for t in trials:
            trials[t].sort()
        fired_trials = {t: sum(1 for x in trials[t] if x[2] > 0) for t in ALL}
        n_tr = {t: len(trials[t]) for t in ALL}
        FIRE_ANY = sorted(t for t in ALL if fired_trials[t] > 0 or amb_task_only.get(t))
        QUIET_G = sorted(t for t in ALL if t not in FIRE_ANY)
        FIRE_ALL = sorted(t for t in ALL if fired_trials[t] == n_tr[t])
        FIRE_SOME = sorted(t for t in FIRE_ANY if t not in FIRE_ALL)
        dist = Counter(fired_trials[t] for t in ALL)
        D["gated_firing"] = {
            "definition_primary": "firing = at least one gate refusal located in ANY gated trial of the task",
            "n_firing_any": len(FIRE_ANY), "n_never_fired": len(QUIET_G), "n_fired_all_trials": len(FIRE_ALL),
            "n_fired_some_not_all": len(FIRE_SOME),
            "distribution_n_fired_trials": {str(a): b for a, b in sorted(dist.items())},
            "tasks_credited_by_ambiguous_event_only": {t: c for t, c in amb_task_only.items() if fired_trials[t] == 0},
            "events_not_attributable_to_one_task": len(unplaced_multi_task),
            "firing_any_tasks": sorted(FIRE_ANY, key=int), "never_fired_tasks": sorted(QUIET_G, key=int),
            "fired_all_tasks": sorted(FIRE_ALL, key=int),
            "refusals_per_task": {t: sum(x[2] for x in trials[t]) for t in sorted(ALL, key=int)},
            "fired_trials_per_task": {t: f"{fired_trials[t]}/{n_tr[t]}" for t in sorted(ALL, key=int)},
            "rows_firing_any": split_rows(pu, pg, FIRE_ANY, K, len(ALL)),
            "rows_never_fired": split_rows(pu, pg, QUIET_G, K, len(ALL)),
            "rows_fired_all": split_rows(pu, pg, FIRE_ALL, K, len(ALL)),
            "rows_fired_some_not_all": split_rows(pu, pg, FIRE_SOME, K, len(ALL)),
        }
        # bound: credit every candidate task of the events not attributable to one task
        upper_extra = set()
        for i in unplaced_multi_task:
            x = loc[i]
            upper_extra |= set(x["feasible_tasks"] or x["candidate_tasks_no_time"])
        FIRE_UP = sorted(set(FIRE_ANY) | upper_extra)
        QUIET_UP = sorted(t for t in ALL if t not in FIRE_UP)
        D["gated_firing"]["upper_bound_variant"] = {
            "definition": "primary plus every candidate task of the events not attributable to one task",
            "n_firing_any": len(FIRE_UP), "n_never_fired": len(QUIET_UP),
            "added_tasks": sorted(set(FIRE_UP) - set(FIRE_ANY), key=int),
            "rows_firing_any": split_rows(pu, pg, FIRE_UP, K, len(ALL)),
            "rows_never_fired": split_rows(pu, pg, QUIET_UP, K, len(ALL))}
        # per-trial view inside the gated run (post-treatment, descriptive)
        tr_f = [x for t in ALL for x in trials[t] if x[2] > 0]
        tr_q = [x for t in ALL for x in trials[t] if x[2] == 0]
        tr_q_in_fire = [x for t in FIRE_SOME for x in trials[t] if x[2] == 0]
        tr_f_in_fire = [x for t in FIRE_SOME for x in trials[t] if x[2] > 0]
        D["gated_firing"]["per_trial"] = {
            "trials_with_refusal": len(tr_f), "pass_rate_trials_with_refusal": r2(100 * sum(x[3] for x in tr_f) / max(1, len(tr_f))),
            "trials_without_refusal": len(tr_q), "pass_rate_trials_without_refusal": r2(100 * sum(x[3] for x in tr_q) / max(1, len(tr_q))),
            "in_fired_some_tasks_trials_with_refusal": len(tr_f_in_fire),
            "in_fired_some_tasks_pass_rate_with_refusal": r2(100 * sum(x[3] for x in tr_f_in_fire) / max(1, len(tr_f_in_fire))),
            "in_fired_some_tasks_trials_without_refusal": len(tr_q_in_fire),
            "in_fired_some_tasks_pass_rate_without_refusal": r2(100 * sum(x[3] for x in tr_q_in_fire) / max(1, len(tr_q_in_fire))),
        }
        G = D["gated_firing"]
        print(f"  gated firing: any={G['n_firing_any']} never={G['n_never_fired']} all={G['n_fired_all_trials']} "
              f"some-not-all={G['n_fired_some_not_all']} dist={G['distribution_n_fired_trials']}")
        for name in ("rows_firing_any", "rows_never_fired", "rows_fired_all", "rows_fired_some_not_all"):
            for r in G[name]:
                if r["k"] in (1, K):
                    print(f"    {name:24s} n={r['n_tasks']:3d} pass^{r['k']}: {r['undef']} -> {r['gate']} d={fs(r['delta'])} "
                          f"[{fs(r['ci_low'])},{fs(r['ci_high'])}]{' *' if r['sig'] else ''} contrib={fs(r['contribution_to_all_delta'])}")
        print(f"    per-trial: {G['per_trial']}")

        # ---- 2. pre-treatment split by baseline replay (the script behind the manuscript sentence)
        D["baseline_replay"] = {}
        for variant, gate, consume, none_fail in (
                ("original_script", gate_orig, False, False),
                ("runtime_gate", gate_rt, True, True)):
            blocks = replay_blocks(su, gate, consume)
            prone = sorted({str(s["task_id"]) for s in su if blocks[s["id"]] > 0})
            quiet = sorted(t for t in ALL if t not in prone)
            if none_fail:
                pu_v, pg_v = pu, pg
            else:  # original script drops reward None and counts reward >= 0.999
                def pt(sims):
                    d_ = defaultdict(list)
                    for s in sims:
                        r = (s.get("reward_info") or {}).get("reward")
                        if r is not None:
                            d_[str(s["task_id"])].append(1 if r >= 0.999 else 0)
                    return d_
                pu_v, pg_v = pt(su), pt(sg)
            blocked_trials = Counter(str(s["task_id"]) for s in su if blocks[s["id"]] > 0)
            D["baseline_replay"][variant] = {
                "definition": "firing-prone = the rule set, replayed over the UNDEFENDED transcripts, refuses at least one recorded call in at least one undefended trial",
                "gate_construction": "from_file(rules) without known/write tools, no confirmation consumption, reward None dropped"
                if variant == "original_script" else
                "from_file(rules, known_tools, write_tools) as GatedLLMAgent, confirmation consumed by admitted guarded write, reward None = fail",
                "n_prone": len(prone), "n_quiet": len(quiet), "prone_tasks": sorted(prone, key=int),
                "quiet_tasks": sorted(quiet, key=int),
                "baseline_trials_with_replayed_block_per_prone_task": {t: blocked_trials[t] for t in sorted(prone, key=int)},
                "rows_prone": split_rows(pu_v, pg_v, prone, K, len(ALL)),
                "rows_quiet": split_rows(pu_v, pg_v, quiet, K, len(ALL)),
            }
            B = D["baseline_replay"][variant]
            print(f"  baseline replay [{variant}]: prone={B['n_prone']} quiet={B['n_quiet']}")
            for name in ("rows_prone", "rows_quiet"):
                for r in B[name]:
                    print(f"    {name:11s} n={r['n_tasks']:3d} pass^{r['k']}: {r['undef']} -> {r['gate']} d={fs(r['delta'])} "
                          f"[{fs(r['ci_low'])},{fs(r['ci_high'])}]{' *' if r['sig'] else ''}")
        # cross-fitted pre-treatment split: define firing-prone on one half of the undefended
        # trials, measure the undefended rate on the other half (removes selection on the same
        # trials); the gated rate uses all gated trials; pass^1 only
        trial_ids = sorted({s["trial"] for s in su})
        halves = [trial_ids[: len(trial_ids) // 2], trial_ids[len(trial_ids) // 2:]]
        blocks_rt = replay_blocks(su, gate_rt, True)
        cf = []
        for f_def, f_eval in ((halves[0], halves[1]), (halves[1], halves[0])):
            prone_f = sorted({str(s["task_id"]) for s in su if s["trial"] in f_def and blocks_rt[s["id"]] > 0})
            quiet_f = sorted(t for t in ALL if t not in prone_f)
            pu_e = defaultdict(list)
            for s in su:
                if s["trial"] in f_eval:
                    pu_e[str(s["task_id"])].append(passed(s))
            row = {"define_on_trials": f_def, "evaluate_undef_on_trials": f_eval, "n_prone": len(prone_f), "n_quiet": len(quiet_f)}
            for name, ts_ in (("prone", prone_f), ("quiet", quiet_f)):
                _, du = task_deltas(pu_e, pg, 1, ts_)
                d, lo, hi = boot_mean(du)
                row[name] = {"undef": r2(passk(pu_e, 1, ts_)), "gate": r2(passk(pg, 1, ts_)), "delta": r2(d),
                             "ci_low": r2(lo), "ci_high": r2(hi), "sig": bool(du) and bool(lo > 0 or hi < 0)}
            cf.append(row)
        D["baseline_replay"]["runtime_gate_crossfit_pass1"] = cf
        for row in cf:
            print(f"  crossfit def={row['define_on_trials']} eval={row['evaluate_undef_on_trials']} prone n={row['n_prone']} "
                  f"{row['prone']} | quiet n={row['n_quiet']} {row['quiet']}")
        # cross-tab pre-treatment (runtime variant) vs gated firing
        prone_rt = set(D["baseline_replay"]["runtime_gate"]["prone_tasks"])
        D["crosstab_prone_vs_gated_firing"] = {
            "prone_and_fired": len(prone_rt & set(FIRE_ANY)), "prone_not_fired": len(prone_rt - set(FIRE_ANY)),
            "quiet_and_fired": len(set(FIRE_ANY) - prone_rt), "quiet_not_fired": len(set(ALL) - prone_rt - set(FIRE_ANY))}
        print(f"  crosstab prone(runtime) x gated-fired: {D['crosstab_prone_vs_gated_firing']}")

        # ---- 3. per-task direction (pass^1 counts)
        diffs = {t: (sum(pg[t]) / len(pg[t]) - sum(pu[t]) / len(pu[t])) for t in ALL}
        up = sorted((t for t in ALL if diffs[t] > 1e-12), key=int)
        down = sorted((t for t in ALL if diffs[t] < -1e-12), key=int)
        eq = sorted((t for t in ALL if abs(diffs[t]) <= 1e-12), key=int)
        D["per_task_direction"] = {
            "n_up": len(up), "n_down": len(down), "n_equal": len(eq),
            "sum_gain_trial_passes": sum(sum(pg[t]) - sum(pu[t]) for t in up),
            "sum_loss_trial_passes": sum(sum(pg[t]) - sum(pu[t]) for t in down),
            "sign_test_p_two_sided": round(sign_test(len(up), len(down)), 4),
            "up_tasks": up, "down_tasks": down,
            "per_task": {t: {"undef": f"{sum(pu[t])}/{len(pu[t])}", "gate": f"{sum(pg[t])}/{len(pg[t])}",
                             "gated_refusals": G["refusals_per_task"][t], "gated_fired_trials": G["fired_trials_per_task"][t]}
                         for t in sorted(ALL, key=int)},
            "equal_tasks_by_level": dict(Counter(f"{sum(pu[t])}/{len(pu[t])}" for t in eq)),
        }
        P = D["per_task_direction"]
        print(f"  per-task pass^1: up={P['n_up']} down={P['n_down']} equal={P['n_equal']} "
              f"gain={P['sum_gain_trial_passes']} loss={P['sum_loss_trial_passes']} sign p={P['sign_test_p_two_sided']}")
        # direction by gated firing
        D["per_task_direction"]["by_gated_firing"] = {
            grp: {"n": len(ts_), "up": sum(1 for t in ts_ if t in up), "down": sum(1 for t in ts_ if t in down),
                  "equal": sum(1 for t in ts_ if t in eq),
                  "net_trial_passes": sum(sum(pg[t]) - sum(pu[t]) for t in ts_)}
            for grp, ts_ in (("firing_any", FIRE_ANY), ("never_fired", QUIET_G))}
        print(f"  direction by gated firing: {D['per_task_direction']['by_gated_firing']}")

        # ---- 4. retail only: one-modification interaction and loss decomposition
        if dom == "retail":
            tasks = json.load(open(cfg["tasks"], encoding="utf-8"))
            tmap = {str(t["id"]): t for t in tasks}
            gold_err = gold_replay_retail(tasks)
            multi = {}
            for t in tasks:
                acts = t["evaluation_criteria"].get("actions") or []
                by_order = defaultdict(list)
                for a in acts:
                    if a["name"] in ORDER_WRITES and a["arguments"].get("order_id"):
                        by_order[a["arguments"]["order_id"]].append(a["name"])
                m_ = {o: v for o, v in by_order.items() if len(v) >= 2}
                if m_:
                    multi[str(t["id"])] = m_
            items_plus = {t: {o: v for o, v in m_.items() if "modify_pending_order_items" in v and
                              any(x in v for x in ("modify_pending_order_address", "modify_pending_order_payment"))}
                          for t, m_ in multi.items()}
            items_plus = {t: v for t, v in items_plus.items() if v}
            gold_items_first = {t: {o: v.index("modify_pending_order_items") < max(
                [i for i, x in enumerate(v) if x in ("modify_pending_order_address", "modify_pending_order_payment")])
                for o, v in m_.items()} for t, m_ in items_plus.items()}
            tasks_with_items = sorted((str(t["id"]) for t in tasks if any(
                a["name"] == "modify_pending_order_items" for a in t["evaluation_criteria"].get("actions") or [])), key=int)
            R = D["retail_ordering"] = {
                "rule_in_environment": "modify_pending_order_items and cancel_pending_order require status == 'pending'; "
                                       "modify_pending_order_address and _payment only require 'pending' in status, so they "
                                       "succeed after an item modification ('pending (item modified)'); the policy text forbids "
                                       "any further modification after an item modification",
                "rule_in_gate": "RET-AUTO-FBD-03 (order_not_pending: status known and != 'pending') refuses address/payment/"
                                "items modification and cancellation once the observed status is 'pending (item modified)'",
                "tasks_multi_write_same_order": {t: multi[t] for t in sorted(multi, key=int)},
                "n_tasks_multi_write_same_order": len(multi),
                "tasks_items_plus_address_or_payment_same_order": {t: items_plus[t] for t in sorted(items_plus, key=int)},
                "n_tasks_items_plus_other_same_order": len(items_plus),
                "gold_order_items_before_other": {t: gold_items_first[t] for t in sorted(gold_items_first, key=int)},
                "n_tasks_with_gold_items_modification": len(tasks_with_items),
                "golden_action_errors": {t: e for t, e in sorted(gold_err.items(), key=lambda x: int(x[0])) if e},
            }
            print(f"  retail multi-write-same-order tasks={len(multi)} items+addr/pay same order={len(items_plus)} "
                  f"{sorted(items_plus, key=int)}; gold errors={R['golden_action_errors']}")

            # per-sim: after a successful items modification on order O, what happened to later modifications of O
            def after_items(sim, cond):
                ev = tool_events(sim)
                out = {"second_mod_ok": 0, "second_mod_tool_error": 0, "gate_refusal_after_items": 0,
                       "items_ok_orders": [], "orders_items_then_other": []}
                items_done = {}
                for (i, name, args, ok, content) in ev:
                    o = args.get("order_id")
                    if name == "modify_pending_order_items" and ok:
                        items_done.setdefault(o, i)
                    elif name in ORDER_WRITES and o in items_done and i > items_done[o]:
                        out["orders_items_then_other"].append((o, name, ok))
                        if ok:
                            out["second_mod_ok"] += 1
                        else:
                            out["second_mod_tool_error"] += 1
                out["items_ok_orders"] = sorted(items_done)
                if cond == "gate":
                    for e in ref_sim_events.get(sim["id"], []):
                        o = e["order_or_res"]
                        if o in items_done and min(e["msg_index"]) > items_done[o] and e["rule_id"] == "RET-AUTO-FBD-03":
                            out["gate_refusal_after_items"] += 1
                return out

            sims_by = {c: defaultdict(list) for c in ("undef", "gate")}
            for c in ("undef", "gate"):
                for s in runs[(dom, c)]["simulations"]:
                    sims_by[c][str(s["task_id"])].append(s)
            # all FBD-03 refusals: where do they fall
            fbd03 = [(i, e, x) for i, (e, x) in enumerate(zip(events, loc)) if e["rule_id"] == "RET-AUTO-FBD-03"]
            fbd03_rows = []
            for i, e, x in fbd03:
                sid = x["feasible_sims"][0] if len(x["feasible_sims"]) == 1 else None
                s = sim_by_id.get(sid)
                st = None
                if s is not None:
                    # observed status of the order at the refused position
                    j = min(jj for (_, jj) in x["positions"])
                    for jj, facts, _ in walk(s, gate_rt):
                        if jj == j:
                            st = (facts.orders.get(e["arguments"].get("order_id")) or {}).get("status")
                            break
                fbd03_rows.append({"event_index": i, "tool": e["tool"], "order_id": e["arguments"].get("order_id"),
                                   "task": x["feasible_tasks"], "sim": sid, "trial": s["trial"] if s else None,
                                   "passed": passed(s) if s else None, "observed_status": st,
                                   "task_in_items_plus_set": bool(x["feasible_tasks"] and x["feasible_tasks"][0] in items_plus)})
            R["fbd03_refusals"] = fbd03_rows
            R["fbd03_by_observed_status"] = dict(Counter(str(r["observed_status"]) for r in fbd03_rows))
            R["fbd03_tasks"] = dict(Counter(r["task"][0] if r["task"] else "?" for r in fbd03_rows))
            print(f"  FBD-03 refusals: {len(fbd03_rows)} by status {R['fbd03_by_observed_status']} tasks {R['fbd03_tasks']}")

            per_ord = {}
            for t in sorted(set(items_plus) | {r["task"][0] for r in fbd03_rows if r["task"]}, key=int):
                row = {}
                for c in ("undef", "gate"):
                    agg = Counter()
                    tr = []
                    for s in sims_by[c][t]:
                        a = after_items(s, c)
                        ev = tool_events(s)
                        # ordering in this trial: first successful/attempted items call before an attempted other mod on same order
                        tr.append({"trial": s["trial"], "passed": passed(s), "items_ok_orders": a["items_ok_orders"],
                                   "items_then_other": a["orders_items_then_other"],
                                   "gate_refusal_after_items": a["gate_refusal_after_items"],
                                   "writes": [(n, ar.get("order_id"), ok) for (_, n, ar, ok, _) in ev if n in ORDER_WRITES or n == "modify_user_address"]})
                        agg["trials"] += 1
                        agg["passed"] += passed(s)
                        agg["trials_items_modified_then_further_write_ok"] += 1 if a["second_mod_ok"] else 0
                        agg["trials_items_modified_then_further_write_tool_error"] += 1 if a["second_mod_tool_error"] else 0
                        agg["trials_gate_refusal_after_items"] += 1 if a["gate_refusal_after_items"] else 0
                    row[c] = {"agg": dict(agg), "trials": tr}
                per_ord[t] = row
            R["per_task_trials"] = per_ord
            # ordering on the gold order of the items+other tasks, per condition and trial
            ordering = {}
            for c in ("undef", "gate"):
                cnt = Counter()
                rows_o = []
                for t in sorted(items_plus, key=int):
                    for s in sims_by[c][t]:
                        ev = tool_events(s)
                        refs = ref_sim_events.get(s["id"], []) if c == "gate" else []
                        for o, names in items_plus[t].items():
                            others = [x for x in names if x != "modify_pending_order_items"]
                            i_items = [i for (i, n, ar, ok, _) in ev if n == "modify_pending_order_items" and ar.get("order_id") == o and ok]
                            i_other_ok = [i for (i, n, ar, ok, _) in ev if n in others and ar.get("order_id") == o and ok]
                            if not i_items:
                                kind = "no_item_modification"
                            elif i_other_ok and min(i_other_ok) < i_items[0]:
                                kind = "other_first(reference_order)"
                            else:
                                after = [(n, ok) for (i, n, ar, ok, _) in ev if n in others and ar.get("order_id") == o and i > i_items[0]]
                                g_ref = [e for e in refs if e["order_or_res"] == o and e["tool"] in others and min(e["msg_index"]) > i_items[0]]
                                if any(ok for _, ok in after):
                                    kind = "items_first_then_other_executed"
                                elif after:
                                    kind = "items_first_then_other_tool_error"
                                elif g_ref:
                                    kind = "items_first_then_other_refused_by_gate"
                                else:
                                    kind = "items_first_other_never_executed"
                            cnt[kind] += 1
                            cnt[kind + "|passed"] += passed(s)
                            rows_o.append({"task": t, "trial": s["trial"], "order": o, "kind": kind, "passed": passed(s)})
                ordering[c] = {"counts": dict(sorted(cnt.items())), "rows": rows_o}
            R["ordering_on_gold_order_items_plus_tasks"] = ordering
            print(f"  ordering (items+other tasks): undef={ordering['undef']['counts']}")
            print(f"                                gate ={ordering['gate']['counts']}")
            # all retail trials: further writes on an order after its successful item modification
            agg_all = {}
            for c in ("undef", "gate"):
                a_ = Counter()
                for s in runs[(dom, c)]["simulations"]:
                    a = after_items(s, c)
                    t = str(s["task_id"])
                    if a["orders_items_then_other"]:
                        a_["trials_with_write_after_items_same_order"] += 1
                        a_["of_which_passed"] += passed(s)
                        a_["of_which_in_items_plus_tasks"] += 1 if t in items_plus else 0
                        for (o, name, ok) in a["orders_items_then_other"]:
                            a_[f"calls_{name}_{'ok' if ok else 'tool_error'}"] += 1
                    if a["gate_refusal_after_items"]:
                        a_["trials_with_gate_refusal_after_items_same_order"] += 1
                        a_["of_which_passed_gate_refusal"] += passed(s)
                agg_all[c] = dict(a_)
            R["writes_after_item_modification_all_trials"] = agg_all
            print(f"  writes after item modification (all trials): {agg_all}")

            # ---- loss decomposition for tasks with a negative per-task difference
            gold_ops = {t: {(a["name"], a["arguments"].get("order_id") or a["arguments"].get("user_id"))
                            for a in tmap[t]["evaluation_criteria"].get("actions") or [] if a["name"] in write}
                        for t in tmap}

            def executed_ok(sim):
                return {(n, ar.get("order_id") or ar.get("user_id")) for (_, n, ar, ok, _) in tool_events(sim) if ok}

            def items_then_other_missing(sim, t):
                """ordering signature: a gold (items + other) order where items succeeded and the other gold write on
                the same order never succeeded after it"""
                if t not in items_plus:
                    return False
                ev = tool_events(sim)
                for o, names in items_plus[t].items():
                    i_items = [i for (i, n, ar, ok, _) in ev if n == "modify_pending_order_items" and ar.get("order_id") == o and ok]
                    if not i_items:
                        continue
                    others = [x for x in names if x != "modify_pending_order_items"]
                    other_before = [i for (i, n, ar, ok, _) in ev if n in others and ar.get("order_id") == o and ok and i < i_items[0]]
                    if not other_before:
                        return True
                return False

            decomp = []
            for t in down:
                g_fail = [s for s in sims_by["gate"][t] if not passed(s)]
                u_fail = [s for s in sims_by["undef"][t] if not passed(s)]
                # (i) a refusal in a failed gated trial of a call the reference needs, never executed successfully later
                i_hits = []
                for s in g_fail:
                    ok_ops = executed_ok(s)
                    for e in ref_sim_events.get(s["id"], []):
                        ev_ = events[e["event_index"]]
                        key = (ev_["tool"], ev_["arguments"].get("order_id") or ev_["arguments"].get("user_id"))
                        if key in gold_ops[t] and key not in ok_ops:
                            i_hits.append({"trial": s["trial"], "rule_id": ev_["rule_id"], "tool": ev_["tool"], "entity": key[1]})
                i_possible_unplaced = [{"event_index": ei, "rule_id": events[ei]["rule_id"], "tool": events[ei]["tool"],
                                        "candidate_tasks": loc[ei]["feasible_tasks"] or loc[ei]["candidate_tasks_no_time"]}
                                       for ei in range(len(events)) if len(loc[ei]["feasible_sims"]) != 1
                                       and t in (loc[ei]["feasible_tasks"] or loc[ei]["candidate_tasks_no_time"])
                                       and (events[ei]["tool"], events[ei]["arguments"].get("order_id") or events[ei]["arguments"].get("user_id")) in gold_ops[t]]
                ii_g = sum(1 for s in g_fail if items_then_other_missing(s, t))
                ii_u = sum(1 for s in u_fail if items_then_other_missing(s, t))
                flags = {"i_gate_refusal_on_needed_call": bool(i_hits),
                         "ii_ordering_both_conditions": ii_g > 0 and ii_u > 0,
                         "ii_ordering_gated_only": ii_g > 0 and ii_u == 0}
                cat = ("i" if flags["i_gate_refusal_on_needed_call"] else
                       "ii" if flags["ii_ordering_both_conditions"] else "iii")
                decomp.append({"task": t, "undef": f"{sum(pu[t])}/{len(pu[t])}", "gate": f"{sum(pg[t])}/{len(pg[t])}",
                               "lost_trial_passes": sum(pu[t]) - sum(pg[t]),
                               "in_items_plus_set": t in items_plus, "in_multi_write_set": t in multi,
                               "gated_refusals_task": G["refusals_per_task"][t],
                               "gated_refusals_in_failed_trials": sum(ref_sim[s["id"]] for s in g_fail),
                               "i_hits": i_hits, "i_possible_from_not_uniquely_located_events": i_possible_unplaced, "ii_failed_trials_with_signature": {"gate": ii_g, "undef": ii_u},
                               "flags": flags, "category": cat})
            R["loss_decomposition"] = decomp
            cat_sum = defaultdict(lambda: {"tasks": 0, "lost_trial_passes": 0})
            for d_ in decomp:
                cat_sum[d_["category"]]["tasks"] += 1
                cat_sum[d_["category"]]["lost_trial_passes"] += d_["lost_trial_passes"]
            R["loss_by_category"] = dict(cat_sum)
            n_sims = len(ALL) * K
            R["loss_by_category_pp_of_pass1"] = {c: r2(-100 * v["lost_trial_passes"] / n_sims) for c, v in cat_sum.items()}
            R["gain_pp_of_pass1"] = r2(100 * P["sum_gain_trial_passes"] / n_sims)
            R["net_pp_of_pass1"] = r2(100 * (P["sum_gain_trial_passes"] + P["sum_loss_trial_passes"]) / n_sims)
            R["down_tasks_in_items_plus_set"] = sorted((t for t in down if t in items_plus), key=int)
            R["up_tasks_in_items_plus_set"] = sorted((t for t in up if t in items_plus), key=int)
            R["equal_tasks_in_items_plus_set"] = sorted((t for t in eq if t in items_plus), key=int)
            R["items_plus_set_net_trial_passes"] = sum(sum(pg[t]) - sum(pu[t]) for t in items_plus)
            # tasks with gated refusals in failed trials vs passed
            print(f"  loss decomposition by category: {R['loss_by_category']} pp={R['loss_by_category_pp_of_pass1']} "
                  f"gain={R['gain_pp_of_pass1']} net={R['net_pp_of_pass1']}")
            print(f"  items+other set: down={R['down_tasks_in_items_plus_set']} up={R['up_tasks_in_items_plus_set']} "
                  f"equal={R['equal_tasks_in_items_plus_set']} net={R['items_plus_set_net_trial_passes']}")
            for d_ in decomp:
                print(f"    task {d_['task']:>3s} {d_['undef']}->{d_['gate']} cat={d_['category']} refusals={d_['gated_refusals_task']} "
                      f"(in failed {d_['gated_refusals_in_failed_trials']}) items+={d_['in_items_plus_set']} "
                      f"ii={d_['ii_failed_trials_with_signature']} i={[(h['trial'], h['rule_id'], h['tool']) for h in d_['i_hits']]}")

        res["domains"][dom]["event_locations"] = [
            {"event_index": i, "rule_id": e["rule_id"], "tool": e["tool"],
             "feasible_sims": x["feasible_sims"], "feasible_tasks": x["feasible_tasks"],
             "trial": (sim_by_id[x["feasible_sims"][0]]["trial"] if len(x["feasible_sims"]) == 1 else None),
             "msg_index": sorted({j for (_, j) in x["positions"]}),
             "n_candidates_entity_and_replay": x["n_candidates_entity_and_replay"]}
            for i, (e, x) in enumerate(zip(events, loc))]

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1, sort_keys=True, default=str)
    print(f"\nwrote {OUT_JSON}")


if __name__ == "__main__":
    main()
