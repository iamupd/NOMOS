# Reference copy published with NOMOS artifacts v1.2. Local paths are replaced by placeholders
# (<paper>, <tau2-bench>, <agentdojo>); the script reads raw run records that are not in this package
# (available from the corresponding author on reasonable request), so it documents the computation
# rather than running as is.
# -*- coding: utf-8 -*-
"""T03 (Reviewer 4, Major 1, ID R4-1): split tau2-bench airline task success by
whether the expected final state requires a state-changing (WRITE) call, and
score a hypothetical agent that never makes a WRITE call.

Computed only from existing run logs and the task definitions. No simulation,
no LLM call. The only environment use is a deterministic replay of each task's
golden actions on the airline DB (the same replay the tau2 scorer performs to
build its gold DB), to confirm which tasks' expected final DB differs from the
initial DB.

Run with the tau2-bench virtualenv (it provides the tau2 package):

    PYTHONIOENCODING=utf-8 <tau2-bench>/.venv/Scripts/python.exe \
        <paper>/IEEE/analysis_r1/T03_airline_split/t03_airline_split.py

Method follows paper/analysis/audit_recompute.py and llama_airline_stats.py:
reward >= 1.0 is a pass (reward None counts as fail), pass^k is the unbiased
estimator C(c,k)/C(n,k) averaged over tasks, deltas use a task-paired
bootstrap (10,000 resamples, random.Random(0), tasks sorted by id string,
percentile interval boots[int(0.025*B)], boots[int(0.975*B)-1]).
"""
import contextlib
import hashlib
import io
import json
import os
import random
import sys
from collections import Counter, defaultdict
from math import comb

# ---------------------------------------------------------------- constants
PAPER = "<paper>"
RUNS = f"{PAPER}/runs/tau2"
TAU2_SRC = "<tau2-bench>/src"
TASKS_JSON = "<tau2-bench>/data/tau2/domains/airline/tasks.json"
OUT_DIR = f"{PAPER}/IEEE/analysis_r1/T03_airline_split"
OUT_JSON = f"{OUT_DIR}/results.json"
# 16-rule airline reference set used by the violation checker (same file as audit_recompute.py)
REF_RULES = "<tau2-bench>/data/tau2/domains/airline/nomos_rules.json"

PAIRS = {
    "gemma": ("airline_base_k4", "airline_gate_gemma_v2_k5"),
    "llama": ("llama_airline_undef_B", "llama_airline_gate_B"),
}
# values stated in the manuscript (revision_r1 tab:passk, tab:twomodels, V-I text)
PAPER_PASSK = {
    "gemma": {1: (39.2, 47.6, 8.4, -1.2, 18.0), 2: (27.4, 40.6, 13.2, 2.6, 24.0),
              3: (23.0, 37.0, 14.0, 2.6, 25.4), 4: (21.2, 34.4, 13.2, 0.8, 25.6),
              5: (20.0, 32.0, 12.0, -2.0, 26.0)},
    "llama": {1: (22.8, 46.8, 24.0, 13.2, 35.6), 5: (14.0, 44.0, 30.0, 18.0, 42.0)},
}
PAPER_WRITES = {"gemma": (353, 116), "llama": (413, 28)}
TOL = 0.15  # percentage points, as in audit_recompute.py

# tools declared ToolType.WRITE in tau2/domains/airline/tools.py
WRITE = {"book_reservation", "cancel_reservation", "send_certificate",
         "update_reservation_baggages", "update_reservation_flights",
         "update_reservation_passengers"}
KMAX = 5
ITERS = 10000
SEED = 0

sys.stdout.reconfigure(encoding="utf-8")


# ---------------------------------------------------------------- helpers
def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def load_run(name):
    p = f"{RUNS}/{name}/results.json"
    j = json.load(open(p, encoding="utf-8"))
    return j, p


def passed(sim):
    r = (sim.get("reward_info") or {}).get("reward")
    return 1 if (r is not None and r >= 1.0) else 0


def per_task(sims):
    per = defaultdict(list)
    for s in sims:
        per[s.get("task_id")].append(passed(s))
    return per


def passk(per, k, tasks=None):
    keys = [t for t in (tasks if tasks is not None else per) if len(per[t]) >= k]
    if not keys:
        return float("nan")
    return 100 * sum(comb(sum(per[t]), k) / comb(len(per[t]), k) for t in keys) / len(keys)


