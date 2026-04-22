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

## G10 — Section Completeness
Each paper section must meet structural and content completeness criteria:

**Abstract**: single paragraph, 150–250 words, no citations, no future work.
**Introduction**: must have Background + Motivation + Contributions subsections.
**Related Work**: each subsection cites 4–10 papers; related-work discussion ≥ 2× length of contribution statement; no equations; no section-level introductory paragraph.
**Methodology**: section title = proposed method name (not "Method"/"Methodology"); every important module has at least one display-math equation.
**Experiments**: all numbers traceable to `results.json`; at least one ablation for MAIN-stage papers.
**Conclusion**: no claims beyond §4 experimental scope (mirrors G8 but checks the section structurally).

Owner: `paper_auditor` (structural check), `paper_writer` (enforce during writing).

## G11 — Numerical Cross-Section Consistency
Every metric value that appears in multiple places (Abstract, tables, body text,
figure captions) must be identical or differ only by documented rounding. A value
reported as "56.9%" in the Abstract and "0.568" in Table 1 is a FAIL unless
the rounding convention is explicit. Cross-references between text and tables
(e.g., "as shown in Table 1") must point to the correct row/column.

Owner: `paper_auditor`.

## G12 — Citation Utilization & Verification
(a) **Utilization rate**: at least 85% of papers listed in `evidence_refs` must
be actively cited (`\cite{}`-d) in the draft. Shallow related work that ignores
available evidence is a WARN below 85%, FAIL below 70%.
(b) **Multi-layer verification**: each `\cite{key}` is checked via:
  1. `references.bib` existence (existing A6 check)
  2. Semantic Scholar API title fuzzy-match (Levenshtein ≥ 0.7)
  3. DOI cross-reference via CrossRef (when DOI is in bib entry)
  4. LLM relevance scoring — is the citation contextually appropriate?
Layers 2–4 are advisory (WARN) for now; Layer 1 remains BLOCK.

Owners: `paper_auditor` (gatekeeper), `paper_writer` (enforce A6 during writing).

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
