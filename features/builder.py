"""
Feature engineering: transform raw DB rows into ML-ready feature vectors.

One row per (rider, stage) pair. Target column: top10 (1 if position <= 10).

Features
────────
Rider form (rolling windows over past results, NOT including the target stage):
  avg_pos_30d / 60d / 90d      average finish position
  top10_rate_30d / 90d         fraction of finishes in top 10
  win_rate_90d                 fraction of finishes in 1st
  dnf_rate_90d                 fraction of starts ending in DNF/DNS/OTL
  races_last_30d               race-day count (fatigue proxy)
  days_since_last_race         freshness

Profile affinity (rider's historical performance by stage type):
  mountain_avg_pos             avg pos on mountain stages (all time)
  flat_avg_pos                 avg pos on flat stages
  hilly_avg_pos                avg pos on hilly stages
  tt_avg_pos                   avg pos on ITT/TT stages

Stage context:
  distance_km
  elevation_m
  profile_type                 one-hot: is_flat / is_hilly / is_mountain / is_tt
  stage_num_norm               stage number / total stages (0=prologue, 1=final)
  is_stage_race

Rider attributes:
  pcs_rank                     lower = better
  speciality_*                 one-hot: gc / sprinter / puncher / classics / tt
"""

import logging
from datetime import datetime, timedelta

import pandas as pd
import numpy as np

from db.database import get_conn

log = logging.getLogger(__name__)

PROFILE_TYPES = ["flat", "hilly", "mountain", "itt", "utt"]
SPECIALITIES  = ["gc", "sprinter", "puncher", "classics", "tt", "climber"]


def build_features(
    cutoff_date: str | None = None,
    race_slug: str | None = None,
) -> pd.DataFrame:
    """
    Build a feature DataFrame.

    Parameters
    ----------
    cutoff_date : ISO date string, e.g. '2024-05-04'
        If given, only stages ON or AFTER this date are used as prediction
        targets. History (for rolling features) still uses all data before
        the stage date. Used for backtesting.
    race_slug : str, e.g. 'race/giro-d-italia/2024'
        If given, only stages belonging to this race are included as targets.

    Returns
    -------
    pd.DataFrame with one row per (rider_id, stage_id), columns described above,
    plus 'top10' target column and metadata (rider_name, stage_date, race_name, position).
    """
    with get_conn() as conn:
        results_df  = _load_results(conn)
        stages_df   = _load_stages(conn)
        races_df    = _load_races(conn)
        riders_df   = _load_riders(conn)

    # Merge stage → race info
    stages_df = stages_df.merge(
        races_df[["id", "name", "pcs_slug", "is_stage_race"]].rename(
            columns={"id": "race_id", "name": "race_name", "pcs_slug": "race_slug"}
        ),
        on="race_id", how="left",
    )

    # Filter target stages
    target_stages = stages_df.copy()
    if cutoff_date:
        target_stages = target_stages[target_stages["date"] >= cutoff_date]
    if race_slug:
        target_stages = target_stages[target_stages["race_slug"].str.startswith(race_slug)]

    if target_stages.empty:
        log.warning("no target stages found (cutoff=%s, race=%s)", cutoff_date, race_slug)
        return pd.DataFrame()

    # Full results with stage date attached
    results_df = results_df.merge(
        stages_df[["id", "date", "profile_type", "distance_km", "elevation_m",
                   "race_id", "stage_num", "is_stage_race", "race_name", "race_slug"]].rename(
            columns={"id": "stage_id"}
        ),
        on="stage_id", how="left",
    )
    results_df["date"] = pd.to_datetime(results_df["date"], errors="coerce")
    results_df = results_df.dropna(subset=["date"])

    rows = []
    for _, stage in target_stages.iterrows():
        stage_date = pd.to_datetime(stage["date"], errors="coerce")
        if pd.isna(stage_date):
            continue

        # Who started this stage?
        stage_results = results_df[results_df["stage_id"] == stage["id"]]
        if stage_results.empty:
            continue

        # Total stages in this race (for stage_num_norm)
        race_stage_count = stages_df[stages_df["race_id"] == stage["race_id"]].shape[0]

        for _, res in stage_results.iterrows():
            rider_id = res["rider_id"]

            # History = all results for this rider BEFORE this stage
            history = results_df[
                (results_df["rider_id"] == rider_id) &
                (results_df["date"] < stage_date)
            ].copy()

            row = _build_row(
                res=res,
                history=history,
                stage=stage,
                stage_date=stage_date,
                race_stage_count=race_stage_count,
                rider_info=riders_df[riders_df["id"] == rider_id].iloc[0]
                           if rider_id in riders_df["id"].values else None,
            )
            rows.append(row)

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    # One-hot encode profile type
    for pt in PROFILE_TYPES:
        df[f"is_{pt}"] = (df["profile_type"] == pt).astype(int)

    # One-hot encode speciality
    for sp in SPECIALITIES:
        df[f"spec_{sp}"] = (df["speciality"] == sp).astype(int)

    return df


