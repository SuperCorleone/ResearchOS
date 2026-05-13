# ResearchOS

A knowledge-driven multi-agent research operating system, built on [OpenHarness](./OpenHarness).

Fully autonomous research pipeline: paper crawling → knowledge compilation → hypothesis discovery → experiment design and execution → LaTeX paper → submission packaging. All steps are persisted to disk; the scheduler is idempotent and recoverable; the contract of every hop is enforced by Pydantic.

For version change history see [CHANGELOG.md](./CHANGELOG.md).

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                  OpenHarness Frontend (React Terminal UI)            │
│  /research dashboard │ launch │ trial │ approve │ priority           │
└───────────────────────────────┬─────────────────────────────────────┘
                                ↓
┌──────────────────────────┐  ┌──────────────────────────────────────┐
│  Heartbeat (arXiv crawl) │  │  Knowledge compiler (PDF → wiki)      │
│  Daily PDF download +    │→ │  /knowledge/raw/ → /knowledge/wiki/   │
│  Semantic Scholar filter │  └──────────────┬───────────────────────┘
└──────────────────────────┘                  ↓
┌─────────────────────────────────────────────────────────────────────┐
│       Knowledge base + Findings Memory (BM25 + n-gram + stage bias) │
│  wiki/ : concepts, methods, experiments, failures, datasets          │
│  findings/ : Critic supported auto-persisted; failures/ 30-day decay │
└──────────┬──────────────────────────────────────┬───────────────────┘
           ↓                                      ↓
┌─────────────────────┐    ┌──────────────────────────────────────────┐
│  Knowledge miner    │    │  Research scheduler                       │
│  Direction-aware    │    │  Stateless tick-driven, priority-scheduled│
│  gap discovery      │    │  Budget control, milestone checkpoints    │
└────────┬────────────┘    └────────────────┬─────────────────────────┘
         ↓                                  ↓
┌──────────────────────────────────────────────────────────────────────┐
│  Topic Evaluator (0+8 steps)                                          │
│  [0] 5-axis 0/1/2 fast gate (<7/10 reject)                            │
│  [1-8] BM25 retrieval → online search → similarity → score →         │
│        novelty claim → falsification → gating                         │
└─────────────────────┬────────────────────────────────────────────────┘
                     ↓
┌──────────────────────────────────────────────────────────────────────┐
│  Pipeline state machine (2026-04 simplified)                          │
│                                                                       │
│  PROPOSAL ──approve──→ Shared prefix (runs once per pipeline          │
│                  │     lifetime, cache reuse)                         │
│                  │   hypothesis → paper_spec → code_recon             │
│                  │   → baseline_analyze → data_recon                  │
│                  │   → concept_paper → concept_code                   │
│                  │   → design → code → code_preflight                 │
│                  ↓                                                    │
│            run(mode) → judge → [figure_compose] → report             │
│                  ↓                           → waiting_approval       │
│        M1: mode=fast_fail (small-scale falsification)  ←─ awaiting   │
│        M2+: mode=main (full scale)                        ↓           │
│                  ↓                         ┌─ approve → next milestone│
│        critic G10 baseline fidelity check  ├─ pivot → awaiting_human_pivot │
│                  ↓                         ├─ stop → completed        │
│        SOFT_FLAG → baseline_diagnose → fixer └─ pause → blocked      │
│        HARD_FLAG → awaiting_human_pivot (human)                       │
│        KILL_FLAG → auto kill + failure knowledge persist              │
│                  ↓                                                    │
│        max_milestones reached → PAPER → completed                     │
│                                                                       │
│  Professor remains the LLM at the decision boundary; but all HITL    │
│  gates (pre-MAIN / milestone-N / pre-publish) were removed in the    │
│  2026-04 refactor, replaced by typed JudgeVerdict + contract_guard   │
│  automatic decisions. kill/tweak retained as emergency brakes.       │
└──────────────────────────────────────────────────────────────────────┘
```

### Pipeline step overview

| Step | Type | Stage | Description |
|---|---|---|---|
| hypothesis | LLM | shared | Hypothesis generation |
| paper_spec | LLM | shared | Extract baseline name, URL, key metrics from base_paper |
| code_recon | Python | shared | Shallow clone reference repos (whitelist + size limit) |
| baseline_analyze | LLM | shared | Code-first analysis of baseline package, outputs baseline_analysis.yaml |
| data_recon | Python | shared | Copy/download data per baseline_analysis.yaml |
| concept_paper | LLM | shared | Paper-side concept extraction |
| concept_code | LLM | shared | Code-side concept extraction |
| design | LLM | shared | Experiment design (incl. scale_profile, skip_fast_fail) |
| code | LLM | shared | Experiment code generation (Case A/B/C hard rules) |
| code_preflight | Python | shared | Static checks (incl. baseline_reuse validation) |
| run | LLM | per milestone | Execute experiment (mode=fast_fail\|main injected by scheduler) |
| judge | LLM | per milestone | Merge critic + analysis + interrogation |
| figure_compose | LLM | per milestone (main) | Paper figures |
| report | LLM | per milestone | Milestone report |
| baseline_diagnose | Python | triggered | Auto-scheduled on critic SOFT_FLAG |
| awaiting_human_pivot | blocking | triggered | Waits for user pivot_response on critic HARD_FLAG |

## Alignment Contract Workflow (added 2026-04-25)

**Problem**: When the user supplies a base paper + their own proposal, the pipeline must operate strictly within the scope of the user's input — baseline / dataset / metric / RQ are obtained directly via structured extraction, and should not be diluted through wiki + BM25 (which easily mistakes the proposal for a background paper, or treats a dataset from a different paper as the baseline).

**Solution**: Insert 4 new steps at the head of the MAIN stage to decompose "user input → experiment design" into four layers: extraction → alignment contract → validation → downstream consumption.

### Inject mode (typed-only, 2026-04-29 refactor)

`researchos inject` is ResearchOS's only user-driven onramp. `--base-paper` is required; the old positional argument `papers ...` ("dump a bunch of PDFs for the Miner to discover") has been removed — that path should go through the heartbeat + Knowledge Miner autonomous discovery track.

```bash
# Minimal invocation (recommended): just supply materials, title / question auto-derived
researchos inject \
  --base-paper paper.pdf \
  --proposal proposal.md \
  --ref-code https://github.com/owner/repo \
  --budget 20000

# Explicit overrides (optional)
researchos inject \
  --base-paper paper.pdf --proposal proposal.md \
  --title "STACK-SAS" --question "Does X reduce uncertainty?"
