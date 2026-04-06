# ResearchOS

Knowledge-Driven Multi-Agent Research Lab OS, built on [OpenHarness](./OpenHarness).

A fully autonomous research pipeline that fetches academic papers, discovers research gaps, designs and runs experiments, writes LaTeX papers, and compiles findings into a persistent knowledge base — all driven by LLM agents with optional human oversight.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                    OpenHarness Frontend (React TUI)                  │
│  /research dashboard │ launch │ fix │ publish │ approve │ priority  │
└───────────────────────────────┬─────────────────────────────────────┘
                                ↓
┌──────────────────────────┐  ┌──────────────────────────────────────┐
│  Heartbeat (arXiv fetch) │  │  Knowledge Compiler (PDF → wiki)     │
│  Daily PDF download +    │→ │  /knowledge/raw/ → /knowledge/wiki/  │
│  Semantic Scholar filter  │  └──────────────┬───────────────────────┘
└──────────────────────────┘                  ↓
┌─────────────────────────────────────────────────────────────────────┐
│          Knowledge Base (BM25 + n-gram rerank via kb_retrieve)      │
│  /knowledge/wiki/{concepts, methods, experiments, failures, datasets}│
└──────────┬──────────────────────────────────────┬───────────────────┘
           ↓                                      ↓
┌─────────────────────┐    ┌──────────────────────────────────────────┐
│  Knowledge Miner    │    │  Research Runtime (Scheduler)             │
│  Direction-aware    │    │  Stateless tick-based, priority-ordered   │
│  gap discovery      │    │  Budget-enforced, checkpointed            │
└────────┬────────────┘    └────────────────┬─────────────────────────┘
         ↓                                  ↓
┌─────────────────────┐    ┌──────────────────────────────────────────┐
│  Professor (LLM)    │    │  Pipeline State Machine                   │
│  Evaluates proposals│←──→│  PROPOSAL → FAST_FAIL → MAIN → PAPER    │
│  Reviews milestones │    │  Fixer agent for auto-corrections         │
│  go/kill/pivot/pause│    └────────────────┬─────────────────────────┘
└─────────────────────┘                     ↓
                           ┌──────────────────────────────────────────┐
                           │  Output Layer                             │
                           │  paper/<id>/ : draft.md → .tex → .pdf    │
                           │  publications/<id>/ : code + data + report│
                           └──────────────────────────────────────────┘
```

## Full Research Lifecycle

```
Raw papers (PDF / arXiv heartbeat)
  → Knowledge Compiler extracts text → wiki pages
    → Knowledge Miner scans wiki → discovers gap → writes proposal (direction-tagged)
      → Topic Evaluator (8-step): KB search → online retrieval → similarity → scoring
        → innovation statement → falsification design → gating rules
          → PASS: Pipeline created → FAST_FAIL (with falsification_design.yaml)
          → PROFESSOR_REVIEW: Professor reviews borderline case → approve / reject
          → BACKLOG: held for weekly re-evaluation (max 3 re-evals, 30-day expiry)
          → REJECT: archived + recorded to wiki/failures/duplicate_ideas.md
        → Pipeline FAST_FAIL quick validation (reads falsification_design.yaml)
          → hypothesis → design (with data plan) → [code + data fetch] → run → critic (G9 data audit) → report
            → Professor reviews milestone → approve / pivot / kill / pause / hold
              ├─ [human-review OFF] Professor LLM decides directly
              └─ [human-review ON]  Professor writes report → user decides
                   └─ [fix command] Fixer agent auto-corrects → resume
            → MAIN cycle (deeper experiments) → milestone review
              → After max_milestones → PAPER stage
                → draft.md → audit (automated G4+G5) → review.md → paper.tex → paper.pdf
                  → Knowledge compilation → wiki updated (+ datasets/) → COMPLETED
                    → [publish] Package: clean code + data + process report + venue recommendations
```

## Quick Start

```bash
# Install dependencies
pip install -e .
cd OpenHarness && pip install -e . && cd ..

# Launch OpenHarness interactive session
oh

