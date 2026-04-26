# Hypothesis

## Research Question
How can stacking-based ensemble methods replace Bayesian Model Averaging in the MAPE-K loop to better handle model uncertainty in self-adaptive systems?

## Background
The reference paper, *Taming Model Uncertainty in Self-adaptive Systems Using Bayesian Model Averaging*, argues that model uncertainty in self-adaptive systems arises when multiple plausible system models can explain observed behavior, yet the MAPE-K analyze component must still choose or combine them to predict future observations and support planning. The paper proposes augmenting the classical feedback loop with Bayesian Model Averaging (BMA) so that predictions are formed by weighting candidate models by posterior probability rather than selecting a single model. Its empirical evaluation is reported on a robotics-domain exemplar and claims improvements in prediction quality and adaptation planning cost-effectiveness. (source: `base_paper.md` via `kb_retrieve`)

The proposal replaces the BMA combiner inside the analyze phase with a stacking-based ensemble. Operationally, this means keeping the same candidate-model pool and the same downstream MAPE-K planning context, but changing the rule that maps base-model predictions to the final prediction: instead of posterior-probability weighting, a level-1 meta-learner is trained on out-of-fold predictions from level-0 base learners. This change is empirically meaningful because stacking does not require the true model to be present in the candidate set and can learn combination weights from predictive performance rather than Bayesian model evidence. (source: `proposal.md` via `kb_retrieve`)

Related ensemble-learning literature also supports the intuition that stacked generalization can outperform fixed or theory-driven averaging when base learners are complementary and errors are heterogeneous. In this project, “better handle model uncertainty” is operationalized as producing lower prediction error on the same robotics dataset used by the reference paper, and—if the full planning loop is reproducible—yielding better adaptation-decision quality under the same experimental setup. Because this claim depends on measured outcomes under the paper’s protocol, it is falsifiable only by experiment, not by argument alone. (sources: `base_paper.md`, `proposal.md` via `kb_retrieve`)

## Hypothesis Statement
Replacing the Bayesian Model Averaging combiner in the TUNE analyze component with a stacking-based ensemble combiner, while keeping the same candidate-model inputs, robotics-domain dataset, preprocessing, and train/test protocol as the reference paper (rescueRobot 450-obs validation set), will improve f1 by at least 0.05 absolute over the published BMA baseline, without regressing precision or recall.

Ablation prediction: removing the stacking meta-learner and reverting to the unmodified BMA combiner should collapse f1 to within 0.01 of the original BMA baseline, showing that any gain is attributable to the stacking mechanism rather than unrelated implementation changes.

Falsification: the hypothesis is falsified if stacking improves f1 by less than 0.05, or if precision or recall regress relative to BMA by more than 0.02, or if the ablation back to BMA changes f1 by more than 0.01.

## Evaluation Protocol (aligned to baseline paper)

| RQ | Paper's Exact Metric | Baseline Value (from paper) | Our Implementation |
|----|---------------------|----------------------------|-------------------|
| RQ1 | precision | from Figure 7(a), BMA cell — to be recovered by baseline reproduction | Compute precision on the rescueRobot validation set (450 obs) for both BMA and stacking arms, identical preprocessing/split/candidate-model-pool from bma-package replication |
| RQ1 | recall | from Figure 7(b), BMA cell — to be recovered by baseline reproduction | Compute recall on the same validation set, both arms |
| RQ1 | f1 | from Figure 7(c), BMA cell — to be recovered by baseline reproduction | Compute f1 on the same validation set, both arms; f1 is the primary metric for the success/falsification gate |
| RQ2 | adaptation_effectiveness | OUT OF SCOPE — requires MAPE-K planning-loop simulation infra (paper Sec 5.2.2) not available in the analyze-component-only swap | — |
| RQ3 | runtime_cost_seconds | OUT OF SCOPE — paper Sec 5.2.3 measures BMA estimate compute time vs sample size; will be incidentally captured as fit_time_seconds during analyze step | — |

**Metric Alignment Rule (mandatory)**: Every metric named in Testable Predictions MUST appear in the Evaluation Protocol table above. The metric names — precision, recall, f1 — are exact identifiers the coder will use as keys in results.json; downstream contract_guard.assert_metric_alignment requires literal-string equality.