```

Each file is classified by role: `base_paper` / `proposal` / `ref_code` / `related_work`.

**Auto-derived title / question**: inject does not strictly require `--title` / `--question`. Just supply materials; the CLI heuristically extracts placeholder values from `proposal.md` / `base_paper.md` (used only for naming + `pipeline_id`):

| Field | Source priority | Failure fallback |
|---|---|---|
| `title` | proposal H1 → proposal first non-sentence line → base_paper H1 → base_paper filename stem | `untitled-inject` |
| `question` | proposal regex for `RQ:` / `Research Question:` / `Hypothesis:` / `We ask/study:` etc. → any sentence ending with `?` | `(placeholder — proposal_spec_extractor will derive...)` |

The extracted source is printed to stdout (one of five: `auto:proposal_h1` / `auto:rq_grep` / `auto:base_paper_stem` / `placeholder`); if dissatisfied, pass `--title` / `--question` explicitly to override.

**Derivation is just a placeholder**: the `proposal_spec_extractor` LLM step inside the pipeline is the authoritative source for research_questions / change_type / new_components — it writes `proposal_spec.yaml`, which is consumed by downstream design / hypothesis steps. CLI extraction is never depended on by downstream routing.

The `create` command (manual proposal mode) still strictly requires `--title` / `--question` — that is a no-material scenario, with nothing to derive from.

**Full isolation**: inject is a standalone track in ResearchOS. base_paper + proposal only enter the pipeline's own directory, and **do not** share any state with the system's other raw / wiki / pipeline:

- `base_paper` PDF→md is **only** written to `artifacts/pipeline_<id>/base_paper.md`. It is **not** copied to `knowledge/raw/`, **not** compiled into `knowledge/wiki/`, and **not** written to `runtime/raw_registry.yaml`
- `proposal` is **only** written to `artifacts/pipeline_<id>/proposal.md`. Likewise completely isolated from wiki / raw / registry
- `ref_code` only records the git URL; CODE_RECON clones + import-tests it inside the pipeline, polluting no global location
- Downstream `kb_retrieve` takes a **dedicated path** `search_pipeline_dir()` inside an inject pipeline: the retrieval corpus is only the `pipeline_<id>/` directory's own base_paper.md / proposal.md / prior step artifacts, and is **completely blind to the global wiki**. Detection key: `state.json::metadata.source` starts with `user_inject` (see `agent_executor._is_inject_pipeline()`)
- inject is a self-driving command: after writing `runtime/pipelines/new_<id>.json` it inline-starts a single_run scheduler that ticks until the pipeline reaches COMPLETED / FAILED / BLOCKED, then exits. **No need to run `run autonomous` afterwards**

**Historical evolution**: In early versions inject covered both "user-driven isolated pipeline" (typed) and "dump PDF for the Miner to discover" (legacy positional `papers`) — two completely different semantics. In the 2026-04-29 refactor the legacy path was removed entirely (heartbeat + Miner is already an independent autonomous-discovery track, no redundant onramp needed), and the canonical source name was simplified from `user_inject_typed` to `user_inject`. The runtime predicate (`is_user_inject_source`) still prefix-matches `user_inject`, so old state.json files are not broken; optionally run `python scripts/migrate_inject_source.py --apply` for a one-time cosmetic migration.

### PDF extraction layer: pluggable + multi-asset products

**Unified abstraction** (2026-04-29): All PDF→markdown paths in the project (inject's base_paper, wiki compiler's raw ingestion) converge on the single abstraction `researchos/runtime/pdf_extraction/`. The `PdfExtractor` Protocol + `PaperExtraction` schema is the contract; concrete extractors are just implementations. Adding an extractor = write one new file + register it in the default chain, with zero downstream changes.

**Default chain**:

| Extractor | Formulas | Tables | Figures | Deployment | Notes |
|---|---|---|---|---|---|
| `FitzExtractor` | ❌ (mojibake) | ❌ (lines joined by whitespace) | ❌ (completely lost) | Pre-installed (`pymupdf`) | Fallback; always available, no deps; no longer hard-truncated at 20 pages |
| `MinerUExtractor` | LaTeX ✅ | HTML structure ✅ | Extraction + caption linkage ✅ | `pip install researchos[mineru]` | subprocess calls mineru CLI; torch does not enter the scheduler main process |

**Auto-detection**: `default_extractor_chain()` checks whether `mineru` is on PATH; if so the chain = `[MinerU, Fitz]`, otherwise = `[Fitz]`. Zero config. Manual overrides:

```bash
RESEARCHOS_PDF_EXTRACTOR=mineru,fitz   # Force MinerU first
RESEARCHOS_PDF_EXTRACTOR=fitz          # Only fitz (even if MinerU is installed)
RESEARCHOS_PDF_EXTRACTOR=mineru        # MinerU only (no fallback; raises on failure)
```

**Multi-asset products** (when MinerU is enabled): besides `base_paper.md`, the pipeline directory also contains:

```
artifacts/pipeline_<id>/
├── base_paper.md
├── paper_assets.yaml          ← Asset index (contract)
└── _extracted/
    ├── figures/<fig_id>.jpg   ← Extracted figures (hardlink, zero-copy)
    ├── tables/<tbl_id>.html   ← Table HTML structure
    └── equations.tex          ← All LaTeX equations
