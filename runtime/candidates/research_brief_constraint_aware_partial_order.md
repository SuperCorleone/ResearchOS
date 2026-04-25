# Research Brief: Constraint-aware partial-order task decomposition for multi-agent workflows

- **Proposal ID**: `constraint_aware_partial_order`
- **Direction**: agentic-workflow-optimization
- **Gate**: `pass` (total=12/15, repeat_degree=`low`)
- **Gate reasons**: total=12 >= 9

## Research Question
Can a constraint-aware partial-order task decomposition strategy improve end-to-end success rate and reduce coordination overhead in multi-agent workflows compared with fixed sequential decomposition?

## Scores
| Dimension | Score (0-3) |
|---|---|
| significance | 3 |
| novelty | 2 |
| differentiation | 2 |
| feasibility | 2 |
| falsifiability | 3 |
| **total** | **12 / 15** |

## Innovation Claim
- **Problem gap**: Existing methods fail because they force multi-agent workflows into rigid sequential decompositions or rely on lightweight adaptive controls without explicitly representing dependency, resource, and parallel-execution constraints, causing unnecessary coordination cost and avoidable failure propagation.
- **Our delta**: We propose a constraint-aware partial-order task decomposition strategy that constructs an explicit dependency graph over subtasks, resources, and execution constraints so independent work can proceed in parallel while required ordering is preserved.
- **Why it matters**: Which improves end-to-end success rate and reduces token/latency overhead under workflows with nontrivial dependency structure, shared resource limits, and partially parallelizable subproblems.

## Falsification Design
- **Hypothesis**: On multi-agent workflows with partially independent subtasks, constraint-aware partial-order decomposition yields higher task success and lower coordination cost than fixed sequential decomposition.
- **Baseline**: Fixed sequential decomposition with the same agent set, prompts, and budget, but a linear execution plan.
- **Metric**: Primary: end-to-end success rate; Secondary: total token usage and wall-clock latency.
- **Failure condition**: If success-rate improvement is < 3 percentage points or if token usage and latency are not both reduced by at least 5% relative to the sequential baseline on the evaluation suite, the hypothesis is not supported.
- **Expected signal**: At least +3 percentage points success rate and at least 5% reduction in both tokens and latency versus baseline.
- **Min budget ratio**: 0.25

## Knowledge Context
1. ## [failures/duplicate_ideas.md] (score: 0.7373)
2. title: Duplicate and Rejected Ideas
category: failures
3. # Duplicate and Rejected Research Ideas This page tracks proposals that were rejected by the Topic Evaluator due to high overlap with existing work or insufficient novelty. ## Bayesian uncertainty-aware scheduler for multi-agent workflows - **ID**: bayesian_uncertainty_aware_sch - **Direction**: agentic-workflow-optimization - **Repeat Degree**: low - **Score**: 0/15 - **Gate Result**: reject - **Reasons**: 5-axis quick gate total=6/10 < 7 (reject short-circuit) - **Rejected At**: 2026-04-24T08:42:45.239884+00:00 ## Verifier-guided adaptive task decomposition for multi-agent workflows - **ID**: verifier_guided_adaptive_task_ - **Direction**: agentic-workflow-optimization - …
