# Post-Experiment Analysis

## 1. Ablation Studies
| Component                                   | Expected Impact                                               | Priority |
|---------------------------------------------|---------------------------------------------------------------|----------|
| Stacking Combiner                           | Revert to BMA will help verify if improvement stems from it   | High     |
| Out-of-Fold Training of Stacking Weights    | Test uniform averaging to see if learned weights drive change | Medium   |

## 2. Sensitivity Analysis
| Hyperparameter          | Range       | Expected Behaviour                          |
|-------------------------|-------------|--------------------------------------------|
| Meta Learner            | Various     | Sensitivity could show small saturation point in benefit |
| Weight Constraint       | Various     | Sharp improvement but potential overfitting |
| Regularization Strength | 0.0 to 1.0  | Gradual over/underfitting depending on constraint |

## 3. Visualization Plan
| # | Chart Type (Taxonomy #)     | Title                                     | X-axis                    | Y-axis                   | Data Source (results.json path) | Rationale                                                               |
|---|-----------------------------|-------------------------------------------|---------------------------|--------------------------|---------------------------------|-------------------------------------------------------------------------|
| 1 | Type 5 (Stacked bar)        | Component Contribution to Ensemble         | Model Components          | Contribution (%)         | Not Available                   | Visualizes which components impact stacking effectiveness               |
| 2 | Type 11 (Heatmap)           | Hyperparameter Sensitivity                 | Hyperparameters           | Accuracy Improvement     | Hyperparameter Results          | Assesses which hyperparameters affect performance most                  |
| 3 | Type 6 (Line with bands)    | Convergence Comparison of Models           | Epochs                    | Accuracy                 | Model Training Results          | Shows convergence speed of stacking vs BMA                             |

## 4. Statistical Significance
The reported metrics are placeholders without substantive comparisons made. Once the dataset recovery is complete, perform t-tests or bootstrap tests with at least a 95% confidence interval to validate any improvements.

## 5. Failure Modes
- Inaccurate model-selection with a drastically different dataset or altered preprocessing could invalidate results.
- Insufficient dataset recovery may render experiment infeasible.

## 6. Recommended Follow-ups
1. **Dataset Recovery**: Priority focus to achieve a reference-faithful split and preprocessing pipeline restoration.
2. **Baseline Re-run**: Validate that the BMA baseline aligns with the paper before revisiting stacking.
3. **Detailed Statistical Analysis**: Once actual results are gathered, conduct robust statistical analysis.
4. **In-Depth Hyperparameter Exploration**: A thorough grid search post baseline validation to ensure capturing true effects of parameters.