```

`paper_assets.yaml` contains lists of sections / figures / tables / equations, each with a stable ID (`fig_<page>_<idx>` / `tbl_<page>_<idx>` / `eq_<page>_<idx>`); downstream `paper_spec.evidence` can reference asset IDs rather than just text fragments. Later stages will upgrade `paper_spec_extractor` prompt and `alignment_validator` to be asset-aware.

**Content-addressed cache**: `knowledge/raw/_extracted/<sha16>/`, indexed by `sha256(pdf_bytes)`. The same PDF shares one extraction product across multiple injects and pipelines — a second MinerU run on the same paper is a sub-second cache hit, no re-parsing.

**Degradation semantics**: when the chain fails, `meta.warnings` records the fallback path ("fallback after: mineru: <reason>") so subsequent lineage / debug can see which path the extraction actually took; `AllExtractorsFailed` is the only hard error, in which case inject raises directly rather than writing a half-finished artifact.

### 4 new pipeline steps

| Step | Implementation | Product |
|---|---|---|
| `PAPER_SPEC` | LLM agent (`paper_spec_extractor.md`) | `paper_spec.yaml` |
| `PROPOSAL_SPEC` | LLM agent (`proposal_spec_extractor.md`) | `proposal_spec.yaml` |
| `ALIGNMENT` | LLM agent (`alignment_synthesizer.md`) + Python orchestrator | `alignment_contract.yaml` |
| `ALIGNMENT_VALIDATOR` | Pure Python (`alignment_validator.py`) | `alignment_validation_report.json` |

### Three strong schemas

- **`paper_spec.yaml`** (`schemas/paper_spec.py`) — verbatim extraction from a single paper: `research_questions[]`, `baseline_method.method_steps[]` (structured method steps, proposal references them via step ID), `dataset.name` (verbatim string), `evaluation_metrics[]`, `reference_code.url`. Every non-empty field must have a verbatim quote in `evidence[]`; the validator does substring matching against the original (tolerant of PDF hyphenation / ligatures).
- **`proposal_spec.yaml`** (`schemas/proposal_spec.py`) — extracts `change_type` (forced 5-way enum: replacement / extension / cross_method / cross_domain / new_task), `new_components.replaces|adds_to` (pointing to paper `method_steps[].id`, mutually exclusive), `borrowed_from_paper[]`, `divergences[].arbitration_hint` (forced `binding=false`).
- **`alignment_contract.yaml`** (`schemas/alignment_contract.py`) — 9-dimensional contract + auto-condensed `executive_summary` + `preconditions[]` hard gates.

### Lock vocabulary (10 values)

```
LOCKED_TO_PAPER                LOCKED_TO_PAPER_VIA_REF_CODE
LOCKED_TO_PAPER_VIA_REIMPL     LOCKED_TO_PAPER_PLUS_PROPOSAL
AUGMENTED_BY_PROPOSAL          ARM_DEPENDENT
NEGOTIATED                     DOMAIN_SUBSTITUTED
FREE                           NOT_APPLICABLE
```

`LOCKED_*` fields require `value_ref` to point to a leaf path in `paper_spec.*` — **inline copying is forbidden** — downstream agents read via dereference, a single source of truth. `ARM_DEPENDENT` is enabled when baseline and proposal arms have conflicting designs, forcing an additional ablation arm to isolate the mechanism vs. data-expansion contributions.

### change_type → default lock table (synthesizer prior)

| Dimension \ change_type | replacement | extension | cross_method | cross_domain | new_task |
|---|---|---|---|---|---|
| dataset | LOCKED | LOCKED | NEGOTIATED | DOMAIN_SUB | FREE |
| split / preprocessing | LOCKED | LOCKED / AUGMENTED | NEGOTIATED | NEGOTIATED | FREE |
| baseline_algorithm | LOCKED_VIA_REF | LOCKED_VIA_REF | LOCKED (each) | LOCKED_VIA_REF | NOT_APPLIC |
| candidate_model_pool | LOCKED | LOCKED | NEGOTIATED | NEGOTIATED | FREE |
| metrics | LOCKED | LOCKED+PROP | NEGOTIATED | NEGOTIATED | FREE |
| rq_coverage | LOCKED | LOCKED+ADD | NEGOTIATED | LOCKED+ADD | FREE |

The LLM may override, but every override must fill in `reason` citing a field path in `paper_spec` / `proposal_spec`.

### 18 validator checks

Hard checks run by `alignment_validator.run_validator()` (any ERROR → BLOCK the entire pipeline):

- Each of the three yamls round-trips through pydantic
- `paper_spec.evidence` coverage ≥ 0.90 (excluding 7 exempt fields)
- Every quote substring-matches `base_paper.md` (≥95% PASS / 80–95% WARN / <80% ERROR)
- Every `RQ.question_verbatim` is found in the original
- `proposal_spec.new_components.replaces|adds_to` resolves to a valid `method_step.id`
- All `value_ref` / `value_ref_subtree` paths are dereferenceable
- `LOCKED_TO_PAPER` references must not resolve to `NOT_STATED`
- `LOCKED_TO_PAPER_VIA_REF_CODE.code_handle.symbol` exists in `code_recon.yaml`
- `reference_code.available=False` and `change_type ∈ {replacement, extension}` → BLOCK
- Every `in_scope_rq` is covered along the metrics dimension
- Every `ARM_DEPENDENT` ablation arm exists in `executive_summary.arms_to_run`

### Contract Gate (HYPOTHESIS / DESIGN enforcement)

A Contract Gate is added at the head of `hypothesis_gen.md` and `exp_designer.md`:
- `contract_status == BLOCKED` or `DRAFT` → immediately EXIT BLOCKED, request human intervention
- `contract_status == FINAL` → LOCKED fields are dereferenced from contract `value_ref`; inline re-copying is forbidden
- When DESIGN writes `experiment_plan.yaml::baseline / metric_alignment / reference_code`, it must 1:1 copy contract content

### Coder hard rules

`coder.md` adds FORBIDDEN PATTERNS (preflight scan triggers BLOCK):
- Literal `NotImplementedError` / `NOT_IMPLEMENTED`
- Function body is only `pass` / line is only `...`
- `results.json::metrics` is empty or non-numeric

REQUIRED PATTERNS (when contract exists):
- `baseline_wiring.py` must `from <import_path> import <symbol>`, **forbidden to re-implement** baseline
- For each arm in `executive_summary.arms_to_run`, `run_experiment.py` must have a function of the same name
- `data_loader.py` must validate `Path(...).exists()` + actually read `dimensions[dim=dataset].code_handle.access_path`

### Compatibility with non-inject pipelines

Pipelines without a typed `base_paper` role (e.g. miner-discovered) automatically write SKIP markers for the 4 new steps and pass straight through to HYPOTHESIS. `scheduler._has_alignment_inputs()` checks both the inject predicate (`is_user_inject`) and the typed roles in `evidence_refs`; behavior is fully backward-compatible.

### Focused mode: inject monopolizes project scope

The design intent of inject is "open a pipeline within the scope of the user-supplied paper+proposal, going PROPOSAL → FAST_FAIL → MAIN, no side branches". Once any pipeline whose `metadata['source']` begins with `user_inject` appears in `artifacts/pipeline_*` — **regardless of whether its stage is active, BLOCKED, COMPLETED, or FAILED** — the project enters focused mode and the following two miner-related entries simultaneously go silent:

- `scheduler._maybe_run_knowledge_miner()`: will not trigger another round of mining; `knowledge/proposals/` will not produce off-topic `new_idea_*.yaml` / `evaluated_*.yaml`.
- `scheduler._intake_knowledge_proposals()`: even if `new_idea_*.yaml` left over from a previous run still exists, intake returns directly and will not spawn parallel pipelines that escape the inject scope.

The decision logic is centralized in `scheduler._is_focused_mode()` (O(N) scan of artifacts/). Exiting focused mode requires explicitly removing (or archiving) the inject pipeline directory; there is no automatic release. Global muting is still available via `touch runtime/.miner_paused`.

## Complete research lifecycle

```
Raw papers (PDF / arXiv heartbeat)
  → Knowledge compiler → wiki knowledge pages
    → Knowledge miner scans wiki → finds gaps (7 novelty patterns + user preference)
      → Topic Evaluator: 5-axis fast gate → KB / online / score / falsification / gating
        → Candidate list (runtime/candidates/ + deterministic briefer.py)
          → User selects → Pipeline BASELINE_CONFIRM (metric_contract.json)
        → FAST_FAIL (slim path, skips heavy quality layers)
          → HYPOTHESIS → DESIGN → CODE → RUN → JUDGE → REPORT
          → [AUTO-GATE] all FAIL + INCONCLUSIVE → auto PAUSE
        → MAIN (full quality chain)
          → HYPOTHESIS → DESIGN (incl. Self-Review) → CODE_RECON
            → BASELINE_ANALYZE (LLM code-first analysis of the replication package:
                                grep IO calls to identify real data)
            → DATA_RECON (thin executor: copies+downloads per baseline_analysis.yaml)
            → CONCEPT_PAPER + CONCEPT_CODE (2-pass concept extraction)
            → CODE (mapped 1-to-1 by concept.id) → CODE_PREFLIGHT (BLOCK on missing items)
            → RUN → RESULTS_REVERIFY (independent subprocess re-run for comparison)
            → JUDGE (merge critic + experiment_analyser + interrogator)
            → FIGURE_COMPOSE (when figure data exists) → REPORT (pure Python render)
          → Max milestones reached → PAPER
            → draft.md → Paper Auditor (G4+G10+G11+G12 + chktex)
            → Reviewer Simulator (single-pass NeurIPS schema + model_override)
            → paper.tex → paper.pdf
            → Knowledge persistence: Critic supported → findings card;
                                     UNSUPPORTED → failure card
```

## Quickstart

```bash
pip install -e .
cd OpenHarness && pip install -e . && cd ..

oh  # Launch OpenHarness interactive session

