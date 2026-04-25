# Research Brief: Token-Budgeted Blackboard Coordination for Multi-Agent Workflows

- **Proposal ID**: `token_budgeted_blackboard_coor`
- **Direction**: agentic-workflow-optimization
- **Gate**: `pass` (total=14/15, repeat_degree=`low`)
- **Gate reasons**: total=14 >= 9

## Research Question
Can a token-budgeted blackboard memory with selective publish/subscribe rules reduce communication overhead in multi-agent workflows without harming task success on decomposition-heavy AI tasks?

## Scores
| Dimension | Score (0-3) |
|---|---|
| significance | 3 |
| novelty | 2 |
| differentiation | 3 |
| feasibility | 3 |
| falsifiability | 3 |
| **total** | **14 / 15** |

## Innovation Claim
- **Problem gap**: Existing methods fail because multi-agent workflows often rely on broad peer-to-peer exchanges or ad hoc communication pruning, which leaves redundant coordination chatter, unstable token usage, and avoidable latency during decomposition-heavy tasks.
- **Our delta**: We propose a token-budgeted blackboard coordination substrate with selective publish/subscribe rules that replaces broad agent-to-agent messaging with shared, relevance-filtered intermediate state under explicit communication constraints.
- **Why it matters**: Which improves token efficiency and coordination latency while preserving task success under decomposition-heavy, multi-agent problem solving with tight communication budgets.

## Falsification Design
- **Hypothesis**: On decomposition-heavy AI tasks, a token-budgeted blackboard with selective publish/subscribe reduces total coordination tokens and end-to-end latency by at least 15% relative to standard peer-to-peer agent messaging, while keeping task success within 1 percentage point of baseline or better.
- **Baseline**: A multi-agent workflow using standard peer-to-peer messaging with the same agents, task decomposition logic, and model budget, but no shared blackboard or selective publish/subscribe filtering.
- **Metric**: Primary: total coordination tokens per completed task. Secondary: end-to-end latency and task success rate over a fixed benchmark suite of decomposition-heavy tasks.
- **Failure condition**: Disproved if coordination tokens improve by less than 15%, or latency improves by less than 10%, or task success drops by more than 1 percentage point versus baseline.
- **Expected signal**: At least 15% fewer coordination tokens and 10% lower latency with task success no worse than -1 percentage point relative to baseline.
- **Min budget ratio**: 0.20

## Knowledge Context
1. ## [failures/duplicate_ideas.md] (score: 0.7315)
2. title: Duplicate and Rejected Ideas
category: failures
3. # Duplicate and Rejected Research Ideas This page tracks proposals that were rejected by the Topic Evaluator due to high overlap with existing work or insufficient novelty. ## Bayesian uncertainty-aware scheduler for multi-agent workflows - **ID**: bayesian_uncertainty_aware_sch - **Direction**: agentic-workflow-optimization - **Repeat Degree**: low - **Score**: 0/15 - **Gate Result**: reject - **Reasons**: 5-axis quick gate total=6/10 < 7 (reject short-circuit) - **Rejected At**: 2026-04-24T08:42:45.239884+00:00 ## Verifier-guided adaptive task decomposition for multi-agent workflows - **ID**: verifier_guided_adaptive_task_ - **Direction**: agentic-workflow-optimization - …
