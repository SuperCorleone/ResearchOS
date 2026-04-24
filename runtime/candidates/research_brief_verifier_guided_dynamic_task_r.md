# Research Brief: Verifier-Guided Dynamic Task Recomposition for Multi-Agent LLM Workflows

- **Proposal ID**: `verifier_guided_dynamic_task_r`
- **Direction**: agentic-workflow-optimization
- **Gate**: `pass` (total=14/15, repeat_degree=`low`)
- **Gate reasons**: total=14 >= 9

## Research Question
Can a verifier-guided orchestration policy that dynamically recomposes task decomposition during execution reduce error propagation and token cost compared with fixed planner-executor multi-agent workflows on long-horizon AI tasks?

## Scores
| Dimension | Score (0-3) |
|---|---|
| significance | 3 |
| novelty | 3 |
| differentiation | 3 |
| feasibility | 2 |
| falsifiability | 3 |
| **total** | **14 / 15** |

## Innovation Claim
- **Problem gap**: Existing methods fail because fixed decompositions and generic critique/escalation policies do not correct the task structure itself after early planning errors or new evidence invalidate subtask boundaries.
- **Our delta**: We propose a verifier-guided orchestration policy that treats decomposition as a revisable hypothesis and selectively triggers task recomposition only at high-risk intermediate states during execution.
- **Why it matters**: Which improves end-task success and reduces token cost under long-horizon, error-sensitive multi-agent workflows where early decomposition mistakes would otherwise propagate.

## Falsification Design
- **Hypothesis**: On long-horizon AI tasks, verifier-guided dynamic task recomposition achieves higher end-task success than a fixed planner-executor workflow while using lower or equal token cost than an always-on critique policy.
- **Baseline**: Fixed planner-executor multi-agent workflow, with an always-on critique variant included as a compute-intensive reference.
- **Metric**: Primary: task success rate. Secondary: total tokens consumed per solved task. Evaluate over a benchmark of long-horizon tasks with statistically significant repeated runs.
- **Failure condition**: Disproved if success rate improvement over fixed planner-executor is < 3 percentage points or if token cost per solved task is not lower than the always-on critique policy.
- **Expected signal**: At least +5 percentage points task success over fixed planner-executor and at least 10% lower tokens per solved task than always-on critique.
- **Min budget ratio**: 0.25

## Knowledge Context
1. ## [concepts/taming_model_uncertainty_in_self_adaptive_systems_using_baye.md] (score: 0.7186)
2. code_refs:
- https://github.com/matteocamilli/bma-package
created: '2026-04-22T19:58:13.367914+00:00'
follow_up_questions:
- 'The application of our approach to addi-

  tional case studies in other domains is part of our future work.'
- 'As future work we plan to extend our study by considering

  heterogeneous requirements, such as threshold and optimization

  goals.'
source: knowledge/raw/Taming model uncertainty in self-adaptive systems using Bayesian
  Model Averaging.pdf
tags:
- raw-import
- pdf
title: Taming model uncertainty in self-adaptive systems using Bayesian Model Averaging
3. # Taming model uncertainty in self-adaptive systems using Bayesian Model Averaging . . Latest updates: hps://dl.acm.org/doi/10.1145/3524844.3528056 . . RESEARCH-ARTICLE Taming model uncertainty in self-adaptive systems using bayesian model averaging MATTEO CAMILLI, Free University of Bozen-Bolzano, Bolzano, BZ, Italy . RAFFAELA MIRANDOLA, Politecnico di Milano, Milan, MI, Italy . PATRIZIA SCANDURRA, University of Bergamo, Bergamo, BG, Italy . . . Open Access Support provided by: . Politecnico di Milano . Free University of Bozen-Bolzano . University of Bergamo . PDF Download …
