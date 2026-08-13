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

PROFILE_TYPES = ["flat", "hilly", "mountain", "itt", "ttt"]
SPECIALITIES  = ["gc", "sprinter", "puncher", "classics", "tt", "climber"]

FEATURE_COLS = [
    "avg_pos_30d", "avg_pos_60d", "avg_pos_90d",
    "top10_rate_30d", "top10_rate_90d",
    "win_rate_90d", "dnf_rate_90d",
    "races_last_30d", "days_since_last_race",
    "race_days_last_7d", "race_days_last_14d",
    "mountain_avg_pos", "flat_avg_pos", "hilly_avg_pos", "tt_avg_pos",
    "hilly_avg_pos_30d", "hilly_avg_pos_90d", "hilly_top10_rate_90d",
    "mountain_avg_pos_30d", "mountain_avg_pos_90d", "mountain_top10_rate_90d",
    "flat_avg_pos_30d", "flat_avg_pos_90d", "flat_top10_rate_90d",
    "tt_avg_pos_90d", "tt_avg_pos_365d", "tt_top10_rate_365d", "tt_win_rate",
    "tt_win_rate_x_itt", "tt_avg_pos_365d_x_itt", "tt_avg_pos_90d_x_itt",
    "relevant_avg_pos_30d", "relevant_avg_pos_90d", "relevant_top10_rate_90d",
    "relevant_avg_pos_365d", "relevant_top10_rate_365d",
    "elevation_per_km",
    "distance_km", "elevation_m",
    "stage_num_norm", "is_stage_race",
    "prev_stage_is_mountain", "prev_stage_is_hilly",
    "pcs_rank", "weight_kg", "height_cm", "age_at_race",
    "is_flat", "is_hilly", "is_mountain", "is_itt", "is_ttt",
    "is_cobbled", "is_gravel",
    "spec_gc", "spec_sprinter", "spec_puncher",
    "spec_classics", "spec_tt", "spec_climber",
    "gradient_final_km", "profile_score",
    "gc_deficit_min", "gc_rank_before",
    "break_top10_rate_365d", "break_top10s_365d",
    "punch_top10_rate_90d", "punch_avg_pos_90d",
    "oneday_top10_rate_365d", "oneday_avg_pos_90d",
    "is_punch_finish", "gradient_x_hilly", "profile_score_per_gradient",
    "is_transition_stage",
    "team_min_gc_deficit", "team_best_gc_rank",
    "gc_out_of_contention", "team_gc_out_of_contention",
    # Stage outcome probabilities (from stage classifier)
    "p_sprint", "p_breakaway", "p_gc",
    # Interactions: outcome probability × rider speciality
    "p_breakaway_x_oneday", "p_breakaway_x_break_rate",
    "p_breakaway_x_gc_out", "p_breakaway_x_team_gc_out",
    "p_sprint_x_flat_form", "p_gc_x_mountain_form",
]


