# Changelog

All notable changes to ResearchOS. Dates reflect the refactor cycle, not calendar release dates.

## 2026-04 — Platform decoupling & typed-digest refactor

Scope: kill four concrete failure-mode root causes observed in production
pipelines (semantic dilution, hard-constraint contradictions, verdict whitewashing, empty contracts). Remove HITL gates, compress agent roster, add Pydantic-enforced step contracts.

### Added
- `researchos/schemas/digest.py` — typed StepDigest models (`HypothesisDigest`, `PlanDigest`, `ConceptsDigest`, `RunDigest`, `JudgeVerdict`) with `model_validator` enforcement that rejects background-noun concepts, mis-aligned metrics, and empty-contract verdicts at schema level.
- `researchos/core/contract_guard.py` — cross-digest invariant guards (`assert_concept_atomicity`, `assert_metric_alignment`, `assert_data_manifest_match`, `assert_baseline_honesty`) wired into `state_manager.save_pipeline()`; violation → `ContractViolation` → pipeline BLOCK.
- `researchos/core/publisher.py::render_milestone` — deterministic `milestone_N.md` renderer from typed digests (zero-LLM, replaces the old `reporter` agent).
- `researchos/agent_runtime/base.py` — `AgentRuntime` Protocol + `AgentContext` / `AgentResult` dataclasses; host-neutral agent execution contract.
- `researchos/agent_runtime/tools.py` — `gen_code_tree_structure`, `grep_code`, `list_file_symbols` for the judge's multi-file verification.
- `researchos/agent_defs/judge.md` — merges the former critic + experiment_analyser + interrogator into a single typed verdict. Digest-first read protocol.
- `researchos/agent_defs/concept_extract_paper.md` + `concept_extract_code.md` — 2-pass concept extraction; intermediate handoff via `concepts_paper.digest.json`.
- `adapters/openharness/` + `adapters/mcp/` + `adapters/skill/` — platform adapter skeletons.

### Changed
- Agent location moved from `.openharness/plugins/researchos/agents/` to `researchos/agent_defs/` (dispatcher searches both paths for backward compatibility).
- `reviewer_simulator.md` trimmed 296 → 121 lines — single-round review, removed ensemble/Rebuttal/Area Chair; preserved NeurIPS JSON schema + `model_override`.
- `coder.md` 258 → 104, `paper_writer.md` 294 → 131, `concept_extractor.md` 257 → 108. Gate code definitions moved out of bodies into `shared/quality_gates.md`.
- `exp_designer.md` gained a Self-Review section covering the scientific validity checks formerly owned by `design_reviewer`.
- `dispatcher._build_step_prompt` inlines upstream `<step>.digest.json` payloads (typed ~2KB) instead of filenames the agent had to `file_read`.
- Paper sections in `paper_writer.md` are conditional on `JudgeVerdict` optional fields — no templated filler when `ablation_notes` / `sensitivity_notes` / `visualization_plan` are null.

