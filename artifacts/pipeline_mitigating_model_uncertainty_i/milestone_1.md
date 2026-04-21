# Milestone Report: Mitigating Model Uncertainty in Self-Adaptive Systems via Stacking-Based Ensemble

**Pipeline ID**: mitigating_model_uncertainty_i  
**Milestone**: M1  
**Date**: 2026-04-19

## Executive Summary
The attempt to replace Bayesian Model Averaging (BMA) with a stacking-based ensemble meta-learner in self-adaptive systems' Analyze component could not be empirically tested. The necessary reference dataset to validate the hypothesis was unavailable, rendering the experiment inconclusive.

## Research Question
Can replacing BMA with a stacking-based ensemble meta-learner in the MAPE-K analyze component reduce model-selection uncertainty and improve adaptation quality in self-adaptive systems?

## Work Completed
1. **Hypothesis**: The hypothesis posited that a stacking-based ensemble meta-learner could reduce predictive uncertainty and enhance adaptation quality compared to BMA. However, due to lack of dataset, this could not be tested.
2. **Experiment Design**: The design focused on reusing the exact reference dataset from the original paper, adhering to same preprocessing, and testing the stacking approach's effectiveness against the BMA baseline.
3. **Results**: No empirical metrics were observed as the reference dataset could not be obtained.
4. **Analysis**: Verdict is INCONCLUSIVE due to absence of reference data necessary for hypothesis testing.

## Data Source & Consistency
- **Data origin**: Search-and-rescue robot runtime traces from "Taming Model Uncertainty in Self-Adaptive Systems Using Bayesian Model Averaging"
- **Reference paper consistency**: The experiment was halted due to missing reference data which is crucial for consistency.
- **Splits used**: The original paper’s test/train split is mandatory, but verification could not be conducted.
- **Preprocessing**: Not applicable as no data was processed.

## Baseline & Proposed Method
- **Baseline method**: Bayesian Model Averaging using candidate Generalized Linear Models.
- **Faithfulness to reference**: Unable to verify due to unavailable dataset.
- **Baseline source**: Reference paper "Taming Model Uncertainty in Self-Adaptive Systems Using Bayesian Model Averaging".
- **Proposed method summary**: The proposed method aimed to use a stacking-based ensemble to see if it surpasses BMA in predictive performance.

## Detailed Metrics Table
No metrics available due to the lack of dataset.

## Critic Verdict
- **Verdict**: INCONCLUSIVE 
- **Rationale**: Without the correct dataset, the experiment cannot verify the hypothesis.
- **Conditions or caveats**: Reference data retrieval is necessary for continuing.

## Key Findings
- Missing dataset critical for any comparative analysis against the BMA baseline.
- An INCONCLUSIVE verdict due to lack of empirical results.

## Open Issues
- FAIL: Brier score improvement vs BMA not measured.
- FAIL: Requirement-violating adaptation rate improvement vs BMA not measured.
- Reference dataset unavailability prevents hypothesis testing.

## Resource Usage
- Budget allocated: 20000
- Budget used: 0
- Steps completed: 1

## Computational Cost (G7)
- Total runtime: 0.0
- Measurement type: simulated
- Hardware: Not applicable
- Cost breakdown by step:
  | Step      | Runtime | Credits Used |
  |-----------|---------|--------------|
  | CODE      | N/A     | N/A          |
  | RUN       | 0.0     | 0            |

## Artifacts Produced
| Artifact             | Status   |
|----------------------|----------|
| hypothesis.md        | present  |
| experiment_plan.yaml | present  |
| results.json         | present  |
| analysis_report.md   | present  |

## Recommendation
ITERATE

## Next Steps (if continuing)
1. Obtain the exact reference dataset and verification of preprocessing steps.
2. Re-run the experiment, ensuring the reference paper's conditions and splits are adhered to.