## Testable Predictions
1. If the hypothesis is true, stacking will improve **f1** on the rescueRobot validation split by **at least 0.05 absolute** over the BMA baseline.
2. If the hypothesis is true, stacking will not regress **precision** or **recall** by more than 0.02 absolute relative to BMA on the same split.
3. If the hypothesis is false, stacking will improve **f1** by less than 0.05, or will worsen precision/recall beyond 0.02, and the ablation back to BMA will change **f1** by less than 0.01.

## Key Variables
- **Independent**: Analyze-component combiner used inside TUNE’s MAPE-K loop: Bayesian Model Averaging versus stacking-based ensemble with a meta-learner trained on out-of-fold base-model predictions.
- **Dependent**: The paper’s exact analyze-component prediction metric (RQ1 placeholder above); and, if reproducible, the paper’s exact planning/adaptation metric (RQ2 placeholder above).
- **Controls**: Same robotics-domain dataset as the reference paper; same preprocessing pipeline; same train/test splits; same candidate base-model set unless the paper explicitly allows otherwise; same planning component; same hyperparameter-budget policy; same random seeds where possible.

## Evidence Supporting This Hypothesis
- The reference paper reports that handling model uncertainty through model combination in the MAPE-K loop improves analyze predictions and planning cost-effectiveness over simpler alternatives, establishing that the combiner is a leverage point worth modifying. (source: `base_paper.md`)
- The proposal notes two concrete mechanisms by which stacking may outperform BMA: learned performance-based combination weights and compatibility with moving-window adaptation under concept drift. (source: `proposal.md`)

## Potential Risks
- The exact evaluation metrics, baseline numerical values, and dataset identifiers are not recoverable from the available legacy-pipeline excerpts, creating alignment risk for downstream experiments.
- Stacking may overfit if the robotics dataset is small or highly autocorrelated, especially if meta-features are generated without strict out-of-fold separation.
- If the original BMA baseline uses only GLMs with variable-subset variation, introducing heterogeneous learners could confound “combiner effect” with “base-model set effect”; a fair comparison may require first testing stacking over the same base models used in the paper.

## Reference Paper Details (if improving upon existing work)
- **Paper**: *Taming Model Uncertainty in Self-adaptive Systems Using Bayesian Model Averaging*
- **Their Method**: The paper augments a classical MAPE-K self-adaptation loop—referred to in the proposal as TUNE—by addressing model-selection uncertainty in the analyze phase using Bayesian Model Averaging. Candidate predictive models are maintained as plausible alternatives, and BMA combines them using posterior-probability weights rather than committing to one model. The resulting prediction is then consumed by the planning phase, which the paper states adopts a metaheuristic optimizing search for adaptation decisions.
- **Their Data**: The paper reports an exemplar case study in the robotics domain. From the available knowledge-base excerpt, the exact dataset name, source, file format, sample count, and split protocol are not recoverable. For fair comparison, downstream work must use the exact robotics-domain dataset, train/test splits, and preprocessing pipeline from the paper; no synthetic or substitute data are permitted.
- **Their Evaluation**: The paper explicitly claims empirical evaluation of prediction improvement in the analyze component and cost-effectiveness improvement in planning. However, the exact RQ labels, metric names, and baseline values are not visible in the available excerpt. These must be extracted from the full paper before benchmark execution. Any unavailable metric should remain marked OUT OF SCOPE rather than replaced with a different one.
- **What We Change**: Replace only the BMA combiner in the analyze component with a stacking-based ensemble combiner. Operationally, level-0 base models produce predictions for the target quantity; level-1 meta-learning is trained using k-fold out-of-fold predictions and outputs the final prediction consumed by planning. The intended benefit is better aggregation under model misspecification and nonstationary performance differences among candidate models.
- **What Must Stay Same**: The robotics-domain dataset, preprocessing steps, train/test splits, candidate-model pool used for the baseline comparison, planning component, and the paper’s exact evaluation metrics must remain the same for a valid BMA-versus-stacking comparison.