# Inside oh, use slash commands:
#   /research launch              — one-click: heartbeat + autonomous scheduler
#   /research launch --human-review  — professor writes reports, you decide
#   /research trial               — heartbeat once + one topic + one pipeline, then stop
#   /research trial --mode simulate  — trial without LLM calls (for testing)
```

Alternatively, run directly without oh:

```bash
python scripts/run_research.py launch                  # Full autonomous
python scripts/run_research.py launch --human-review   # With human review
python scripts/run_research.py trial                   # Single topic test run
python scripts/run_research.py trial --mode simulate   # Trial without LLM
```

## Entry Points

Four ways to start research, all sharing the same knowledge base:

| Mode | Command | Flow |
|------|---------|------|
| Fully autonomous | `launch` | Heartbeat → Miner → Professor → Pipeline → Paper |
| Trial (single topic) | `trial` | Heartbeat once → Miner once → one pipeline → stop |
| Inject papers | `inject paper1.pdf paper2.pdf` | Papers → wiki → Miner discovers topics → Pipeline |
| Inject + proposal | `inject paper.pdf --title "..." --question "..."` | Papers → wiki + Pipeline directly (skip Miner) |
| Manual proposal | `create --title "..." --question "..."` | Pipeline created directly |

## Frontend (OpenHarness UI)

Start with `oh` to enter the OpenHarness interactive session. All operations are available via slash commands. The React dashboard component renders pipeline status inline.

| Command | Action |
|---------|--------|
| `/research dashboard` | Pipeline overview with priorities and budget |
| `/research launch [--human-review]` | Start scheduler |
| `/research trial [--human-review] [--mode simulate]` | Trial: one topic, one pipeline, then stop |
| `/research create --title "..." --question "..."` | New proposal |
| `/research inject file.pdf [--title "..." --question "..."]` | Import papers |
| `/research fix --pipeline ID` | Auto-fix blocked pipeline from professor report |
| `/research publish --pipeline ID` | Package for submission |
| `/research approve ID [--paper]` | Approve pipeline (optionally advance to paper) |
| `/research kill ID` | Terminate pipeline |
| `/research priority ID 0-3` | Set priority (0=low, 1=normal, 2=high, 3=urgent) |
| `/research pause-miner` / `resume-miner` | Toggle topic discovery |
| `/heartbeat status` / `enable` / `disable` / `fetch` | Paper fetcher control |

## Heartbeat (Daily Paper Fetcher)

Downloads actual paper PDFs from arXiv, filters by Semantic Scholar citation impact, saves to `/knowledge/raw/`.

```yaml
# runtime/heartbeat_config.yaml
enabled: true
fetch_hour: 6
max_papers_per_direction: 5
professor:
  domain: "cs.AI"
  domain_description: "Artificial Intelligence"
directions:
  - name: "agentic-workflow-optimization"
    keywords: ["multi-agent systems", "agent orchestration"]
    priority: high
