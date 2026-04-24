---
created: '2026-04-24T17:29:39.989580+00:00'
follow_up_questions:
- 'While the original TUNE approach leverages Bayesian Model Averaging (BMA) to address
  this, BMA has limitations: it assumes that the true model is among the candidates
  and weights models by their posterior probability.'
source: knowledge/raw/research proposal.txt
tags:
- raw-import
- txt
title: research proposal
---

# research proposal

Mitigating Model Uncertainty in Self-Adaptive Systems

Problem Statement: 
Self-adaptive systems (SAS) face model-selection uncertainty when multiple competing models could explain system behavior. While the original TUNE approach leverages Bayesian Model Averaging (BMA) to address this, BMA has limitations: it assumes that the true model is among the candidates and weights models by their posterior probability. Our research proposes a novel approach that replaces BMA with a stacking-based ensemble method in the analyze component of the MAPE-K loop.

Motivation for leveraging stacking over BMA:
Recent research demonstrates that stacking offers several advantages over traditional averaging methods:

a)Flexible Weight Learning: Unlike BMA, which uses posterior probabilities as weights, stacking employs a meta-learner that learns optimal combinations of base model predictions based on their actual performance.

b)Adaptive Capability: Stacking frameworks can be enhanced with moving window techniques to adapt to concept drift in dynamic environments, making them particularly suitable for self-adaptive systems operating under uncertainty.

Proposed Methodology: 
The proposed approach enhances the MAPE-K feedback loop by replacing the BMA module with a stacking-based ensemble framework. 
The analyze component implements a two-level stacking architecture:

Level-0: Base Learners 
Maintain a diverse set of candidate models M = {M₁, ..., Mₙ} where each Mᵢ is a different type of predictive model
Unlike the original TUNE, which uses only GLMs with different variable subsets, STACK-SAS incorporates heterogeneous model types:


Level-1: Meta-Learner
The meta-learner learns to combine predictions from all base models.

Input features: Predictions from all base models [p̂₁(σ|X), p̂₂(σ|X), ..., p̂ₙ(σ|X)]
Output: Final stacked prediction p̂_stack(σ|X)

Training Process:
K-Fold Cross-Validation Training: To prevent overfitting, use k-fold cross-validation to generate training data for the meta-learner.
For each fold, train base models on k-1 folds, and generate predictions on the held-out fold.
Collect all out-of-fold predictions to form the meta-features dataset.
Train the meta-learner on this dataset.