# Inside oh:
#   /research launch              — Fully autonomous: heartbeat + scheduling
#   /research trial               — Run one pipeline and stop
#   /research trial --mode simulate   — No LLM calls (pure testing)
```

## Entry modes

| Mode | Command | Flow |
|------|------|------|
| Default | `launch` | Heartbeat → miner → candidate list → **user selects** → Pipeline → paper |
| Fully autonomous | `launch --auto-select` | Heartbeat → miner → auto-create Pipeline → paper |
| Trial run | `trial` | Heartbeat once → miner once → run one Pipeline → stop |
| **Inject** (user-driven, self-driving) | `inject --base-paper paper.pdf --proposal proposal.md [--ref-code <git-url>] [--title "..."] [--question "..."]` | Fully isolated standalone track: base_paper / proposal **only** enter `artifacts/pipeline_<id>/`, no raw / wiki / registry write; kb_retrieve uses `search_pipeline_dir` to look only inside this pipeline's directory; inline-starts a single_run scheduler that self-drives to COMPLETED / FAILED / BLOCKED |
| Manual proposal | `create --title "..." --question "..." --refs ref1.pdf` | Directly creates a Pipeline |

## Runtime guarantees: StepDigest + ContractGuard

The 2026-04 refactor changed the contract between pipeline steps from "have the downstream LLM re-parse the upstream markdown" to "enforced Pydantic-model checks". Each step now produces two outputs:

1. `<step>.<ext>` — human-readable archive
2. `<step>.digest.json` — Pydantic-validated typed payload (~2KB)

The dispatcher injects the digest JSON into the downstream prompt, rather than the raw markdown. Digests live in `researchos/schemas/digest.py`:

| Digest | Example failure modes caught by validator |
|---|---|
| `HypothesisDigest` | metric_names not bound to a rq_id |
| `PlanDigest` | `baseline_strategy=waived_with_reason` with no reason |
| `ConceptsDigest` | > 30% of concepts are background nouns (no algorithmic verb + no formula) |
| `RunDigest` | data_hash / data_rows disagree with the actual file |
| `JudgeVerdict` | per_concept empty + fully_correct=False (empty contract, no evidence) |

Cross-digest invariants live in `researchos/core/contract_guard.py`:
- `assert_metric_alignment` — plan.metric_names ⊇ hypothesis; run.metrics.keys() ⊇ plan
- `assert_data_manifest_match` — run.data_hash must equal data_manifest.yaml::directory_hash
- `assert_baseline_honesty` — run_inline must have baseline_metrics; waived must not
- `assert_concept_atomicity` — delegated to ConceptsDigest model_validator
- `assert_strategy_actions_consistent` — `BaselineAnalysis.analysis_strategy` must agree with output:
  - `code_primary` ⇒ `analyzed_repos[*].dataset_paths` non-empty + `status ≠ no_data_found`
  - `text_fallback` ⇒ `fallback_used=true` + non-empty `notes`
  - Any non-`none` strategy + `data_manifest.yaml` exists ⇒ `executed_actions` non-empty
  - Trigger scenario: the "strategy=text_fallback + executed_actions=[]" contradiction seen in the BMA pipeline will be immediately BLOCKed

`state_manager.save_pipeline()` automatically runs `enforce_all` after every save; on violation → pipeline stage=BLOCKED + `ContractViolation` exception bubbles up.

### LLM no longer picks strategies, only describes facts (2026-04-26 expansion)

The following links — "seemingly an LLM decision, actually a factual judgement" — have all degenerated to pure Python:

| Function/location | Before | After |
|---|---|---|
| `scheduler._run_baseline_analyze` | LLM picks `code_primary` / `text_fallback` itself | Look at whether the cloned repo actually has data-shape files (`data_shape.decide_strategy`); LLM mismatch → BLOCK |
| `topic_evaluator.competitive_select` | LLM does pairwise head-to-head selection N→top_k | Direct 5-dim weighted sum on `EvaluationScores` + lexicographic tie-break; O(N log N) pure function; weights in `research_config.yaml::tournament.weights` |
| BASELINE_ANALYZE URL classification | LLM labels each URL with `should_download` / `kind` | Scheduler first runs `url_classify` for high-confidence verdicts; LLM transcribes the high entries; contradictions → BLOCK |

Principle: anything that can be solved by `os.listdir` / weighted sum / regex host matching, the LLM no longer touches; the LLM is invoked only where genuine semantic interpretation is needed (writing hypotheses, designing experiments, writing code, writing papers).

## Agents (post-refactor: 10 core + 3 alignment + 2 conditional)

| # | Agent | Tier | Responsibility |
|---|---|---|---|
| 1 | knowledge_miner | T1 | wiki scan → candidate proposals (with research_goal) |
| **A** | **paper_spec_extractor** | T2 | **Single-doc strong-schema extraction from base_paper.md to paper_spec.yaml; no kb_retrieve; NOT_STATED is legal** |
| **B** | **proposal_spec_extractor** | T2 | **Extract proposal_spec.yaml from proposal.md; forced 5-way change_type; forbidden to read paper** |
| **C** | **alignment_synthesizer** | T2 | **Synthesize alignment_contract.yaml (9-dim lock + executive_summary); pure structured reasoning, no external tools** |
| 2 | hypothesis_gen | T1 | Atomic falsifiable hypothesis; when contract is FINAL, kb_retrieve cannot redo RQ/metric |
| 3 | exp_designer | T1 | Experiment plan + Self-Review; contract LOCKED fields must dereference, not recopy |
| 4 | concept_extract_paper | T2 | Concept definitions + math + refs |
| 5 | concept_extract_code | T2 | Concept ↔ code locator binding (`<file>::<sym>` or `<slug>::<path>::L<a>-L<b>`) |
| 6 | coder | T1 | Implement by concept.id; FORBIDDEN PATTERNS (no placeholders) + REQUIRED PATTERNS (must direct-import baseline) |
| 7 | experiment_runner | T3 | Execute; produces results.json + data_manifest.yaml |
| 8 | **judge** | T1 | **Merge critic + experiment_analyser + interrogator**; produces JudgeVerdict |
| 9 | paper_writer | T1 | Sectional writing; sections conditional on JudgeVerdict optional fields |
| 10 | paper_auditor | T2 | G4/G10/G11/G12 + chktex |

Conditional triggers: `vlm_reviewer` (when PNG/JPG present), `figure_composer` (when visualization_plan present).

**Pure-Python steps** (no LLM): `alignment_validator` (18 checks), `code_recon`, `data_recon`, `baseline_confirm`, `results_reverify`, `report` (publisher render).

## Auto data and code discovery (CODE_RECON → BASELINE_ANALYZE → DATA_RECON)

ResearchOS's core promise is **"the user only hands in the paper + proposal; everything else is found by the system"**. v5.3 splits this pipeline into **three steps with clean separation**: Python does cloning and IO, LLM does semantic judgement. **The user never needs to manually drop anything into `data/raw/<id>/`.**

> **Design principle (v5.3 redesign)**: any "is this data / is this baseline / is this decoration"
> semantic judgement is **fully delegated to the LLM**; hard-coded host whitelists, suffix
> blacklists, and directory-naming heuristics have all been removed. Python only handles
> deterministic IO + manifest generation.
>
> Key insight: **when a code repo exists, code analysis must be primary**, far superior to
> scanning URLs from paper text — `pd.read_csv()` / `torch.load()` / `wget` calls in the code
> are the ground truth of what data the baseline actually used. The paper text may list 5
> datasets but only 1 is actually used; a badge SVG never appears as an argument to `pd.read_csv`.
> Text scanning is a fallback only when *no code is available to analyze*.

### Step 1: CODE_RECON (pure Python)

`researchos/runtime/code_recon.py` — finds all code-hosting URLs from paper / proposal / wiki frontmatter and shallow-clones them to `scratch/<id>/code_refs/<owner>__<repo>/`.

URL sources (descending priority):

1. `experiment_plan.yaml::reference_codebases` (if explicitly listed)
2. `state.json::evidence_refs[*]` fields: `url` / `repo` / `code_url` / `path_or_url`
3. `code_refs:` in wiki page frontmatter — matched to `knowledge/wiki/concepts/<slug>.md` after normalizing the evidence_ref's stem via `_slugify()`

Matches GitHub / GitLab / Bitbucket / HuggingFace Hub / Codeberg. Shallow clone (`--depth=1 --single-branch`), 500 MB cap, and after download immediately prunes large weights like `.pth/.bin/.zip/.parquet/...` to save scratch space.

CODE_RECON v5.3 **no longer scans READMEs to derive dataset_hints** (the v5.2 heuristic was removed) — that work is now done better by BASELINE_ANALYZE with the LLM.

**Entry-point detection (strengthened 2026-04-26)**: `_detect_entry_points` has three layers of fallback to avoid `entry_points: []` for unconventionally-named repos (e.g. BMA's `bma_adaptation.py` / `bma_generator.py`), which would leave the LLM disoriented:

1. Match against the `_ENTRY_POINT_NAMES` whitelist (`train.py / main.py / run.py / experiment.py / inference.py / eval.py / benchmark.py`) — full-tree search
2. Top-level `*.py` containing an `if __name__ == "__main__":` block — catches "entry scripts named after the method"
3. Any top-level `*.py` as a final fallback — even library-style repos with no CLI entry give the LLM something to grep

Excludes `tests/ test/ .git/ build/ dist/ docs/ doc/ node_modules/ site-packages/ venv/` etc. `limit=10`.

**data_shape persistence (added 2026-04-26)**: `run_code_recon` now writes the result of `data_shape.scan_repo_for_data` (top 20 hits + total count) into `code_recon.yaml` under `repos[*].data_shape_hits` / `data_shape_hit_count`, where previously it only lived in scheduler memory feeding the LLM prompt. Benefits: (a) the artifact is self-contained, no rescan needed; (b) lineage / alignment_validator can consume it directly; (c) on failure audit can see exactly which data list the LLM had at the time.

### Step 2: BASELINE_ANALYZE (LLM, code-first) — added in v5.3

`researchos/agent_defs/baseline_analyzer.md` + output schema `researchos/schemas/baseline_analysis.py::BaselineAnalysis`. Reads the repos CODE_RECON cloned and **first greps real IO calls in the entry-point code** (`pd.read_csv` / `np.load` / `torch.load` / `urllib.request.urlretrieve` / `wget` / `hf_hub_download` / etc.), inferring from there the files and external URLs the baseline actually uses, then doing semantic classification.

Output `baseline_analysis.yaml`:

```yaml
analysis_strategy: code_primary       # or text_fallback / upstream_trusted / mixed
analyzed_repos:
  - slug: matteocamilli__bma-package
    role: "Complete replication package for the BMA paper"
    entry_points: [bma_adaptation.py, bma_generator.py]
    code_data_calls:                  # hard evidence from grep
      - {file: bma_adaptation.py, lineno: 87,
         call_text: "pd.read_csv('data/training_rescueRobot_25600_64.csv')",
         resolved_path: data/training_rescueRobot_25600_64.csv}
    dataset_paths:                    # to be copied (with the call above as evidence)
      - {path: data/training_rescueRobot_25600_64.csv, kind: training,
         purpose: "Loaded by bma_adaptation.py at L87 to fit BMA models."}
    auxiliary_paths:                  # explicit skips
      - {path: plots/, reason: "post-hoc visualisations"}
      - {path: logs/,  reason: "stale run logs"}
