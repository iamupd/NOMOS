# NOMOS Artifacts

Reproduction artifacts for the paper **"NOMOS: Statically Verified Policy Gates
for Tool-Using LLM Agents"** (Yu, Kim, and Choi).

This package lets a reader verify the paper's numbers without the proprietary
implementation. It contains the authored policies, every compiled rule set (the
same rules printed verbatim in the paper's appendices), the hand-written
*independent* compliance checker used to measure violation rates, the
compilation reports, representative gate block logs, aggregate results, and
analysis scripts.

## What is *not* here (and why)

The compiler's LLM extraction prompt and the runtime gate implementation are
proprietary and are withheld. They are not needed to check the results: every
enforced rule is given in full (see `rules/`), the predicate semantics are
specified in Section III of the paper, and every enforced rule appears verbatim
in Appendices A and C. A reader can therefore re-implement the gate from the
paper and these artifacts, and reproduce the reported rule sets and violation
rates. Raw simulation transcripts are omitted for size and are available from
the corresponding author on reasonable request.

## Layout

```
policies/            Authored one-page security policies (banking, slack, travel, workspace)
rules/
  agentdojo/         Compiled rule sets for the four AgentDojo suites
  tau2/              Compiled rule sets for tau2-bench (auto = enforced,
                     manual = the independent checker, frontier = frontier-model compile)
  compile_reports/   Per-domain repair/reject counts (the basis for Table I / III-C)
logs/                Gate block logs (which tool call was refused by which rule)
results/             Aggregate results as CSV (ASR, benign utility, pass^k, model invariance)
scripts/             Analysis scripts (rule counts, block attribution)
```

Each rule is a tuple `(id, type, tools, predicate, policy_clause)`. `type` is
`forbidden` (blocks a guarded call while the predicate holds) or `precondition`
(blocks until it holds). `policy_clause` is the sentence of the source policy the
rule was compiled from.

## Reproducing key numbers

**Rule counts (paper Appendices A, C).**
```
python scripts/rule_stats.py rules/agentdojo/banking_rules.json
# -> domain: banking_v2 | rules: 3 (forbidden=2, precondition=1)
python scripts/rule_stats.py rules/tau2/airline_rules.auto.json
# -> rules: 10
```

**Block attribution (paper Section V-A).**
```
python scripts/block_log_stats.py logs/tau2_airline_gate.jsonl
# -> 226 blocked calls; AIR-AUTO-PRE-10 (user confirmation) = 60.6%,
#    AIR-AUTO-FBD-04 (cancellation eligibility) = 22.6%
```

**Static verification counts (paper Table I).**
See `rules/compile_reports/tau2_airline.report.json` and
`rules/compile_reports/tau2_retail.report.json`: `candidates`, `emitted`, and
the per-check drop/repair counts match the table.

**Violation rate independence (paper Section IV).**
`rules/tau2/*_rules.manual.json` is the hand-written reference rule set used to
*measure* violation rates. It was never used for enforcement; the enforced set
is `*_rules.auto.json`. Replaying the manual set over recorded transcripts is
what yields the 13.1% -> 0.2% (airline) and 5.0% -> 0.6% (retail) figures,
breaking the circularity of scoring a gate with its own rules.

**Headline results (paper Tables IV-VIII).**
`results/agentdojo_summary.csv` and `results/tau2_passk.csv` hold the aggregate
ASR, benign-utility, and pass^k values, including the model-invariance rows
(gemma-4-26B and Llama-3.3-70B).

## Environment

All experiments use public, unmodified benchmarks: tau2-bench at commit
`363133a` and all four AgentDojo suites, with their own scorers. The agent and
user simulator are single open-weight models (gemma-4-26B-A4B and, for the
model-invariance check, Llama-3.3-70B-Instruct).

## License

See `LICENSE`. Rule sets, policies, reports, logs, and results are released
under CC BY 4.0; the analysis scripts under the MIT License.

## Citation

See `CITATION.cff`.
