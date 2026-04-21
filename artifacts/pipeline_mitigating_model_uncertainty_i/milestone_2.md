# Milestone Report: Mitigating Model Uncertainty in Self-Adaptive Systems via Stacking-Based Ensemble

**Pipeline ID**: mitigating_model_uncertainty_i
**Milestone**: M2
**Date**: 2026-04-19T20:00:08.180882+00:00

## Executive Summary
This step attempted to evaluate the hypothesis that using a stacking-based ensemble meta-learner instead of Bayesian Model Averaging (BMA) in the MAPE-K Analyze component could improve adaptation quality in self-adaptive systems. However, the analysis was inconclusive due to missing the exact reference dataset required for a valid experiment execution.

## Research Question
Can replacing BMA with a stacking-based ensemble meta-learner in the MAPE-K analyze component reduce model-selection uncertainty and improve adaptation quality in self-adaptive systems?

## Work Completed
1. **Hypothesis**: The hypothesis was that the stacking-based meta-learner would outperform BMA in reducing predictive uncertainty and improving adaptation quality, operationalized as a lower Brier score and fewer requirement-violating adaptations.
2. **Experiment Design**: The design was formulated to replicate previous work using the exact dataset and splits, applying the stacking technique to observe improvements over the baseline.
3. **Results**: No empirical metrics were provided due to the absence of the exact dataset required by the reference study.
4. **Analysis**: INCONCLUSIVE - The analysis identified that without the reference dataset, the evaluation could not support or refute the hypothesis.

## Data Source & Consistency
- **Data origin**: Search-and-rescue robot runtime traces from Taming Model Uncertainty in Self-Adaptive Systems Using Bayesian Model Averaging
- **Reference paper consistency**: Dataset consistency could not be confirmed due to unavailability of the exact reference dataset.
- **Splits used**: Intended to match exactly with the reference study but not executed.
- **Preprocessing**: No preprocessing was applied due to missing dataset.

## Baseline & Proposed Method
- **Baseline method**: Bayesian Model Averaging (BMA) as used in referenced TUNE study.
- **Faithfulness to reference**: Cannot be determined due to missing baseline execution data.
- **Baseline source**: Reference TUNE paper.
- **Proposed method summary**: The proposed method involves replacing BMA with a stacking meta-learner to potentially improve calibration and reduce requirement violations, maintaining all other pipeline elements identical to the original study.

## Detailed Metrics Table
No metrics were captured since the experiment leveraging the correct dataset was not executed.

## Critic Verdict
- **Verdict**: INCONCLUSIVE
- **Rationale**: The study could not proceed due to the absence of the exact dataset and thus failed to produce empirical results.
- **Conditions or caveats**: The required dataset must be obtained to validate the hypothesis as originally intended.

## Key Findings
- The project's key hypothesis remains untested due to execution-blocking data availability problems.
- A valid comparison between stacking and BMA was not conducted due to data incompleteness.

## Open Issues
- FAIL: The exact dataset required from the reference paper was unavailable, preventing a valid experimental run.

## Resource Usage
- Budget allocated: 20000
- Budget used: 1599
- Steps completed: 1

## Computational Cost (G7)
- Total runtime: 0.0 seconds
- Measurement type: estimated
- Hardware: N/A
- Cost breakdown by step:
  | Step      | Runtime | Credits Used |
  |-----------|---------|--------------|
  | CODE      | N/A     | N/A          |
  | RUN       | N/A     | N/A          |

## Artifacts Produced
| Artifact | Status |
|----------|--------|
| hypothesis.md | present |
| experiment_plan.yaml | present |
| results.json | missing (due to missing data) |
| analysis_report.md | present |

## Recommendation
ITERATE

## Next Steps (if continuing)
1. Secure the exact robotics runtime dataset used by the reference paper.
2. Once obtained, execute the planned experiments to validate the hypothesis effectively.