external_data_urls:
  - {url: "...zenodo.org/badge/...svg", should_download: false,
     kind: decorative_badge, reason: "DOI shield SVG, not data"}
```

**Strategy selection (since 2026-04-26 a deterministic verdict, no longer delegated to the LLM)**:

The strategy is decided by `scheduler._run_baseline_analyze` in pure Python **before** dispatching to the LLM (`researchos/utils/data_shape.py::decide_strategy`), and written into the prompt as a `MANDATORY analysis_strategy: X` block. The LLM only describes facts.

| Input | strategy | Basis |
|---|---|---|
| At least one ok/cached clone repo + `scan_repo_for_data` has high/medium hits | `code_primary` | The repo directory really contains data-shape files |
| ok/cached clone repo exists but 0 hits | `text_fallback` | The repo exists but contains no data |
| No ok/cached clone repo | `text_fallback` | All clones failed / were whitelist-rejected |

`scan_repo_for_data` uses two confidence layers:
- **high**: path contains conventional segments like `data/` `datasets/` `fixtures/` `simulation*/` `examples/` `benchmarks/`
- **medium**: extension ∈ `{.csv,.tsv,.parquet,.h5,.npy,.npz,.pkl,.zip,.gz,.xlsx,...}`
- Excluded: `.git/` `tests/` `docs/` `figures/` `__pycache__/` `node_modules/` `venv/` etc.

Post-LLM validation: if the LLM writes an `analysis_strategy` inconsistent with the enforced strategy, the scheduler directly BLOCKs the pipeline (`last_error: baseline_analyze:strategy mismatch:...`), preventing the LLM from arbitrarily choosing `text_fallback` while the repo actually does contain data.

**URL pre-classification (since 2026-04-26)**: before dispatching to the LLM, the scheduler scans every cloned repo's README/`*.md` for all URLs and runs them through host+extension rules in `researchos/utils/url_classify.py` to get `(kind, should_download, confidence)`:
- **high**: badge/DOI/known dataset host (`zenodo.org` `huggingface.co/datasets` `archive.ics.uci.edu` `figshare.com` etc.), unambiguous extensions (`.svg/.png` → badge; `.csv/.parquet/.h5` → data; `.pt/.safetensors` → model_weights)
- **medium**: data extension on an unknown host (hints to the LLM that it may be data, needs confirmation)
- **low**: no signal at all → LLM judges by itself

The prompt injects a "URL pre-classification" block; high-confidence entries require the LLM to copy them verbatim into `external_data_urls[*]`. Post-LLM validation: if the LLM disagrees with a high-confidence result on `kind` or `should_download` → BLOCK (canonical anti-example: "wants to download a shields.io SVG").

### Step 3: DATA_RECON (pure Python, thin executor)

`researchos/runtime/data_recon.py` was rewritten in v5.3 as a **thin executor** — zero heuristics, zero whitelists, zero directory guessing. Strictly executes per `baseline_analysis.yaml`:

- For each `analyzed_repos[*].dataset_paths[*]`: from `scratch/<id>/code_refs/<slug>/<path>` materialise via **hardlink → symlink → copy three-tier fallback** into `data/raw/<id>/<slug>/<path>` (same fs → hardlink, zero extra bytes, inode sharing makes the hash directly correspond to the original file; different fs → symlink; both fail → copy). Each manifest row records `materialise_mode`.
- For each `external_data_urls[*]` with `should_download: true`: `urlretrieve` into `data/raw/<id>/`
- Generate `data_manifest.yaml` (SHA256 per file + `directory_hash`)

URL download budget: 500 MB cap per pipeline, 90 s timeout per file.

**Idempotent**: if `data_manifest.yaml` exists and `data_recon_status ∈ {success, partial, cached}`, the entire step is skipped. Delete the manifest to force a rerun.

**When baseline_analysis is missing or `status: failed`**: writes a stub manifest with `data_recon_status: no_baseline_analysis` or `baseline_analyzer_failed`; the pipeline is not blocked and continues. Downstream DESIGN sees this status and decides whether to fall back to `synthetic` data (which must be noted in `risk_notes`).

### Upstream Trusted Sources interface (v5.3 reserved, not yet enabled)

A future version will add an upstream academic-research agent that can find high-confidence data/code sources in advance from the paper / official supplements / repo lists, annotated with confidence, and written into `artifacts/pipeline_<id>/upstream_sources.yaml`:

```yaml
pipeline_id: my_pipe
sources:
  - kind: dataset_url
    url: "https://zenodo.org/record/12345/files/main.zip"
    confidence: trusted          # trusted | high | medium
    annotated_by: literature_researcher_v2
    annotation: "Official dataset cited in §4.2 of the supplementary material"
