# Hypothesis

## Research Question
How can stacking-based ensemble methods replace Bayesian Model Averaging (BMA) in self-adaptive systems to better handle model-selection uncertainty in the MAPE-K feedback loop?

## Background
The baseline paper, *Taming Model Uncertainty in Self-adaptive Systems Using Bayesian Model Averaging*, argues that self-adaptive systems suffer from model-selection uncertainty when several plausible predictive models can explain observed behavior. In that work, the TUNE framework augments the MAPE-K loop by using Bayesian Model Averaging in the Analyze phase to combine competing models instead of committing to a single model. The paper reports three explicit research questions: prediction accuracy, effectiveness of adaptation decisions, and cost of computing BMA estimates. The knowledge base digest exposes the paper's exact RQs and indicates that the published evaluation protocol names **accuracy** as the metric for RQ1, RQ2, and RQ3. The same digest also states that the approach was evaluated in a robotics-domain exemplar and that the Plan phase uses metaheuristic optimizing search guided by predictions from the Analyze phase.

The proposal for this pipeline targets a limitation of BMA: BMA weights models by posterior probability and assumes the candidate set is well specified, whereas stacking learns combination weights from held-out predictive performance. Operationally, in this pipeline, a **stacking-based ensemble** means a two-level predictor inserted into the Analyze phase: level-0 base learners generate predictions for the same target variable on the same input window, and a level-1 meta-learner is trained on out-of-fold predictions to produce the final prediction used by the planner. **Better handling model-selection uncertainty** is operationalized as achieving higher prediction **accuracy** than the BMA-based Analyze module under the same data, train/test split, preprocessing pipeline, and planner. Because the baseline paper's future-work note mentions extending beyond a narrow model family and considering broader requirements, stacking is a plausible extension path rather than a completely unrelated replacement.

A second relevant point from the knowledge base is that the baseline paper emphasizes cost-effectiveness, not raw predictive performance alone. Therefore, any claim for replacing BMA must remain aligned to the original evaluation axes: prediction **accuracy**, adaptation-decision **accuracy**, and computational-cost **accuracy** as named by the paper digest. Since the current stage is fast-fail and the full robotics setup and complete TUNE execution environment are not yet available in the pipeline artifacts, the hypothesis must preserve these metrics while explicitly marking parts that require unavailable infrastructure as out of scope for immediate execution.

## Hypothesis Statement
Augmenting the TUNE self-adaptive system with a stacking-based Analyze module in place of the original Bayesian Model Averaging module will improve **accuracy** on the baseline paper's prediction task by at least **5 percentage points** relative to unmodified BMA, using the **same dataset, same train/test split, same preprocessing pipeline, and same planner inputs** as the reference paper, because the stacking meta-learner can learn empirical combination weights from out-of-fold predictive performance instead of relying on posterior model probabilities alone.

Ablation prediction: Removing the level-1 meta-learner and reverting the ensemble to the original BMA combination rule should reduce **accuracy** to within **1 percentage point** of the published BMA baseline on the same split, indicating that any gain is attributable to the stacking mechanism rather than changes in data handling.

Falsification: The hypothesis is falsified if the stacking-based Analyze module improves prediction **accuracy** by **less than 5 percentage points** over BMA, or if removing the stacking meta-learner causes **less than 2 percentage points** degradation in **accuracy** relative to the full stacked variant.

## Evaluation Protocol (aligned to baseline paper)

| RQ | Paper's Exact Metric | Baseline Value (from paper) | Our Implementation |
|----|---------------------|----------------------------|-------------------|
| RQ1 | `accuracy` | not found | Compute prediction **accuracy** for stacked Analyze output and BMA Analyze output on the exact reference-paper test split. |
| RQ2 | `accuracy` | not found | Compute adaptation-decision **accuracy** by feeding each Analyze module's predictions into the unchanged TUNE planner and comparing chosen adaptation decisions against the reference outcome labels on the exact reference-paper setup. |
| RQ3 | `accuracy` | not found | OUT OF SCOPE — the knowledge-base digest confirms an explicit RQ on the cost of calculating BMA estimates, but the exact cost-measurement procedure and full TUNE execution infrastructure are not available in the current pipeline artifacts. |

**Metric Alignment Rule (mandatory)**: Every metric named in Testable Predictions appears in the Evaluation Protocol table above exactly as **accuracy**, matching the baseline paper digest.

