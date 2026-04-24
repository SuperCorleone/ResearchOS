# Research Brief: Entropy-Budgeted Shared Memory for Multi-Agent Workflows

- **Proposal ID**: `entropy_budgeted_shared_memory`
- **Direction**: agentic-workflow-optimization
- **Gate**: `pass` (total=13/15, repeat_degree=`low`)
- **Gate reasons**: total=13 >= 9

## Research Question
Can an entropy-budgeted shared-memory mechanism that compresses and selectively refreshes intermediate agent outputs improve end-to-end task success and token efficiency in multi-agent workflows compared with standard full-context message passing?

## Scores
| Dimension | Score (0-3) |
|---|---|
| significance | 3 |
| novelty | 2 |
| differentiation | 3 |
| feasibility | 2 |
| falsifiability | 3 |
| **total** | **13 / 15** |

## Innovation Claim
- **Problem gap**: Existing methods fail because multi-agent workflows often pass full intermediate context indiscriminately, causing token waste, context saturation, and propagation of low-value or redundant information without adapting memory retention to uncertainty.
- **Our delta**: We propose an entropy-budgeted shared-memory mechanism that compresses intermediate outputs, preserves only high-information artifacts, and selectively refreshes memory when uncertainty rises.
- **Why it matters**: Which improves end-to-end task success and token efficiency under limited context budgets and coordination-heavy multi-agent settings.

## Falsification Design
- **Hypothesis**: Compared with standard full-context message passing, entropy-budgeted shared memory increases or preserves task success while reducing total tokens consumed in multi-agent workflows.
- **Baseline**: A standard multi-agent workflow where each agent receives the full uncompressed history or full shared context at every step.
- **Metric**: Primary: success rate on a benchmark suite of multi-agent tasks; Secondary: total tokens consumed per solved task. Evaluate budgeted memory at matched task sets and fixed model/backbone.
- **Failure condition**: Success rate is not improved or drops by more than 1 percentage point while token reduction is less than 15%, averaged across tasks.
- **Expected signal**: At least +2 percentage points success rate or equivalent non-inferior success (within 1 point) with >=20% lower token use.
- **Min budget ratio**: 0.35

## Knowledge Context
1. ## [failures/duplicate_ideas.md] (score: 0.7327)
2. title: Duplicate and Rejected Ideas
category: failures
3. # Duplicate and Rejected Research Ideas This page tracks proposals that were rejected by the Topic Evaluator due to high overlap with existing work or insufficient novelty. ## Bayesian uncertainty-aware scheduler for multi-agent workflows - **ID**: bayesian_uncertainty_aware_sch - **Direction**: agentic-workflow-optimization - **Repeat Degree**: low - **Score**: 0/15 - **Gate Result**: reject - **Reasons**: 5-axis quick gate total=6/10 < 7 (reject short-circuit) - **Rejected At**: 2026-04-24T08:42:45.239884+00:00 ## Verifier-guided adaptive task decomposition for multi-agent workflows - **ID**: verifier_guided_adaptive_task_ - **Direction**: agentic-workflow-optimization - …