def task_deltas(a, b, k, tasks):
    ts = sorted(t for t in tasks if len(a[t]) >= k and len(b[t]) >= k)
    return ts, [comb(sum(b[t]), k) / comb(len(b[t]), k) - comb(sum(a[t]), k) / comb(len(a[t]), k)
                for t in ts]


def boot_mean(du, iters=ITERS, seed=SEED):
    """Same resampling loop as audit_recompute.passk_ci."""
    rng = random.Random(seed)
    boots = []
    for _ in range(iters):
        s = [du[rng.randrange(len(du))] for _ in du]
        boots.append(100 * sum(s) / len(s))
    boots.sort()
    return 100 * sum(du) / len(du), boots[int(0.025 * iters)], boots[int(0.975 * iters) - 1]


def boot_split_diff(du_nw, du_w, iters=ITERS, seed=SEED):
    """Stratified task bootstrap of (mean delta on no-write tasks) - (mean delta on write tasks)."""
    rng = random.Random(seed)
    boots = []
    for _ in range(iters):
        a = [du_nw[rng.randrange(len(du_nw))] for _ in du_nw]
        b = [du_w[rng.randrange(len(du_w))] for _ in du_w]
        boots.append(100 * (sum(a) / len(a) - sum(b) / len(b)))
    boots.sort()
    point = 100 * (sum(du_nw) / len(du_nw) - sum(du_w) / len(du_w))
    return point, boots[int(0.025 * iters)], boots[int(0.975 * iters) - 1]


def sim_writes(sim):
    """WRITE calls present in the transcript (blocked calls never reach it) and how many returned without error."""
    called = ok = 0
    names = {}
    for m in sim.get("messages") or []:
        if m.get("role") == "assistant":
            for c in m.get("tool_calls") or []:
                names[c["id"]] = c["name"]
                if c["name"] in WRITE:
                    called += 1
        elif m.get("role") == "tool" and names.get(m.get("id")) in WRITE:
            if not m.get("error"):
                ok += 1
    return called, ok


def comm_met(sim):
    ri = sim.get("reward_info") or {}
    return (ri.get("reward_breakdown") or {}).get("COMMUNICATE")


# ---------------------------------------------------------------- gold DB replay
def gold_db_changes(tasks):
    """Replay golden actions exactly as EnvironmentEvaluator does; report whether the gold DB hash differs from the initial one."""
    sys.path.insert(0, TAU2_SRC)
    from loguru import logger
    logger.remove()
    from tau2.registry import registry
    ctor = registry.get_env_constructor("airline")
    out = {}
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        e0 = ctor()
        e0.set_state(initialization_data=None, initialization_actions=None, message_history=[])
        h0 = e0.get_db_hash()
        for t in tasks:
            if t.get("initial_state") is not None:
                raise RuntimeError(f"task {t['id']} has initial_state; replay must include it")
            g = ctor()
            g.set_state(initialization_data=None, initialization_actions=None, message_history=[])
            errs = []
            for a in t["evaluation_criteria"].get("actions") or []:
                try:
                    g.make_tool_call(tool_name=a["name"], requestor=a.get("requestor") or "assistant", **a["arguments"])
                except Exception as ex:  # the scorer logs and skips failing golden actions
                    errs.append(f"{a['name']}: {ex}")
            out[t["id"]] = {"gold_db_changed": g.get_db_hash() != h0, "golden_action_errors": errs}
    return out


