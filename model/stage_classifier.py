"""
Stage outcome classifier: predict how a stage will be decided.

Three classes:
  sprint    — large bunch sprint (n_zero_time >= 6 in top-10)
  gc        — small GC group decides (winner was inside top-15 on GC)
  breakaway — small group, winner was outside top-15 on GC (or one-day non-flat)

The classifier uses only pre-race stage features so it can be applied to
upcoming stages without any result data.
"""
import logging
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

from db.database import get_conn

log = logging.getLogger(__name__)

CLASSIFIER_PATH = Path("model/stage_classifier.pkl")
LABELS = ["sprint", "breakaway", "gc"]

STAGE_FEATURES = [
    "is_flat", "is_hilly", "is_mountain", "is_itt",
    "is_cobbled", "is_gravel",
    "gradient_final_km", "profile_score", "profile_score_per_gradient",
    "elevation_per_km", "distance_km", "elevation_m",
    "stage_num_norm", "is_stage_race",
    "prev_stage_is_mountain", "prev_stage_is_hilly",
    "is_transition_stage",
]


# ── labelling ─────────────────────────────────────────────────────────────────

def _compute_gc_ranks(results: pd.DataFrame, stages: pd.DataFrame) -> pd.DataFrame:
    """
    Compute winner's GC rank before each stage — same logic as features._gc_state
    but returns only the winner row with gc_rank_before.
    """
    st = stages[["id", "race_id", "date", "is_stage_race"]].rename(
        columns={"id": "stage_id"}
    )
    df = results.merge(st, on="stage_id")
    df = df[df["is_stage_race"].fillna(0).astype(int) == 1].copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values(["race_id", "rider_id", "date"])

    gap = df["time_seconds"].where(df["status"] == "finished").fillna(0.0)
    grp = [df["race_id"], df["rider_id"]]
    df["_cum_before"] = gap.groupby(grp).cumsum() - gap
    df["_n_before"] = gap.groupby(grp).cumcount()

    valid = df[df["_n_before"] >= 1].copy()
    if valid.empty:
        return pd.DataFrame(columns=["stage_id", "rider_id", "gc_rank_before"])

    leader = valid.groupby("stage_id")["_cum_before"].transform("min")
    valid["gc_rank_before"] = valid.groupby("stage_id")["_cum_before"].rank(method="min")

    winners = valid[valid["position"] == 1][["stage_id", "gc_rank_before"]]
    return winners