```

When `baseline_analyzer` sees this file: `trusted` entries are **passed verbatim** into `baseline_analysis.yaml` (no re-scoring), `high` entries are added to the evidence pool as strong hints, and `medium` entries follow normal evidence rules. Schema: `researchos/schemas/baseline_analysis.py::UpstreamSources`.

### How DESIGN consumes it

The dispatcher injects **both** the `data_manifest.yaml` summary and the `baseline_analysis.yaml` summary into the DESIGN prompt — the latter carries code-evidence per dataset path, which is much stronger than "file exists". `exp_designer` is forced to:

- `data.source: "file"`
- `plan_dataset.dataset_location: "data/raw/<pipeline_id>/"`
- `plan_dataset.read_data_step` must reference a real filename from `manifest.files[]`
- `data.consistency_requirement.reference_paper` is taken from `paper_spec.yaml`'s title

`synthetic` is permitted only when the manifest does not exist or `data_recon_status` indicates no data.

### CONCEPT gatekeeping

The LLM occasionally writes a repo URL directly into `concepts.yaml::code_implementation` (the schema only allows `<file>::<func>` / `<slug>::<path>::L<a>-L<b>` / `NOT_AVAILABLE`), which would otherwise BLOCK at CODE_PREFLIGHT. After the agent writes its output, `scheduler._run_concept_extract` calls `schemas/concept.py::sanitize_concepts_yaml`, which automatically downgrades illegal values like URLs to `NOT_AVAILABLE` and logs a WARN.

### Why v5.2 → v5.3 was redesigned

v5.2 used four hard-coded tables inside DATA_RECON (`_BADGE_FRAGMENTS`, `_DATA_HOSTS`, `_DATA_EXTENSIONS`, `_CLONE_DATA_DIRS`) to judge "is this data". The BMA case exposed two anti-patterns:

- Zenodo badge SVG `zenodo.org/badge/452358696.svg` hit the host whitelist and was downloaded as data (it's actually a 1 KB decorative image)
- Assuming a baseline's data must live in a `data/` subdirectory — true by coincidence for BMA, completely wrong for ROS or repos with custom layouts

**v5.3 cuts the knot**: all blacklists and whitelists were removed; judgements are made by the `baseline_analyzer` LLM looking at code, and DATA_RECON degenerates into a yaml interpreter. One line of `pd.read_csv('data/x.csv')` tells the LLM that `data/x.csv` is real data; it will never "see a Zenodo badge URL and misclassify it as data", because a badge never appears as an argument to an IO call.

The REPORT step no longer has an agent — `researchos/core/publisher.py::render_milestone` deterministically renders milestone_N.md from digests (zero LLM).

## Research artifact graph (lineage.json)

All artifacts produced by a pipeline — ref papers / wiki / code repos / data files / concepts / decisions / milestones — are no longer isolated. A **derived view** `artifacts/pipeline_<id>/lineage.json` ties them together with a directed graph, which the scheduler **rebuilds after each successful step**, always reflecting the current truth.

> **Design principle**: lineage is **a derived view, never a source of truth**. The real
> information of every edge already lives in some yaml/json; this module only joins them.
> `lineage.json` can be deleted and regenerated at any time; losing it loses no research
> information.

### Node types (NodeKind)

| kind | Source | Example |
|---|---|---|
| `pipeline` | state.json | The pipeline itself (root node) |
| `paper` | evidence_refs + knowledge/wiki | Taming BMA paper (PDF + compiled wiki page) |
| `proposal` | evidence_refs (role=proposal) | User proposal text |
| `code_repo` | code_recon.yaml | matteocamilli/bma-package |
| `dataset_file` | baseline_analysis.yaml + data_manifest.yaml | training_rescueRobot_25600_64.csv (with sha256) |
| `dataset_url` | baseline_analysis.yaml::external_data_urls | Zenodo badge URL (marked should_download=false) |
| `concept` | concepts.yaml | C2 = "Bayesian Model Averaging" |
| `step` | state.json::artifacts + workflow_config | step:hypothesis / step:design / ... |
| `decision` | decisions/<pid>_*.yaml | Professor approve/pause/kill |
| `milestone_report` | milestone_N.md | milestone 1, milestone 2 |
| `paper_draft` | paper/<pid>_*.tex | Final paper draft |

### Edge relations (RelKind)

| rel | Semantics | Example |
|---|---|---|
| `derived_from` | Derivation | pipeline `derived_from` paper |
| `references` | Reference | paper `references` code_repo |
| `produces` | Production | step:data_recon `produces` dataset_file |
| `consumes` | Consumption | step:design `consumes` data_manifest.yaml |
| `ships` | Containment | code_repo `ships` dataset_file (data shipped with the repo) |
| `selects` | Selection (with evidence) | step:baseline_analyze `selects` dataset_file (evidence: grepped IO call file + line) |
| `defined_in` | Concept → origin | concept:C2 `defined_in` paper |
| `implemented_in` | Concept → code | concept:C2 `implemented_in` `<file>::<func>` |
| `next_step` | Workflow order | step:hypothesis `next_step` step:design |
| `applies_to` | Decision target | decision:approval_X `applies_to` pipeline |
| `evaluates` | Judgement | step:judge `evaluates` step:run |
| `tested_with` | Experiment ↔ data | step:run `tested_with` dataset_file |
| `cites` | Report → concept | milestone `cites` concept |

### Node ID convention (reserved for future cross-pipeline merging)

Cross-pipeline-shareable nodes use a **content hash**:
- `paper:<sha256(filename)[:12]>` → the same paper has the same ID across pipelines
- `code_repo:<owner>__<repo>` → the same GitHub repo has the same ID
- `dataset_file:<sha256(content)[:12]>` or fallback `dataset_file:p<sha(path)>`

Pipeline-local nodes carry a pipeline_id prefix:
- `concept:<pid>:<concept_id>`
- `step:<pid>:<step_name>`
- `decision:<pid>:<decision_filename>`

A future Layer 3 (project-level `runtime/research_graph.json`) can then automatically merge the lineages of many pipelines by node ID — "which pipelines have used the BMA paper" is just the in-edges of `paper:2c67b7b1bfa7`.

### Rebuild timing

The scheduler calls `lineage.write_pipeline_lineage(pdir, root)` once after every successful step:
- After specialized steps (CODE_RECON / DATA_RECON / BASELINE_ANALYZE / ALIGNMENT_VALIDATOR / ...)
- After standard LLM steps

Failures are swallowed (`logger.exception` leaves a trace but does not block the pipeline). Simulate mode is skipped.

Measured overhead: ~10–50 ms per rebuild (no LLM, pure yaml read), negligible compared to the seconds-to-minutes of a step.

### CLI usage

```bash
# Summary: node count / edge count / per-kind node stats
researchos lineage <pipeline_id>

# List all nodes of a kind (with attrs)
researchos lineage <pid> --kind dataset_file
researchos lineage <pid> --kind concept

# Full JSON
researchos lineage <pid> --format json > lineage.json

# Mermaid graph (paste into markdown directly)
researchos lineage <pid> --format mermaid

# Debug: which extractors ran, what they found
researchos lineage <pid> --diagnostics
```

### Real-data example (BMA pipeline)

```
$ researchos lineage stacking_based_uncertainty_han
Pipeline: stacking_based_uncertainty_han
Nodes: 74  Edges: 44

Node counts by kind:
  dataset_file         39   (4 actually used by baseline + 35 listed in manifest)
  step                 14
  decision              7
  concept               6
  paper                 2
  dataset_url           2
  milestone_report      2
  pipeline              1
  code_repo             1

Edge counts by relation:
  next_step            13   (workflow ordering)
  produces              9   (step → artifact)
  applies_to            7   (decision → pipeline)
  defined_in            6   (concept → paper)
  ships                 4   (repo → dataset)
  derived_from          2   (pipeline → paper/proposal)
  references            2   (paper → code_repo)
  evaluates             1   (judge → run)
```

You can directly answer: "Where is the BMA concept C2 defined?" — look up the `defined_in` out-edges of `concept:<pid>:C2`, leading back to `paper:<bma>`; "Which datasets did the BMA paper provide?" — `paper:<bma> -references-> code_repo:matteocamilli__bma-package -ships-> dataset_file:*`.

### Relation to Layer 1 / Layer 3

- **Layer 1 (artifact-embedded provenance header)**: future incremental work. Each newly-written yaml/json adds a `provenance: {pipeline_id, step, producer, inputs[], created_at, content_hash}` block at the head, so lineage can read it directly without reverse parsing. **Not needed now** — Layer 2 reverse parsing already covers 90% of edges.
- **Layer 3 (project-level `runtime/research_graph.json`)**: merges all pipelines' lineages by node ID into one global graph, answering cross-pipeline questions ("which concepts are reused across multiple pipelines", "how many hypotheses has the BMA paper supported in total"). The design is in place (ID system + edge semantics consistent across pipelines); **implementation on demand**.

## Atomic concept ↔ code bidirectional mapping

The LLM's most stubborn failure class is "appears to be implemented but missing a key component". The flow (MAIN):

1. **CONCEPT_PAPER** (T2) — reads hypothesis + plan, produces `concepts_paper.digest.json`: each concept `{id, definition, math_formula, reference_papers}`. The concept definition must contain an algorithmic verb (combine/select/learn/optimize/...) OR a non-empty math_formula — the ConceptsDigest validator rejects background nouns at the boundary.
2. **CONCEPT_CODE** (T2) — reads code_recon.yaml + concepts_paper.digest.json, and uses `list_file_symbols` to traverse cloned repos and bind each concept's `code_implementation`. Produces `concepts.digest.json`. Coverage < 0.5 (when usable repos exist) → validator BLOCK.
3. **CODE** — coder reads concepts.digest.json; each concept.id must be mapped or explicitly SKIPPED in the CLAIM-TO-CODE MAPPING table at the bottom of the main script.
4. **CODE_PREFLIGHT** — preflight.py validates per-ID; missing items BLOCK.
5. **JUDGE** — uses gen_code_tree_structure → list_file_symbols → narrow file_read on demand, validating mechanism ↔ math_formula concept by concept; greps to verify the metric is actually computed; import smoke-tests; outputs `judge.digest.json` (Pydantic).

## fast_fail vs main run-mode design

design + code stages run only once; the run stage executes with different parameters at different milestones:

```yaml
# experiment_plan.yaml example
scale_profile:
  fast_fail:
    data_slice: "first_50_rows"
    seeds: [42]
    baselines: ["primary"]
    ablations: false
  main:
    data_slice: "all"
    seeds: [42, 7, 13, 21, 99]
    baselines: "all"
    ablations: true