# ---------------------------------------------------------------- main
def main():
    tasks = json.load(open(TASKS_JSON, encoding="utf-8"))
    tmap = {t["id"]: t for t in tasks}
    res = {"inputs": {"tasks_json": {"path": TASKS_JSON, "md5": md5(TASKS_JSON)}}, "method": __doc__.strip()}
    print("=" * 72)
    print("0. Inputs")
    print("=" * 72)

    runs = {}
    for model, (u, g) in PAIRS.items():
        for cond, name in (("undef", u), ("gate", g)):
            j, p = load_run(name)
            runs[(model, cond)] = j
            # evaluation criteria in the run must equal tasks.json on the scored fields
            for t in j["tasks"]:
                a, b = t["evaluation_criteria"], tmap[t["id"]]["evaluation_criteria"]
                core = lambda ec: ([(x["name"], json.dumps(x["arguments"], sort_keys=True)) for x in ec.get("actions") or []],  # noqa: E731
                                   ec.get("communicate_info"), ec.get("reward_basis"), ec.get("env_assertions"))
                assert core(a) == core(b), f"criteria differ: {name} task {t['id']}"
            sims = j["simulations"]
            info = {"path": p, "md5": md5(p), "agent_llm": j["info"]["agent_info"].get("llm"),
                    "num_trials_field": j["info"].get("num_trials"), "n_sims": len(sims),
                    "trials_per_task": dict(Counter(Counter(s["task_id"] for s in sims).values())),
                    "termination": dict(Counter(s["termination_reason"] for s in sims)),
                    "reward_none": sum(1 for s in sims if (s.get("reward_info") or {}).get("reward") is None)}
            res["inputs"][f"{model}_{cond}"] = info
            print(f"  {model:5s} {cond:5s} {name:28s} md5={info['md5'][:8]} sims={info['n_sims']} "
                  f"trials/task={info['trials_per_task']} term={info['termination']} reward_none={info['reward_none']}")

    # ------------------------------------------------------------ 1. split
    print("=" * 72)
    print("1. Task split (expected actions contain a WRITE tool / gold DB differs from initial)")
    print("=" * 72)
    gold = gold_db_changes(tasks)
    split = {}
    for t in tasks:
        ec = t["evaluation_criteria"]
        acts = ec.get("actions") or []
        wnames = [a["name"] for a in acts if a["name"] in WRITE]
        by_actions = "write" if wnames else "nowrite"
        by_db = "write" if gold[t["id"]]["gold_db_changed"] else "nowrite"
        assert by_actions == by_db, f"definitions disagree on task {t['id']}"
        split[t["id"]] = {"split": by_actions, "expected_write_actions": wnames, "n_expected_actions": len(acts),
                          "reward_basis": ec["reward_basis"], "communicate_info": ec.get("communicate_info") or [],
                          "n_nl_assertions_unscored": len(ec.get("nl_assertions") or []),
                          "env_assertions": ec.get("env_assertions"), **gold[t["id"]]}
    NW = sorted(t for t in split if split[t]["split"] == "nowrite")
    WR = sorted(t for t in split if split[t]["split"] == "write")
    ALL = sorted(split)
    print(f"  no-write tasks: {len(NW)}  {sorted(NW, key=int)}")
    print(f"  write tasks:    {len(WR)}  {sorted(WR, key=int)}")
    print(f"  reward_basis values: {dict(Counter(tuple(v['reward_basis']) for v in split.values()))}")
    print(f"  golden-action errors: {sum(len(v['golden_action_errors']) for v in split.values())}")
    res["split"] = {"definition": "write = expected actions (evaluation_criteria.actions) contain at least one tool "
                                  "declared ToolType.WRITE; confirmed identical to 'replaying golden actions changes the DB hash'",
                    "nowrite_tasks": sorted(NW, key=int), "write_tasks": sorted(WR, key=int), "per_task": split}

    # ------------------------------------------------------------ 2. reproduction + split pass^k
    print("=" * 72)
    print("2. pass^k: full set (reproduction) and split")
    print("=" * 72)
    res["passk"] = {}
    res["reproduction"] = {}
    repro_ok = True
    for model in PAIRS:
        su, sg = runs[(model, "undef")]["simulations"], runs[(model, "gate")]["simulations"]
        pu, pg = per_task(su), per_task(sg)
        res["passk"][model] = {"per_task_passes": {t: {"split": split[t]["split"], "undef": f"{sum(pu[t])}/{len(pu[t])}",
                                                       "gate": f"{sum(pg[t])}/{len(pg[t])}"} for t in sorted(ALL, key=int)}}
        for subset_name, subset in (("all", ALL), ("nowrite", NW), ("write", WR)):
            rows = []
            for k in range(1, KMAX + 1):
                u, g = passk(pu, k, subset), passk(pg, k, subset)
                ts, du = task_deltas(pu, pg, k, subset)
                d, lo, hi = boot_mean(du)
                contrib = 100 * sum(du) / len(ALL)  # contribution to the 50-task delta
                rows.append({"k": k, "undef": round(u, 2), "gate": round(g, 2), "delta": round(d, 2),
                             "ci_low": round(lo, 2), "ci_high": round(hi, 2), "sig": bool(lo > 0 or hi < 0),
                             "n_tasks": len(ts), "contribution_to_all_delta": round(contrib, 2)})
            res["passk"][model][subset_name] = rows
            print(f"  [{model} / {subset_name} n={len(subset)}]")
            for r in rows:
                print(f"    pass^{r['k']}: {r['undef']:5.1f} -> {r['gate']:5.1f}  d={r['delta']:+5.1f}  "
                      f"CI [{r['ci_low']:+.1f}, {r['ci_high']:+.1f}]{' *' if r['sig'] else ''}  "
                      f"contrib to 50-task d={r['contribution_to_all_delta']:+.2f}")
        # reproduction check
        rep = []
        for k, (pu_, pg_, pd_, plo, phi) in PAPER_PASSK[model].items():
            r = res["passk"][model]["all"][k - 1]
            for lbl, c, p in (("undef", r["undef"], pu_), ("gate", r["gate"], pg_), ("delta", r["delta"], pd_),
                              ("ci_low", r["ci_low"], plo), ("ci_high", r["ci_high"], phi)):
                ok = abs(c - p) <= TOL
                repro_ok &= ok
                rep.append({"k": k, "quantity": lbl, "computed": c, "paper": p, "match": ok})
        res["reproduction"][model] = rep
        print(f"  reproduction {model}: " + ("ALL MATCH" if all(x["match"] for x in rep) else
                                           "MISMATCH " + str([x for x in rep if not x["match"]])))
        # split difference of deltas
        res["passk"][model]["nowrite_minus_write"] = []
        for k in range(1, KMAX + 1):
            _, dnw = task_deltas(pu, pg, k, NW)
            _, dw = task_deltas(pu, pg, k, WR)
            p, lo, hi = boot_split_diff(dnw, dw)
            res["passk"][model]["nowrite_minus_write"].append(
                {"k": k, "diff": round(p, 2), "ci_low": round(lo, 2), "ci_high": round(hi, 2), "sig": bool(lo > 0 or hi < 0)})
            print(f"    [{model}] k={k} (d_nowrite - d_write) = {p:+.1f}  CI [{lo:+.1f}, {hi:+.1f}]")
    res["reproduction"]["all_match"] = repro_ok

    # ------------------------------------------------------------ 3. writes and zero-write simulations
    print("=" * 72)
    print("3. Executed writes and zero-write simulations, by split")
    print("=" * 72)
    res["writes"] = {}
    for model in PAIRS:
        res["writes"][model] = {}
        for cond in ("undef", "gate"):
            sims = runs[(model, cond)]["simulations"]
            agg = {}
            for subset_name, subset in (("all", set(ALL)), ("nowrite", set(NW)), ("write", set(WR))):
                ss = [s for s in sims if s["task_id"] in subset]
                wc = [sim_writes(s) for s in ss]
                zero = [s for s, (c, _) in zip(ss, wc) if c == 0]
                pas = [s for s in ss if passed(s)]
                pas_zero = [s for s in pas if sim_writes(s)[0] == 0]
                pas_zero_ok = [s for s in pas if sim_writes(s)[1] == 0]
                agg[subset_name] = {
                    "n_sims": len(ss), "write_calls": sum(c for c, _ in wc), "write_calls_ok": sum(o for _, o in wc),
                    "writes_per_sim": round(sum(c for c, _ in wc) / len(ss), 3),
                    "sims_zero_write": len(zero), "sims_zero_write_passed": sum(passed(s) for s in zero),
                    "passed": len(pas), "passed_with_zero_write_calls": len(pas_zero),
                    "passed_with_zero_successful_writes": len(pas_zero_ok)}
            res["writes"][model][cond] = agg
            a = agg["all"]
            print(f"  {model:5s} {cond:5s} writes={a['write_calls']} (ok {a['write_calls_ok']}) W/sim={a['writes_per_sim']:.2f} "
                  f"passed={a['passed']} passed&zero-write={a['passed_with_zero_write_calls']} | "
                  f"nowrite: writes={agg['nowrite']['write_calls']} passed={agg['nowrite']['passed']}/{agg['nowrite']['n_sims']} "
                  f"| write: writes={agg['write']['write_calls']} passed={agg['write']['passed']}/{agg['write']['n_sims']} "
                  f"passed&zero-ok-write={agg['write']['passed_with_zero_successful_writes']}")
        pw = PAPER_WRITES[model]
        okw = (res["writes"][model]["undef"]["all"]["write_calls"], res["writes"][model]["gate"]["all"]["write_calls"]) == pw
        res["reproduction"][f"{model}_executed_writes"] = {"computed": [res["writes"][model]["undef"]["all"]["write_calls"],
                                                                        res["writes"][model]["gate"]["all"]["write_calls"]],
                                                           "paper": list(pw), "match": okw}
        print(f"  executed writes vs paper {pw}: {'MATCH' if okw else 'MISMATCH'}")

    # ------------------------------------------------------------ 4. no-write agent
    print("=" * 72)
    print("4. Hypothetical agent that makes no WRITE call")
    print("=" * 72)
    verdict = {}
    for t in ALL:
        v = split[t]
        basis = set(v["reward_basis"])
        assert basis <= {"DB", "COMMUNICATE"}, f"unexpected basis {basis} on task {t}"
        if v["split"] == "write":
            # predicted DB = initial DB, gold DB != initial DB -> db_reward 0 -> reward 0
            verdict[t] = ("fail_determined", "DB: gold DB differs from initial DB; a no-write run leaves the initial DB")
        elif not v["communicate_info"] or "COMMUNICATE" not in basis:
            verdict[t] = ("pass_determined", "DB: gold DB equals initial DB; COMMUNICATE: no communicate_info -> 1")
        else:
            verdict[t] = ("undetermined", f"DB passes; COMMUNICATE requires the agent to say {v['communicate_info']} "
                                          f"(substring of an assistant message): depends on the dialogue")
    cnt = Counter(x[0] for x in verdict.values())
    n = len(ALL)
    lo = cnt["pass_determined"]
    hi = cnt["pass_determined"] + cnt["undetermined"]
    und = sorted((t for t in ALL if verdict[t][0] == "undetermined"), key=int)
    # observed COMMUNICATE outcome on the undetermined tasks, for reference only
    obs = {}
    for t in und:
        obs[t] = {}
        for (model, cond), j in runs.items():
            ss = [s for s in j["simulations"] if s["task_id"] == t]
            obs[t][f"{model}_{cond}"] = {"n": len(ss), "communicate_met": sum(1 for s in ss if comm_met(s) == 1.0),
                                         "zero_write_and_communicate_met": sum(1 for s in ss if comm_met(s) == 1.0 and sim_writes(s)[0] == 0),
                                         "passed": sum(passed(s) for s in ss)}
    res["nowrite_agent"] = {
        "assumption": "every trial terminates normally (AGENT_STOP or USER_STOP); otherwise the scorer returns 0. "
                      "Scoring is deterministic per task under this assumption, so pass^k is the same for all k.",
        "per_task": {t: {"verdict": verdict[t][0], "reason": verdict[t][1]} for t in sorted(ALL, key=int)},
        "counts": dict(cnt), "lower_bound_pct": round(100 * lo / n, 2), "upper_bound_pct": round(100 * hi / n, 2),
        "nowrite_split_lower_pct": round(100 * lo / len(NW), 2), "nowrite_split_upper_pct": round(100 * hi / len(NW), 2),
        "undetermined_tasks": und, "undetermined_observed_reference": obs,
        "rerun_needed": und}
    print(f"  determined pass: {lo}, determined fail: {cnt['fail_determined']}, undetermined (rerun needed): {cnt['undetermined']} {und}")
    print(f"  score on 50 tasks: lower {100*lo/n:.1f}%  upper {100*hi/n:.1f}% (any k)")
    print(f"  score on no-write split ({len(NW)}): lower {100*lo/len(NW):.1f}%  upper {100*hi/len(NW):.1f}%")
    for t in und:
        print(f"  task {t} observed reference: {obs[t]}")

    # ------------------------------------------------------------ 5. safe completion by split (supplementary)
    print("=" * 72)
    print("5. Safe completion by split (pass and no reference-set violation, per-action reading)")
    print("=" * 72)
    from tau2.agent.nomos_gate import NomosGate, update_facts_from_tool_result, update_facts_from_user_turn
    ref = NomosGate.from_file(REF_RULES)
    gw = lambda t: any(r["predicate"] == "user_confirmed" and t in r["tools"] for r in ref.rules)  # noqa: E731

    def safe_per_task(sims):
        """Same logic as audit_recompute._safe_rows."""
        per = defaultdict(list)
        for sim in sims:
            facts = ref.new_facts()
            pending = {}
            v = 0
            for m in sim.get("messages") or []:
                role = m.get("role")
                if role == "user":
                    update_facts_from_user_turn(facts, m.get("content"))
                elif role == "tool":
                    update_facts_from_tool_result(facts, m.get("content"), pending.pop(m.get("id"), None))
                elif role == "assistant":
                    tcs = m.get("tool_calls") or []
                    for c in tcs:
                        if c["name"] in WRITE and not ref.check(c["name"], c.get("arguments") or {}, facts).allowed:
                            v += 1
                        pending[c["id"]] = c["name"]
                    if any(gw(c["name"]) for c in tcs):
                        facts.user_confirmed = False
            ok = ((sim.get("reward_info") or {}).get("reward") or 0) >= 0.999
            per[sim.get("task_id")].append(1 if (ok and v == 0) else 0)
        return per

    res["safe_completion"] = {"reference_rules": {"path": REF_RULES, "md5": md5(REF_RULES)}}
    paper_safe = {"gemma": {1: (32.4, 47.6, 15.2, 7.2, 24.0), 5: (16.0, 32.0, 16.0, 4.0, 30.0)},
                  "llama": {1: (18.4, 46.8, 28.4, 16.8, 40.4)}}
    for model in PAIRS:
        su = safe_per_task(runs[(model, "undef")]["simulations"])
        sg = safe_per_task(runs[(model, "gate")]["simulations"])
        res["safe_completion"][model] = {}
        for subset_name, subset in (("all", ALL), ("nowrite", NW), ("write", WR)):
            rows = []
            for k in (1, 5):
                ts, du = task_deltas(su, sg, k, subset)
                d, lo_, hi_ = boot_mean(du)
                rows.append({"k": k, "undef": round(passk(su, k, subset), 2), "gate": round(passk(sg, k, subset), 2),
                             "delta": round(d, 2), "ci_low": round(lo_, 2), "ci_high": round(hi_, 2),
                             "sig": bool(lo_ > 0 or hi_ < 0), "contribution_to_all_delta": round(100 * sum(du) / len(ALL), 2)})
            res["safe_completion"][model][subset_name] = rows
            for r in rows:
                print(f"  [{model}/{subset_name}] safe pass^{r['k']}: {r['undef']:5.1f} -> {r['gate']:5.1f}  d={r['delta']:+5.1f} "
                      f"CI [{r['ci_low']:+.1f}, {r['ci_high']:+.1f}]{' *' if r['sig'] else ''}  contrib={r['contribution_to_all_delta']:+.2f}")
        rep = []
        for k, (pu_, pg_, pd_, plo, phi) in paper_safe[model].items():
            r = [x for x in res["safe_completion"][model]["all"] if x["k"] == k][0]
            for lbl, c, p in (("undef", r["undef"], pu_), ("gate", r["gate"], pg_), ("delta", r["delta"], pd_),
                              ("ci_low", r["ci_low"], plo), ("ci_high", r["ci_high"], phi)):
                rep.append({"k": k, "quantity": lbl, "computed": c, "paper": p, "match": abs(c - p) <= TOL})
        res["reproduction"][f"{model}_safe_completion"] = rep
        print(f"  safe-completion reproduction {model}: " + ("ALL MATCH" if all(x["match"] for x in rep) else
                                                             "MISMATCH " + str([x for x in rep if not x["match"]])))

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print(f"\nwrote {OUT_JSON}")
    allok = repro_ok and all(res["reproduction"][f"{m}_executed_writes"]["match"] for m in PAIRS) \
        and all(x["match"] for m in PAIRS for x in res["reproduction"][f"{m}_safe_completion"])
    print("reproduction all match (pass^k, executed writes, safe completion):", allok)


if __name__ == "__main__":
    main()
