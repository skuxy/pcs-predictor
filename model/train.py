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


def model_paths(gender: str = "men") -> tuple[Path, Path, Path]:
    suffix = "" if gender == "men" else f"_{gender}"
    return (
        MODEL_DIR / f"trained_model{suffix}.pkl",
        MODEL_DIR / f"feature_names{suffix}.json",
        MODEL_DIR / f"metrics{suffix}.json",
    )


# Backwards-compatible aliases for the men's model
MODEL_PATH   = MODEL_DIR / "trained_model.pkl"
META_PATH    = MODEL_DIR / "feature_names.json"
METRICS_PATH = MODEL_DIR / "metrics.json"


def train(
    train_cutoff: str = "2024-05-04",
    val_race_slug: str = "race/giro-d-italia/2024",
    gender: str = "men",
) -> None:
    """
    Train on all data before train_cutoff; validate on val_race_slug.

    Default: train on 2023 + early 2024, validate on Giro 2024 (men).
    For women use gender='women' and appropriate cutoff/val_race.
    """
    model_path, meta_path, metrics_path = model_paths(gender)
    log.info("building features (gender=%s) …", gender)
    df = build_features(gender=gender)

    if df.empty:
        log.error("no features built — run the scraper first")
        return

    # Drop rows where target is unknown (DNF riders have NaN top10)
    df = df.dropna(subset=["top10"])
    df["top10"] = df["top10"].astype(int)

    # Time-based split: train on everything before cutoff, validate on after
    df["stage_date"] = pd.to_datetime(df["stage_date"])
    train_df = df[df["stage_date"] < pd.Timestamp(train_cutoff)]
    val_df   = df[df["stage_date"] >= pd.Timestamp(train_cutoff)]

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
    with open(model_path, "wb") as f:
        pickle.dump(model, f)
    meta_path.write_text(json.dumps(FEATURE_COLS, indent=2))
    metrics_path.write_text(json.dumps(metrics, indent=2))

    log.info("model saved to %s", model_path)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    train()