```

- One professor per domain (single-domain mode)
- Direction-aware: Miner tags proposals by research direction
- Papers < 6 months skip citation filter; older papers need minimum citations
- Graceful fallback on API failures

## Topic Evaluator (8-Step Proposal Gating)

All miner proposals pass through an automated evaluation pipeline before reaching the Professor:

| Step | Action | Output |
|------|--------|--------|
| 1. KB Search | BM25 + character n-gram rerank against wiki, pipelines, failures | Duplicates & related work |
| 2. Online Retrieval | Semantic Scholar + arXiv (24h cache, time decay + citation weighting) | Top-10 papers |
| 3. Four-Axis Similarity | LLM classifies problem/method/setting/metric as similar/different | Repeat degree |
| 4. Five-Dimension Scoring | significance, novelty, differentiation, feasibility, falsifiability (0-3 each) | Score /15 |
| 5. Innovation Statement | "Existing methods fail because ___; We propose ___; Which improves ___" | Forced template |
| 6. Falsification Design | Testable hypothesis, baseline, metric, failure condition, expected signal | `falsification_design` |
| 7. Gating Rules | Direct reject / professor review / pass / backlog based on scores & repeat degree | Gate result |
| 8. Output | `evaluated_<id>.yaml` with all analysis | Evaluation artifact |

**Gating rules** (`runtime/research_config.yaml`):
- **Direct reject**: novelty=0, falsifiability=0, significance<=1, or 4/4 axes similar
- **Professor review**: 3/4 axes similar, or differentiation<=1
- **Pass**: total >= 9, significance >= 2, falsifiability >= 2
- **Backlog**: total in [5, 9) — held for weekly re-evaluation (max 3 re-evals, 30-day expiry)
- **Reject**: total < 5 — archived + recorded to `wiki/failures/duplicate_ideas.md`

## Backlog & Archive

| Directory | Purpose |
|-----------|---------|
| `knowledge/backlog/` | Proposals with potential (score 5-8), awaiting re-evaluation |
| `knowledge/archive/expired_ideas/` | Backlog items that expired (>30 days or 3 re-evals) |
| `knowledge/archive/rejected_ideas/` | Permanently rejected proposals |
| `knowledge/wiki/failures/duplicate_ideas.md` | Wiki record of all rejections for future reference |

The scheduler automatically sweeps expired backlog items and re-evaluates candidates each tick cycle. Promoted proposals become pipelines; re-rejected proposals are permanently archived.

## Pipeline State Machine

```
PROPOSAL ──approve──→ FAST_FAIL ──→ hypothesis → design →[code?]→ run → critic → report
                 │                      ↑                   │
                 │                      │   exp_designer decides: requires_code true/false
                 │  falsification_design.yaml propagated     ↓
                 │  from evaluation to pipeline dir
                     ┌──────────────── MILESTONE ←─── waiting_approval
                     │
                     ├─ approve ──→ MAIN ──→ hypothesis → design →[code?]→ run → critic → report
                     ├─ pivot ────→ MAIN (new direction)
                     ├─ kill ─────→ COMPLETED (+ knowledge compilation)
                     ├─ pause ────→ BLOCKED → [fix command] → auto-correct → resume
                     └─ hold ─────→ BLOCKED (pending re-evaluation)
                                                      ↓
After max_milestones:  MILESTONE → PAPER → draft.md → review.md → paper.tex → paper.pdf
                                                                       ↓
                                                                KNOWLEDGE → wiki → COMPLETED
                                                                       ↓
                                                      [publish] → publications/<id>/
```

## Professor Decision Modes

| Mode | Flag | Behavior |
|------|------|----------|
| Interactive | `run interactive` | Human is the professor (CLI prompts) |
| Autonomous | `run autonomous` | Professor LLM decides; pauses on failure (never auto-approves) |
| Human Review | `run autonomous --human-review` | Professor LLM writes recommendation report (.md); pipeline pauses; human makes final call |

### Decision Types

| Decision | Effect |
|----------|--------|
| `approve` | Continue to next milestone / advance stage |
| `kill` | Terminate pipeline + knowledge compilation |
| `pivot` | Restart MAIN with new direction |
| `pause` | Block pipeline until manual intervention |
| `hold` | Block pipeline pending re-evaluation (e.g., waiting for related results) |

Decisions support `allocated_budget` (explicit total) and `budget_adjustment` (delta) for fine-grained budget control.

### Auto-Fix

When a pipeline is BLOCKED with a professor review report:

```bash
python scripts/run_research.py fix --pipeline ID           # Fix + auto-resume
python scripts/run_research.py fix --pipeline ID --no-resume  # Fix only
```

Fixer agent reads the report, identifies required corrections, modifies experiment code, reruns experiments, writes `correction_report.md`, then resumes the pipeline.

## Data Management

Two-phase data lifecycle integrated into the pipeline:

**Phase 1 — Data Awareness** (in agent prompts):
- `exp_designer` outputs a `data` section in `experiment_plan.yaml`: source, description, preprocessing, split strategy, risk notes
- `coder` enforces split-before-preprocessing, prints data provenance
- `experiment_runner` captures `data_source`, `data_rows`, `split_seed`, `data_hash` in results.json config
- `critic` runs G9 Data Validity Audit (source match, leakage check, provenance verification)

**Phase 2 — Data Lifecycle** (code infrastructure):

| Component | Purpose |
|-----------|---------|
| `fetch_dataset` tool | 5th agent tool: download datasets from URL to `/data/raw/<pipeline_id>/`, auto-generates `data_manifest.yaml` |
| `data_manifest.py` | Code-generated (not LLM): SHA256 hashes per file + directory, row counts, script version tracking |
| `/data/raw/` | Raw downloaded datasets per pipeline |
| `/data/processed/` | Cleaned/split datasets per pipeline |
| `knowledge_compiler` | Extracts `data_manifest.yaml` → `wiki/datasets/data_<id>.md` for knowledge reuse |
| `publisher` | Generates `REPRODUCIBILITY_CHECKLIST.md` with data integrity checks |

**Data manifest example** (`data_manifest.yaml`):
```yaml
dataset_name: "MovieLens-1M"
version: "v2023"
directory_hash: "sha256:7a8f3c..."
total_files: 3
total_rows: 1000209
files:
  - path: "ratings.csv"
    size_bytes: 24594131
    hash: "sha256:9d4e1f..."
    rows: 1000209
