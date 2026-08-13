"""Generate top-10 probability predictions for a given race/stage."""
import json
import logging
import pickle
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from features.builder import build_features, FEATURE_COLS, _gc_state, PROFILE_TYPES, SPECIALITIES
from db.database import get_conn, _load_results, _load_stages, _load_races, _load_riders
from model.train import model_paths
from scraper.startlist import fetch_startlist

log = logging.getLogger(__name__)


def _load_model(path: Path):
    if path.exists():
        with open(path, "rb") as f:
            return pickle.load(f)
    return None


def predict_race(race_slug: str, cutoff_date: str, gender: str = "men") -> pd.DataFrame:
    """
    Predict top-10 probabilities for all starters in a race.
    Works for both historic races (uses result rows) and upcoming stages
    (uses start list + historical features only).
    """
    model_path, meta_path, _ = model_paths(gender)
    if not model_path.exists():
        raise FileNotFoundError(f"No trained model at {model_path}. Run train first.")

    with open(model_path, "rb") as f:
        model = pickle.load(f)
    feature_cols = json.loads(meta_path.read_text())

    win_path, _, _  = model_paths(gender, "win")
    top3_path, _, _ = model_paths(gender, "top3")
    win_model  = _load_model(win_path)
    top3_model = _load_model(top3_path)

    log.info("building features for %s (gender=%s) …", race_slug, gender)
    df = build_features(cutoff_date=cutoff_date, race_slug=race_slug, gender=gender)

    if df.empty:
        log.warning("no features built — trying start list prediction")
        df = predict_from_startlist(race_slug, cutoff_date, model, feature_cols, gender=gender,
                                    win_model=win_model, top3_model=top3_model)

    if df.empty:
        return pd.DataFrame()

    X = df[feature_cols]
    df["top10_prob"] = model.predict_proba(X)[:, 1]
    if win_model is not None:
        df["win_prob"] = win_model.predict_proba(X)[:, 1]
    if top3_model is not None:
        df["top3_prob"] = top3_model.predict_proba(X)[:, 1]
    df["predicted_top10"] = (df["top10_prob"] >= 0.5).astype(int)

    return df.sort_values(["stage_date", "top10_prob"], ascending=[True, False])


def _fetch_active_starters(
    race_slug: str,
    race_id: int,
    cutoff_date: str,
    riders_df: pd.DataFrame,
    conn,
) -> tuple[list[dict], list]:
    """
    Fetch startlist for race_slug and filter out riders who abandoned before cutoff.
    Returns (starters, completed_stage_rows) where completed_stage_rows is the
    list of sqlite Row objects from: SELECT id FROM stages WHERE race_id=? AND date<?
    """
    starters = fetch_startlist(race_slug)
    if not starters:
        return [], []

    completed = conn.execute(
        "SELECT id FROM stages WHERE race_id = ? AND date < ? ORDER BY date DESC",
        (race_id, cutoff_date),
    ).fetchall()

    if completed:
        latest_id = completed[0]["id"]
        active_rows = conn.execute(
            "SELECT rider_id FROM results WHERE stage_id = ? AND status = 'finished'",
            (latest_id,),
        ).fetchall()
        if active_rows:
            active_set = {r["rider_id"] for r in active_rows}
            slug_to_id = {}
            if "pcs_slug" in riders_df.columns:
                slug_to_id = {
                    row["pcs_slug"]: row["id"]
                    for _, row in riders_df[
                        riders_df["pcs_slug"].isin([s["slug"] for s in starters])
                    ].iterrows()
                }
            before = len(starters)
            starters = [s for s in starters if slug_to_id.get(s["slug"]) in active_set]
            dropped = before - len(starters)
            if dropped:
                log.info("filtered %d abandoned riders from startlist (last stage %d)", dropped, latest_id)

    return starters, completed


