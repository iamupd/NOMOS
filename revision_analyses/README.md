# Analyses added in the November 2026 revision

Each folder holds the output of one analysis added while revising the paper for resubmission to IEEE Access.
Local file paths are replaced by placeholders (`<paper>`, `<tau2-bench>`, `<agentdojo>`, `<repo>`). Every
analysis reads recorded runs of the public benchmarks; raw simulation transcripts are not in this package
(available from the corresponding author on reasonable request). Each JSON output has a `reproduction` or
`meta` block recording its inputs and the values quoted in the paper.

| Folder | Paper items | Files | Script |
|---|---|---|---|
| `airline_split/` | Table 10 (airline task success split by whether the expected final state requires a write, both agent models), the no-write agent's score (46.0-48.0%) and the write counts of Section V-B | `results.json` | `t03_airline_split.py` (reference copy) |
| `preflight_intervals/` | exact 95% intervals in Table 14, the confirmation-consumption counts of Section V-F, the threshold sensitivity of Table 26 and the list of few-call bindings in Appendix E | `results.json` | withheld: it runs the behavioral preflight |
| `preflight_seeding/` | the seeded-defect test of Appendix E (Table 27) and its summary in Sections III-D and V-F | `results.json` (per-mutant static-check detail omitted; each mutant's static verdict is kept), `inputs_md5.txt` | withheld: it runs the behavioral preflight |
| `zero_count_bounds/` | exact two-sided 95% Clopper-Pearson upper bounds of every zero-count cell (Section IV "Zero counts", captions of Tables 16, 18, 20 and 21, Section V-H, contribution 6) | `results.csv` | `zero_asr_ci.py` (reference copy) |
| `llama_banking_pairs/` | the composition of the Llama-3.3-70B banking cells (records, unscored pairs and their treatment; caption of Table 18, Section V-J) | `results.json` | `llama_banking_pairs.py` (reference copy) |
| `retail_firing/` | not reported in the paper: the split of tasks by whether the gate fires and the retail ordering check, examined during the revision | `results.json` | `t22_firing_retail.py` (reference copy) |
