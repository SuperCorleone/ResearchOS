# Research Brief: Bayesian model-averaged orchestration for self-adaptive multi-agent workflows

- **Proposal ID**: `bayesian_model_averaged_orches`
- **Direction**: agentic-workflow-optimization
- **Gate**: `pass` (total=12/15, repeat_degree=`low`)
- **Gate reasons**: total=12 >= 9

## Research Question
Can Bayesian model averaging over multiple workflow-performance models improve runtime task allocation and replanning in multi-agent AI systems compared with selecting a single best orchestration model?

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
- **Problem gap**: Existing methods fail because they commit to a single orchestration-performance model inside adaptive control loops, ignoring model-selection uncertainty and becoming overconfident when workflow conditions, agent reliability, or task dependencies shift.
- **Our delta**: We propose Bayesian model averaging over multiple candidate orchestration models, with posterior updates at runtime to drive task allocation and replanning decisions instead of selecting one fixed best model.
- **Why it matters**: Which improves task success and reduces unnecessary replanning under dynamic, nonstationary multi-agent workflow conditions.

## Falsification Design
- **Hypothesis**: In dynamic multi-agent workflows with runtime shifts in agent reliability and task dependencies, Bayesian model-averaged orchestration yields higher end-to-end task success and fewer unnecessary replans than a single-best-model orchestration policy.
- **Baseline**: Adaptive orchestration that selects and replans using the single currently best predictive workflow-performance model (e.g., highest posterior/validation-score model) without model averaging.
- **Metric**: Primary: end-to-end task success rate. Secondary: unnecessary replan rate, defined as replans that do not improve downstream execution outcome relative to continuing the prior plan.
- **Failure condition**: Disprove the hypothesis if task success improvement over baseline is < 2% absolute or non-positive, and unnecessary replan rate is not reduced by at least 5% relative across dynamic test scenarios.
- **Expected signal**: At least +2% absolute task success and -5% relative unnecessary replans versus the single-model baseline.
- **Min budget ratio**: 0.25

## Knowledge Context
1. ## [concepts/taming_model_uncertainty_in_self_adaptive_systems_using_baye.md] (score: 0.7285)
2. code_refs:
- https://github.com/matteocamilli/bma-package
created: '2026-04-19T17:49:51.302904+00:00'
source: knowledge/raw/Taming model uncertainty in self-adaptive systems using Bayesian
  Model Averaging.pdf
tags:
- raw-import
- pdf
title: Taming model uncertainty in self-adaptive systems using Bayesian Model Averaging
3. # Taming model uncertainty in self-adaptive systems using Bayesian Model Averaging . . Latest updates: hps://dl.acm.org/doi/10.1145/3524844.3528056 . . RESEARCH-ARTICLE Taming model uncertainty in self-adaptive systems using bayesian model averaging MATTEO CAMILLI, Free University of Bozen-Bolzano, Bolzano, BZ, Italy . RAFFAELA MIRANDOLA, Politecnico di Milano, Milan, MI, Italy . PATRIZIA SCANDURRA, University of Bergamo, Bergamo, BG, Italy . . . Open Access Support provided by: . Politecnico di Milano . Free University of Bozen-Bolzano . University of Bergamo . PDF Download …