skip_fast_fail: false  # Set true when the data scale is insufficient to reveal a scale gap
```

The scheduler writes a sidecar `run_mode.txt`; experiment_runner reads it and runs the corresponding profile.

## Professor decision mode

```yaml
# decisions/approval_<pipeline_id>_<ts>.yaml
pipeline_id: demo
action: "continue"   # or stop / tweak / launch / branch / write / finalize
stage_after: "MAIN"
comment: "baseline is locked in, proceed to MAIN to run the main experiment"
```

Professor is triggered at:
- After PROPOSAL evaluation (approval required to launch)
- After each milestone completes
- On every JUDGE ITERATE/PIVOT suggestion
- On reaching max_iter or budget cap

## Quality gates G1–G12

Defined in `researchos/agent_defs/shared/quality_gates.md`; agents declare "owns + how" rather than inlining the full text.

| Gate | Meaning | Responsible agent |
|---|---|---|
| G1 | Hypothesis is falsifiable + has operationalized metric | hypothesis_gen (produces) / judge (audits) |
| G2 | Causality/control: one variable per arm + resource parity | exp_designer |
| G3 | Atomic concept ↔ code bidirectional mapping completeness | coder (produces) / judge (result-level) |
| G4 | Every number in the paper is traceable to results.json | paper_auditor |
| G5 | MAIN statistical reporting complete (effect size + p + N + CI) | exp_designer + judge + paper_auditor |
| G6 | Reproducible: deps + seed + single entry-point | coder |
| G7 | Resource report explicitly tags measured/estimated/simulated | exp_designer + publisher |
| G8 | Conclusion does not exceed experimental scope; Limitations contains ≥3 failure conditions | paper_writer + reviewer_simulator |
| G9 | Data: explicit source + pre-preprocessing split + provenance | exp_designer + coder + judge |
| G10 | Section completeness (Abstract/Intro/Related/Method/Experiments/Conclusion) | paper_auditor |
| G11 | Cross-section numerical consistency (Abstract vs tables vs body vs figure captions) | paper_auditor |
| G12 | Citation utilisation ≥85% + multi-layer verification | paper_auditor |

## Baseline reproduction fidelity (G10 Baseline)

At every milestone, critic automatically compares the baseline numbers produced by the experiment against the paper's reported numbers:

| Relative error | Verdict | Follow-up |
|---|---|---|
| ≤ 5% | REPRODUCED | None |
| (5%, 15%] | SOFT_FLAG | Auto baseline_diagnose; if fixable, fixer intervenes; otherwise escalate |
| (15%, 50%] | HARD_FLAG | Professor writes `pivot_request_*.md`, pipeline enters awaiting_human_pivot |
| > 50% or NaN | KILL_FLAG | Auto kill + writes `wiki/failures/baseline_reproduction_*.md` |

Thresholds configured at `runtime/research_config.yaml::critic.baseline_divergence_thresholds`; cross-language porting (cross_language_porting) uses looser thresholds (10/25/60%).

### Human pivot protocol

On HARD_FLAG the professor does not auto-decide, but writes:

```markdown
decisions/pivot_request_<id>_<ts>.md
  - baseline confidence
  - G10 deviation list
  - baseline_diagnose result
  - three suggested directions (tune params / switch benchmark / terminate)
  - professor recommendation
```

User replies:

```yaml
# decisions/pivot_response_<id>_<ts>.yaml
decision: pivot|kill|continue_with_known_divergence
direction: a|b|c
comment: "..."
```

The scheduler sees the response, unblocks, and proceeds according to the decision.

## Directory structure

```
researchos/
├── schemas/           # Pydantic models (digest, pipeline, decision, ...)
│   ├── digest.py            # StepDigest contracts
│   ├── paper_spec.py        # 2026-04 PaperSpec strong schema
│   ├── proposal_spec.py     # 2026-04 ProposalSpec strong schema
│   └── alignment_contract.py # 2026-04 AlignmentContract strong schema
├── core/              # Platform-agnostic logic (contract_guard, publisher, ...)
│   ├── contract_guard.py
│   └── publisher.py   # Pure-Python milestone rendering
├── agent_runtime/     # Agent execution abstraction
│   ├── base.py        # AgentRuntime Protocol
│   └── tools.py       # gen_code_tree_structure, grep_code, list_file_symbols
├── agent_defs/        # Agent prompt definitions (single source of truth; platform-neutral)
│   ├── paper_spec_extractor.md      # 2026-04 single-doc strong extraction
│   ├── proposal_spec_extractor.md   # 2026-04 single-doc strong extraction
│   ├── alignment_synthesizer.md     # 2026-04 9-dim contract synthesis
│   ├── judge.md       # Replaces critic + experiment_analyser + interrogator
│   ├── concept_extract_paper.md / concept_extract_code.md
│   ├── coder.md / exp_designer.md / hypothesis_gen.md / ...
│   └── shared/quality_gates.md
├── runtime/           # Scheduler, dispatcher, state_manager
│   ├── alignment_validator.py    # 2026-04 18 pure-Python checks
│   ├── alignment_synthesizer.py  # 2026-04 LLM orchestration + hash guard
│   └── ...
└── mcp_servers/       # MCP kb_retrieve

adapters/
├── openharness/       # OH plugin + OpenHarnessRuntime
├── mcp/               # MCP server (exposes launch/state/kill as tools)
└── skill/             # Claude Skill adapter

artifacts/pipeline_<id>/    # Pipeline products (state.json, digests, markdown)
│  ├── base_paper.md           # inject mode: base paper converted to markdown
│  ├── proposal.md             # inject mode: user proposal (not in wiki)
│  ├── paper_spec.yaml         # 2026-04 PAPER_SPEC product
│  ├── proposal_spec.yaml      # 2026-04 PROPOSAL_SPEC product
│  ├── alignment_contract.yaml # 2026-04 ALIGNMENT product
│  └── alignment_validation_report.json  # 2026-04 ALIGNMENT_VALIDATOR report
knowledge/
├── raw/              # Raw PDFs (note: under inject, base_paper / proposal are not copied here)
├── wiki/             # Structured knowledge (concepts/methods/experiments/failures/datasets)
├── findings/         # Critic SUPPORTED persisted here
└── failures/         # UNSUPPORTED persisted here, 30-day decay
runtime/               # Scheduler state, budget, workflow_config.yaml
decisions/             # Professor decision files
paper/                 # Paper drafts + PDF
tests/                 # 279 passing / 4 skipped (2026-04-25 baseline)
```

## Configuration

### Environment variables

```bash
# OpenAI direct (recommended)
export RESEARCHOS_API_KEY="sk-..."
export RESEARCHOS_BASE_URL="https://api.openai.com/v1"
export RESEARCHOS_MODEL="gpt-5.4"