### Removed
- HITL gates: `WAITING_APPROVAL`, `PRE_PUBLISH_GATE`, `pre_main_review`, `user_approve/` staging, `CanonicalAction` enum.
- Agents: `research_briefer`, `pre_main_reviewer`, `pipeline_manager`, `branch_selector`, `lit_scout`, `fixer`, `reporter`, `design_reviewer`. (critic / experiment_analyser / interrogator remain for backward compatibility but are superseded by `judge`.)
- Pipeline steps: `DESIGN_REVIEW` (scientific review now runs inside `exp_designer`'s Self-Review pass; structural checks remain in `design_checks.py`, zero-token).
- `researchos/runtime/hitl_synthesizers.py`.
- `scripts/run_research.py` subcommands `review` / `continue` / branch selection.

### Tests
- 224 baseline → 228 passing after refactor (all 5 pre-existing prose-drift failures resolved).

---

## v4.5 — Review & memory upgrades

Borrowed from AgentReview, ARIS, Imbad0202 academic-research-skills, AutoResearchClaw MetaClaw.

- **Cross-model adversarial review** — `reviewer_simulator` `review.model_override` knob lets the adversarial reviewer use a different model family than the paper author, killing same-model self-audit blindspots.
- **Anti-sycophancy concession protocol** — reviewers must emit `concession_score` (1–5) before raising a score; concessions < 4 rejected; overall concession rate > 40 % triggers `sycophancy_warning`; overall score jump > 2 across reflection rounds triggers frame-lock rollback.
- **Author rebuttal simulation** — `paper_writer` generates structured rebuttal between independent reviews and Area Chair aggregation; reviewers may raise (not lower) scores post-rebuttal; Area Chair aggregates post-rebuttal. Artifacts: `rebuttal_v<N>.md`, `review_v<N>_r<i>_post_rebuttal.json`.
- **Failure → self-evolving skill injection** — `findings_memory.py` gained 8th memory kind `failures`; `FailureCard` model with `failure_mode` / `remediation` / `decay_days`; Critic UNSUPPORTED verdicts auto-store; 30-day time decay on retrieval; dispatcher injects active cards into the next pipeline as avoidance guidance.
- **Figure composition agent** — new `figure_composer.md`: Plan → CodeGen (matplotlib / seaborn, colorblind-friendly, DPI 300) → Render (`execute_python` retries ≤ 3) → Critic (`vlm_reviewer` ≤ 2 revision rounds) → Integrate (`figure_manifest.yaml` with LaTeX captions). Non-informative figures demoted to appendix, not deleted. MAIN sequence adds `figure_compose` between experiment_analysis and interrogation.

## v4.4 — Paper quality upgrades

Borrowed from awesome-ai-research-writing, PaperOrchestra, AutoResearchClaw, sciwrite.

- **De-AI-flavor checklist** — 75-word blacklist (Leverage / Delve / Nuanced / Pivotal …) + 6 structural pattern checks (em-dash ≤ 2 / section, no rhetorical filler, no sales pitch, no vague attribution, no three-adjective stacking, no "In recent years" openings). Self-reflection loop adds a fifth step: run de-AI checks after LaTeX error_list passes.
- **Claims-Evidence pre-writing matrix** — `paper_composer.py` gained Step 0: extract every empirical claim from `results.json` + `experiment_analysis.md` and map each to (artifact key path, exact value, target section). Every downstream section MUST reference matrix rows; unmapped claims blocked.
- **Score-gated iterative revision** — Step 5: after checklist passes, score each section 1–10; score < 8 triggers revision then rescore; if revision drops the score, discard the revision. Score ≥ 8 skips revision to save tokens.
- **G11 numerical cross-section consistency** — same metric in Abstract / tables / body / figure captions MUST be identical (modulo documented rounding). Cross-references ("as shown in Table 1") must point at the correct row/column.
- **G12 citation utilization + multi-layer verification** — ≥ 85 % of `evidence_refs` must be `\cite{}`-d (< 70 % = FAIL). 4-layer chain: L1 bib existence (BLOCK), L2 Semantic Scholar fuzzy title match, L3 CrossRef DOI, L4 LLM relevance. L2–L4 advisory.
- **19-class academic chart taxonomy** — `experiment_analyser` visualization proposal upgraded to structured 19-type table (grouped bar / Pareto / radar / heatmap / violin / faceted grid …) with selection rules (main results → Type 1, ablation → Type 5, sensitivity → Type 11). Output: `Chart Type / Title / Axes / Data Source / Rationale` table.

## v4.3 — deep-research adoption

Borrowed from dzhng/deep-research.

- **Pre-research clarification questions** — `clarification.py` generates 3 targeted questions (scope boundaries, metric preference, dataset leaning) before PROPOSAL; `enrich_query_with_answers` merges answers into `research_question`. Artifact: `clarification_qa.yaml`.
- **Structured research goal** — `knowledge_miner` proposals gain `research_goal` field: not only "what to research" but "what to do after finding X + where to go if the hypothesis holds or fails."
- **Follow-up iterative knowledge discovery** — `knowledge_compiler.extract_follow_up_questions` heuristically pulls research questions from paper future-work / limitations sections into wiki frontmatter `follow_up_questions`. Miner prefers seeds from pages carrying this field, forming a `compile → follow_up → mine → compile` loop.
- **Native JSON Schema output** — `supports_json_schema(model)` (returns True for gpt-4o / 5.x / o1 / o3 / o4) and `build_json_schema_param` convert Pydantic models to OpenAI `response_format`. Replaces brittle `extract_json_between_markers` regex. Unsupported models retain the fallback.
- **Proactive token-budget management** — `estimate_tokens` becomes tiktoken-accurate when available; `trim_to_budget` head/tail ratio; `get_context_size(model)` window lookup. Dispatcher trims `_build_step_prompt` output reserving 15 % for the response — preventive rather than reactive.
- **Schema field-level prompt injection** — `AtomicConcept`, `PipelineState`, `ProfessorDecision`, `GateResult` core Pydantic fields gained generator-directive descriptions (e.g. `code_implementation` explicitly requires `<file>::<function>`, rejects natural language). When native JSON Schema is active these become LLM output constraints.
- **Cleanups**: `research_briefer` marked deprecated (production path runs through `briefer.py::render_brief()`). `route_model` now handles `o4` series.

## v4.2 — AI-Researcher deeper adoption

- **Section-level paper quality checklists** — `paper_writer` gained 6 per-section lists: Abstract (150–250 words, no citations, no future work, three-part structure); Introduction (Background 2 paragraphs → Motivation 1–2 → Our Method → Contributions list); Related Work (each subsection 4–10 citations, related work length ≥ 2× contribution statement, no section-level intro paragraph, no equations); Methodology (title = method name, every important module has display math); Experiments (numbers from `results.json`, MAIN must include ablation); Conclusion (no claims beyond experiments).
- **G10 section completeness gate** — `paper_auditor` owns G10; 11-row audit table; MAIN-stage G10 FAIL forces heavy peer-review ensemble.
- **Judge → Coder iteration protocol** — `coder` reads `advisor_verdict.yaml`, modifies only `fail` concepts (leaves `pass` alone), annotates fixes with `# A16-fix iter-N: ...`; 2 consecutive smoke failures emit `# A16-unresolved:`; `iteration_count.txt` drives scheduler `max_iter_times` (default 3).
- **Survey notes merge output** — `concept_extractor` Phase 4 adds `survey_notes.md`: definition → display math → code locator → source papers per concept + Integration Notes (dependencies) + Known Gaps (LOW / MEDIUM / HIGH complexity). `coder.md` lists it as Input, ahead of `kb_retrieve`.

## v4.1 — AI-Researcher mechanism adoption

- **Three-tier model routing** — T1 (gpt-5.4) / T2 (gpt-4o) / T3 (o4-mini) in `workflow_config.yaml::model_tiers`; 21 agents assigned; `get_model_for_agent` routes by tier; `${RESEARCHOS_MODEL_T1:-default}` env overrides supported.
- **Context overflow auto-recovery** — `is_context_overflow(exc)` detects window / token BadRequestError; `truncate_to_tokens` 60/35 head-tail ratio on the last message; retry.
- **Non-function-calling model fallback** — `_supports_function_calling` detection; DeepSeek / some Qwen auto-switch to XML tag tool calls (`<function=name><parameter=key>value</parameter></function>`); schema injected into system prompt.
- **Step-level checkpoint / resume** — `step_cache.py::StepCache` stores results at `pipeline_dir/.step_cache/{stage}__{step}.json`; idempotent replay after crash; `invalidate_from(step)` cascades.
- **Multi-source resource collection** — heartbeat gained GitHub repo search, Crossref venue / DOI resolution, Semantic Scholar 1-hop citation graph; PDF sidecar metadata includes `venue` / `doi` / `github_repos`.
- **Section-by-section paper writer** — `paper_composer.py::PaperComposer` walks sections in dependency order (abstract → introduction → related_work → method → experiments → results → discussion → conclusion); each section runs 4 steps (structure → detailize → fuse → checklist); later sections reference earlier ones to avoid contradictions.
- **Competitive idea selection** — `topic_evaluator.competitive_select` runs LLM tournament rounds over candidates; `refine_idea` sharpens hypothesis / narrows scope / strengthens motivation on the winner. Miner now yields 3–5 diverse candidates.
- **Post-experiment analysis** — `EXPERIMENT_ANALYSIS` step between CRITIC and INTERROGATION; new `experiment_analyser` agent (T2) emits `experiment_analysis.md` (ablation design / sensitivity / visualization plan / statistical significance / failure modes / recommended follow-ups).
- **Tool output truncation** — `MAX_TOOL_OUTPUT_CHARS = 12000`, 60/40 head-tail ratio on `file_read` / `kb_retrieve` / `execute_python`.

## v3.4 — Incremental

- **Innovation type expansion** — added `methodological_extension` and `hybrid_combination` to the `innovation_type` enum. Tournament duplicate detection gets extension tolerance (needs 4/4 axis similarity for HIGH vs 3/4 for MEDIUM_LOW); extension bonus adds up to 3 to novelty score.
- **FAST_FAIL auto-gate** — after FAST_FAIL, scheduler inspects `analysis_report.md`: verdict = INCONCLUSIVE + recommendation = ITERATE + all Key Metrics rows FAIL → auto-PAUSE with `[AUTO-GATE]` decision (prevents wasting MAIN tokens). Toggle `gates.fast_fail_auto_block`.
- **Concept quality validation** — preflight `_check_concept_quality` accepts only `<file>::<function>`, `<slug>::<path>::L<start>-L<end>`, or `NOT_AVAILABLE`; everything else BLOCKs. `AtomicConcept` field_validator enforces this at schema level. > 50 % `NOT_AVAILABLE` → WARN.
- **Coder multi-file + smoke test** — coder may split complex experiments into multiple modules (`run_experiment.py` entry); self-verification protocol runs `execute_python` import check + minimal config run, retries ≤ 3 before producing `code_patch.diff`. `_load_code` concatenates all `.py` in pipeline dir (entry first).
- **Concept extractor 4-phase** — Phase 0 (Code Reconnaissance) added: read `code_recon.yaml`, browse cloned repo structure. Dispatcher auto-injects `code_recon.yaml` into `CONCEPT_EXTRACT` prompt.
- **Model compatibility** — gpt-5.x and o-series use `max_completion_tokens`. Default T3 fix: `gpt-o4-mini` typo → `o4-mini`.