# ── row builder ───────────────────────────────────────────────────────────────

def _build_row(res, history, stage, stage_date, race_stage_count, rider_info) -> dict:
    finished = history[history["status"] == "finished"].copy()
    pos = finished["position"].dropna()

    def rolling_pos(days):
        cutoff = stage_date - timedelta(days=days)
        sub = finished[finished["date"] >= cutoff]["position"].dropna()
        return sub.mean() if len(sub) else np.nan

    def rolling_top10_rate(days):
        cutoff = stage_date - timedelta(days=days)
        sub = finished[finished["date"] >= cutoff]["position"].dropna()
        return (sub <= 10).mean() if len(sub) else np.nan

    def win_rate(days):
        cutoff = stage_date - timedelta(days=days)
        sub = finished[finished["date"] >= cutoff]["position"].dropna()
        return (sub == 1).mean() if len(sub) else np.nan

    def dnf_rate(days):
        cutoff = stage_date - timedelta(days=days)
        sub = history[history["date"] >= cutoff]
        if sub.empty:
            return np.nan
        bad = sub["status"].isin(["dnf", "dns", "otl", "dsq"]).sum()
        return bad / len(sub)

    def profile_avg_pos(ptype):
        sub = finished[finished["profile_type"] == ptype]["position"].dropna()
        return sub.mean() if len(sub) else np.nan

    races_30d = history[history["date"] >= stage_date - timedelta(days=30)]["stage_id"].nunique()
    last_race = history["date"].max()
    days_since = (stage_date - last_race).days if pd.notna(last_race) else np.nan

    stage_num = stage.get("stage_num") or 0
    stage_num_norm = (stage_num / race_stage_count) if race_stage_count else np.nan

    position = res.get("position")
    top10 = int(position <= 10) if pd.notna(position) else np.nan

    row = {
        # metadata (not used as features)
        "rider_id":    res["rider_id"],
        "stage_id":    res["stage_id"],
        "stage_date":  stage_date,
        "race_name":   stage.get("race_name"),
        "rider_name":  rider_info["name"] if rider_info is not None else None,
        "position":    position,
        "status":      res.get("status"),

        # target
        "top10": top10,

        # form features
        "avg_pos_30d":      rolling_pos(30),
        "avg_pos_60d":      rolling_pos(60),
        "avg_pos_90d":      rolling_pos(90),
        "top10_rate_30d":   rolling_top10_rate(30),
        "top10_rate_90d":   rolling_top10_rate(90),
        "win_rate_90d":     win_rate(90),
        "dnf_rate_90d":     dnf_rate(90),
        "races_last_30d":   races_30d,
        "days_since_last_race": days_since,

        # profile affinity
        "mountain_avg_pos": profile_avg_pos("mountain"),
        "flat_avg_pos":     profile_avg_pos("flat"),
        "hilly_avg_pos":    profile_avg_pos("hilly"),
        "tt_avg_pos":       profile_avg_pos("itt"),

        # stage context
        "distance_km":      stage.get("distance_km"),
        "elevation_m":      stage.get("elevation_m"),
        "profile_type":     stage.get("profile_type"),
        "stage_num_norm":   stage_num_norm,
        "is_stage_race":    stage.get("is_stage_race", 0),

        # rider attributes
        "pcs_rank":    rider_info["pcs_rank"]   if rider_info is not None else np.nan,
        "speciality":  rider_info["speciality"] if rider_info is not None else None,
    }
    return row


# ── DB loaders ────────────────────────────────────────────────────────────────

def _load_results(conn) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT id, stage_id, rider_id, position, status, time_seconds, "
        "points_pcs, points_uci FROM results",
        conn,
    )


def _load_stages(conn) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT id, race_id, stage_num, date, distance_km, elevation_m, "
        "profile_type FROM stages",
        conn,
    )


def _load_races(conn) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT id, pcs_slug, name, year, is_stage_race FROM races",
        conn,
    )


def _load_riders(conn) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT id, pcs_slug, name, pcs_rank, speciality FROM riders",
        conn,
    )


# ── feature columns used by the model (excludes metadata & target) ────────────

FEATURE_COLS = [
    "avg_pos_30d", "avg_pos_60d", "avg_pos_90d",
    "top10_rate_30d", "top10_rate_90d",
    "win_rate_90d", "dnf_rate_90d",
    "races_last_30d", "days_since_last_race",
    "mountain_avg_pos", "flat_avg_pos", "hilly_avg_pos", "tt_avg_pos",
    "distance_km", "elevation_m",
    "stage_num_norm", "is_stage_race",
    "pcs_rank",
    "is_flat", "is_hilly", "is_mountain", "is_itt", "is_utt",
    "spec_gc", "spec_sprinter", "spec_puncher",
    "spec_classics", "spec_tt", "spec_climber",
]