# Anthropic
export RESEARCHOS_API_KEY="sk-ant-..."
export RESEARCHOS_BASE_URL="https://api.anthropic.com"
export RESEARCHOS_MODEL="claude-opus-4-7"

# OpenRouter (mixed backend)
export RESEARCHOS_API_KEY="sk-or-..."
export RESEARCHOS_BASE_URL="https://openrouter.ai/api/v1"
export RESEARCHOS_MODEL="qwen/qwen3.6-plus:free"

# Local Ollama
export RESEARCHOS_BASE_URL="http://localhost:11434/v1"
export RESEARCHOS_MODEL="ollama/llama3.1:70b"

# Independent re-execution verification (off by default)
export RESEARCHOS_REVERIFY=1

# VLM figure review (separate model)
export RESEARCHOS_VLM_MODEL=gpt-4o

# Three-tier model override
export RESEARCHOS_MODEL_T1=gpt-5.4   # primary generation
export RESEARCHOS_MODEL_T2=gpt-4o    # structured
export RESEARCHOS_MODEL_T3=o4-mini   # low-reasoning
```

### Key workflow_config.yaml settings

```yaml
retry:
  max_runs: 5
  max_iters: 4
  exp_timeout: 7200
  plot_timeout: 600

gates:
  fast_fail_auto_block: true   # all FAIL + INCONCLUSIVE → auto PAUSE

review:
  model_override: claude-opus-4-7   # adversarial reviewer uses a different model family

model_tiers:
  T1: ${RESEARCHOS_MODEL_T1:-gpt-5.4}
  T2: ${RESEARCHOS_MODEL_T2:-gpt-4o}
  T3: ${RESEARCHOS_MODEL_T3:-o4-mini}
```

### Proxy environment variables (must be unset)

If your shell has a SOCKS proxy set:

```bash
unset ALL_PROXY all_proxy GRPC_PROXY grpc_proxy FTP_PROXY ftp_proxy RSYNC_PROXY
```

httpx (used inside the OpenAI SDK) does not recognise the `socks5h://` scheme. The code layer already isolates via `httpx.Client(trust_env=False)`, but this note is preserved in the README as a paper trail for older versions. Keeping HTTP_PROXY/HTTPS_PROXY has no effect.

### Multi-backend support

`llm_client.py` auto-routes by model prefix: `claude-*` → Anthropic SDK; `gpt-*` / `o1` / `o3` / `o4` / `o5` → OpenAI SDK; `qwen/` / `deepseek/` etc. → OpenRouter; `ollama/*` → local Ollama.

## Dependencies

- Python >= 3.10
- pydantic >= 2.0, pyyaml, openai, pymupdf
- `backoff` (optional; `llm_client.retry_llm` prefers it, with a local fallback when not installed)
- pdflatex + chktex (optional; paper_writer's hardening pass requires chktex)
- `pip install researchos[vector|wandb|mlflow]` (optional extras)

## Docker deployment

```bash
docker compose up -d
export RESEARCHOS_API_KEY=your_key
docker compose up
```

`docker-compose.yaml` mounts knowledge/, artifacts/, runtime/, decisions/, paper/, data/, and knowledge/findings/ as persistent volumes.

## Running

```bash
python scripts/run_research.py launch              # fully autonomous
python scripts/run_research.py trial               # single-topic trial
python scripts/run_research.py trial --mode simulate   # no LLM calls
```

## Tests

```bash
python -m pytest tests/ -q
# 279 passed, 4 skipped (2026-04-25 alignment-contract workflow baseline)
# +53 over 2026-04: alignment_validator helpers (25)
# + alignment_synthesizer orchestrator (15) + typed evidence_refs (13)
```

## Core design principles

1. **File-based state machine** — all state in JSON/YAML; the scheduler is stateless, idempotent, and recoverable
2. **Knowledge-driven** — `kb_retrieve` (BM25 + n-gram + optional FAISS) accesses the knowledge base
3. **Typed digest is the contract** (StepDigest + contract_guard) — cross-step invariants enforced by Pydantic; on violation → stage=BLOCKED, rather than letting a downstream LLM discover it
4. **Atomic concept is the code contract** — every reference-paper atomic concept must be explicitly mapped to `<file>::<sym>` or explicitly SKIPPED; boundary validation rejects background nouns
5. **Independent verification over trust** — RESULTS_REVERIFY subprocess re-runs (>10% drift → BLOCK)
6. **Rules first, LLM second** — 5-axis gate → topic_evaluator; design_checks → exp_designer Self-Review; preflight → judge; paper_auditor → reviewer_simulator
7. **Every LLM call goes through one exit** — extract_json + strip_reasoning + exponential backoff + multi-backend routing
8. **Prevention beats recovery** — proactive token-budget trimming (reserve 15% for output); 12K head+tail truncation of tool output; overflow auto-healing as fallback
9. **Single source of truth for retry/timeout/budget constants** — `workflow_config.yaml::retry`
10. **Findings accumulate across pipelines** — SUPPORTED → findings; UNSUPPORTED → failures (30-day decay); the dispatcher injects active failure cards as avoidance guidance for the next pipeline
11. **Baseline locked before the experiment** — `BASELINE_CONFIRM` is the first step of FAST_FAIL; the table is filled after the run
12. **Citations come from evidence_refs** — paper_writer may only cite papers in `knowledge/raw/` or in evidence_refs
13. **Step-level idempotent replay** — `.step_cache/{stage}__{step}.json`; after a crash only unfinished steps are rerun
14. **Sectional writing prevents inter-section contradiction** — paper_composer runs a 4-step per-section pipeline in dependency order; later sections reference earlier sections' outputs
15. **Section conditionalisation, no template fill** — when fields like `JudgeVerdict.ablation_notes` are Optional and null, paper_writer writes a SKIP sentinel rather than generating boilerplate filler
16. **Reviewer-model diversity eliminates self-review blind spots** — `review.model_override` lets the adversarial reviewer use a different model family
17. **Judge → Coder fixes only failed concepts** — per-concept verdicts in advisor_verdict.yaml drive coder's precise fixes; passing concepts are preserved; iteration_count.txt prevents infinite loops
18. **HITL is not the default** — the 2026-04 refactor removed 3 HITL gates; `kill` / `tweak` decisions are retained as emergency brakes
19. **User input is the source of alignment** (2026-04-25) — in inject mode base_paper / proposal are each single-doc strong-schema extracted, with no kb_retrieve cross-document dilution; alignment_contract is the single source of truth for all subsequent paper-aligned decisions in the pipeline
22. **Inject is a typed-only isolated track** (2026-04-29) — the inject command accepts only `--base-paper` (required) + `--proposal` and other typed roles; the early mixed positional `papers` legacy path is removed (autonomous discovery goes through heartbeat + Miner, not via the inject onramp). base_paper / proposal are **not** written to `knowledge/raw/` / `knowledge/wiki/` / `runtime/raw_registry.yaml`; downstream kb_retrieve is routed by `agent_executor._is_inject_pipeline()` to `search_pipeline_dir()`, **completely blind to the global wiki**; the inject command itself inline-starts a single_run scheduler that self-drives to a terminal pipeline state, with no need to run `run autonomous` afterwards
20. **value_ref instead of inline copy** (2026-04-25) — LOCKED dimensions store only the dotted path referencing a paper_spec leaf; downstream dereferences. Code that inline-copies contract field values is BLOCKed by the validator
21. **Baseline direct import rather than re-implementation** (2026-04-25) — when ref_code is available, coder must `from <pkg> import <symbol>`; preflight scans for re-implementations and BLOCKs; the goal is numerical fidelity, not code-style consistency

## License

For the base framework license, see OpenHarness.
