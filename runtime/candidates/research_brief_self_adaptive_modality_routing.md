# Research Brief: Self-Adaptive Modality Routing for Multimodal Agent Workflows

- **Proposal ID**: `self_adaptive_modality_routing`
- **Direction**: agentic-workflow-optimization
- **Gate**: `pass` (total=12/15, repeat_degree=`low`)
- **Gate reasons**: total=12 >= 9

## Research Question
Can a self-adaptive controller that dynamically selects when an agent workflow should invoke visual reasoning, textual reasoning, or both reduce unnecessary multimodal calls while improving end-task accuracy under changing input difficulty?

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
- **Problem gap**: Existing methods fail because they treat uncertainty-aware orchestration too generically, without explicitly deciding when visual reasoning is actually necessary, leading to unnecessary multimodal calls, higher cost/latency, and brittle failures when a single mode is overconfident.
- **Our delta**: We propose a self-adaptive MAPE-K-style controller that monitors uncertainty and task context to route each subtask among text-only, vision-language, and hybrid reasoning modes.
- **Why it matters**: Which improves end-task accuracy and cost-efficiency under changing input difficulty and heterogeneous multimodal workloads.

## Falsification Design
- **Hypothesis**: A self-adaptive modality-routing controller achieves a better cost-accuracy trade-off than a static always-multimodal pipeline by reducing multimodal invocations while maintaining or improving task accuracy across mixed-difficulty inputs.
- **Baseline**: Static always-multimodal workflow that invokes vision-language reasoning for every eligible subtask.
- **Metric**: Primary: accuracy at equal token/API budget; Secondary: multimodal-call reduction rate at equal or better accuracy.
- **Failure condition**: Disproved if accuracy improvement is < 2% at matched budget, or if multimodal-call reduction is < 15% while accuracy is non-superior on the mixed-difficulty evaluation set.
- **Expected signal**: At least 2% absolute accuracy gain at equal budget, or at least 15% fewer multimodal calls with no accuracy drop.
- **Min budget ratio**: 0.20

## Knowledge Context
1. ## [concepts/taming_model_uncertainty_in_self_adaptive_systems_using_baye.md] (score: 0.7215)
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
