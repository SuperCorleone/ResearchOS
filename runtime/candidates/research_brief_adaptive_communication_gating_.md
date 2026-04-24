# Research Brief: Adaptive communication gating for multi-agent workflows

- **Proposal ID**: `adaptive_communication_gating_`
- **Direction**: agentic-workflow-optimization
- **Gate**: `pass` (total=14/15, repeat_degree=`low`)
- **Gate reasons**: total=14 >= 9

## Research Question
Can a MAPE-K-inspired controller that dynamically adjusts inter-agent communication frequency and routing improve task success per token and latency in multi-agent LLM workflows compared with fixed communication patterns?

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
- **Problem gap**: Existing methods fail because fixed inter-agent communication policies either overspend tokens through unnecessary exchanges or miss critical coordination opportunities when runtime uncertainty changes collaboration needs.
- **Our delta**: We propose a MAPE-K-inspired adaptive communication controller that monitors workflow signals and dynamically triggers, suppresses, delays, or reroutes inter-agent messages instead of relying on static communication patterns.
- **Why it matters**: Which improves task success per token and end-to-end latency under heterogeneous task difficulty and runtime uncertainty in multi-agent LLM workflows.

## Falsification Design
- **Hypothesis**: A MAPE-K-inspired adaptive communication gating controller increases success per token by at least 5% while maintaining or reducing median latency versus a fixed fully-connected communication policy across a diverse benchmark of multi-agent LLM tasks.
- **Baseline**: Static communication topology with fixed message frequency and routing (e.g., every agent broadcasts at each coordination step).
- **Metric**: Primary: task success per 1K tokens. Secondary: median end-to-end latency, with non-inferior absolute task success rate.
- **Failure condition**: success per 1K tokens improvement < 5% or median latency increases by > 5% or absolute task success rate drops by > 2 percentage points versus baseline.
- **Expected signal**: >=5% improvement in task success per 1K tokens with no latency regression and no meaningful quality drop.
- **Min budget ratio**: 0.20

## Knowledge Context
1. ## [concepts/taming_model_uncertainty_in_self_adaptive_systems_using_baye.md] (score: 0.7318)
2. code_refs: - https://github.com/matteocamilli/bma-package created: '2026-04-24T08:36:27.280852+00:00' evaluation_protocol: evaluation_setup: '' has_explicit_rqs: true rq_list: - baseline_result: '' id: RQ1 metrics: - accuracy question: What is the prediction accuracy obtained through BMA? - baseline_result: '' id: RQ2 metrics: - accuracy question: What is the effectiveness of the adaptation decisions taken? - baseline_result: '' id: RQ3 metrics: - accuracy question: What is the cost of calculating the BMA estimates in TUNE? - baseline_result: '' id: RQ1 metrics: - accuracy question: prediction accuracy). To address RQ1 …
3. # Taming model uncertainty in self-adaptive systems using Bayesian Model Averaging . . Latest updates: hps://dl.acm.org/doi/10.1145/3524844.3528056 . . RESEARCH-ARTICLE Taming model uncertainty in self-adaptive systems using bayesian model averaging MATTEO CAMILLI, Free University of Bozen-Bolzano, Bolzano, BZ, Italy . RAFFAELA MIRANDOLA, Politecnico di Milano, Milan, MI, Italy . PATRIZIA SCANDURRA, University of Bergamo, Bergamo, BG, Italy . . . Open Access Support provided by: . Politecnico di Milano . Free University of Bozen-Bolzano . University of Bergamo . PDF Download …
