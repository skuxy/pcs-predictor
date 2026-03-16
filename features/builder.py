"""
Feature engineering: transform raw DB rows into ML-ready feature vectors.

One row per (rider, stage) pair. Target column: top10 (1 if position <= 10).

All rolling calculations are done with vectorised pandas merge/groupby — no
Python-level loops over individual (rider, stage) pairs.
"""

import logging

import pandas as pd
import numpy as np

from db.database import get_conn

log = logging.getLogger(__name__)

PROFILE_TYPES = ["flat", "hilly", "mountain", "itt", "utt"]
SPECIALITIES  = ["gc", "sprinter", "puncher", "classics", "tt", "climber"]

FEATURE_COLS = [
    "avg_pos_30d", "avg_pos_60d", "avg_pos_90d",
    "top10_rate_30d", "top10_rate_90d",
    "win_rate_90d", "dnf_rate_90d",
    "races_last_30d", "days_since_last_race",
    "mountain_avg_pos", "flat_avg_pos", "hilly_avg_pos", "tt_avg_pos",
    "hilly_avg_pos_30d", "hilly_avg_pos_90d", "hilly_top10_rate_90d",
    "mountain_avg_pos_90d", "flat_avg_pos_30d",
    "elevation_per_km",
    "distance_km", "elevation_m",
    "stage_num_norm", "is_stage_race",
    "prev_stage_is_mountain", "prev_stage_is_hilly",
    "pcs_rank",
    "is_flat", "is_hilly", "is_mountain", "is_itt", "is_utt",
    "is_cobbled", "is_gravel",
    "spec_gc", "spec_sprinter", "spec_puncher",
    "spec_classics", "spec_tt", "spec_climber",
]


