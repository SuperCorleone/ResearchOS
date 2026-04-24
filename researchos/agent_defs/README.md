# Agent Definitions

Platform-neutral home for agent prompt definitions (`<name>.md`). Every
runtime adapter (OpenHarness / Skill / SDK / MCP) reads from this single
source — the old location `.openharness/plugins/researchos/agents/` has
been retired.

Dispatcher search order:

1. `researchos/agent_defs/<name>.md` (primary)
2. `.openharness/plugins/researchos/agents/<name>.md` (legacy fallback,
   for external clones that haven't migrated)

## Inventory

Core agents (10 + 2 conditional per REFACTOR_PLAN.md target):

- Pipeline: `hypothesis_gen`, `exp_designer`, `coder`, `experiment_runner`,
  `judge`, `paper_writer`, `paper_auditor`, `professor`, `knowledge_miner`,
  `reviewer_simulator`
- Concepts (2-pass): `concept_extract_paper`, `concept_extract_code`
- Conditional: `vlm_reviewer`, `figure_composer`
- Still present, scheduled for removal: `concept_extractor` (legacy),
  `critic`, `experiment_analyser`, `interrogator`, `reporter`,
  `design_reviewer` — replaced by judge + publisher + exp_designer
  self-review

## Rules for agents defined here

1. Each file ≤ 150 lines (hard cap)
2. No version codes (A1/B2/C3/U4/R5) in the body — those belong in CHANGELOG.md
3. Shared prose lives in `shared/` and is referenced, not duplicated
4. Typed contract fields (from `researchos/schemas/digest.py`) are cited
   verbatim, not paraphrased
