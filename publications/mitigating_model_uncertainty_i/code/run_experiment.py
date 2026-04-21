#!/usr/bin/env python3
"""Stacking vs BMA experiment for mitigating_model_uncertainty_i.

Dataset: rescue-robot runtime traces from the TUNE/BMA reference paper.
Columns: illuminance, smoke, size, distance, firm, power, band, quality, speed -> hazard (binary)
'firm' is categorical (Yes/No), all others numeric.
"""

import argparse
import hashlib
import json
import random
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold, cross_val_predict
from sklearn.metrics import brier_score_loss, log_loss, accuracy_score
from sklearn.preprocessing import LabelEncoder

SEED = 42
PIPELINE_ID = "mitigating_model_uncertainty_i"

# Resolve data dir relative to project root (3 levels up from this file)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "raw" / PIPELINE_ID

TRAIN_CSV = DATA_DIR / "training_rescueRobot_450.csv"
VAL_CSV   = DATA_DIR / "validation_rescueRobot_450.csv"

TARGET = "hazard"
CATEGORICAL = ["firm"]


def load_data(path: Path) -> tuple:
    df = pd.read_csv(path)
    for col in CATEGORICAL:
        if col in df.columns:
            le = LabelEncoder()
            df[col] = le.fit_transform(df[col].astype(str))
    X = df.drop(columns=[TARGET]).values.astype(float)
    y = df[TARGET].values.astype(int)
    return X, y


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def bma_uniform(X_train, y_train, X_val):
    """BMA approximation: uniform-weight ensemble of 5 logistic GLMs."""
    Cs = [0.01, 0.1, 1.0, 10.0, 100.0]
    preds = []
    for C in Cs:
        m = LogisticRegression(C=C, max_iter=1000, random_state=SEED)
        m.fit(X_train, y_train)
        preds.append(m.predict_proba(X_val)[:, 1])
    return np.mean(preds, axis=0)


def stacking(X_train, y_train, X_val):
    """Stacking: 5 base GLMs -> logistic meta-learner on OOF predictions."""
    Cs = [0.01, 0.1, 1.0, 10.0, 100.0]
    skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=SEED)

    oof_meta = np.zeros((len(X_train), len(Cs)))
    for j, C in enumerate(Cs):
        base = LogisticRegression(C=C, max_iter=1000, random_state=SEED)
        oof_meta[:, j] = cross_val_predict(
            base, X_train, y_train, cv=skf, method="predict_proba"
        )[:, 1]

    val_meta = np.zeros((len(X_val), len(Cs)))
    for j, C in enumerate(Cs):
        base = LogisticRegression(C=C, max_iter=1000, random_state=SEED)
        base.fit(X_train, y_train)
        val_meta[:, j] = base.predict_proba(X_val)[:, 1]

    meta = LogisticRegression(C=1.0, max_iter=1000, random_state=SEED)
    meta.fit(oof_meta, y_train)
    return meta.predict_proba(val_meta)[:, 1]


def compute_metrics(y_true, y_prob, name):
    return {
        f"{name}_brier": float(brier_score_loss(y_true, y_prob)),
        f"{name}_logloss": float(log_loss(y_true, y_prob)),
        f"{name}_accuracy": float(accuracy_score(y_true, (y_prob >= 0.5).astype(int))),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()

    random.seed(args.seed)
    np.random.seed(args.seed)

    t0 = time.monotonic()

    if not TRAIN_CSV.exists() or not VAL_CSV.exists():
        result = {
            "metrics": {},
            "config": {"data_source": "file", "split_seed": args.seed,
                       "data_rows": None, "data_hash": "missing",
                       "pipeline_id": PIPELINE_ID},
            "seed": args.seed, "runtime_seconds": 0.0,
            "notes": f"Data files not found at {DATA_DIR}",
        }
        print(json.dumps(result))
        sys.exit(0)

    X_train, y_train = load_data(TRAIN_CSV)
    X_val,   y_val   = load_data(VAL_CSV)

    data_hash = sha256_file(TRAIN_CSV)

    bma_preds   = bma_uniform(X_train, y_train, X_val)
    stack_preds = stacking(X_train, y_train, X_val)

    m = {}
    m.update(compute_metrics(y_val, bma_preds,   "bma"))
    m.update(compute_metrics(y_val, stack_preds, "stacking"))
    m["brier_improvement_pct"] = float(
        (m["bma_brier"] - m["stacking_brier"]) / m["bma_brier"] * 100
    )

    result = {
        "metrics": m,
        "config": {
            "data_source": "file",
            "split_seed": args.seed,
            "data_rows": int(len(X_train) + len(X_val)),
            "data_hash": data_hash,
            "pipeline_id": PIPELINE_ID,
        },
        "seed": args.seed,
        "runtime_seconds": float(time.monotonic() - t0),
        "notes": "BMA=uniform GLM ensemble (5 C values); Stacking=logistic meta-learner on OOF predictions",
    }
    print(json.dumps(result))


if __name__ == "__main__":
    main()