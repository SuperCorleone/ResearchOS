# Research Brief: Uncertainty-Triggered Communication Gating for Multi-Agent Workflows

- **Proposal ID**: `uncertainty_triggered_communic`
- **Direction**: agentic-workflow-optimization
- **Gate**: `pass` (total=13/15, repeat_degree=`low`)
- **Gate reasons**: total=13 >= 9

## Research Question
Can uncertainty-triggered communication gating reduce token cost and latency in multi-agent workflows without degrading task success compared with always-on agent communication?

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
- **Problem gap**: Existing methods fail because they treat communication adaptation mainly as control/coordination in physical or networked multi-agent systems, or they optimize workflow orchestration heuristics without framing inter-agent messaging in LLM workflows as a calibrated uncertainty-triggered decision under joint quality, latency, and token-cost objectives.
- **Our delta**: We propose an uncertainty-triggered communication gate for multi-agent workflows that invokes inter-agent communication only when an agent's local uncertainty exceeds a calibrated threshold, with threshold selection explicitly optimized against task success, latency, and token budget.
- **Why it matters**: Which improves communication efficiency and end-to-end responsiveness under constrained token budgets and latency-sensitive multi-agent LLM tasks without materially reducing task success.

## Falsification Design
- **Hypothesis**: Across representative multi-agent LLM workflow tasks, uncertainty-triggered communication gating can reduce inter-agent communication tokens by at least 25% and end-to-end latency by at least 15% while keeping task success within 2 percentage points of always-on communication.
- **Baseline**: Always-on agent communication with the same agents, prompts, and workflow topology but mandatory inter-agent messaging at every coordination step.
- **Metric**: Primary: task success rate, total communication tokens, and end-to-end latency; Secondary: calibrated uncertainty quality (e.g., ECE/Brier) and cost-success Pareto efficiency.
- **Failure condition**: Communication-token reduction < 25%, or latency reduction < 15%, or task success drops by more than 2 percentage points versus always-on baseline.
- **Expected signal**: >=25% fewer communication tokens and >=15% lower latency with <=2 percentage point success degradation.
- **Min budget ratio**: 0.35

## Knowledge Context
1. ## [failures/duplicate_ideas.md] (score: 0.7348)
2. title: Duplicate and Rejected Ideas
category: failures
3. # Duplicate and Rejected Research Ideas This page tracks proposals that were rejected by the Topic Evaluator due to high overlap with existing work or insufficient novelty. ## Bayesian uncertainty-aware scheduler for multi-agent workflows - **ID**: bayesian_uncertainty_aware_sch - **Direction**: agentic-workflow-optimization - **Repeat Degree**: low - **Score**: 0/15 - **Gate Result**: reject - **Reasons**: 5-axis quick gate total=6/10 < 7 (reject short-circuit) - **Rejected At**: 2026-04-24T08:42:45.239884+00:00 ## Verifier-guided adaptive task decomposition for multi-agent workflows - **ID**: verifier_guided_adaptive_task_ - **Direction**: agentic-workflow-optimization - …
