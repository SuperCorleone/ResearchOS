# Research Brief: Budgeted State Summaries for Communication-Efficient Multi-Agent Workflows

- **Proposal ID**: `budgeted_state_summaries_for_c`
- **Direction**: agentic-workflow-optimization
- **Gate**: `pass` (total=13/15, repeat_degree=`low`)
- **Gate reasons**: total=13 >= 9

## Research Question
Can a workflow controller that adaptively compresses inter-agent messages into budgeted state summaries maintain task success while reducing communication cost and latency in multi-agent orchestration?

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
- **Problem gap**: Existing methods fail because they mainly optimize which agent communicates or when communication occurs, while still transmitting verbose reasoning traces instead of compact, decision-critical workflow state.
- **Our delta**: We propose an adaptive summarization layer that compresses inter-agent messages into budgeted state summaries while preserving uncertainty- and decision-relevant information needed for downstream coordination.
- **Why it matters**: Which improves task success–cost efficiency and end-to-end latency under constrained communication budgets in multi-agent orchestration.

## Falsification Design
- **Hypothesis**: Compared with passing full inter-agent reasoning traces, adaptive budgeted state summaries can reduce total communication tokens and workflow latency by at least 30% while preserving task success within a 2 percentage point absolute drop across representative multi-agent workflow tasks.
- **Baseline**: Standard multi-agent workflow execution with full message passing of uncompressed intermediate reasoning traces between agents.
- **Metric**: Primary: task success rate; Secondary: total communication tokens and end-to-end latency per workflow.
- **Failure condition**: If the summarization method fails to achieve at least 30% reduction in communication tokens or latency, or if task success drops by more than 2 percentage points absolute versus baseline, the hypothesis is disproved.
- **Expected signal**: At least 30% lower communication cost and latency with no more than 2 percentage point absolute degradation in task success.
- **Min budget ratio**: 0.20

## Knowledge Context
1. ## [failures/duplicate_ideas.md] (score: 0.7362)
2. title: Duplicate and Rejected Ideas
category: failures
3. # Duplicate and Rejected Research Ideas This page tracks proposals that were rejected by the Topic Evaluator due to high overlap with existing work or insufficient novelty. ## Bayesian uncertainty-aware scheduler for multi-agent workflows - **ID**: bayesian_uncertainty_aware_sch - **Direction**: agentic-workflow-optimization - **Repeat Degree**: low - **Score**: 0/15 - **Gate Result**: reject - **Reasons**: 5-axis quick gate total=6/10 < 7 (reject short-circuit) - **Rejected At**: 2026-04-24T08:42:45.239884+00:00 ## Verifier-guided adaptive task decomposition for multi-agent workflows - **ID**: verifier_guided_adaptive_task_ - **Direction**: agentic-workflow-optimization - …