## Testable Predictions
1. If the hypothesis is true, prediction **accuracy** for the stacking-based Analyze module will be at least **0.05** higher than BMA **accuracy** on the same reference-paper test split.
2. If the hypothesis is true, adaptation-decision **accuracy** produced by the unchanged planner driven by stacking predictions will be at least **0.03** higher than adaptation-decision **accuracy** produced when the planner is driven by BMA predictions.
3. If the hypothesis is false, stacking will improve prediction **accuracy** by **less than 0.05**, and the ablation that removes the meta-learner will change **accuracy** by **less than 0.02**, indicating that the added stacking mechanism is not justified.

## Key Variables
- **Independent**: Analyze-module combination method, operationalized as either (a) original BMA weighting from the reference paper or (b) stacking with out-of-fold meta-learner trained on base-model predictions.
- **Dependent**: (1) prediction **accuracy** on the reference-paper test split and (2) adaptation-decision **accuracy** after passing predictions to the unchanged planner.
- **Controls**: Same reference dataset, same train/test split, same preprocessing pipeline, same candidate base-model outputs available to the combiner, same planner/metaheuristic search, same target variable, and same evaluation script where possible.

## Evidence Supporting This Hypothesis
- The baseline paper states that model-selection uncertainty in self-adaptive systems can be mitigated by combining plausible models in the MAPE-K loop rather than selecting a single one; this supports replacing a fixed combination rule with a learned one. (source: `concepts/taming_model_uncertainty_in_self_adaptive_systems_using_baye.md`)
- The research proposal states that stacking provides flexible weight learning through a meta-learner trained on held-out predictive performance and can be extended for adaptation under drift, which directly motivates a learned replacement for BMA in Analyze. (source: `concepts/research_proposal.md`)

## Potential Risks
- The baseline paper digest does not expose the exact dataset name, split details, or published numeric baselines, so faithful reproduction may stall until downstream agents recover those details from the paper or code reference.
- Stacking may overfit if the reference dataset is small and the meta-learner is trained without strictly out-of-fold meta-features matching the original preprocessing pipeline.

## Reference Paper Details (if improving upon existing work)
- **Paper**: *Taming Model Uncertainty in Self-adaptive Systems Using Bayesian Model Averaging*
- **Their Method**: The reference method is the TUNE self-adaptive framework enhanced with Bayesian Model Averaging in the Analyze phase. The digest indicates that TUNE combines competing predictive models to mitigate model-selection uncertainty, and that the Plan phase uses metaheuristic optimizing search guided by Analyze predictions. The proposal context further suggests the original TUNE candidate models are GLMs with different variable subsets; this should be verified during experiment design against the paper/code before implementation.
- **Their Data**: The knowledge base states that evaluation used an exemplar case study in the robotics domain. The exact dataset name, source, file format, key statistics, and official train/test split are **not found** in the current digest and must be extracted from the reference paper or code before any comparison. Per pipeline rules, we must use the same dataset, same split, and same preprocessing pipeline as the paper.
- **Their Evaluation**: The paper has explicit RQs in the knowledge base digest: **RQ1** “What is the prediction accuracy obtained through BMA?”, **RQ2** “What is the effectiveness of the adaptation decisions taken?”, and **RQ3** “What is the cost of calculating the BMA estimates in TUNE?” The digest names **accuracy** as the exact metric for each RQ. The full experimental setup details are not fully exposed in the digest and must be recovered before execution.
- **What We Change**: Replace the BMA combiner inside the Analyze phase with a stacking ensemble that trains a level-1 meta-learner on out-of-fold predictions from the same candidate models. The planner, data, split, preprocessing, and downstream control loop remain unchanged.
- **What Must Stay Same**: The robotics-domain reference dataset, its train/test split, preprocessing pipeline, candidate model outputs supplied to the combiner, planner/metaheuristic search, and the paper's named evaluation metric **accuracy** for RQ1–RQ3 must remain the same for fair comparison.

## Working Notes: Extracted Baseline Evaluation Protocol
```yaml
paper_evaluation_protocol:
  rq_list:
    - id: "RQ1"
      question: "What is the prediction accuracy obtained through BMA?"
      metrics: ["accuracy"]
    - id: "RQ2"
      question: "What is the effectiveness of the adaptation decisions taken?"
      metrics: ["accuracy"]
    - id: "RQ3"
      question: "What is the cost of calculating the BMA estimates in TUNE?"
      metrics: ["accuracy"]
```