def _gc_snapshot(
    completed_stage_rows: list,
    results_df: pd.DataFrame,
) -> tuple[dict, dict, dict, dict, dict]:
    """
    Compute current GC state and team-GC context from completed stage results.

    Returns:
        gc_deficit_now   rider_id -> minutes behind virtual leader
        gc_rank_now      rider_id -> GC rank (1 = leader)
        team_min_deficit team_id  -> min deficit across team members
        team_best_rank   team_id  -> best rank across team members
        rider_team_id    rider_id -> team_id  (bib // 10)
    """
    gc_deficit_now: dict = {}
    gc_rank_now: dict    = {}
    team_min_deficit: dict = {}
    team_best_rank: dict   = {}
    rider_team_id: dict    = {}

    comp_ids = [r["id"] for r in completed_stage_rows]
    if not comp_ids:
        return gc_deficit_now, gc_rank_now, team_min_deficit, team_best_rank, rider_team_id

    cur = results_df[
        results_df["stage_id"].isin(comp_ids) & (results_df["status"] == "finished")
    ]
    cum = cur.groupby("rider_id")["time_seconds"].sum()
    if len(cum):
        gc_deficit_now = ((cum - cum.min()) / 60.0).to_dict()
        gc_rank_now    = cum.rank(method="min").to_dict()

    cur_bib = (
        results_df[results_df["stage_id"].isin(comp_ids) & results_df["bib"].notna()]
        .drop_duplicates("rider_id")[["rider_id", "bib"]]
    )
    for _, row in cur_bib.iterrows():
        rider_team_id[row["rider_id"]] = int(row["bib"]) // 10
    for rid, deficit in gc_deficit_now.items():
        tid = rider_team_id.get(rid)
        if tid is not None and (tid not in team_min_deficit or deficit < team_min_deficit[tid]):
            team_min_deficit[tid] = deficit
    for rid, rank in gc_rank_now.items():
        tid = rider_team_id.get(rid)
        if tid is not None and (tid not in team_best_rank or rank < team_best_rank[tid]):
            team_best_rank[tid] = rank

    return gc_deficit_now, gc_rank_now, team_min_deficit, team_best_rank, rider_team_id