def _gc_state(results: pd.DataFrame, stages: pd.DataFrame) -> pd.DataFrame:
    """
    GC standing of each rider immediately BEFORE each stage-race stage.

    `results` needs [stage_id, rider_id, time_seconds, status]; `stages` needs
    [id, race_id, date, is_stage_race]. Returns one row per stage-race result
    where the rider has at least one prior stage in the same race, with
    gc_deficit_min (minutes behind the virtual leader) and gc_rank_before.

    time_seconds is the gap behind the stage winner, so cumulative gaps
    approximate GC gaps (bonus seconds ignored). Missing stage times
    contribute 0 rather than invalidating the rider's whole GC.
    """
    st = stages[["id", "race_id", "date", "is_stage_race"]].rename(
        columns={"id": "stage_id"}
    )
    df = results[["stage_id", "rider_id", "time_seconds", "status"]].merge(
        st, on="stage_id"
    )
    df = df[df["is_stage_race"].fillna(0).astype(int) == 1].copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values(
        ["race_id", "rider_id", "date"], kind="stable"
    )

    gap = df["time_seconds"].where(df["status"] == "finished").fillna(0.0)
    grp = [df["race_id"], df["rider_id"]]
    df["_cum_before"] = gap.groupby(grp).cumsum() - gap
    df["_n_before"] = gap.groupby(grp).cumcount()

    valid = df[df["_n_before"] >= 1].copy()
    if valid.empty:
        return pd.DataFrame(
            columns=["stage_id", "rider_id", "gc_deficit_min", "gc_rank_before"]
        )
    leader = valid.groupby("stage_id")["_cum_before"].transform("min")
    valid["gc_deficit_min"] = (valid["_cum_before"] - leader) / 60.0
    valid["gc_rank_before"] = valid.groupby("stage_id")["_cum_before"].rank(method="min")
    return valid[["stage_id", "rider_id", "gc_deficit_min", "gc_rank_before"]]


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
            "SELECT id, stage_id, rider_id, position, status, time_seconds, bib FROM results",
            conn,
        )
        stages = pd.read_sql(
            "SELECT id, race_id, stage_num, date, distance_km, elevation_m, profile_type, surface, gradient_final_km, profile_score FROM stages",
            conn,
        )
        races = pd.read_sql(
            "SELECT id, pcs_slug, name, year, is_stage_race, gender FROM races",
            conn,
        )
        riders = pd.read_sql(
            "SELECT id, name, pcs_rank, speciality, weight_kg, height_cm, dob FROM riders",
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

    # GC standing before each stage (stage races only; NaN elsewhere)
    gc = _gc_state(results, stages)
    results = results.merge(gc, on=["stage_id", "rider_id"], how="left")

    # Join stage date onto results (needed for rolling)
    results = results.merge(
        stages[["id", "date", "profile_type", "surface", "gradient_final_km", "is_stage_race"]].rename(columns={"id": "stage_id"}),
        on="stage_id",
    )

    # ── team GC context ───────────────────────────────────────────────────────
    # Group riders by team using bib // 10 — UCI stage races allocate bibs in
    # blocks of 10 per team (1-8, 11-18, 21-28 …), so integer division is a
    # reliable team proxy without needing a separate team lookup.
    results["team_id"] = (results["bib"] // 10).where(results["bib"].notna())
    _team_gc = (
        results[results["is_stage_race"].fillna(0).astype(int) == 1]
        .groupby(["stage_id", "team_id"])
        .agg(
            team_min_gc_deficit=("gc_deficit_min", "min"),
            team_best_gc_rank=("gc_rank_before", "min"),
        )
        .reset_index()
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
    # Note: "surface", "gradient_final_km", and "is_stage_race" are already on
    # base from the results→stages join above; omit them here to avoid _x/_y
    # collision on merge.
    base = base.merge(
        target_stages[["id", "date", "distance_km", "elevation_m", "profile_type",
                        "prev_profile_type",
                        "stage_num_norm", "race_name", "race_slug",
                        "profile_score"]].rename(
            columns={"id": "stage_id", "date": "stage_date", "profile_type": "stage_profile"}
        ),
        on="stage_id",
    )
    base["top10"] = base["is_top10"].astype(float)

    # Attach team GC stats via bib-based team_id (already on base from results)
    base["team_id"] = (base["bib"] // 10).where(base["bib"].notna())
    base = base.merge(_team_gc, on=["stage_id", "team_id"], how="left")

    # ── rolling features via self-join ────────────────────────────────────────
    # For each (rider, stage_date), we need stats from results where date < stage_date.
    # Strategy: cross-join on rider_id then filter by date, aggregate with groupby.
    # To keep this efficient we use a merge + conditional aggregation pattern.

    log.info("computing rolling features …")

    # history = all results with dates, for rolling lookups
    hist = results[["rider_id", "date", "position", "is_finished",
                     "is_dnf", "is_top10", "is_win", "profile_type", "surface", "stage_id",
                     "gc_rank_before", "gradient_final_km", "is_stage_race"]].copy()

    # Join base (rider, stage_date) onto hist on rider_id, then filter date < stage_date
    # We do this with a merge and then mask — tractable because we group immediately after
    joined = base[["rider_id", "stage_id", "stage_date"]].drop_duplicates().merge(
        hist.rename(columns={
            "date": "hist_date", "stage_id": "hist_stage_id",
            "profile_type": "hist_profile", "surface": "hist_surface",
            "gc_rank_before": "hist_gc_rank",
            "gradient_final_km": "hist_gradient",
            "is_stage_race": "hist_is_stage_race",
        }),
        on="rider_id",
        how="left",
    )
    # Only past results
    joined = joined[joined["hist_date"] < joined["stage_date"]]
    joined["_day_delta"] = (joined["stage_date"] - joined["hist_date"]).dt.days

    # TTT results reflect team performance, not individual ability — exclude
    # them from general rolling form so they don't inflate/distort rider metrics.
    joined_ind = joined[joined["hist_profile"] != "ttt"]

    def _rolling(days: int | None, col: str, agg: str) -> pd.Series:
        """Aggregate `col` over the last `days` days, grouped by (rider_id, stage_id)."""
        if days is not None:
            sub = joined[joined["_day_delta"] <= days]
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

    def _rolling_ind(days: int | None, col: str, agg: str) -> pd.Series:
        """Like _rolling but on joined_ind (TTT stages excluded)."""
        sub = joined_ind[joined_ind["_day_delta"] <= days] if days is not None else joined_ind
        if agg == "mean":
            return sub.groupby(["rider_id", "stage_id"])[col].mean()
        if agg == "sum":
            return sub.groupby(["rider_id", "stage_id"])[col].sum()
        if agg == "count":
            return sub.groupby(["rider_id", "stage_id"])[col].count()
        if agg == "max_date":
            return sub.groupby(["rider_id", "stage_id"])["hist_date"].max()
        raise ValueError(agg)

    attach(_rolling_ind(30,  "position",    "mean"),  "avg_pos_30d")
    attach(_rolling_ind(60,  "position",    "mean"),  "avg_pos_60d")
    attach(_rolling_ind(90,  "position",    "mean"),  "avg_pos_90d")
    attach(_rolling_ind(30,  "is_top10",    "mean"),  "top10_rate_30d")
    attach(_rolling_ind(90,  "is_top10",    "mean"),  "top10_rate_90d")
    attach(_rolling_ind(90,  "is_win",      "mean"),  "win_rate_90d")
    attach(_rolling_ind(90,  "is_dnf",      "mean"),  "dnf_rate_90d")
    attach(_rolling_ind(30,  "hist_stage_id","count"), "races_last_30d")

    last_race = _rolling_ind(None, "hist_date", "max_date")
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
        ("mountain", 30,  "mountain_avg_pos_30d"),
        ("mountain", 90,  "mountain_avg_pos_90d"),
        ("flat",     30,  "flat_avg_pos_30d"),
        ("flat",     90,  "flat_avg_pos_90d"),
        ("itt",      90,  "tt_avg_pos_90d"),
        ("itt",      365, "tt_avg_pos_365d"),
    ]:
        mask = (joined["hist_profile"] == ptype) & (joined["_day_delta"] <= days)
        series = joined[mask].groupby(["rider_id", "stage_id"])["position"].mean()
        attach(series, col)

    for ptype, col in [
        ("hilly",    "hilly_top10_rate_90d"),
        ("mountain", "mountain_top10_rate_90d"),
        ("flat",     "flat_top10_rate_90d"),
        ("itt",      "tt_top10_rate_365d"),
    ]:
        days = 90 if ptype != "itt" else 365
        sub = joined[(joined["hist_profile"] == ptype) & (joined["_day_delta"] <= days)]
        attach(sub.groupby(["rider_id", "stage_id"])["is_top10"].mean(), col)

    # ITT win rate (all-time) — highly distinctive for pure TT specialists
    itt_sub = joined[joined["hist_profile"] == "itt"]
    attach(itt_sub.groupby(["rider_id", "stage_id"])["is_win"].mean(), "tt_win_rate")

    # ── relevant-stage rolling (profile + surface matched) ─────────────────────
    log.info("computing relevant-stage and fatigue features …")

    # Merge target stage surface into joined
    stage_meta = (
        base[["stage_id", "stage_profile", "surface"]]
        .drop_duplicates("stage_id")
        .rename(columns={"surface": "target_surface", "stage_profile": "target_profile"})
    )
    joined = joined.merge(stage_meta, on="stage_id", how="left")
    joined["hist_surface"]   = joined["hist_surface"].fillna("road")
    joined["target_surface"] = joined["target_surface"].fillna("road")

    is_special    = joined["target_surface"].isin(["cobbled", "gravel"])
    surface_match = (~is_special) | (joined["hist_surface"] == joined["target_surface"])
    relevant_mask = (joined["hist_profile"] == joined["target_profile"]) & surface_match

    relevant_30 = joined[relevant_mask & (joined["_day_delta"] <= 30)]
    relevant_90 = joined[relevant_mask & (joined["_day_delta"] <= 90)]

    relevant_365 = joined[relevant_mask & (joined["_day_delta"] <= 365)]

    attach(relevant_30.groupby(["rider_id", "stage_id"])["position"].mean(), "relevant_avg_pos_30d")
    attach(relevant_90.groupby(["rider_id", "stage_id"])["position"].mean(), "relevant_avg_pos_90d")
    attach(relevant_90.groupby(["rider_id", "stage_id"])["is_top10"].mean(), "relevant_top10_rate_90d")
    attach(relevant_365.groupby(["rider_id", "stage_id"])["position"].mean(), "relevant_avg_pos_365d")
    attach(relevant_365.groupby(["rider_id", "stage_id"])["is_top10"].mean(), "relevant_top10_rate_365d")

    # ── fatigue: calendar days raced ──────────────────────────────────────────
    race_days_7  = joined[joined["_day_delta"] <= 7 ].groupby(["rider_id", "stage_id"])["hist_date"].nunique()
    race_days_14 = joined[joined["_day_delta"] <= 14].groupby(["rider_id", "stage_id"])["hist_date"].nunique()
    attach(race_days_7,  "race_days_last_7d")
    attach(race_days_14, "race_days_last_14d")

    # ── breakaway propensity ──────────────────────────────────────────────────
    # Top-10s scored while starting the stage >20th on GC come from breakaways,
    # not the favourites' group — a distinct skill the form features miss.
    break_sub = joined_ind[
        (joined_ind["hist_gc_rank"] > 20) & (joined_ind["_day_delta"] <= 365)
    ]
    attach(break_sub.groupby(["rider_id", "stage_id"])["is_top10"].mean(), "break_top10_rate_365d")
    attach(break_sub.groupby(["rider_id", "stage_id"])["is_top10"].sum(),  "break_top10s_365d")
    base["break_top10s_365d"] = base["break_top10s_365d"].fillna(0)

    # ── punch-finish affinity ─────────────────────────────────────────────────
    # Hilly stages with steep final km (>=5%) are classics/puncher territory,
    # distinct from GC-style mountain stages or lumpy breakaway stages.
    punch_sub = joined_ind[
        (joined_ind["hist_profile"] == "hilly") &
        (joined_ind["hist_gradient"].fillna(0) >= 5.0) &
        (joined_ind["_day_delta"] <= 90)
    ]
    attach(punch_sub.groupby(["rider_id", "stage_id"])["is_top10"].mean(), "punch_top10_rate_90d")
    attach(punch_sub.groupby(["rider_id", "stage_id"])["position"].mean(), "punch_avg_pos_90d")

    # ── one-day race form (breakaway proxy) ───────────────────────────────────
    # Riders who succeed in one-day races perform similarly in GT breakaways:
    # both require attacking from a non-GC position and staying away.
    oneday = joined_ind[joined_ind["hist_is_stage_race"].fillna(0) == 0]
    attach(
        oneday[oneday["_day_delta"] <= 365].groupby(["rider_id", "stage_id"])["is_top10"].mean(),
        "oneday_top10_rate_365d",
    )
    attach(
        oneday[oneday["_day_delta"] <= 90].groupby(["rider_id", "stage_id"])["position"].mean(),
        "oneday_avg_pos_90d",
    )

    # ── elevation density ─────────────────────────────────────────────────────
    base["elevation_per_km"] = base["elevation_m"] / base["distance_km"].replace(0, np.nan)

    # ── GC & team out-of-contention flags ─────────────────────────────────────
    # Riders / teams >10 min down have strong incentive to go in breaks.
    # NaN (no GC data: stage 1 or one-day race) is left as NaN — HGBC handles it.
    base["gc_out_of_contention"] = np.where(
        base["gc_deficit_min"].notna(),
        (base["gc_deficit_min"] > 10).astype(float),
        np.nan,
    )
    base["team_gc_out_of_contention"] = np.where(
        base["team_min_gc_deficit"].notna(),
        (base["team_min_gc_deficit"] > 10).astype(float),
        np.nan,
    )

    # ── rider attributes ──────────────────────────────────────────────────────
    base = base.merge(
        riders[["id", "name", "pcs_rank", "speciality", "weight_kg", "height_cm", "dob"]].rename(
            columns={"id": "rider_id", "name": "rider_name"}
        ),
        on="rider_id", how="left",
    )
    base["dob_dt"] = pd.to_datetime(base["dob"], errors="coerce")
    base["age_at_race"] = (base["stage_date"] - base["dob_dt"]).dt.days / 365.25

    # ── one-hot encode ────────────────────────────────────────────────────────
    for pt in PROFILE_TYPES:
        base[f"is_{pt}"] = (base["stage_profile"] == pt).astype(int)
    for sp in SPECIALITIES:
        base[f"spec_{sp}"] = (base["speciality"] == sp).astype(int)

    base["is_cobbled"] = (base["surface"] == "cobbled").astype(int)
    base["is_gravel"]  = (base["surface"] == "gravel").astype(int)

    # Punch-finish: hilly stage with steep final km → punchers/classics riders
    base["is_punch_finish"] = (
        (base["stage_profile"] == "hilly") &
        (base["gradient_final_km"].fillna(0) >= 5.0)
    ).astype(int)
    # Continuous version for the model to interpolate on
    base["gradient_x_hilly"] = (
        base["gradient_final_km"].fillna(0) * (base["stage_profile"] == "hilly").astype(int)
    )
    # Breakaway-prone signal: high climbing load but gentle finish → peloton lets break go.
    # profile_score captures total climbing difficulty; dividing by (gradient+1) isolates
    # stages where the climbing is in the middle, not a steep final summit.
    base["profile_score_per_gradient"] = (
        base["profile_score"].fillna(0) / (base["gradient_final_km"].fillna(0) + 1.0)
    )
    # Transition stage: hilly or flat day after a mountain stage — teams recover,
    # breakaways succeed at a much higher rate.
    base["is_transition_stage"] = (
        (base["prev_profile_type"] == "mountain") &
        (base["stage_profile"].isin(["hilly", "flat"]))
    ).astype(int)

    base["prev_stage_is_mountain"] = (base["prev_profile_type"] == "mountain").astype(int)
    base["prev_stage_is_hilly"]    = (base["prev_profile_type"] == "hilly").astype(int)

    base = base.drop(columns=["profile_type"], errors="ignore").rename(columns={
        "stage_profile": "profile_type",
    })

    # ── ITT interaction features ──────────────────────────────────────────────
    # Explicit interactions so the model can learn "TT form matters *on ITT stages*"
    # without needing many examples to discover the split on is_itt internally.
    is_itt = base["profile_type"] == "itt"
    base["tt_win_rate_x_itt"]      = base["tt_win_rate"].fillna(0)      * is_itt.astype(int)
    base["tt_avg_pos_365d_x_itt"]  = base["tt_avg_pos_365d"].fillna(50) * is_itt.astype(int)
    base["tt_avg_pos_90d_x_itt"]   = base["tt_avg_pos_90d"].fillna(50)  * is_itt.astype(int)

    # ── stage outcome probabilities ───────────────────────────────────────────
    # Load stage classifier (trained separately). If not yet available, fill with
    # zeros so the main model trains without it and learns to ignore them.
    from model.stage_classifier import load as _load_clf, predict_proba as _clf_proba
    _clf_bundle = _load_clf()
    if _clf_bundle is not None and not base.empty:
        stage_meta = base.drop_duplicates("stage_id").set_index("stage_id")
        proba = _clf_proba(stage_meta, _clf_bundle)
        base = base.join(proba, on="stage_id")
    else:
        log.warning("stage classifier skipped (not trained or empty base) — p_sprint/p_breakaway/p_gc set to NaN")
        base["p_sprint"] = np.nan
        base["p_breakaway"] = np.nan
        base["p_gc"] = np.nan

    # ── stage outcome × rider skill interactions ──────────────────────────────
    # Explicit products so the model doesn't have to discover them via splits.
    _pb = base["p_breakaway"].fillna(0)
    _ps = base["p_sprint"].fillna(0)
    _pg = base["p_gc"].fillna(0)
    base["p_breakaway_x_oneday"]     = _pb * base["oneday_top10_rate_365d"].fillna(0)
    base["p_breakaway_x_break_rate"] = _pb * base["break_top10_rate_365d"].fillna(0)
    base["p_breakaway_x_gc_out"]     = _pb * base["gc_out_of_contention"].fillna(0)
    base["p_breakaway_x_team_gc_out"]= _pb * base["team_gc_out_of_contention"].fillna(0)
    base["p_sprint_x_flat_form"]     = _ps * base["flat_top10_rate_90d"].fillna(0)
    base["p_gc_x_mountain_form"]     = _pg * base["mountain_top10_rate_90d"].fillna(0)

    log.info("features built: %d rows, %d columns", len(base), len(base.columns))
    return base


# ── DB loaders (also used by predict.py for start-list predictions) ───────────

def _load_results(conn) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT id, stage_id, rider_id, position, status, time_seconds FROM results", conn
    )


def _load_stages(conn) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT id, race_id, stage_num, date, distance_km, elevation_m, profile_type, surface, gradient_final_km, profile_score FROM stages",
        conn,
    )


def _load_races(conn) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT id, pcs_slug, name, year, is_stage_race FROM races", conn
    )


def _load_riders(conn) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT id, pcs_slug, name, team, pcs_rank, speciality, weight_kg, height_cm, dob FROM riders",
        conn,
    )
