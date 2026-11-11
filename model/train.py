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
    """(model pickle, feature list, metrics json) for a gender and target."""
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


# Finish thresholds the models are trained for.  top10 is the headline
# output; top3 and win give P(podium) and P(win) next to it.
TARGETS = {"top10": 10, "top3": 3, "win": 1}

# Leaf-wise trees with column subsampling ranked the walk-forward test stages
# better than the earlier depth-4 trees (p@10 +0.005, see
# results/accuracy_2026-11.md); class weights made no difference once the
# output is calibrated, so they are gone.
HGB_PARAMS = dict(
    max_iter=400,
    learning_rate=0.05,
    max_leaf_nodes=31,
    max_depth=None,
    min_samples_leaf=40,
    max_features=0.7,
    l2_regularization=1.0,
)


def training_rows(df: pd.DataFrame, target: str = "top10") -> tuple[pd.DataFrame, pd.Series]:
    """Rows a model can learn from, and their label for ``target``.

    A stage that has not been ridden (start-list rows) has no label, and a
    team time trial's "position" is the team's, so both are left out.  A rider
    who did not finish counts as outside the top N.
    """
    known = df[df["top10"].notna() & (df["profile_type"] != "ttt")]
    y = (known["position"] <= TARGETS[target]).astype(int)
    return known, y


def fit_model(X: pd.DataFrame, y, groups, verbose: int = 0):
    """Fit the calibrated top-N classifier (shared by ``train`` and the
    walk-forward evaluation in ``model.evaluate``).

    Five gradient-boosted models, each fitted on four fifths of the stages and
    isotonic-calibrated on the rest; a prediction is the mean of the five
    calibrated probabilities.
    """
    # HistGradientBoosting handles NaN natively — no imputer needed.
    # verbose=1 makes each boosting round log a progress line (to stdout) so
    # a long-running fit is visibly making progress.
    base = HistGradientBoostingClassifier(random_state=42, verbose=verbose, **HGB_PARAMS)
    # n_jobs=1 (explicit): each fold's HistGradientBoostingClassifier.fit()
    # already saturates all available cores internally via its own OpenMP
    # thread pool, so folds must run one at a time — running them
    # concurrently (n_jobs>1) would oversubscribe every core N-fold-fits-deep.
    cv = GroupKFold(n_splits=5)
    model = CalibratedClassifierCV(base, cv=cv, method="isotonic", n_jobs=1)

    model.fit(X, y, groups=groups)
    return model


def feature_importance(model, X: pd.DataFrame, y, top: int = 30, max_rows: int = 5000,
                       seed: int = 0) -> dict:
    """Permutation importance (drop in average precision when a column is
    shuffled) on a sample of rows.  HistGradientBoosting has no built-in
    importances; this is measured on training rows, so it shows what the
    model leans on rather than what generalises."""
    from sklearn.inspection import permutation_importance

    if len(X) > max_rows:
        idx = np.random.default_rng(seed).choice(len(X), max_rows, replace=False)
        X, y = X.iloc[idx], np.asarray(y)[idx]
    imp = permutation_importance(model, X, y, scoring="average_precision", n_repeats=2,
                                 random_state=seed, n_jobs=1)
    pairs = sorted(zip(X.columns, imp.importances_mean.tolist()), key=lambda kv: kv[1], reverse=True)[:top]
    return {k: round(v, 6) for k, v in pairs}


def train(
    train_cutoff: str | None = None,
    val_race_slug: str | None = None,
    gender: str = "men",
    target: str = "all",
    importances: bool = True,
) -> None:
    """
    Train on every stage before ``train_cutoff`` (default: everything stored).

    ``target`` is top10, top3, win or all (the three of them).  ``val_race_slug``
    is scored only when the race lies on or after the cutoff, i.e. when the
    model has not seen it; for an honest accuracy figure over two seasons run
    ``python main.py evaluate``.
    """
    targets = list(TARGETS) if target == "all" else [target]
    for t in targets:
        if t not in TARGETS:
            raise ValueError(f"Unknown target: {t}")

    log.info("building features (gender=%s) …", gender)
    df = build_features(gender=gender)

    if df.empty:
        log.error("no features built — run the scraper first")
        return

    df["stage_date"] = pd.to_datetime(df["stage_date"])
    cutoff = pd.Timestamp(train_cutoff) if train_cutoff else df["stage_date"].max() + pd.Timedelta(days=1)

    for t in targets:
        model_path, meta_path, metrics_path = model_paths(gender, t)
        known, y = training_rows(df, t)
        mask = known["stage_date"] < cutoff
        train_df, y_train = known[mask], y[mask]
        if train_df.empty or y_train.nunique() < 2:
            log.error("no usable training rows before %s for target %s", cutoff.date(), t)
            continue
        log.info("target %s: train rows %d (%d stages, last %s), positive rate %.3f",
                 t, len(train_df), train_df["stage_id"].nunique(),
                 train_df["stage_date"].max().date(), y_train.mean())

        model = fit_model(train_df[FEATURE_COLS], y_train, train_df["stage_id"].values)

        metrics: dict = {
            "train_cutoff": str(cutoff.date()),
            "last_training_stage": str(train_df["stage_date"].max().date()),
            "gender": gender,
            "target": t,
            "train_rows": len(train_df),
            "train_stages": int(train_df["stage_id"].nunique()),
            "n_features": len(FEATURE_COLS),
        }
        if val_race_slug:
            val_mask = known["race_slug"].str.startswith(val_race_slug, na=False) & (known["stage_date"] >= cutoff)
            val_df, y_val = known[val_mask], y[val_mask]
            if len(val_df) and y_val.nunique() > 1:
                val_proba = model.predict_proba(val_df[FEATURE_COLS])[:, 1]
                metrics["val_race"] = val_race_slug
                metrics["val_auc"]  = round(float(roc_auc_score(y_val, val_proba)), 4)
                metrics["val_ap"]   = round(float(average_precision_score(y_val, val_proba)), 4)
                metrics["val_rows"] = len(val_df)
                log.info("val AUC=%.4f  AP=%.4f", metrics["val_auc"], metrics["val_ap"])
            else:
                log.warning("%s has no stages on or after the cutoff — the model has seen it, "
                            "no validation metrics", val_race_slug)

        if importances and t == "top10":
            log.info("measuring feature importances …")
            recent = train_df["stage_date"] >= train_df["stage_date"].max() - pd.Timedelta(days=365)
            metrics["feature_importances"] = feature_importance(
                model, train_df.loc[recent, FEATURE_COLS], y_train[recent])

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