def label_stages(cutoff_date: str | None = None) -> pd.DataFrame:
    """
    Return a DataFrame with one row per stage that has results, labelled with
    'outcome' ∈ {sprint, breakaway, gc}.

    Only stages strictly before cutoff_date are included (no leakage).
    """
    with get_conn() as conn:
        results = pd.read_sql(
            "SELECT id, stage_id, rider_id, position, status, time_seconds FROM results",
            conn,
        )
        stages = pd.read_sql(
            """SELECT s.id, s.race_id, s.stage_num, s.date, s.distance_km,
                      s.elevation_m, s.profile_type, s.surface,
                      s.gradient_final_km, s.profile_score,
                      r.is_stage_race, r.year
               FROM stages s JOIN races r ON s.race_id = r.id""",
            conn,
        )

    stages["date"] = pd.to_datetime(stages["date"], errors="coerce")
    if cutoff_date:
        stages = stages[stages["date"] < pd.Timestamp(cutoff_date)]

    # Only stages that have results
    stages_with_results = set(results["stage_id"].unique())
    stages = stages[stages["id"].isin(stages_with_results)].copy()

    # ── stage sequence features ───────────────────────────────────────────────
    stage_counts = stages.groupby("race_id")["id"].count().rename("race_stage_count")
    stages = stages.join(stage_counts, on="race_id")
    stages["stage_num_norm"] = stages["stage_num"].fillna(1) / stages["race_stage_count"]

    stages_sorted = stages.sort_values(["race_id", "date"])
    stages["prev_profile_type"] = stages_sorted.groupby("race_id")["profile_type"].shift(1).values

    # ── n_zero_time: riders in top-10 finishing same time as winner ───────────
    top10 = results[results["position"] <= 10].copy()
    n_zero = (
        top10[top10["time_seconds"] == 0]
        .groupby("stage_id")["rider_id"].count()
        .rename("n_zero_time")
    )
    stages = stages.join(n_zero, on="id")
    stages["n_zero_time"] = stages["n_zero_time"].fillna(0)

    # ── winner GC rank (stage races only) ────────────────────────────────────
    winner_gc = _compute_gc_ranks(results, stages)
    stages = stages.merge(
        winner_gc.rename(columns={"stage_id": "id"}),
        on="id", how="left",
    )

    # ── compute outcome label ─────────────────────────────────────────────────
    def _label(row):
        ptype = row["profile_type"]
        if ptype in ("itt", "ttt"):
            return "gc"  # solo / team effort — treat as GC-style for routing

        n_zero = row["n_zero_time"]
        gc_rank = row["gc_rank_before"]  # NaN if stage 1 or one-day race
        is_stage = int(row["is_stage_race"] or 0)

        if n_zero >= 6:
            return "sprint"

        # Small group finish
        if is_stage and pd.notna(gc_rank):
            return "gc" if gc_rank <= 15 else "breakaway"

        # One-day race or stage 1 (no GC rank yet): fall back to profile
        return "sprint" if ptype == "flat" else "breakaway"

    stages["outcome"] = stages.apply(_label, axis=1)

    # ── encode stage features needed for training ─────────────────────────────
    for pt in ("flat", "hilly", "mountain", "itt"):
        stages[f"is_{pt}"] = (stages["profile_type"] == pt).astype(int)
    stages["is_cobbled"] = (stages["surface"] == "cobbled").astype(int)
    stages["is_gravel"]  = (stages["surface"] == "gravel").astype(int)
    stages["elevation_per_km"] = stages["elevation_m"] / stages["distance_km"].replace(0, np.nan)
    stages["profile_score_per_gradient"] = (
        stages["profile_score"].fillna(0) / (stages["gradient_final_km"].fillna(0) + 1.0)
    )
    stages["prev_stage_is_mountain"] = (stages["prev_profile_type"] == "mountain").astype(int)
    stages["prev_stage_is_hilly"]    = (stages["prev_profile_type"] == "hilly").astype(int)
    stages["is_transition_stage"]    = (
        (stages["prev_profile_type"] == "mountain") &
        (stages["profile_type"].isin(["hilly", "flat"]))
    ).astype(int)

    return stages


# ── training ──────────────────────────────────────────────────────────────────

def train(cutoff_date: str | None = None) -> None:
    """Train and save the stage outcome classifier."""
    log.info("labelling stages for stage classifier …")
    stages = label_stages(cutoff_date=cutoff_date)
    stages = stages.dropna(subset=["outcome"])

    label_counts = stages["outcome"].value_counts()
    log.info("stage outcome distribution: %s", label_counts.to_dict())

    X = stages[STAGE_FEATURES]
    y = stages["outcome"]

    clf = HistGradientBoostingClassifier(
        max_iter=300,
        learning_rate=0.05,
        max_depth=4,
        min_samples_leaf=10,
        random_state=42,
    )
    clf.fit(X, y)

    CLASSIFIER_PATH.parent.mkdir(exist_ok=True)
    with open(CLASSIFIER_PATH, "wb") as f:
        pickle.dump({"model": clf, "labels": clf.classes_.tolist()}, f)
    log.info("stage classifier saved → %s  (classes: %s)", CLASSIFIER_PATH, clf.classes_.tolist())


# ── inference ─────────────────────────────────────────────────────────────────

def load() -> dict | None:
    """Load the stage classifier. Returns None if not yet trained."""
    if not CLASSIFIER_PATH.exists():
        return None
    with open(CLASSIFIER_PATH, "rb") as f:
        return pickle.load(f)


def predict_proba(stage_rows: pd.DataFrame, clf_bundle: dict) -> pd.DataFrame:
    """
    Given a DataFrame of stage rows (one row per stage, with STAGE_FEATURES),
    return a DataFrame with columns p_sprint, p_breakaway, p_gc.
    """
    clf    = clf_bundle["model"]
    labels = clf_bundle["labels"]

    X = stage_rows[STAGE_FEATURES].copy()
    proba = clf.predict_proba(X)

    out = pd.DataFrame(proba, columns=[f"p_{c}" for c in labels], index=stage_rows.index)
    # Ensure all three columns exist even if a class was absent in training
    for c in ("sprint", "breakaway", "gc"):
        if f"p_{c}" not in out.columns:
            out[f"p_{c}"] = 0.0
    return out[["p_sprint", "p_breakaway", "p_gc"]]
