"""Train the top-10 finish probability model."""
import json
import logging
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn import set_config
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score, average_precision_score
from sklearn.model_selection import GroupKFold

from features.builder import build_features, FEATURE_COLS

log = logging.getLogger(__name__)

# Without this, CalibratedClassifierCV silently drops the `groups` kwarg
# passed to .fit() instead of routing it to GroupKFold's splitter, which then
# raises "The 'groups' parameter should not be None." (sklearn SLEP006).
set_config(enable_metadata_routing=True)

MODEL_DIR  = Path("model")


def model_paths(gender: str = "men", target: str = "top10") -> tuple[Path, Path, Path]:
    suffix = "" if gender == "men" else f"_{gender}"
    target_suffix = "" if target == "top10" else f"_{target}"
    return (
        MODEL_DIR / f"trained_model{suffix}{target_suffix}.pkl",
        MODEL_DIR / f"feature_names{suffix}.json",
        MODEL_DIR / f"metrics{suffix}{target_suffix}.json",
    )


# Backwards-compatible aliases for the men's model
MODEL_PATH   = MODEL_DIR / "trained_model.pkl"
META_PATH    = MODEL_DIR / "feature_names.json"
METRICS_PATH = MODEL_DIR / "metrics.json"


def train(
    train_cutoff: str = "2024-05-04",
    val_race_slug: str = "race/giro-d-italia/2024",
    gender: str = "men",
    target: str = "top10",
) -> None:
    """
    Train on all data before train_cutoff; validate on val_race_slug.

    Default: train on 2023 + early 2024, validate on Giro 2024 (men).
    For women use gender='women' and appropriate cutoff/val_race.
    """
    model_path, meta_path, metrics_path = model_paths(gender, target)
    log.info("building features (gender=%s) …", gender)
    df = build_features(gender=gender)

    if df.empty:
        log.error("no features built — run the scraper first")
        return

    # Drop rows where target is unknown and set target column
    if target == "top10":
        df = df.dropna(subset=["top10"])
        y_col = "top10"
        df[y_col] = df[y_col].astype(int)
    elif target == "top3":
        df = df.dropna(subset=["position"])
        df["_y"] = (df["position"] <= 3).astype(int)
        y_col = "_y"
    elif target == "win":
        df = df.dropna(subset=["position"])
        df["_y"] = (df["position"] == 1).astype(int)
        y_col = "_y"
    else:
        raise ValueError(f"Unknown target: {target}")

    # Time-based split: train on everything before cutoff, validate on val race
    df["stage_date"] = pd.to_datetime(df["stage_date"])
    train_df = df[df["stage_date"] < pd.Timestamp(train_cutoff)]
    val_df   = df[df["race_slug"].str.startswith(val_race_slug, na=False)]

    log.info("train rows: %d  val rows: %d", len(train_df), len(val_df))
    log.info("train top10 rate: %.3f", train_df[y_col].mean())

    X_train = train_df[FEATURE_COLS]
    y_train = train_df[y_col]
    X_val   = val_df[FEATURE_COLS]
    y_val   = val_df[y_col]

    # HistGradientBoosting handles NaN natively — no imputer needed
    #
    # max_iter=500 mostly runs to the cap without early stopping converging
    # first (verified: 5-fold CV on ~209k rows / 111 features hit 300-500/500
    # boosting rounds per fold) — 300 (matching the stage classifier's own
    # budget) cuts ~30% of that CPU time for a <0.002 AUC / <0.004 AP change.
    # verbose=1 makes each boosting round log a progress line (to stdout) so
    # a long-running fit is visibly making progress instead of going silent
    # for the whole training/calibration step, which previously looked
    # indistinguishable from a hang.
    base = HistGradientBoostingClassifier(
        max_iter=300,
        learning_rate=0.05,
        max_depth=4,
        min_samples_leaf=20,
        class_weight="balanced",
        random_state=42,
        verbose=1,
    )
    # n_jobs=1 (explicit): each fold's HistGradientBoostingClassifier.fit()
    # already saturates all available cores internally via its own OpenMP
    # thread pool, so folds must run one at a time — running them
    # concurrently (n_jobs>1) would oversubscribe every core N-fold-fits-deep.
    cv = GroupKFold(n_splits=5)
    model = CalibratedClassifierCV(base, cv=cv, method="isotonic", n_jobs=1)

    log.info("training …")
    model.fit(X_train, y_train, groups=train_df["stage_id"].values)

    # Extract feature importances from calibrated submodels
    try:
        all_importances = [
            clf.estimator.feature_importances_
            for clf in model.calibrated_classifiers_
        ]
        mean_imp = np.mean(all_importances, axis=0)
        feat_imp_pairs = sorted(
            zip(FEATURE_COLS, mean_imp.tolist()),
            key=lambda x: x[1], reverse=True,
        )[:30]
        _feat_imp_dict = {k: round(v, 6) for k, v in feat_imp_pairs}
    except Exception as _e:
        log.warning("could not extract feature importances: %s", _e)
        _feat_imp_dict = {}

    # Metrics
    metrics: dict = {
        "train_cutoff": train_cutoff,
        "val_race": val_race_slug,
        "gender": gender,
        "train_rows": len(train_df),
        "n_features": len(FEATURE_COLS),
    }
    if len(val_df) and y_val.nunique() > 1:
        val_proba = model.predict_proba(X_val)[:, 1]
        metrics["val_auc"]  = round(float(roc_auc_score(y_val, val_proba)), 4)
        metrics["val_ap"]   = round(float(average_precision_score(y_val, val_proba)), 4)
        metrics["val_rows"] = len(val_df)
        log.info("val AUC=%.4f  AP=%.4f", metrics["val_auc"], metrics["val_ap"])
    else:
        log.warning("validation set empty or single class — skipping metrics")

    if _feat_imp_dict:
        metrics["feature_importances"] = _feat_imp_dict

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
