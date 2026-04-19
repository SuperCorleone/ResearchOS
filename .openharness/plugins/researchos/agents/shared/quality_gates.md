# ResearchOS Quality Gates (G1–G9) — Authoritative Definitions

This file is the single source of truth for the G1–G9 quality gate definitions.
Agents reference it by name; do NOT inline full re-definitions in agent prompts.
Each agent's `.md` still lists **which** gates it owns and **how** it checks them
operationally, but the gate's meaning lives here.

---

## G1 — Claim–Experiment Consistency
Every key term in a hypothesis MUST be operationalized (measurable definition)
and every testable prediction MUST name a concrete metric + threshold.
A hypothesis that can be confirmed/denied by argument alone fails G1.

Owners: `hypothesis_gen` (produce), `critic` (audit at result time).

## G2 — Causal & Control Validity
Each experiment group changes exactly ONE variable. Baselines and the proposed
method use comparable resources (params, steps, data, compute). The strongest
available baseline must be included or exclusion must be justified. MAIN-stage
experiments must include at least one ablation. `confound_controls` is mandatory.

Owner: `exp_designer`.

## G3 — Implementation Truth (Claim-to-Code)
Every atomic concept in `concepts.yaml` MUST appear as a row in the
CLAIM-TO-CODE MAPPING table at the end of the main script. A concept deliberately
not implemented MUST be written `C_i: SKIPPED because <reason>`. A missing id
is a BLOCK-severity error at preflight. Trivial implementations (constants,
pass-through, empty bodies) are flagged by preflight.

Owners: `coder` (produce mapping), `critic` (result-level plausibility).

## G4 — Numerical Consistency
Every number in the paper draft must be traceable to `results.json`.
Arithmetic (averages, percentages, deltas) must be correct. No duplicate rows,
no copy-paste errors, consistent decimal conventions. `audit_report.md` is
authoritative — other agents cite it rather than recomputing.

Owner: `paper_auditor`.

## G5 — Statistical Reporting Completeness
MAIN-stage comparisons MUST report: effect size, test statistic with df, exact
p-value, sample size (N), and 95% CI (or SE). FAST_FAIL stage may report only
mean ± std and treat the rest as advisory.

Owners: `exp_designer` (plan), `critic` (advisory/blocking by stage),
`paper_auditor` (gatekeeper for paper).

## G6 — Reproducibility
The experiment script MUST: (1) print exact dependency versions at startup,
(2) accept `--seed` and set ALL random sources, (3) emit results as JSON on
the last stdout line, (4) be runnable with `pip install -r requirements.txt &&
python run_experiment.py`, (5) provide a single entry point across stages.

Owner: `coder`.

## G7 — Resource Reporting
Resource costs MUST be tagged `measured` (from actual execution),
`estimated` (projected), or `simulated` (from synthetic benchmarks). Reporter
and designer both distinguish these explicitly.

Owners: `exp_designer` (plan), `reporter` (milestone).

## G8 — Conclusion Boundaries
The Conclusion MUST NOT claim beyond the tested scope (tasks, datasets, model
sizes, metrics). Toy/synthetic results must not be generalized to real-world
without qualification. §Limitations MUST include (a) >=3 concrete failure
conditions and (b) explicit separation of fixable vs. fundamental limitations.

Owners: `paper_writer` (enforce while writing), `reviewer_simulator` (audit).

## G9 — Data Design & Integrity
`data` section is REQUIRED in the plan and includes source, license, split
strategy, seed. Split happens BEFORE any feature engineering or global-stat
computation. `data.source` in code MUST match the plan (no sklearn/torchvision
substitution when plan says file/api). Reference-paper consistency
(`consistency_requirement`) mandates matching dataset/split/preprocessing.

Owners: `exp_designer` (plan), `coder` (implement), `experiment_runner`
(fetch + provenance), `critic` (audit).

---

## Referencing protocol (for agents)

At the top of each `## Quality Gates` section an agent declares:

```
## Quality Gates (this agent owns: G<x>, G<y>)
Definitions: see `shared/quality_gates.md`.
- G<x>: <one-line operational reminder specific to this agent>
- G<y>: <one-line operational reminder>
```

Agent prompts keep their **operational checks** (what the agent must do, how it
writes a check table, which sentinels it emits) inline, but drop the long
definitional prose — it lives here.