def _rider_stage_features(
    starter: dict,
    stage,
    rider_row,          # pandas Series or None
    history_df: pd.DataFrame,
    stage_date,         # pd.Timestamp
    stage_num_norm: float,
    is_stage_race: int,
    prev_pt,            # str or None
    gc_deficit_now: dict,
    gc_rank_now: dict,
    team_min_deficit: dict,
    team_best_rank: dict,
    rider_team_id: dict,
) -> dict:
    """
    Build one feature-dict for a single (starter, stage) pair.
    Mirrors the column layout produced by features/builder.py.
    """
    stage_profile_type = stage["profile_type"]
    stage_surface      = (stage["surface"] or "road") if "surface" in stage.keys() else "road"
    is_special         = stage_surface in ("cobbled", "gravel")

    if rider_row is None:
        rider_id = pcs_rank = weight_kg = height_cm = dob = speciality = None
        pcs_rank = weight_kg = height_cm = np.nan
    else:
        rider_id   = rider_row["id"]
        pcs_rank   = rider_row.get("pcs_rank", np.nan)
        speciality = rider_row.get("speciality")
        weight_kg  = rider_row.get("weight_kg", np.nan)
        height_cm  = rider_row.get("height_cm", np.nan)
        dob        = rider_row.get("dob")

    age_at_race = np.nan
    if dob:
        try:
            age_at_race = (stage_date - pd.to_datetime(dob)).days / 365.25
        except Exception:
            pass

    finished = pd.DataFrame()
    history  = pd.DataFrame()
    if rider_id is not None and not history_df.empty:
        history  = history_df[(history_df["rider_id"] == rider_id) & (history_df["date"] < stage_date)].copy()
        finished = history[history["status"] == "finished"].copy()
        if not finished.empty:
            finished.loc[:, "is_top10"] = (finished["position"] <= 10) & finished["position"].notna()
            finished.loc[:, "is_win"]   = finished["position"] == 1
        if not history.empty:
            history.loc[:, "is_dnf"] = history["status"].isin(["dnf", "dns", "otl", "dsq"])

    def rm(col, days):
        if finished.empty: return np.nan
        sub = finished[finished["date"] >= stage_date - pd.Timedelta(days=days)]
        return sub[col].mean() if len(sub) else np.nan

    def pa(ptype):
        if finished.empty: return np.nan
        sub = finished[finished["profile_type"] == ptype]["position"].dropna()
        return sub.mean() if len(sub) else np.nan

    def par(ptype, days):
        if finished.empty: return np.nan
        sub = finished[(finished["profile_type"] == ptype) & (finished["date"] >= stage_date - pd.Timedelta(days=days))]["position"].dropna()
        return sub.mean() if len(sub) else np.nan

    def ptr(ptype, days):
        if finished.empty: return np.nan
        sub = finished[(finished["profile_type"] == ptype) & (finished["date"] >= stage_date - pd.Timedelta(days=days))]
        return sub["is_top10"].mean() if len(sub) else np.nan

    def rel_sub(days):
        if finished.empty: return pd.DataFrame()
        hs = finished.get("surface", pd.Series("road", index=finished.index)).fillna("road")
        pm = finished["profile_type"] == stage_profile_type
        sm = (hs == stage_surface) if is_special else ~hs.isin(["cobbled", "gravel"])
        w  = finished["date"] >= stage_date - pd.Timedelta(days=days)
        return finished[pm & sm & w]

    races_30d = 0 if history.empty else history[history["date"] >= stage_date - pd.Timedelta(days=30)]["stage_id"].nunique()
    last_race  = history["date"].max() if not history.empty else pd.NaT
    days_since = (stage_date - last_race).days if pd.notna(last_race) else np.nan

    _rel30  = rel_sub(30);  _rel90  = rel_sub(90);  _rel365 = rel_sub(365)

    _brk = pd.DataFrame()
    if not finished.empty and "gc_rank_before" in finished.columns:
        _brk = finished[(finished["gc_rank_before"] > 20) & (finished["date"] >= stage_date - pd.Timedelta(days=365)) & (finished["profile_type"] != "ttt")]

    _punch = pd.DataFrame()
    if not finished.empty and "gradient_final_km" in finished.columns:
        _punch = finished[(finished["profile_type"] == "hilly") & (finished["gradient_final_km"].fillna(0) >= 5.0) & (finished["date"] >= stage_date - pd.Timedelta(days=90))]

    _od = pd.DataFrame()
    _od365 = pd.DataFrame(); _od90 = pd.DataFrame()
    if not finished.empty and "is_stage_race" in finished.columns:
        _od    = finished[finished["is_stage_race"].fillna(0) == 0]
        _od365 = _od[_od["date"] >= stage_date - pd.Timedelta(days=365)]
        _od90  = _od[_od["date"] >= stage_date - pd.Timedelta(days=90)]

    def count_race_days(days):
        if history.empty: return 0
        return int(history[history["date"] >= stage_date - pd.Timedelta(days=days)]["date"].dt.normalize().nunique())

    gc_def   = gc_deficit_now.get(rider_id, np.nan)
    gc_rank  = gc_rank_now.get(rider_id, np.nan)
    _team    = rider_team_id.get(rider_id)
    t_def    = team_min_deficit.get(_team, np.nan) if _team is not None else np.nan
    t_rank   = team_best_rank.get(_team, np.nan)   if _team is not None else np.nan
    t_gc_out = float(t_def > 10) if not (isinstance(t_def, float) and np.isnan(t_def)) else np.nan
    gc_out   = float(gc_def > 10) if rider_id in gc_deficit_now else np.nan

    prev_pt = prev_pt or ""
    is_itt_stage = int(stage_profile_type == "itt") if stage_profile_type else 0

    row = {
        "rider_id": rider_id, "stage_id": stage["id"], "stage_date": stage_date,
        "race_name": "", "rider_name": starter["name"],
        "position": None, "status": None, "top10": np.nan,
        "avg_pos_30d": rm("position", 30), "avg_pos_60d": rm("position", 60), "avg_pos_90d": rm("position", 90),
        "top10_rate_30d": rm("is_top10", 30), "top10_rate_90d": rm("is_top10", 90),
        "win_rate_90d": rm("is_win", 90),
        "dnf_rate_90d": history["is_dnf"].mean() if not history.empty else np.nan,
        "races_last_30d": races_30d, "days_since_last_race": days_since,
        "mountain_avg_pos": pa("mountain"), "flat_avg_pos": pa("flat"),
        "hilly_avg_pos": pa("hilly"), "tt_avg_pos": pa("itt"),
        "hilly_avg_pos_30d": par("hilly", 30), "hilly_avg_pos_90d": par("hilly", 90),
        "hilly_top10_rate_90d": ptr("hilly", 90),
        "mountain_avg_pos_30d": par("mountain", 30), "mountain_avg_pos_90d": par("mountain", 90),
        "mountain_top10_rate_90d": ptr("mountain", 90),
        "flat_avg_pos_30d": par("flat", 30), "flat_avg_pos_90d": par("flat", 90),
        "flat_top10_rate_90d": ptr("flat", 90),
        "tt_avg_pos_90d": par("itt", 90), "tt_avg_pos_365d": par("itt", 365),
        "tt_top10_rate_365d": ptr("itt", 365),
        "tt_win_rate": finished[finished["profile_type"] == "itt"]["is_win"].mean() if not finished.empty else np.nan,
        "relevant_avg_pos_30d": _rel30["position"].mean() if len(_rel30) else np.nan,
        "relevant_avg_pos_90d": _rel90["position"].mean() if len(_rel90) else np.nan,
        "relevant_top10_rate_90d": _rel90["is_top10"].mean() if len(_rel90) else np.nan,
        "relevant_avg_pos_365d": _rel365["position"].mean() if len(_rel365) else np.nan,
        "relevant_top10_rate_365d": _rel365["is_top10"].mean() if len(_rel365) else np.nan,
        "race_days_last_7d": count_race_days(7), "race_days_last_14d": count_race_days(14),
        "elevation_per_km": (stage["elevation_m"] / stage["distance_km"] if stage["elevation_m"] and stage["distance_km"] else np.nan),
        "distance_km": stage["distance_km"], "elevation_m": stage["elevation_m"],
        "profile_type": stage_profile_type,
        "stage_num_norm": stage_num_norm, "is_stage_race": is_stage_race,
        "prev_stage_is_mountain": int(prev_pt == "mountain"),
        "prev_stage_is_hilly":    int(prev_pt == "hilly"),
        "pcs_rank": pcs_rank, "weight_kg": weight_kg, "height_cm": height_cm,
        "age_at_race": age_at_race, "speciality": speciality,
        "gradient_final_km": stage["gradient_final_km"],
        "profile_score": stage["profile_score"],
        "gc_deficit_min": gc_def, "gc_rank_before": gc_rank,
        "break_top10_rate_365d": _brk["is_top10"].mean() if len(_brk) else np.nan,
        "break_top10s_365d": float(_brk["is_top10"].sum()) if len(_brk) else 0.0,
        "punch_top10_rate_90d": _punch["is_top10"].mean() if len(_punch) else np.nan,
        "punch_avg_pos_90d":    _punch["position"].mean() if len(_punch) else np.nan,
        "oneday_top10_rate_365d": _od365["is_top10"].mean() if len(_od365) else np.nan,
        "oneday_avg_pos_90d":     _od90["position"].mean() if len(_od90) else np.nan,
        "is_punch_finish": int(stage_profile_type == "hilly" and (stage["gradient_final_km"] or 0) >= 5.0),
        "gradient_x_hilly": (stage["gradient_final_km"] or 0) * int(stage_profile_type == "hilly"),
        "profile_score_per_gradient": (stage["profile_score"] or 0) / ((stage["gradient_final_km"] or 0) + 1.0),
        "is_transition_stage": int(prev_pt == "mountain" and stage_profile_type in ("hilly", "flat")),
        "gc_out_of_contention": gc_out,
        "team_min_gc_deficit": t_def, "team_best_gc_rank": t_rank,
        "team_gc_out_of_contention": t_gc_out,
        "is_cobbled": int(stage_surface == "cobbled"), "is_gravel": int(stage_surface == "gravel"),
    }
    for pt in PROFILE_TYPES:
        row[f"is_{pt}"] = int(stage_profile_type == pt) if stage_profile_type else 0
    for sp in SPECIALITIES:
        row[f"spec_{sp}"] = int(speciality == sp) if speciality else 0

    tt_wr_v   = row["tt_win_rate"] if not (isinstance(row["tt_win_rate"], float) and np.isnan(row["tt_win_rate"])) else 0
    tt_365_v  = row["tt_avg_pos_365d"] if not (isinstance(row["tt_avg_pos_365d"], float) and np.isnan(row["tt_avg_pos_365d"])) else 50
    tt_90_v   = row["tt_avg_pos_90d"]  if not (isinstance(row["tt_avg_pos_90d"], float)  and np.isnan(row["tt_avg_pos_90d"]))  else 50
    row["tt_win_rate_x_itt"]     = tt_wr_v  * is_itt_stage
    row["tt_avg_pos_365d_x_itt"] = tt_365_v * is_itt_stage
    row["tt_avg_pos_90d_x_itt"]  = tt_90_v  * is_itt_stage

    return row