def build_features(
    cutoff_date: str | None = None,
    race_slug: str | None = None,
    gender: str = "men",
) -> pd.DataFrame:
    """
    Build a feature DataFrame efficiently using vectorised operations.

    Parameters
    ----------
    cutoff_date : ISO date string e.g. '2024-05-04'
        Only stages ON or AFTER this date are prediction targets.
        History still uses all data before each stage's own date.
    race_slug : str e.g. 'race/giro-d-italia/2024'
        If given, only stages in this race are targets.
    gender : 'men' or 'women'
        Filter races by gender.
    """
    log.info("loading data from DB …")
    with get_conn() as conn:
        results = pd.read_sql(
            "SELECT id, stage_id, rider_id, position, status, time_seconds FROM results",
            conn,
        )
        stages = pd.read_sql(
            "SELECT id, race_id, stage_num, date, distance_km, elevation_m, profile_type, surface FROM stages",
            conn,
        )
        races = pd.read_sql(
            "SELECT id, pcs_slug, name, year, is_stage_race, gender FROM races",
            conn,
        )
        riders = pd.read_sql(
            "SELECT id, name, pcs_rank, speciality FROM riders",
            conn,
        )

    # ── filter by gender ──────────────────────────────────────────────────────
    races = races[races["gender"].fillna("men") == gender]

    # ── basic prep ────────────────────────────────────────────────────────────
    stages = stages.merge(
        races[["id", "name", "pcs_slug", "is_stage_race"]].rename(
            columns={"id": "race_id", "name": "race_name", "pcs_slug": "race_slug"}
        ),
        on="race_id",
    )
    stages["date"] = pd.to_datetime(stages["date"], errors="coerce")
    stages = stages.dropna(subset=["date"])

    # stage_num_norm: stage number / total stages in race
    stage_counts = stages.groupby("race_id")["id"].count().rename("race_stage_count")
    stages = stages.join(stage_counts, on="race_id")
    stages["stage_num_norm"] = stages["stage_num"].fillna(1) / stages["race_stage_count"]

    # prev_stage profile: sort by (race_id, date), shift within each race
    stages_sorted = stages.sort_values(["race_id", "date"])
    prev = stages_sorted.groupby("race_id")["profile_type"].shift(1)
    stages["prev_profile_type"] = prev.values

    results["is_finished"] = results["status"] == "finished"
    results["is_dnf"]      = results["status"].isin(["dnf", "dns", "otl", "dsq"])
    results["is_top10"]    = (results["position"] <= 10) & results["position"].notna()
    results["is_win"]      = results["position"] == 1

    # Join stage date onto results (needed for rolling)
    results = results.merge(
        stages[["id", "date", "profile_type"]].rename(columns={"id": "stage_id"}),
        on="stage_id",
    )

    # ── select target stages ──────────────────────────────────────────────────
    target_stages = stages.copy()
    if cutoff_date:
        target_stages = target_stages[target_stages["date"] >= pd.Timestamp(cutoff_date)]
    if race_slug:
        target_stages = target_stages[target_stages["race_slug"].str.startswith(race_slug, na=False)]

    if target_stages.empty:
        log.warning("no target stages found")
        return pd.DataFrame()

    log.info("target stages: %d", len(target_stages))

    # ── base: one row per (rider, target stage) from results ──────────────────
    base = results[results["stage_id"].isin(target_stages["id"])].copy()
    base = base.merge(
        target_stages[["id", "date", "distance_km", "elevation_m", "profile_type",
                        "surface", "prev_profile_type",
                        "stage_num_norm", "is_stage_race", "race_name", "race_slug"]].rename(
            columns={"id": "stage_id", "date": "stage_date", "profile_type": "stage_profile"}
        ),
        on="stage_id",
    )
    base["top10"] = base["is_top10"].astype(float)

    # ── rolling features via self-join ────────────────────────────────────────
    # For each (rider, stage_date), we need stats from results where date < stage_date.
    # Strategy: cross-join on rider_id then filter by date, aggregate with groupby.
    # To keep this efficient we use a merge + conditional aggregation pattern.

    log.info("computing rolling features …")

    # history = all results with dates, for rolling lookups
    hist = results[["rider_id", "date", "position", "is_finished",
                     "is_dnf", "is_top10", "is_win", "profile_type", "stage_id"]].copy()

    # Join base (rider, stage_date) onto hist on rider_id, then filter date < stage_date
    # We do this with a merge and then mask — tractable because we group immediately after
    joined = base[["rider_id", "stage_id", "stage_date"]].drop_duplicates().merge(
        hist.rename(columns={
            "date": "hist_date", "stage_id": "hist_stage_id",
            "profile_type": "hist_profile",
        }),
        on="rider_id",
        how="left",
    )
    # Only past results
    joined = joined[joined["hist_date"] < joined["stage_date"]]

    def _rolling(days: int | None, col: str, agg: str) -> pd.Series:
        """Aggregate `col` over the last `days` days, grouped by (rider_id, stage_id)."""
        if days is not None:
            mask = (joined["stage_date"] - joined["hist_date"]).dt.days <= days
            sub = joined[mask]
        else:
            sub = joined
        if agg == "mean":
            return sub.groupby(["rider_id", "stage_id"])[col].mean()
        if agg == "sum":
            return sub.groupby(["rider_id", "stage_id"])[col].sum()
        if agg == "count":
            return sub.groupby(["rider_id", "stage_id"])[col].count()
        if agg == "max_date":
            return sub.groupby(["rider_id", "stage_id"])["hist_date"].max()
        raise ValueError(agg)

    key = base.set_index(["rider_id", "stage_id"])

    def attach(series: pd.Series, name: str):
        base[name] = base.set_index(["rider_id", "stage_id"]).index.map(series).values

    attach(_rolling(30,  "position",    "mean"),  "avg_pos_30d")
    attach(_rolling(60,  "position",    "mean"),  "avg_pos_60d")
    attach(_rolling(90,  "position",    "mean"),  "avg_pos_90d")
    attach(_rolling(30,  "is_top10",    "mean"),  "top10_rate_30d")
    attach(_rolling(90,  "is_top10",    "mean"),  "top10_rate_90d")
    attach(_rolling(90,  "is_win",      "mean"),  "win_rate_90d")
    attach(_rolling(90,  "is_dnf",      "mean"),  "dnf_rate_90d")
    attach(_rolling(30,  "hist_stage_id","count"), "races_last_30d")

    last_race = _rolling(None, "hist_date", "max_date")
    last_race_mapped = base.set_index(["rider_id", "stage_id"]).index.map(last_race)
    base["days_since_last_race"] = (
        base["stage_date"].values - pd.to_datetime(last_race_mapped).values
    ) / np.timedelta64(1, "D")

    # ── profile affinity ──────────────────────────────────────────────────────
    log.info("computing profile affinity …")
    for ptype, col in [("mountain", "mountain_avg_pos"), ("flat", "flat_avg_pos"),
                       ("hilly", "hilly_avg_pos"), ("itt", "tt_avg_pos")]:
        prof_sub = joined[joined["hist_profile"] == ptype]
        series = prof_sub.groupby(["rider_id", "stage_id"])["position"].mean()
        attach(series, col)

    # ── profile-specific rolling features ─────────────────────────────────────
    for ptype, days, col in [
        ("hilly",    30,  "hilly_avg_pos_30d"),
        ("hilly",    90,  "hilly_avg_pos_90d"),
        ("mountain", 90,  "mountain_avg_pos_90d"),
        ("flat",     30,  "flat_avg_pos_30d"),
    ]:
        mask = (joined["hist_profile"] == ptype) & (
            (joined["stage_date"] - joined["hist_date"]).dt.days <= days
        )
        series = joined[mask].groupby(["rider_id", "stage_id"])["position"].mean()
        attach(series, col)

    hilly_90 = joined[
        (joined["hist_profile"] == "hilly") &
        ((joined["stage_date"] - joined["hist_date"]).dt.days <= 90)
    ]
    attach(hilly_90.groupby(["rider_id", "stage_id"])["is_top10"].mean(), "hilly_top10_rate_90d")

    # ── elevation density ─────────────────────────────────────────────────────
    base["elevation_per_km"] = base["elevation_m"] / base["distance_km"].replace(0, np.nan)

    # ── rider attributes ──────────────────────────────────────────────────────
    base = base.merge(
        riders[["id", "name", "pcs_rank", "speciality"]].rename(
            columns={"id": "rider_id", "name": "rider_name"}
        ),
        on="rider_id", how="left",
    )

    # ── one-hot encode ────────────────────────────────────────────────────────
    for pt in PROFILE_TYPES:
        base[f"is_{pt}"] = (base["stage_profile"] == pt).astype(int)
    for sp in SPECIALITIES:
        base[f"spec_{sp}"] = (base["speciality"] == sp).astype(int)

    base["is_cobbled"] = (base["surface"] == "cobbled").astype(int)
    base["is_gravel"]  = (base["surface"] == "gravel").astype(int)

    base["prev_stage_is_mountain"] = (base["prev_profile_type"] == "mountain").astype(int)
    base["prev_stage_is_hilly"]    = (base["prev_profile_type"] == "hilly").astype(int)

    base = base.drop(columns=["profile_type"], errors="ignore").rename(columns={
        "stage_profile": "profile_type",
    })

    log.info("features built: %d rows, %d columns", len(base), len(base.columns))
    return base


# ── DB loaders (also used by predict.py for start-list predictions) ───────────

def _load_results(conn) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT id, stage_id, rider_id, position, status FROM results", conn
    )


def _load_stages(conn) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT id, race_id, stage_num, date, distance_km, elevation_m, profile_type, surface FROM stages",
        conn,
    )


def _load_races(conn) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT id, pcs_slug, name, year, is_stage_race FROM races", conn
    )


def _load_riders(conn) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT id, pcs_slug, name, pcs_rank, speciality FROM riders", conn
    )
