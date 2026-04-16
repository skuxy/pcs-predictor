"""Generate top-10 probability predictions for a given race/stage."""
import json
import logging
import pickle
from pathlib import Path

import pandas as pd

from features.builder import build_features, FEATURE_COLS, _load_results, _load_stages, _load_races, _load_riders
from model.train import model_paths
from db.database import get_conn

log = logging.getLogger(__name__)


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

    log.info("building features for %s (gender=%s) …", race_slug, gender)
    df = build_features(cutoff_date=cutoff_date, race_slug=race_slug, gender=gender)

    if df.empty:
        log.warning("no features built — trying start list prediction")
        df = predict_from_startlist(race_slug, cutoff_date, model, feature_cols, gender=gender)

    if df.empty:
        return pd.DataFrame()

    X = df[feature_cols]
    df["top10_prob"] = model.predict_proba(X)[:, 1]
    df["predicted_top10"] = (df["top10_prob"] >= 0.5).astype(int)

    return df.sort_values(["stage_date", "top10_prob"], ascending=[True, False])


def predict_from_startlist(
    race_slug: str,
    cutoff_date: str,
    model,
    feature_cols: list[str],
    gender: str = "men",
) -> pd.DataFrame:
    """
    Build predictions for upcoming stages using the start list.
    No result rows needed — features are built purely from history.
    """
    from scraper.startlist import fetch_startlist
    from features.builder import PROFILE_TYPES, SPECIALITIES
    from datetime import timedelta
    import numpy as np

    starters = fetch_startlist(race_slug)
    if not starters:
        return pd.DataFrame()

    with get_conn() as conn:
        # Find upcoming stages for this race
        race_row = conn.execute(
            "SELECT id FROM races WHERE pcs_slug = ?", (race_slug,)
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

        # For in-progress stage races: filter starters to only those who
        # finished the most recently completed stage (removes abandonments).
        # Only applies when at least one prior stage of this race has results.
        completed_stage_ids = conn.execute(
            "SELECT id FROM stages WHERE race_id = ? AND date < ? ORDER BY date DESC",
            (race_row["id"], cutoff_date),
        ).fetchall()
        if completed_stage_ids:
            latest_stage_id = completed_stage_ids[0]["id"]
            active_rider_ids = conn.execute(
                "SELECT rider_id FROM results WHERE stage_id = ? AND status = 'finished'",
                (latest_stage_id,),
            ).fetchall()
            if active_rider_ids:
                active_set = {r["rider_id"] for r in active_rider_ids}
                # Map starter slugs to rider_ids for filtering
                slug_to_id = {
                    row["pcs_slug"]: row["id"]
                    for _, row in riders_df[riders_df["pcs_slug"].isin(
                        [s["slug"] for s in starters]
                    )].iterrows()
                } if "pcs_slug" in riders_df.columns else {}
                before = len(starters)
                starters = [
                    s for s in starters
                    if slug_to_id.get(s["slug"]) in active_set
                ]
                dropped = before - len(starters)
                if dropped:
                    log.info(
                        "filtered %d abandoned/DNS riders from startlist "
                        "(based on last completed stage %d)",
                        dropped, latest_stage_id,
                    )

    stages_all = stages_all.merge(
        races_df[["id", "pcs_slug", "is_stage_race"]].rename(
            columns={"id": "race_id", "pcs_slug": "race_slug"}
        ), on="race_id",
    )
    results_df = results_df.merge(
        stages_all[["id", "date", "profile_type", "surface"]].rename(columns={"id": "stage_id"}),
        on="stage_id",
    )
    results_df["date"] = pd.to_datetime(results_df["date"], errors="coerce")

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
        stage_surface = (stage["surface"] or "road") if "surface" in stage.keys() else "road"
        stage_profile_type = stage["profile_type"]
        is_special = stage_surface in ("cobbled", "gravel")

        for starter in starters:
            # Look up or match rider_id
            match = riders_df[riders_df["pcs_slug"] == starter["slug"]] \
                if "pcs_slug" in riders_df.columns else pd.DataFrame()

            # Fallback: match by name
            if match.empty:
                match = riders_df[
                    riders_df["name"].str.lower() == starter["name"].lower()
                ]

            if match.empty:
                rider_id   = None
                pcs_rank   = np.nan
                speciality = None
            else:
                rider_id   = match.iloc[0]["id"]
                pcs_rank   = match.iloc[0].get("pcs_rank", np.nan)
                speciality = match.iloc[0].get("speciality")

            history = pd.DataFrame()
            if rider_id is not None:
                history = results_df[
                    (results_df["rider_id"] == rider_id) &
                    (results_df["date"] < stage_date)
                ].copy()

            finished = history[history["status"] == "finished"] if not history.empty else pd.DataFrame()

            def rolling_mean(col, days):
                if finished.empty: return np.nan
                sub = finished[finished["date"] >= stage_date - pd.Timedelta(days=days)]
                return sub[col].mean() if len(sub) else np.nan

            def rolling_rate(col, days):
                if finished.empty: return np.nan
                sub = finished[finished["date"] >= stage_date - pd.Timedelta(days=days)]
                return sub[col].mean() if len(sub) else np.nan

            def profile_avg(ptype):
                if finished.empty: return np.nan
                sub = finished[finished["profile_type"] == ptype]["position"].dropna()
                return sub.mean() if len(sub) else np.nan

            def profile_avg_rolling(ptype, days):
                if finished.empty: return np.nan
                sub = finished[
                    (finished["profile_type"] == ptype) &
                    (finished["date"] >= stage_date - pd.Timedelta(days=days))
                ]["position"].dropna()
                return sub.mean() if len(sub) else np.nan

            def profile_top10_rate(ptype, days):
                if finished.empty: return np.nan
                sub = finished[
                    (finished["profile_type"] == ptype) &
                    (finished["date"] >= stage_date - pd.Timedelta(days=days))
                ]
                return sub["is_top10"].mean() if len(sub) else np.nan

            def relevant_sub(days):
                if finished.empty: return pd.DataFrame()
                hist_surf = finished.get("surface", pd.Series("road", index=finished.index)).fillna("road")
                prof_match = finished["profile_type"] == stage_profile_type
                surf_match = (hist_surf == stage_surface) if is_special else ~hist_surf.isin(["cobbled", "gravel"])
                window = finished["date"] >= stage_date - pd.Timedelta(days=days)
                return finished[prof_match & surf_match & window]

            def count_race_days(days):
                if history.empty: return 0
                w = history[history["date"] >= stage_date - pd.Timedelta(days=days)]
                return int(w["date"].dt.normalize().nunique())

            races_30d = 0 if history.empty else \
                history[history["date"] >= stage_date - pd.Timedelta(days=30)]["stage_id"].nunique()

            last_race = history["date"].max() if not history.empty else pd.NaT
            days_since = (stage_date - last_race).days if pd.notna(last_race) else np.nan

            if not finished.empty:
                finished = finished.copy()
                finished.loc[:, "is_top10"] = (finished["position"] <= 10) & finished["position"].notna()
                finished.loc[:, "is_win"]   = finished["position"] == 1
            if not history.empty:
                history = history.copy()
                history.loc[:, "is_dnf"] = history["status"].isin(["dnf", "dns", "otl", "dsq"])

            _rel30  = relevant_sub(30)
            _rel90  = relevant_sub(90)
            _rel365 = relevant_sub(365)

            row = {
                "rider_id":   rider_id,
                "stage_id":   stage["id"],
                "stage_date": stage_date,
                "race_name":  race_slug,
                "rider_name": starter["name"],
                "position":   None,
                "status":     None,
                "top10":      np.nan,

                "avg_pos_30d":          rolling_mean("position", 30),
                "avg_pos_60d":          rolling_mean("position", 60),
                "avg_pos_90d":          rolling_mean("position", 90),
                "top10_rate_30d":       rolling_rate("is_top10", 30) if not finished.empty else np.nan,
                "top10_rate_90d":       rolling_rate("is_top10", 90) if not finished.empty else np.nan,
                "win_rate_90d":         rolling_rate("is_win", 90) if not finished.empty else np.nan,
                "dnf_rate_90d":         history["is_dnf"].mean() if not history.empty else np.nan,
                "races_last_30d":       races_30d,
                "days_since_last_race": days_since,

                "mountain_avg_pos": profile_avg("mountain"),
                "flat_avg_pos":     profile_avg("flat"),
                "hilly_avg_pos":    profile_avg("hilly"),
                "tt_avg_pos":       profile_avg("itt"),

                "hilly_avg_pos_30d":    profile_avg_rolling("hilly", 30),
                "hilly_avg_pos_90d":    profile_avg_rolling("hilly", 90),
                "hilly_top10_rate_90d": profile_top10_rate("hilly", 90),
                "mountain_avg_pos_90d": profile_avg_rolling("mountain", 90),
                "flat_avg_pos_30d":     profile_avg_rolling("flat", 30),

                "relevant_avg_pos_30d":     _rel30["position"].mean()  if len(_rel30)  else np.nan,
                "relevant_avg_pos_90d":     _rel90["position"].mean()  if len(_rel90)  else np.nan,
                "relevant_top10_rate_90d":  _rel90["is_top10"].mean()  if len(_rel90)  else np.nan,
                "relevant_avg_pos_365d":    _rel365["position"].mean() if len(_rel365) else np.nan,
                "relevant_top10_rate_365d": _rel365["is_top10"].mean() if len(_rel365) else np.nan,
                "race_days_last_7d":       count_race_days(7),
                "race_days_last_14d":      count_race_days(14),

                "elevation_per_km": (
                    stage["elevation_m"] / stage["distance_km"]
                    if stage["elevation_m"] and stage["distance_km"] else np.nan
                ),

                "distance_km":    stage["distance_km"],
                "elevation_m":    stage["elevation_m"],
                "profile_type":   stage["profile_type"],
                "stage_num_norm": stage_num_norm,
                "is_stage_race":  1,
                "prev_stage_is_mountain": int(prev_pt == "mountain") if prev_pt else 0,
                "prev_stage_is_hilly":    int(prev_pt == "hilly")    if prev_pt else 0,
                "pcs_rank":       pcs_rank,
                "speciality":     speciality,
                "gradient_final_km": stage["gradient_final_km"],
                "profile_score":     stage["profile_score"],
            }

            surface = stage["surface"] if "surface" in stage.keys() else "road"
            row["is_cobbled"] = int(surface == "cobbled")
            row["is_gravel"]  = int(surface == "gravel")

            for pt in PROFILE_TYPES:
                row[f"is_{pt}"] = int(stage["profile_type"] == pt) if stage["profile_type"] else 0
            for sp in SPECIALITIES:
                row[f"spec_{sp}"] = int(speciality == sp) if speciality else 0

            rows.append(row)

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    df["top10_prob"] = model.predict_proba(df[feature_cols])[:, 1]
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