def predict_from_startlist(
    race_slug: str,
    cutoff_date: str,
    model,
    feature_cols: list[str],
    gender: str = "men",
    win_model=None,
    top3_model=None,
) -> pd.DataFrame:
    """
    Build predictions for upcoming stages using the start list.
    No result rows needed — features are built purely from history.
    """
    with get_conn() as conn:
        # Find upcoming stages for this race
        race_row = conn.execute(
            "SELECT id, is_stage_race FROM races WHERE pcs_slug = ?", (race_slug,)
        ).fetchone()
        if not race_row:
            log.warning("race not found in DB: %s", race_slug)
            return pd.DataFrame()

        stages = conn.execute(
            "SELECT id, stage_num, date, distance_km, elevation_m, profile_type, "
            "surface, gradient_final_km, profile_score, race_id FROM stages "
            "WHERE race_id = ? AND date >= ? ORDER BY date",
            (race_row["id"], cutoff_date),
        ).fetchall()

        if not stages:
            log.warning("no upcoming stages found for %s from %s", race_slug, cutoff_date)
            return pd.DataFrame()

        # Load all historical data for rolling features
        results_df = _load_results(conn)
        stages_all = _load_stages(conn)
        races_df   = _load_races(conn)
        riders_df  = _load_riders(conn)

        starters, completed_stage_ids = _fetch_active_starters(
            race_slug, race_row["id"], cutoff_date, riders_df, conn
        )

    if not starters:
        return pd.DataFrame()

    stages_all = stages_all.merge(
        races_df[["id", "pcs_slug", "is_stage_race"]].rename(
            columns={"id": "race_id", "pcs_slug": "race_slug"}
        ), on="race_id",
    )
    # GC standing before each historical stage (feeds breakaway propensity)
    gc_hist = _gc_state(results_df, stages_all)
    results_df = results_df.merge(gc_hist, on=["stage_id", "rider_id"], how="left")

    results_df = results_df.merge(
        stages_all[["id", "date", "profile_type", "surface", "gradient_final_km", "is_stage_race"]].rename(columns={"id": "stage_id"}),
        on="stage_id",
    )
    results_df["date"] = pd.to_datetime(results_df["date"], errors="coerce")

    gc_deficit_now, gc_rank_now, team_min_gc_deficit, team_best_gc_rank, rider_team_id = _gc_snapshot(
        completed_stage_ids, results_df
    )

    race_stage_count = len(stages)

    # Build prev_profile_type lookup: {stage_id: profile_type of preceding stage}
    stages_sorted = sorted(stages, key=lambda s: (s["date"] or ""))
    prev_profile = {}
    for i, s in enumerate(stages_sorted):
        prev_profile[s["id"]] = stages_sorted[i - 1]["profile_type"] if i > 0 else None

    rows = []

    for stage in stages:
        stage_date = pd.to_datetime(stage["date"])
        stage_num  = stage["stage_num"] or 1
        stage_num_norm = stage_num / race_stage_count
        prev_pt = prev_profile.get(stage["id"])

        for starter in starters:
            # Look up or match rider_id
            match = riders_df[riders_df["pcs_slug"] == starter["slug"]] \
                if "pcs_slug" in riders_df.columns else pd.DataFrame()

            # Fallback: match by name
            if match.empty:
                match = riders_df[
                    riders_df["name"].str.lower() == starter["name"].lower()
                ]

            rider_row = match.iloc[0] if not match.empty else None

            row = _rider_stage_features(
                starter=starter,
                stage=stage,
                rider_row=rider_row,
                history_df=results_df,
                stage_date=stage_date,
                stage_num_norm=stage_num_norm,
                is_stage_race=race_row["is_stage_race"] or 0,
                prev_pt=prev_pt,
                gc_deficit_now=gc_deficit_now,
                gc_rank_now=gc_rank_now,
                team_min_deficit=team_min_gc_deficit,
                team_best_rank=team_best_gc_rank,
                rider_team_id=rider_team_id,
            )
            # Override race_name to match original behaviour (used race_slug in original)
            row["race_name"] = race_slug
            rows.append(row)

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    # Add stage classifier predictions
    from model.stage_classifier import (
        load as _load_clf, predict_proba as _clf_proba,
        STAGE_FEATURES as _STAGE_FEATURES,
    )
    from features.builder import PROFILE_TYPES as _PROFILE_TYPES
    _clf_bundle = _load_clf()
    if _clf_bundle is not None:
        # One-hot encode profile columns if not already present
        for pt in _PROFILE_TYPES:
            col = f"is_{pt}"
            if col not in df.columns:
                df[col] = (df["profile_type"] == pt).astype(int)
        # Compute elevation_per_km if missing
        if "elevation_per_km" not in df.columns:
            df["elevation_per_km"] = df["elevation_m"] / df["distance_km"].replace(0, np.nan)
        # Stage num norm — already in df; ensure STAGE_FEATURES columns exist
        for col in _STAGE_FEATURES:
            if col not in df.columns:
                df[col] = np.nan
        stage_meta = df.drop_duplicates("stage_id").set_index("stage_id")
        proba = _clf_proba(stage_meta, _clf_bundle)
        df = df.join(proba, on="stage_id")
    else:
        df["p_sprint"]    = np.nan
        df["p_breakaway"] = np.nan
        df["p_gc"]        = np.nan

    _pb = df["p_breakaway"].fillna(0)
    _ps = df["p_sprint"].fillna(0)
    _pg = df["p_gc"].fillna(0)
    df["p_breakaway_x_oneday"]      = _pb * df["oneday_top10_rate_365d"].fillna(0)
    df["p_breakaway_x_break_rate"]  = _pb * df["break_top10_rate_365d"].fillna(0)
    df["p_breakaway_x_gc_out"]      = _pb * df["gc_out_of_contention"].fillna(0)
    df["p_breakaway_x_team_gc_out"] = _pb * df["team_gc_out_of_contention"].fillna(0)
    df["p_sprint_x_flat_form"]      = _ps * df["flat_top10_rate_90d"].fillna(0)
    df["p_gc_x_mountain_form"]      = _pg * df["mountain_top10_rate_90d"].fillna(0)

    df["top10_prob"] = model.predict_proba(df[feature_cols])[:, 1]
    if win_model is not None:
        df["win_prob"] = win_model.predict_proba(df[feature_cols])[:, 1]
    if top3_model is not None:
        df["top3_prob"] = top3_model.predict_proba(df[feature_cols])[:, 1]
    df["predicted_top10"] = (df["top10_prob"] >= 0.5).astype(int)
    return df.sort_values(["stage_date", "top10_prob"], ascending=[True, False])


def print_predictions(df: pd.DataFrame, top_n: int = 15) -> None:
    """Pretty-print predictions grouped by stage."""
    if df.empty:
        print("No predictions.")
        return

    for stage_date, group in df.groupby("stage_date"):
        group = group.sort_values("top10_prob", ascending=False)
        race = group["race_name"].iloc[0]
        profile = group["profile_type"].iloc[0] if "profile_type" in group.columns else "?"
        print(f"\n{'='*65}")
        print(f"  {race}  |  Stage {str(stage_date)[:10]}  |  {profile}")
        print(f"{'='*65}")
        print(f"  {'Rank':>4}  {'Rider':<30}  {'P(top10)':>8}  {'Actual':>8}")
        print(f"  {'-'*55}")
        for rank, (_, row) in enumerate(group.head(top_n).iterrows(), 1):
            actual = int(row["position"]) if pd.notna(row.get("position")) else "-"
            print(f"  {rank:>4}  {str(row['rider_name']):<30}  {row['top10_prob']:>8.3f}  {str(actual):>8}")
        print()
