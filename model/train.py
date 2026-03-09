"""Train the top-10 finish probability model."""
import json
import logging
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score, average_precision_score
from sklearn.pipeline import Pipeline

from features.builder import build_features, FEATURE_COLS

log = logging.getLogger(__name__)

MODEL_DIR  = Path("model")
MODEL_PATH = MODEL_DIR / "trained_model.pkl"
META_PATH  = MODEL_DIR / "feature_names.json"
METRICS_PATH = MODEL_DIR / "metrics.json"


def train(
    train_cutoff: str = "2024-05-04",   # Giro 2024 start → everything before is training
    val_race_slug: str = "race/giro-d-italia/2024",
) -> None:
    """
    Train on all data before train_cutoff; validate on val_race_slug.

    Default: train on 2023 + early 2024, validate on Giro 2024.
    """
    log.info("building features …")
    df = build_features()

    if df.empty:
        log.error("no features built — run the scraper first")
        return

    # Drop rows where target is unknown (DNF riders have NaN top10)
    df = df.dropna(subset=["top10"])
    df["top10"] = df["top10"].astype(int)

    # Time-based split
    train_df = df[df["stage_date"] < pd.Timestamp(train_cutoff)]
    val_df   = df[df["stage_date"] >= pd.Timestamp(train_cutoff)]

    if val_race_slug:
        val_df = val_df[val_df["race_name"].str.lower().str.contains("giro", na=False)
                        | val_df["stage_date"] >= pd.Timestamp(train_cutoff)]

    log.info("train rows: %d  val rows: %d", len(train_df), len(val_df))
    log.info("train top10 rate: %.3f", train_df["top10"].mean())

    X_train = train_df[FEATURE_COLS]
    y_train = train_df["top10"]
    X_val   = val_df[FEATURE_COLS]
    y_val   = val_df["top10"]

    # HistGradientBoosting handles NaN natively — no imputer needed
    base = HistGradientBoostingClassifier(
        max_iter=500,
        learning_rate=0.05,
        max_depth=4,
        min_samples_leaf=20,
        class_weight="balanced",
        random_state=42,
    )
    model = CalibratedClassifierCV(base, cv=3, method="isotonic")

    log.info("training …")
    model.fit(X_train, y_train)

    # Metrics
    metrics: dict = {}
    if len(val_df) and y_val.nunique() > 1:
        val_proba = model.predict_proba(X_val)[:, 1]
        metrics["val_auc"]  = round(float(roc_auc_score(y_val, val_proba)), 4)
        metrics["val_ap"]   = round(float(average_precision_score(y_val, val_proba)), 4)
        metrics["val_rows"] = len(val_df)
        log.info("val AUC=%.4f  AP=%.4f", metrics["val_auc"], metrics["val_ap"])
    else:
        log.warning("validation set empty or single class — skipping metrics")

    # Save artefacts
    MODEL_DIR.mkdir(exist_ok=True)
    with open(MODEL_PATH, "wb") as f:
        pickle.dump(model, f)
    META_PATH.write_text(json.dumps(FEATURE_COLS, indent=2))
    METRICS_PATH.write_text(json.dumps(metrics, indent=2))

    log.info("model saved to %s", MODEL_PATH)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    train()
