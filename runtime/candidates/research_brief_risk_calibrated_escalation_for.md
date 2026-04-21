# Research Brief: Risk-Calibrated Escalation for Multi-Agent Workflows

- **Proposal ID**: `risk_calibrated_escalation_for`
- **Direction**: agentic-workflow-optimization
- **Gate**: `pass` (total=13/15, repeat_degree=`low`)
- **Gate reasons**: total=13 >= 9

## Research Question
Can a multi-agent workflow controller that explicitly models model-selection uncertainty and escalates only high-ambiguity subproblems to additional agents or verification steps improve task success per unit cost versus fixed orchestration policies?

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
- **Problem gap**: Existing methods fail because fixed orchestration policies and single-best-controller decisions treat uncertain subproblems too confidently, causing either unnecessary verification cost or missed escalations on genuinely ambiguous steps.
- **Our delta**: We propose a risk-calibrated multi-agent workflow controller that maintains multiple plausible execution hypotheses and escalates only subproblems whose posterior ambiguity remains high, using added critique, retrieval, or verification selectively rather than uniformly.
- **Why it matters**: Which improves task success per unit cost and reliability under constrained inference budgets and heterogeneous subproblem uncertainty in agentic workflow execution.

## Falsification Design
- **Hypothesis**: A risk-calibrated escalation controller will achieve higher task success per unit cost than a fixed orchestration policy by selectively escalating only high-ambiguity subproblems.
- **Baseline**: A fixed orchestration workflow that applies the same decomposition and verification pattern to every subproblem, with a matched average cost budget.
- **Metric**: Primary metric: task success per dollar (or per 1,000 tokens / unit compute). Secondary check: task success rate at equal average cost.
- **Failure condition**: Disproved if the proposed controller improves task success per unit cost by less than 5% over the fixed-policy baseline, or if success rate is not higher at matched average cost across the evaluation suite.
- **Expected signal**: At least 5-10% improvement in task success per unit cost, with non-inferior or improved absolute success rate at equal average cost.
- **Min budget ratio**: 0.20

## Knowledge Context
1. ## [concepts/taming_model_uncertainty_in_self_adaptive_systems_using_baye.md] (score: 0.7271)
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
