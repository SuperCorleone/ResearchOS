# Milestone Report: Mitigating Model Uncertainty in Self-Adaptive Systems

**Pipeline ID**: mitigating_model_uncertainty_i  
**Milestone**: M1  
**Date**: 2026-04-24  

## Executive Summary
This milestone aimed to explore stacking-based ensemble methods as replacements for Bayesian Model Averaging (BMA) in the MAPE-K loop to handle model-selection uncertainty in self-adaptive systems. However, execution is currently blocked because the exact dataset version, train/test split, and preprocessing pipeline from the reference TUNE paper have not been recovered. No substantive experiment has yet run.

## Research Question
How can stacking-based ensemble methods replace Bayesian Model Averaging (BMA) in the MAPE-K loop to better handle model-selection uncertainty in self-adaptive systems?

## Work Completed
1. **Hypothesis**: The hypothesis posits that replacing BMA with a stacking-based ensemble, while maintaining the same dataset, split, and preprocessing, will improve prediction and adaptation accuracy by 2 percentage points, with computational cost not worsening by more than 5 points.
2. **Experiment Design**: Experiments were planned to compare stacking vs. BMA using the same candidate logistic models on a reference robotics dataset, maintaining identical train/test splits from the reference paper.
3. **Results**: Placeholder results due to execution block, shown as `accuracy_rq1=0.0`, `accuracy_rq2=0.0` (indicating blocked run).
4. **Analysis**: INCONCLUSIVE — execution is blocked due to missing reference-faithful dataset and preprocessing.

## Data Source & Consistency
- **Data origin**: Search-and-rescue robot case study dataset from TUNE paper artifacts
- **Reference paper consistency**: Dataset matches reference paper requirements; however, the exact version and splits need to be verified.
- **Splits used**: Placeholder status; actual split is pending recovery.
- **Preprocessing**: Not yet executed; pipeline matching the reference is yet to be verified.

## Baseline & Proposed Method
- **Baseline method**: TUNE with Bayesian Model Averaging, using logistic regression models in a MAPE-K loop.
- **Faithfulness to reference**: Baseline comparison blocked until data recovery.
- **Baseline source**: Reference paper's repository.
- **Proposed method summary**: Stacking replaces BMA to combine model predictions, maintaining the original adaptation interface.

## Detailed Metrics Table
| Metric | Baseline | Proposed | Delta | Unit |
|--------|----------|----------|-------|------|
| accuracy_rq1 | 0.50* | 0.0 | -0.50 | Accuracy |
| accuracy_rq2 | 0.50* | 0.0 | -0.50 | Accuracy |

(*Baseline values are placeholders pending data recovery and actual measurement)

## Critic Verdict
- **Verdict**: INCONCLUSIVE
- **Rationale**: Unable to execute baseline comparison due to data availability constraints—zero scores reflect blocking, not model failure.
- **Conditions or caveats**: Immediate focus is on recovering reference dataset, splits, and preprocessing.

## Key Findings
- Execution must pause until exact reference split and preprocessing are verified.
- Placeholder results show the need to prioritize faithful reproduction of original conditions.

## Open Issues
- FAIL: Reference-faithful data preprocessing and split not yet recovered.

## Resource Usage
- Budget allocated: 20000 credits
- Budget used: 0 credits
- Steps completed: 1

## Computational Cost (G7)
- Total runtime: 0.0 seconds
- Measurement type: estimated
- Hardware: unspecified
- Cost breakdown by step:
  | Step      | Runtime    | Credits Used |
  |-----------|-----------|--------------|
  | CODE      | N/A       | N/A          |
  | RUN       | 0.0 seconds | 0           |

## Artifacts Produced
| Artifact | Status |
|----------|--------|
| hypothesis.md | present |
| experiment_plan.yaml | present |
| results.json | present |
| analysis_report.md | present |

## Recommendation
ITERATE

## Next Steps (if continuing)
1. Recover the full dataset, preprocessing steps, and original train/test split from reference artifacts.
2. Execute baseline BMA to validate setup fidelity before integrating stacking.