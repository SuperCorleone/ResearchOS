# Research Brief: Memory-Bounded Blackboard Scheduling for Multi-Agent Workflows

- **Proposal ID**: `memory_bounded_blackboard_sche`
- **Direction**: agentic-workflow-optimization
- **Gate**: `pass` (total=13/15, repeat_degree=`low`)
- **Gate reasons**: total=13 >= 9

## Research Question
Can a memory-bounded blackboard scheduler that writes only utility-ranked intermediate state to a shared workspace reduce communication cost while maintaining or improving task success in multi-agent workflows?

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
- **Problem gap**: Existing methods fail because they either communicate too much intermediate state or rely on fixed coordination patterns, wasting context and tokens on low-value information when communication budgets are tight.
- **Our delta**: We propose a memory-bounded blackboard scheduler that ranks intermediate artifacts by estimated utility and persists only the highest-value shared state under a strict memory budget.
- **Why it matters**: Which improves task success-per-token and latency under constrained communication, context-window, and multi-agent coordination budgets.

## Falsification Design
- **Hypothesis**: On complex multi-agent tasks, a utility-ranked memory-bounded blackboard scheduler achieves higher success-per-token than an always-communicate shared-workspace baseline while maintaining no worse than 2% absolute task success.
- **Baseline**: Always-communicate shared blackboard that writes all intermediate artifacts to the shared workspace.
- **Metric**: Primary: success-per-1K tokens; secondary: absolute task success rate and end-to-end latency.
- **Failure condition**: Disproved if success-per-1K tokens improves by less than 5% over baseline, or if task success drops by more than 2% absolute, or if latency does not improve.
- **Expected signal**: At least 5% improvement in success-per-1K tokens with non-inferior task success (<=2% absolute drop) and measurable latency reduction.
- **Min budget ratio**: 0.20

## Knowledge Context
1. ## [failures/duplicate_ideas.md] (score: 0.7345)
2. title: Duplicate and Rejected Ideas
category: failures
3. # Duplicate and Rejected Research Ideas This page tracks proposals that were rejected by the Topic Evaluator due to high overlap with existing work or insufficient novelty. ## Bayesian uncertainty-aware scheduler for multi-agent workflows - **ID**: bayesian_uncertainty_aware_sch - **Direction**: agentic-workflow-optimization - **Repeat Degree**: low - **Score**: 0/15 - **Gate Result**: reject - **Reasons**: 5-axis quick gate total=6/10 < 7 (reject short-circuit) - **Rejected At**: 2026-04-24T08:42:45.239884+00:00 ## Verifier-guided adaptive task decomposition for multi-agent workflows - **ID**: verifier_guided_adaptive_task_ - **Direction**: agentic-workflow-optimization - …
