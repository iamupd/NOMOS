# NOMOS Artifacts (v1.1)

Reproduction artifacts for the paper **"NOMOS: Compiling Written Policies into Statically Verified Tool-Call Gates for LLM Agents"** (Yu, Kim, and Choi).

This package lets a reader check the paper's numbers without the proprietary
implementation. It contains the authored policies (both versions of the banking
policy), every rule set the gate enforces in the reported experiments, the
hand-written *independent* compliance checker used to measure violation rates,
the compilation reports, representative gate block logs, the checker-audit and
refusal-audit sheets, the run manifest and audit report that map every headline
number to its run, aggregate results, and analysis scripts.

Version history: v1.0 (2026-08-27) accompanied an earlier draft and is
superseded; v1.1 corresponds to the manuscript revised in October 2026. Use the
concept DOI 10.5281/zenodo.22123419 to resolve the latest version.

## What is *not* here (and why)

The implementation is proprietary and is withheld: the compiler (including its
LLM extraction prompt, the vocabulary's `Enable` and `Reads` tables and the
AgentDojo port's predicate-generalization table), the
behavioral preflight, and the runtime gate with its predicate evaluators. A
reader can inspect every enforced rule (`rules/`, and Appendices A and C of the
paper), the checks applied to it, and the block logs, but cannot re-run the
compiler or reproduce the gate's decisions without the source. Raw simulation
transcripts are omitted for size and are available from the corresponding
author on reasonable request.

## Layout

```
policies/            Authored one-page security policies (banking, slack, travel, workspace);
                     banking.md is the evaluated (revised) version, banking_v1.md the first version
rules/
  agentdojo/         Rule sets for the four AgentDojo suites, all compiled by the compiler
                     (<suite>_rules.json; slack, travel and workspace with the extended
                     vocabulary and the accessor-activation check), used by both agent models;
                     sensitivity/ holds the other compiled sets of the compile-sensitivity
                     table (default vocabulary; extended vocabulary without the check)
  tau2/              Rule sets for tau2-bench (auto = compiled and enforced,
                     manual = the independent checker: 16 airline and 11 retail rules,
                     frontier = frontier-model compile)
  compile_reports/   Per-domain repair/reject counts (the basis for the static-verification table)
logs/                Gate block logs (which tool call was refused by which rule)
results/             Aggregate results (CSV), AUDIT_REPORT.txt, RUN_MANIFEST.md,
                     checker-audit and refusal-audit sheets (JSON, HTML)
scripts/             Analysis scripts (audit recomputation, rule counts, block attribution)
```

Each rule is a tuple `(id, type, tools, predicate, policy_clause)`. `type` is
`forbidden` (blocks a guarded call while the predicate holds) or `precondition`
(blocks until it holds). `policy_clause` is the sentence of the source policy the
rule was compiled from.

## Provenance and audit

`results/RUN_MANIFEST.md` maps every headline number in the paper to the exact
canonical run it comes from, including the claims re-verified in the October
2026 revision. `scripts/audit_recompute.py` recomputes the numbers from those
runs and cross-checks each against the paper; its output is
`results/AUDIT_REPORT.txt` (every value reproduces, `FAILS: none`). The script
reads the raw simulation logs, which are not included here; the path constants
at the top of the script point to their location.

## Reproducing key numbers

**Rule counts (paper Appendices A, C).**
```
python scripts/rule_stats.py rules/agentdojo/banking_rules.json
# -> domain: banking_v2 | rules: 3 (forbidden=2, precondition=1)
python scripts/rule_stats.py rules/tau2/airline_rules.auto.json
# -> rules: 10
```

**Compiled AgentDojo sets (paper Section V-K, Appendix C).**
`rules/agentdojo/{slack,travel,workspace}_rules.json` are byte-identical to the
files the evaluated runs loaded: their sha256 equals the `rules_sha256` recorded
in each run cell's manifest, which `scripts/audit_recompute.py` checks. Each
compiled rule carries its source `policy_clause`; `source_policy` holds the local
path the compiler read, and the policy text is `policies/<suite>.md`.
`rules/compile_reports/agentdojo_<suite>.report.json` records the one
accessor-activation retarget per suite (`repaired_inactive_accessor`);
`*_extended_nocheck.report.json` and `agentdojo_slack_default_vocab.report.json`
belong to the sensitivity sets.

**Block attribution (paper Section V-A).**
```
python scripts/block_log_stats.py logs/tau2_airline_gate.jsonl
# -> 226 blocked calls; AIR-AUTO-PRE-10 (user confirmation) = 60.6%,
#    AIR-AUTO-FBD-04 (cancellation eligibility) = 22.6%
```

**Static verification counts (paper static-verification table).**
See `rules/compile_reports/tau2_airline.report.json` and
`rules/compile_reports/tau2_retail.report.json`. Counted per candidate, the
structural defects are 29 of 79 (airline) and 7 of 53 (retail); with the
stability filter 32 of 79 and 8 of 53; no candidate is rejected as a
hallucinated tool reference. Note that the compiler logs a candidate emptied by
the argument check a second time under "no known tool in [<tool>]"; those
records are the same rejection, not hallucinations (`scripts/audit_recompute.py`
de-duplicates them).

**Violation rates (paper Section V-A).**
`rules/tau2/*_rules.manual.json` is the hand-written reference rule set used to
*measure* violation rates. It was never used for the enforced condition; the
enforced set is `*_rules.auto.json`. Replaying the reference set over recorded
transcripts, counting calls to the tools the benchmark declares WRITE, yields
66.3% -> 2.6% (airline, 234/353 -> 3/116) and 30.8% -> 6.9% (retail, 188/611 ->
40/582); under the batch reading of the confirmation clause 36.8% -> 2.6% and
13.6% -> 5.3%.

**Headline results.**
`results/agentdojo_summary.csv` and `results/tau2_passk.csv` hold the aggregate
ASR, benign-utility, and pass^k values, including the second-model rows
(gemma-4-26B and Llama-3.3-70B). In `agentdojo_summary.csv`, condition `gate`
is the compiled set for both models; `gate_compiled_*` rows are the other rule
sets of the paper's compile-sensitivity table. `reportsets of the paper's compile-sensitivity table. `reported_asr_percent` is
AgentDojo's security flag; `executed_asr_percent` counts only flagged pairs in
which a malicious state-changing call actually ran (gated and compile-sensitivity
rows). Injected calls that ran outside the flag (banking payments diverted to a
payee already in the account's history, the Llama workspace deletions) are
listed in `results/RUN_MANIFEST.md`.sults/checker_write_sample.json`, `checker_write_judged.json`,
`checker_write_judged_clauses.json` and `checker_write_classified.json` hold the
write-only audit of the checker: 160 state-changing calls judged by an
independent frontier model (Claude Sonnet 4.5) with untruncated context, raw
agreement 50.0% (kappa -0.01) against the full policy and 51.6% (kappa 0.02,
159 calls) against the encoded clauses, and the mechanical classification of the
77 encoded-clause disagreements. `checker_write_adjudication.html` is the
per-case sheet. `refusal_{sample,judged,report}.json` hold the refusal-precision
audit (109 refusals inside task-passing baseline runs). The earlier 120-verdict
audit (`checker_audit.csv`) had tool results cut at 600 characters and is
superseded.
`results/benign_utility_ci.csv` holds the AgentDojo benign-utility deltas with
task-paired bootstrap CIs (gemma, compiled sets).

**Behavioral preflight (paper Sections III-D and V-F).**
`results/preflight_survey.csv` is the per-(domain, tool) refusal distribution
from replaying each rule set over its canonical undefended baseline with the
gate's confirmation consumption: no airline or retail binding reaches the 0.9
flag line (worst 83.3% and 31.6%), and the pre-repair telecom rule is flagged at
exactly the self-defeating binding (send_payment_request, 47/49 = 95.9%) while
the same predicate's resume_line binding (0/44) is kept.

## Environment

All experiments use public, unmodified benchmarks: tau2-bench at commit
`363133a` and all four AgentDojo suites (benchmark versions are recorded in
`results/RUN_MANIFEST.md`), with their own scorers. The agent and user
simulator are gemma-4-26B-A4B in every experiment except the second-model runs,
in which Llama-3.3-70B-Instruct is the agent.

## License

See `LICENSE`. All materials in this package (rule sets, policies, reports, logs, results,
and analysis scripts) are released under CC BY-NC 4.0 for non-commercial use
with attribution.

## Citation

See `CITATION.cff`.
