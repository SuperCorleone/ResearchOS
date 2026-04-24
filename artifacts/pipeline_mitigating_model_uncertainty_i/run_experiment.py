#!/usr/bin/env python3
"""Stacked ensemble vs BMA comparison for rescue robot hazard prediction."""

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, brier_score_loss, log_loss, roc_auc_score
from sklearn.model_selection import StratifiedKFold, train_test_split
from sklearn.preprocessing import StandardScaler

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

PIPELINE_ID = "mitigating_model_uncertainty_i"
DATA_DIR = Path("data/raw") / PIPELINE_ID
OUTPUT_FILE = Path("artifacts") / PIPELINE_ID / "results.json"


def load_data():
    data_file = DATA_DIR / "training_rescueRobot_450.csv"
    df = pd.read_csv(data_file)
    logger.info(f"Loaded: {df.shape[0]} rows, {df.shape[1]} cols")

    target_col = "hazard"
    X = df.drop(columns=[target_col])
    y = df[target_col]

    for col in X.columns:
        if X[col].dtype == "object":
            X[col] = X[col].map({"Yes": 1, "No": 0}).fillna(0).astype(float)

    logger.info(f"Features: {X.shape[1]}, Target: {y.value_counts().to_dict()}")
    return X, y


def train_bma(X_train, y_train, X_test):
    scaler = StandardScaler()
    X_train_s = scaler.fit_transform(X_train)
    X_test_s = scaler.transform(X_test)

    model = LogisticRegression(max_iter=1000, random_state=42)
    model.fit(X_train_s, y_train)
    return model.predict_proba(X_test_s)[:, 1]


def train_stacking(X_train, y_train, X_test, n_folds=5):
    scaler = StandardScaler()
    X_train_s = scaler.fit_transform(X_train)
    X_test_s = scaler.transform(X_test)

    base_models = [
        LogisticRegression(C=1.0, max_iter=1000, random_state=42),
        LogisticRegression(C=0.5, penalty="l1", solver="saga", max_iter=1000, random_state=42),
        LogisticRegression(C=10.0, max_iter=1000, random_state=43),
    ]

    n_train = len(X_train)
    n_test = len(X_test)
    n_models = len(base_models)

    oof_preds = np.zeros((n_train, n_models))
    test_preds = np.zeros((n_test, n_models))

    skf = StratifiedKFold(n_splits=n_folds, shuffle=True, random_state=42)

    for i, base_model in enumerate(base_models):
        for train_idx, val_idx in skf.split(X_train_s, y_train):
            m = type(base_model)(**base_model.get_params())
            m.fit(X_train_s[train_idx], y_train.iloc[train_idx])
            oof_preds[val_idx, i] = m.predict_proba(X_train_s[val_idx])[:, 1]

        final = type(base_model)(**base_model.get_params())
        final.fit(X_train_s, y_train)
        test_preds[:, i] = final.predict_proba(X_test_s)[:, 1]

    meta = LogisticRegression(max_iter=1000, random_state=42)
    meta.fit(oof_preds, y_train)
    return meta.predict_proba(test_preds)[:, 1]


def compute_metrics(y_true, y_proba):
    y_pred = (y_proba >= 0.5).astype(int)
    return {
        "accuracy": float(accuracy_score(y_true, y_pred)),
        "brier_score": float(brier_score_loss(y_true, y_proba)),
        "log_loss": float(log_loss(y_true, y_proba)),
        "auc": float(roc_auc_score(y_true, y_proba)),
    }


def run(seed=42):
    np.random.seed(seed)
    X, y = load_data()

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=seed, stratify=y
    )

    bma_proba = train_bma(X_train, y_train, X_test)
    stack_proba = train_stacking(X_train, y_train, X_test)

    bma_metrics = compute_metrics(y_test.values, bma_proba)
    stack_metrics = compute_metrics(y_test.values, stack_proba)

    logger.info(f"BMA: {bma_metrics}")
    logger.info(f"Stack: {stack_metrics}")

    return {"bma": bma_metrics, "stacking": stack_metrics, "seed": seed}


def main():
    results = {"config": {"status": "completed"}, "metrics": {}}

    seeds = [42, 123, 456]
    all_results = []

    for seed in seeds:
        logger.info(f"Running with seed {seed}...")
        all_results.append(run(seed))

    bma_acc = [r["bma"]["accuracy"] for r in all_results]
    bma_brier = [r["bma"]["brier_score"] for r in all_results]
    bma_logloss = [r["bma"]["log_loss"] for r in all_results]

    stack_acc = [r["stacking"]["accuracy"] for r in all_results]
    stack_brier = [r["stacking"]["brier_score"] for r in all_results]
    stack_logloss = [r["stacking"]["log_loss"] for r in all_results]

    results["metrics"] = {
        "bma_accuracy_mean": float(np.mean(bma_acc)),
        "bma_accuracy_std": float(np.std(bma_acc)),
        "bma_brier_mean": float(np.mean(bma_brier)),
        "bma_logloss_mean": float(np.mean(bma_logloss)),
        "stacking_accuracy_mean": float(np.mean(stack_acc)),
        "stacking_accuracy_std": float(np.std(stack_acc)),
        "stacking_brier_mean": float(np.mean(stack_brier)),
        "stacking_logloss_mean": float(np.mean(stack_logloss)),
        "improvement_accuracy_pct": float(np.mean(stack_acc) - np.mean(bma_acc)),
        "improvement_brier_pct": float(np.mean(stack_brier) - np.mean(bma_brier)),
    }

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(results, f, indent=2)

    logger.info(f"Results: {json.dumps(results['metrics'], indent=2)}")


if __name__ == "__main__":
    main()