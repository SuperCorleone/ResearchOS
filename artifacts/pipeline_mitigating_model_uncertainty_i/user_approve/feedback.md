# User Feedback

## What to change

- **DO NOT regenerate run_experiment.py**. The file `artifacts/pipeline_mitigating_model_uncertainty_i/run_experiment.py` has already been written correctly and verified to run successfully. Keep it exactly as-is.

- **Re-execute the existing run_experiment.py**: Run `python artifacts/pipeline_mitigating_model_uncertainty_i/run_experiment.py` to produce real metrics. The script already:
  - Resolves the data path correctly via `Path(__file__).resolve().parent.parent.parent / "data" / "raw" / "mitigating_model_uncertainty_i"`
  - Uses the correct target column `hazard` (binary: 0/1)
  - Encodes the categorical column `firm` (Yes/No → 0/1)
  - Implements BMA as uniform-weight ensemble of 5 logistic GLMs (C=0.01,0.1,1,10,100)
  - Implements Stacking with logistic meta-learner on 5-fold OOF predictions
  - Outputs JSON metrics as the last stdout line

- **Update results.json** with the output of the script. The expected output format is:
  ```json
  {"metrics": {"bma_brier": ..., "bma_logloss": ..., "bma_accuracy": ..., "stacking_brier": ..., "stacking_logloss": ..., "stacking_accuracy": ..., "brier_improvement_pct": ...}, "config": {...}, "seed": 42, "runtime_seconds": ..., "notes": "..."}
  ```

## What to keep

- The existing `run_experiment.py` file — do not overwrite it
- Seed: 42
- The train/val split: `training_rescueRobot_450.csv` / `validation_rescueRobot_450.csv`

## Additional notes

The experiment has been verified locally and produces real metrics. BMA Brier≈0.172, Stacking Brier≈0.173, accuracy ~73%. The data IS available at the correct path.

