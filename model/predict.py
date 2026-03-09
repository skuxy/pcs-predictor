"""Generate top-10 probability predictions for a given race/stage."""
import json
import logging
import pickle
from pathlib import Path

import pandas as pd

from features.builder import build_features, FEATURE_COLS
from model.train import MODEL_PATH, META_PATH

log = logging.getLogger(__name__)


def predict_race(race_slug: str, cutoff_date: str) -> pd.DataFrame:
    """
    Predict top-10 probabilities for all starters in a race.

    Parameters
    ----------
    race_slug : e.g. 'race/giro-d-italia/2024'
    cutoff_date : ISO date — only use history before this date for features.
                  Should be the race start date to avoid leakage.

    Returns
    -------
    DataFrame with columns: rider_name, stage_date, race_name, position,
    top10_prob, predicted_top10, sorted by stage_date then top10_prob desc.
    """
    if not MODEL_PATH.exists():
        raise FileNotFoundError(f"No trained model found at {MODEL_PATH}. Run model/train.py first.")

    with open(MODEL_PATH, "rb") as f:
        model = pickle.load(f)

    feature_cols = json.loads(META_PATH.read_text())

    log.info("building features for %s …", race_slug)
    df = build_features(cutoff_date=cutoff_date, race_slug=race_slug)

    if df.empty:
        log.warning("no features built for %s", race_slug)
        return pd.DataFrame()

    X = df[feature_cols]
    df["top10_prob"] = model.predict_proba(X)[:, 1]
    df["predicted_top10"] = (df["top10_prob"] >= 0.5).astype(int)

    return df.sort_values(["stage_date", "top10_prob"], ascending=[True, False])
