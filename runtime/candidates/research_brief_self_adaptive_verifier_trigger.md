# Research Brief: Self-adaptive verifier-triggered orchestration for multi-agent workflows

- **Proposal ID**: `self_adaptive_verifier_trigger`
- **Direction**: agentic-workflow-optimization
- **Gate**: `pass` (total=12/15, repeat_degree=`low`)
- **Gate reasons**: total=12 >= 9

## Research Question
Can a multi-agent workflow improve task success per token and reduce cascading errors by adding a self-adaptive control loop that selectively triggers verifier and replanning agents only when runtime signals indicate elevated decomposition or execution risk?

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
- **Problem gap**: Existing methods fail because static orchestration and always-on verification either waste tokens on low-risk steps or miss the right intervention points, allowing decomposition and execution errors to cascade across long-horizon workflows.
- **Our delta**: We propose a self-adaptive MAPE-K-style orchestration controller that monitors runtime trace signals, uncertainty proxies, and task-state inconsistencies to selectively trigger verifier and replanning agents only when estimated risk is elevated.
- **Why it matters**: Which improves task success per token and reduces cascading failures under long-horizon, budget-constrained multi-agent execution.

## Falsification Design
- **Hypothesis**: On long-horizon benchmark tasks, a runtime-signal-triggered verifier/replanning controller achieves higher success per token than a static workflow with always-on verification, while maintaining equal or lower total token cost.
- **Baseline**: Static multi-agent orchestration with verifier invoked after every step or subtask (always-on verification).
- **Metric**: Primary: successful task completion per 1,000 tokens; Secondary: final task success rate and mean tokens consumed per task.
- **Failure condition**: Improvement in success per 1,000 tokens is < 5% versus baseline, or success rate is not improved while token cost is equal or higher.
- **Expected signal**: At least 5-10% improvement in success per 1,000 tokens with no increase in average token budget.
- **Min budget ratio**: 0.20

## Knowledge Context
1. ## [concepts/taming_model_uncertainty_in_self_adaptive_systems_using_baye.md] (score: 0.7384)
2. code_refs: - https://github.com/matteocamilli/bma-package created: '2026-04-24T08:36:27.280852+00:00' evaluation_protocol: evaluation_setup: '' has_explicit_rqs: true rq_list: - baseline_result: '' id: RQ1 metrics: - accuracy question: What is the prediction accuracy obtained through BMA? - baseline_result: '' id: RQ2 metrics: - accuracy question: What is the effectiveness of the adaptation decisions taken? - baseline_result: '' id: RQ3 metrics: - accuracy question: What is the cost of calculating the BMA estimates in TUNE? - baseline_result: '' id: RQ1 metrics: - accuracy question: prediction accuracy). To address RQ1 …
3. # Taming model uncertainty in self-adaptive systems using Bayesian Model Averaging . . Latest updates: hps://dl.acm.org/doi/10.1145/3524844.3528056 . . RESEARCH-ARTICLE Taming model uncertainty in self-adaptive systems using bayesian model averaging MATTEO CAMILLI, Free University of Bozen-Bolzano, Bolzano, BZ, Italy . RAFFAELA MIRANDOLA, Politecnico di Milano, Milan, MI, Italy . PATRIZIA SCANDURRA, University of Bergamo, Bergamo, BG, Italy . . . Open Access Support provided by: . Politecnico di Milano . Free University of Bozen-Bolzano . University of Bergamo . PDF Download …
