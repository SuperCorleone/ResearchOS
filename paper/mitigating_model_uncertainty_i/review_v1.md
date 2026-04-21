# Peer Review: Mitigating Model Uncertainty in Self-Adaptive Systems via Stacking-Based Ensemble

## Overall Assessment
This draft is clear and appropriately cautious about what it does and does not establish. Its central idea—replacing Bayesian Model Averaging (BMA) with a stacking-based meta-learner in the Analyze stage of a MAPE-K loop while holding the rest of the pipeline fixed—is reasonable and well motivated. However, the paper currently contains no valid empirical evaluation, and by its own account the research question remains unanswered. As a result, the paper is better understood as an evaluation-readiness or negative-status report than as a completed research contribution.

**Recommendation**: Reject in current form as a research paper; potentially acceptable as a short artifact/status report if the venue supports that format.

## Summary
The paper proposes substituting the BMA combiner used in the TUNE self-adaptive systems setting with a stacking-based ensemble trained on out-of-fold predictions from the same candidate GLMs. The intended comparison is well scoped: same robotics traces, same split, same preprocessing, same planner, only the Analyze-stage combiner changes. However, the authors report that the exact reference traces were unavailable, so no faithful BMA-versus-stacking experiment was run and no claims about Brier score, calibration, adaptation quality, or latency are supported.

## Strengths
1. **Clarity and honesty**: The draft is unusually explicit about the boundary between proposed method and validated result. It repeatedly states that no empirical improvement can be claimed.
2. **Well-isolated intervention**: The one-factor experimental design is appropriate for the stated research question. Keeping the model space, preprocessing, split, and planning logic fixed is the right way to test whether the combiner itself matters.
3. **Good problem motivation**: The paper correctly situates the work in the context of model-selection uncertainty in self-adaptive systems, grounded in the prior SEAMS 2022 BMA paper.
4. **Reasonable methodological proposal**: Stacking over out-of-fold base-model predictions is a sensible alternative to BMA, and the proposed logistic meta-learner is a plausible baseline.
5. **Scope discipline**: The limitations section is stronger than average and mostly avoids overclaiming beyond the available evidence.

## Weaknesses
1. **No empirical contribution yet**: The main weakness is decisive: there are no valid experimental results. The paper therefore does not answer its stated research question.
2. **Insufficient novelty evidence**: Based on the knowledge-base context, the prior work clearly establishes BMA for this self-adaptive setting, but this draft does not convincingly show that using stacking as an alternative combiner is itself a substantial research novelty rather than a straightforward methodological substitution.
3. **Audit-flagged numerical issues**: Per `audit_report.md` G4, numerical consistency fails with 7 unmatched numbers. In particular, the report flags unsupported values for seeds 123 and 456, the preregistered threshold 0.02, and the 10% latency threshold. These numbers may represent planned settings rather than measured outcomes, but they currently appear without authoritative support from results.
4. **Audit-flagged statistical incompleteness**: Per `audit_report.md` G5, statistical reporting fails 0/5 items present: effect size, p-value, confidence interval, test statistic, and sample size are all missing. Given that no experiment was executed, this is understandable but still blocks scientific evaluation.
5. **Weak validation of baseline faithfulness**: The paper says baseline-faithfulness conditions matter, but it does not provide a concrete recovery plan or a checklist demonstrating how parity with the original TUNE pipeline would be verified once data are obtained.
6. **Limited contribution framing**: The manuscript currently oscillates between a method paper and a negative report. That ambiguity weakens the contribution because the paper is not yet strong as either a full empirical paper or a dedicated reproducibility/artifact paper.
7. **G8 scope audit concern**: The conclusion is mostly cautious, but the paper still spends substantial space motivating expected benefits of stacking despite having no evidence in the target setting. This is not fatal, but the contribution should be framed even more strictly as a blocked evaluation rather than as a near-miss performance study.

## Evaluation by Criterion

### Clarity: 4/5
The paper is clearly written, logically structured, and easy to follow. The distinction between intended method and unavailable evidence is explicit. Sectioning is effective, and the limitations are concrete.

### Novelty: 2/5
The idea of replacing BMA with stacking is plausible, but the novelty appears modest without either theoretical analysis, a new self-adaptive systems formulation, or empirical evidence. The knowledge base confirms that the BMA baseline is established in this domain; this paper does not yet demonstrate that the stacking substitution yields a nontrivial new scientific contribution.

### Methodology: 2/5
The planned methodology is sound in design, especially the controlled one-factor comparison. But as executed, the study is incomplete. Per `audit_report.md`, numerical consistency fails and statistical reporting is absent, and the exact dataset required for a faithful comparison is missing.

### Results: 1/5
There are effectively no results. The paper is commendably honest about that, but from a review perspective this remains a critical deficiency.

### Significance: 2/5
If completed, the work could be meaningful for uncertainty-aware self-adaptive systems. In its present form, the significance is mostly methodological: it documents evaluation prerequisites and failure modes for faithful reproduction, rather than delivering new evidence about adaptation quality.

## Specific Comments and Questions
1. Can the contribution be reframed explicitly as a reproducibility/status paper rather than a standard research paper?
2. What exact artifact, repository version, or author correspondence is needed to recover the reference runtime traces and preprocessing pipeline?
3. How will baseline fidelity be verified once data are recovered? A concrete checklist would help.
4. Why is logistic regression the preferred stacking combiner? Is there a principled reason beyond simplicity and low variance?
5. If the exact reference traces remain unavailable, is there a secondary contribution the authors can still make—for example, a theory or simulation study clearly separated from the target empirical claim?

## Claim Check Against Knowledge Base
The knowledge base entry for *Taming Model Uncertainty in Self-adaptive Systems Using Bayesian Model Averaging* supports the paper’s description that prior work integrates BMA into the MAPE-K loop and reports improved predictions and planning-related outcomes in a robotics case study. That said, the current draft does not establish comparable evidence for stacking; it only proposes the substitution and documents that evaluation was blocked.

## G8 Scope Audit
- **Conclusion vs. evidence**: Mostly acceptable. The paper appropriately avoids claiming that stacking improves Brier score or adaptation outcomes.
- **Toy-to-real generalization**: No major issue; the draft does not generalize beyond the reference robotics case study.
- **Limitations quality**: Good. The limitations section contains more than three failure conditions and distinguishes fixable from fundamental limitations.
- **Hypotheses unsupported by evidence**: The motivational claims about why stacking might help are reasonable hypotheses, but they remain hypotheses only and should be marked even more explicitly as such throughout.

## Recommended Decision
**Reject** for a standard conference paper in its current form.

The draft is clear, disciplined, and methodologically thoughtful, but it does not yet contain the empirical evidence needed to support its main claim. The most important next step is not polishing the prose but recovering the exact reference dataset and demonstrating faithful baseline reproduction before any BMA-versus-stacking comparison is made.