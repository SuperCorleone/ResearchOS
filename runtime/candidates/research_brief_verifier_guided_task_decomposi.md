# Research Brief: Verifier-Guided Task Decomposition for Multi-Agent LLM Workflows

- **Proposal ID**: `verifier_guided_task_decomposi`
- **Direction**: agentic-workflow-optimization
- **Gate**: `pass` (total=13/15, repeat_degree=`low`)
- **Gate reasons**: total=13 >= 9

## Research Question
Can integrating a lightweight verifier that scores decomposition soundness and inter-agent dependency consistency improve task success and reduce wasted agent calls in multi-agent LLM workflows compared with standard planner-only orchestration?

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
- **Problem gap**: Existing methods fail because planner-only multi-agent workflows can commit to flawed task decompositions and inconsistent dependencies too early, causing downstream errors and unnecessary agent calls before problems are detected.
- **Our delta**: We propose a verifier-guided orchestration loop that scores decomposition soundness and inter-agent dependency consistency before and during execution, and triggers selective re-planning only when verification flags issues.
- **Why it matters**: Which improves end-to-end task success and reduces orchestration cost under multi-agent LLM workflows where poor decomposition quality would otherwise propagate expensive execution-time failures.

## Falsification Design
- **Hypothesis**: A verifier-guided orchestration loop achieves higher end-to-end success rate and at least modestly lower agent-call cost than a planner-only workflow on the same multi-agent benchmark tasks.
- **Baseline**: Standard planner-only orchestration that generates a decomposition once and executes it without verification-driven re-planning.
- **Metric**: Primary: task success rate. Secondary: mean agent calls per successfully completed task (or total orchestration cost per task).
- **Failure condition**: Disproved if success-rate improvement is < 2 percentage points or if cost is not reduced by at least 5% versus the planner-only baseline across the evaluation suite.
- **Expected signal**: >= 2 percentage point absolute gain in success rate and >= 5% reduction in agent-call cost.
- **Min budget ratio**: 0.25

## Knowledge Context
1. ## [concepts/taming_model_uncertainty_in_self_adaptive_systems_using_baye.md] (score: 0.7240)
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
