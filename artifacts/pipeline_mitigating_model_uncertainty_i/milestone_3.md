# Milestone Report: Mitigating Model Uncertainty in Self-Adaptive Systems via Stacking-Based Ensemble

**Pipeline ID**: mitigating_model_uncertainty_i
**Milestone**: M3
**Date**: 2026-04-19T20:22:44.125379+00:00

## Executive Summary
The objective is to replace Bayesian Model Averaging (BMA) with a stacking-based ensemble meta-learner in the MAPE-K analyze component to reduce model-selection uncertainty and improve the adaptation quality. However, the experiment data from the reference paper was not available, making it impossible to advance the evaluation in this milestone.

## Research Question
Can replacing BMA with a stacking-based ensemble meta-learner in the MAPE-K analyze component reduce model-selection uncertainty and improve adaptation quality in self-adaptive systems?

## Work Completed
1. **Hypothesis**: If a stacking-based ensemble meta-learner is used instead of BMA with the same candidate GLM model space and other fixed components, predictive uncertainty error and adaptation quality should improve.
2. **Experiment Design**: Use the reference paper's dataset and methodology with a stacking meta-learner to predict improved performance over BMA.
3. **Results**: Metrics could not be measured as the exact reference dataset was unavailable, leading to a block in execution.
4. **Analysis**: INCONCLUSIVE - Verdict indicates that without the data, no valid assessment of the performance hypothesis could be made.

## Data Source & Consistency
- **Data origin**: Intended as search-and-rescue robot runtime traces from the reference paper.
- **Reference paper consistency**: Not possible due to unavailable dataset.
- **Splits used**: Would match reference paper's temporal split.
- **Preprocessing**: As in the reference paper, but not executed due to data absence.

## Baseline & Proposed Method
- **Baseline method**: Bayesian Model Averaging as per reference study.
- **Faithfulness to reference**: Validation blocked by missing data.
- **Baseline source**: Intended own implementation from reference material.
- **Proposed method summary**: A stacking-based meta-learner is expected to improve prediction and adaptation outcomes.

## Detailed Metrics Table
| Metric | Baseline | Proposed | Delta | Unit |
|--------|----------|----------|-------|------|
| Brier Score | N/A | N/A | N/A | N/A |
| Requirement-violating Adaptation Rate | N/A | N/A | N/A | N/A |

## Critic Verdict
- **Verdict**: INCONCLUSIVE
- **Rationale**: Lack of dataset prevents any empirical evaluation for this hypothesis.
- **Conditions or caveats**: Experiment cannot proceed without original dataset.

## Key Findings
- No progress in hypothesis testing due to lack of data.

## Open Issues
- **FAIL**: Missing dataset prevents hypothesis testing and metric generation.

## Resource Usage
- Budget allocated: 20000
- Budget used: 1759
- Steps completed: 3

## Computational Cost (G7)
- Total runtime: Not applicable due to data absence.
- Measurement type: estimated
- Hardware: Intended CPU context not utilized.
- Cost breakdown by step:
  | Step      | Runtime    | Credits Used |
  |-----------|-----------|--------------|
  | CODE      | N/A       | N/A          |
  | RUN       | N/A       | N/A          |

## Artifacts Produced
| Artifact | Status |
|----------|--------|
| hypothesis.md | present |
| experiment_plan.yaml | present |
| results.json | present but incomplete |
| analysis_report.md | present |

## Recommendation
ITERATE

## Next Steps (if continuing)
1. Obtain the exact dataset used in the reference paper or determine its unavailability officially.
2. Upon obtaining the data, replicate reference study pre-processing and splitting accurately to measure stacking impact.