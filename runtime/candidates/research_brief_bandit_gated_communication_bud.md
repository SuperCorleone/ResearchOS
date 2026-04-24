# Research Brief: Bandit-Gated Communication Budgets for Multi-Agent Workflows

- **Proposal ID**: `bandit_gated_communication_bud`
- **Direction**: agentic-workflow-optimization
- **Gate**: `pass` (total=13/15, repeat_degree=`low`)
- **Gate reasons**: total=13 >= 9

## Research Question
Can a contextual bandit that adaptively decides when agents should communicate, query peers, or act independently reduce token cost and latency while preserving or improving task success in multi-agent AI workflows?

## Scores
| Dimension | Score (0-3) |
|---|---|
| significance | 3 |
| novelty | 2 |
| differentiation | 2 |
| feasibility | 3 |
| falsifiability | 3 |
| **total** | **13 / 15** |

## Innovation Claim
- **Problem gap**: Existing methods fail because they optimize generic coordination or communication policies without directly learning the marginal value of each communication action under explicit token and latency budgets in LLM-style multi-agent workflows.
- **Our delta**: We propose a contextual bandit that decides at each workflow step whether an agent should communicate, query peers, or act independently based on predicted coordination value relative to budget cost.
- **Why it matters**: Which improves cost-efficiency and responsiveness while preserving task success under constrained token and latency budgets.

## Falsification Design
- **Hypothesis**: A contextual-bandit communication gate can reduce total tokens and end-to-end latency by at least 20% versus fixed always-communicate or heuristic-threshold communication policies while maintaining task success within 2 percentage points on representative multi-agent workflow benchmarks.
- **Baseline**: Fixed communication topology (always communicate at predefined stages), plus a heuristic uncertainty-triggered communication policy.
- **Metric**: Primary: success rate under a fixed token budget; Secondary: total tokens consumed, end-to-end latency, and success-per-1k-tokens Pareto frontier.
- **Failure condition**: Token or latency reduction is < 20% at matched success, or success drops by > 2 percentage points relative to the best fixed/heuristic baseline.
- **Expected signal**: At least 20% lower token cost and 15% lower latency with no more than 2 percentage point loss in success rate, or improved success-per-1k-tokens by at least 10%.
- **Min budget ratio**: 0.20

## Knowledge Context
1. ## [failures/duplicate_ideas.md] (score: 0.7354)
2. title: Duplicate and Rejected Ideas
category: failures
3. # Duplicate and Rejected Research Ideas This page tracks proposals that were rejected by the Topic Evaluator due to high overlap with existing work or insufficient novelty. ## Bayesian uncertainty-aware scheduler for multi-agent workflows - **ID**: bayesian_uncertainty_aware_sch - **Direction**: agentic-workflow-optimization - **Repeat Degree**: low - **Score**: 0/15 - **Gate Result**: reject - **Reasons**: 5-axis quick gate total=6/10 < 7 (reject short-circuit) - **Rejected At**: 2026-04-24T08:42:45.239884+00:00 ## Verifier-guided adaptive task decomposition for multi-agent workflows - **ID**: verifier_guided_adaptive_task_ - **Direction**: agentic-workflow-optimization - …