cleaning_script: "clean_movielens.py"
cleaning_script_hash: "sha256:2b6c..."
```

## Paper Output & Publication Packaging

Each pipeline produces `paper/<pipeline_id>/`:
```
draft_v1.md → audit_report.md → review_v1.md → paper.tex → paper.pdf
```

Package for submission with:
```bash
python scripts/run_research.py publish --pipeline ID
```

Creates `publications/<pipeline_id>/`:
```
├── paper/                        # LaTeX, PDF, figures
├── code/                         # Clean code (academic English comments) + README
├── data/                         # results.json, datasets, data_manifest.yaml
├── PROCESS_REPORT.md             # Research journey + tiered venue recommendations (A*/A/B/Journal)
└── REPRODUCIBILITY_CHECKLIST.md  # G6 checklist: code, deps, seeds, data, README verification
```

## Pipeline Dashboard

```bash
python scripts/run_research.py dashboard              # Visual overview
python scripts/dashboard.py --priority my_pipeline 3   # Set priority (0-3)
python scripts/dashboard.py --pause-miner              # Stop new topic discovery
python scripts/dashboard.py --resume-miner             # Resume discovery
```

Shows pipeline stages (icons), priority labels, budget bars, research direction tracking, miner status. Scheduler dispatches higher priority pipelines first.

## Directory Layout

| Directory | Purpose | Writer |
|-----------|---------|--------|
| `researchos/` | Python package | Developers |
| `.openharness/plugins/researchos/` | Plugin: agents, commands, hooks | Developers |
| `knowledge/raw/` | Raw PDFs and papers | Heartbeat / manual |
| `knowledge/wiki/` | Compiled knowledge | Knowledge Compiler |
| `knowledge/proposals/` | Miner research ideas + evaluated reports | Knowledge Miner / Topic Evaluator |
| `knowledge/backlog/` | Proposals awaiting re-evaluation | Topic Evaluator |
| `knowledge/archive/` | Expired and rejected proposals | BacklogManager |
| `artifacts/pipeline_<id>/` | Pipeline state, outputs, data_manifest.yaml | Pipeline agents |
| `data/raw/` | Raw downloaded datasets per pipeline | fetch_dataset tool |
| `data/processed/` | Cleaned/split datasets per pipeline | Coder scripts |
| `runtime/` | Scheduler state, budget, heartbeat config | Scheduler |
| `decisions/` | Professor decisions and review reports | Professor agent |
| `paper/<pipeline_id>/` | Paper outputs (md, tex, pdf) | Paper Writer |
| `publications/<pipeline_id>/` | Submission packages | Publisher |

## Core Components

### Runtime (`researchos/runtime/`)

| Module | Role |
|--------|------|
| `scheduler.py` | Tick-based orchestrator with priority scheduling + backlog processing |
| `topic_evaluator.py` | 8-step proposal evaluation pipeline + BacklogManager |
| `agent_executor.py` | OpenAI-compatible tool-use loop (DeepSeek, OpenAI, etc.) |
| `dispatcher.py` | Prompt builder + multi-backend routing |
| `state_manager.py` | Atomic file-based CRUD |
| `knowledge_compiler.py` | PDF/MD → wiki; pipeline artifacts + data manifests → wiki |
| `paper_audit.py` | Automated G4 numerical consistency + G5 statistical completeness scan |
| `latex_compiler.py` | Markdown → LaTeX → PDF |
| `heartbeat.py` | arXiv PDF fetcher with citation filtering |
| `publisher.py` | Submission packaging with venue recommendations |
| `frontend_state.py` | JSON state provider for React dashboard |
| `recovery.py` | Checkpoints and exponential backoff retry |
| `hooks.py` | Artifact write, knowledge access, budget validation, results.json schema enforcement |

### Agents (13)

Agents enforce 9 quality gates (G1-G9) distributed across the pipeline:

| Gate | What | Where Enforced |
|------|------|---------------|
| G1 | Claim-experiment consistency | hypothesis_gen, critic |
| G2 | Causal & control validity | exp_designer |
| G3 | Implementation truth | coder, critic |
| G4 | Data & result consistency | paper_auditor (automated + LLM), reviewer_simulator |
| G5 | Statistical reporting | exp_designer, critic, paper_auditor (automated) |
| G6 | Reproducibility | coder, publisher (REPRODUCIBILITY_CHECKLIST.md) |
| G7 | Resource reporting | exp_designer, reporter |
| G8 | Conclusion boundaries | paper_writer, reviewer_simulator |
| G9 | Data validity & provenance | exp_designer, coder, experiment_runner, critic |

| Agent | Role |
|-------|------|
| `professor` | Evaluates proposals, reviews milestones, go/kill/pivot/pause/hold |
| `knowledge_miner` | Direction-aware gap discovery from wiki |
| `hypothesis_gen` | Falsifiable hypotheses + G1 keyword operationalization |
| `exp_designer` | Experiment plans + data section + G2 causal controls + G5 stats + G7 cost + G9 data design |
| `coder` | Experiment code + G3 claim-to-code mapping + G6 reproducibility + G9 data integrity (fetch + clean) |
| `experiment_runner` | Executes via `execute_python` + `fetch_dataset`; records data provenance (G9) |
| `critic` | Results analysis + G1 claim-experiment audit + G3 implementation check + G5 stats + G9 data validity |
| `reporter` | Milestone reports + G7 computational cost breakdown |
| `paper_writer` | Academic paper drafts + G8 conclusion boundaries + structured limitations |
| `paper_auditor` | Automated G4 numerical scan (code) + G5 statistical completeness (code) + LLM semantic audit |
| `reviewer_simulator` | Peer review + G4 data audit + G8 scope/over-claim check |
| `fixer` | Auto-corrects code from professor review feedback |
| `lit_scout` | Literature survey via kb_retrieve |

## Key Design Principles

1. **File-based state machine** — All state in JSON/YAML; scheduler is stateless and idempotent
2. **Knowledge-driven** — KB access through `kb_retrieve` (BM25 + n-gram rerank); datasets tracked in `wiki/datasets/`
3. **Topic-evaluated + Professor-gated** — Proposals go through 8-step evaluation (similarity, scoring, falsification) then Professor review; never auto-approve on failure
4. **Adaptive pipeline** — Agents decide dynamically whether steps are needed
5. **Human-in-the-loop optional** — `--human-review` for professor recommendation reports
6. **Auto-fix loop** — Fixer agent reads review, corrects code, reruns, resumes
7. **Budget-enforced** — Per-pipeline credit model with overage blocking
8. **Priority-scheduled** — Higher priority pipelines dispatched first
9. **Checkpointed** — Automatic snapshots at milestones with rollback
10. **Publication-ready** — Clean code packaging with venue recommendations + reproducibility checklist
11. **Data-versioned** — SHA256 hashed data manifests, `fetch_dataset` tool, split-before-preprocess enforcement

## Configuration

| Variable | Purpose | Default |
|----------|---------|---------|
| `RESEARCHOS_API_KEY` | LLM API key | (required) |
| `RESEARCHOS_BASE_URL` | API base URL | `https://api.deepseek.com` |
| `RESEARCHOS_MODEL` | Model name | `deepseek-chat` |

## Requirements

- Python >= 3.10
- pydantic >= 2.0, pyyaml >= 6.0, openai >= 1.0, pymupdf >= 1.23
- pdflatex (optional, for PDF compilation)

## License

See OpenHarness for base framework license.
