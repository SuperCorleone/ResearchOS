# Research Brief: Curriculum-Adaptive Task Decomposition for Multi-Agent Workflows

- **Proposal ID**: `curriculum_adaptive_task_decom`
- **Direction**: agentic-workflow-optimization
- **Gate**: `pass` (total=13/15, repeat_degree=`low`)
- **Gate reasons**: total=13 >= 9

## Research Question
Can a multi-agent workflow controller that adapts task decomposition granularity online using failure patterns and task-difficulty curricula improve end-to-end success rate and reduce redundant agent calls compared with fixed decomposition strategies?

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
- **Problem gap**: Existing methods fail because fixed decomposition granularity treats easy and hard tasks alike, causing either unnecessary agent/tool calls on simple cases or compounded failures on difficult cases, while prior adaptive workflow ideas emphasize routing, uncertainty, or verifier control rather than online curriculum-based granularity adjustment from observed failure trajectories.
- **Our delta**: We propose a multi-agent workflow controller that starts with coarse task decomposition and adaptively refines only recurrently failing or high-difficulty subtasks using online failure patterns and a task-difficulty curriculum.
- **Why it matters**: Which improves end-to-end success rate and lowers token/agent-execution cost under heterogeneous task difficulty and limited inference budgets.

## Falsification Design
- **Hypothesis**: An online curriculum-adaptive decomposition controller achieves higher end-to-end task success than a fixed coarse decomposition baseline while using fewer redundant agent calls than a fixed fine decomposition baseline on mixed-difficulty benchmark tasks.
- **Baseline**: Compare against two fixed strategies: (1) fixed coarse decomposition and (2) fixed fine decomposition, with matched model pool and stopping budget.
- **Metric**: Primary: end-to-end success rate. Secondary: mean agent calls per solved task and total token cost. Aggregate comparison can use success rate subject to non-inferior cost, or cost-normalized success.
- **Failure condition**: Disproved if the adaptive controller improves success rate by less than 2 percentage points over fixed coarse, or fails to reduce mean agent calls/token cost by at least 10% versus fixed fine, or loses on both success and cost-normalized success.
- **Expected signal**: >= 3% absolute success-rate gain over fixed coarse and >= 10% reduction in agent calls or token cost versus fixed fine.
- **Min budget ratio**: 0.20

## Knowledge Context
1. ## [failures/duplicate_ideas.md] (score: 0.7436)
2. title: Duplicate and Rejected Ideas
category: failures
3. # Duplicate and Rejected Research Ideas This page tracks proposals that were rejected by the Topic Evaluator due to high overlap with existing work or insufficient novelty. ## Bayesian uncertainty-aware scheduler for multi-agent workflows - **ID**: bayesian_uncertainty_aware_sch - **Direction**: agentic-workflow-optimization - **Repeat Degree**: low - **Score**: 0/15 - **Gate Result**: reject - **Reasons**: 5-axis quick gate total=6/10 < 7 (reject short-circuit) - **Rejected At**: 2026-04-24T08:42:45.239884+00:00 ## Verifier-guided adaptive task decomposition for multi-agent workflows - **ID**: verifier_guided_adaptive_task_ - **Direction**: agentic-workflow-optimization - …
