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
from features.ratings import compute_ratings

log = logging.getLogger(__name__)

PROFILE_TYPES = ["flat", "hilly", "mountain", "itt", "ttt"]
SPECIALITIES  = ["gc", "sprinter", "puncher", "classics", "tt", "climber"]

LEGACY_FEATURE_COLS = [
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


# Point-in-time strength: PCS points and field-relative finishing level over
# rolling windows (points are tier-weighted by PCS, so they also say how hard
# the races were).
STRENGTH_COLS = [
    "pts_90d", "pts_365d", "pts_730d", "pts_ewm",
    "pct_mean_90d", "pct_mean_365d", "pct_ewm",
    "top3_rate_365d", "top10_rate_365d", "wins_365d", "best_pos_365d",
    "n_results_365d", "n_results_all", "tier_mean_365d",
]
# Same profile type (and surface, for cobbles / gravel) as the target stage.
RELEVANT_COLS = [
    "rel_pts_365d", "rel_pts_730d", "rel_pct_mean_365d",
    "rel_top3_rate_730d", "rel_best_pos_730d", "rel_n_730d",
]
# Skills by how past stages were actually decided (bunch sprint / small
# group), which the coarse profile label cannot tell: half of all "hilly"
# stages end in a bunch sprint.
FINISH_TYPE_COLS = [
    "bunch_top10_rate_730d", "bunch_top3_rate_730d", "bunch_pts_730d", "bunch_n_730d", "bunch_pct_mean_365d",
    "select_top10_rate_730d", "select_pts_730d", "select_n_730d", "select_pct_mean_365d",
    "uphill_top10_rate_730d", "uphill_pts_730d",
    "mtn_lgap_mean_365d", "itt_gap_per_km_365d", "itt_pts_730d",
]
# Earlier editions of the same race, and earlier stages of this edition.
RACE_HISTORY_COLS = [
    "race_hist_best_pos", "race_hist_pct_mean", "race_hist_n", "race_hist_top10s", "race_hist_pts",
    "cur_pct_mean", "cur_top10s", "cur_n", "cur_pts", "cur_last_pos",
    "cur_same_pct_mean", "cur_same_best_pos", "cur_bunch_pct_mean", "cur_select_pct_mean",
]
# Role inside the team (teams share a bib decade; bib x1 is the nominal leader).
TEAM_COLS = [
    "team_size", "team_rank_pts", "team_rank_rel", "team_best_pts", "team_pts_sum", "team_pts_share",
    "bib_is_leader",
]
# Where the rider stands among today's starters (1.0 = best in the field).
FIELD_RANK_SOURCES = [
    "pts_365d", "pts_ewm", "pct_ewm", "pct_mean_365d", "rel_pts_730d", "rel_pct_mean_365d",
    "bunch_pts_730d", "select_pts_730d", "top10_rate_90d", "relevant_top10_rate_365d",
    "race_hist_pct_mean", "cur_same_pct_mean", "cur_pct_mean",
]
FIELD_COLS = [f"{c}_frank" for c in FIELD_RANK_SOURCES] + [
    "field_size", "field_pts_top10", "tier_code",
    "race_bunch_rate_prev", "race_prev_stages",
]
# Elo-style ratings before the stage (features.ratings): strength adjusted
# for the fields a rider's results were achieved against.
RATING_COLS = [
    "elo_all", "elo_bunch", "elo_select", "elo_itt", "elo_mountain", "elo_cobbled",
    "elo_all_frank", "elo_all_gap", "elo_itt_frank", "elo_mountain_frank", "elo_cobbled_frank",
    "team_rank_elo",
]
# Finish-type skills weighted by the stage classifier's sprint probability
# (set in attach_stage_outcome, because the classifier is refitted per fold).
OUTCOME_MIX_COLS = [
    "exp_type_top10", "exp_type_pts", "exp_type_top10_frank", "exp_type_pts_frank",
    "elo_exp", "elo_exp_frank", "elo_exp_gap", "team_rank_elo_exp",
]

# Results on stages like this one: every earlier result weighted by how close
# that stage's profile score (and, for simg_*, the gradient of its final km)
# was to the target stage's.  A continuous version of RELEVANT_COLS, which
# only know flat / hilly / mountain.
SIMILAR_STAGE_COLS = [
    "sim_pct", "sim_top10", "sim_pts", "sim_n",
    "simg_pct", "simg_top10", "simg_pts", "simg_n",
    "sim_pct_frank", "sim_pts_frank", "simg_pct_frank",
]

# "Stages like this one" kernels: (name, also match the final-km gradient).
SIM_KERNELS = (("sim", False), ("simg", True))
SIM_PROFILE_BANDWIDTH = 2.0     # in square roots of the profile score: 100 vs 64 is one width
SIM_GRADIENT_BANDWIDTH = 2.5    # percent of final-km gradient
SIM_HALF_LIFE_DAYS = 365
SIM_WINDOW_DAYS = 730
# What a typical stage of each profile icon looks like (medians of the stored
# stages): the stand-in for a route PCS has not published yet, or whose page
# could not be read.  Without it such a stage loses a whole top-10 hit
# (results/accuracy_2026-11.md, round 6); with it about 0.4.
ROUTE_DEFAULTS = {
    "flat":     {"profile_score": 10.0, "gradient_final_km": 0.3, "elevation_per_km": 5.0},
    "hilly":    {"profile_score": 85.0, "gradient_final_km": 1.1, "elevation_per_km": 12.6},
    "mountain": {"profile_score": 220.0, "gradient_final_km": 5.2, "elevation_per_km": 19.6},
    "itt":      {"profile_score": 8.0, "gradient_final_km": 0.6, "elevation_per_km": 9.5},
    "ttt":      {"profile_score": 8.0, "gradient_final_km": 0.6, "elevation_per_km": 7.0},
}

# Rider-page facts as of the day the profile was scraped, not of the race:
# pcs_rank (empty in today's database) and the speciality PCS derives from
# career points.  Used for a 2024 stage they would be a look into the future,
# and leaving them out cost nothing in the walk-forward test; the
# point-in-time strength and rating features carry that information.
_NOT_POINT_IN_TIME = {"pcs_rank"} | {f"spec_{sp}" for sp in SPECIALITIES}

FEATURE_COLS = [c for c in (
    LEGACY_FEATURE_COLS + STRENGTH_COLS + RELEVANT_COLS + FINISH_TYPE_COLS
    + RACE_HISTORY_COLS + TEAM_COLS + FIELD_COLS + RATING_COLS + OUTCOME_MIX_COLS + SIMILAR_STAGE_COLS
) if c not in _NOT_POINT_IN_TIME]

KEY = ["rider_id", "stage_id"]

# status of a start-list row for a stage that has not been ridden yet
ENTRY_STATUS = "startlist"

TIER_WEIGHT = {"WorldTour": 1.0, "ProSeries": 0.6, ".1": 0.4}
TIER_CODE = {"WorldTour": 3, "ProSeries": 2, ".1": 1}


def race_tier(race_class: str | None) -> str:
    """WorldTour / ProSeries / .1 from a UCI class code (``2.UWT``, ``1.Pro``, ``2.1``)."""
    c = (race_class or "").upper()
    if c.endswith("UWT") or c.endswith("WWT"):
        return "WorldTour"
    if c.endswith("PRO") or c.endswith("HC"):
        return "ProSeries"
    return ".1"


def race_base(race_slug: pd.Series) -> pd.Series:
    """``race/tour-de-france/2026`` → ``race/tour-de-france`` (same race, any edition)."""
    return race_slug.str.replace(r"/\d{4}$", "", regex=True)


def stage_finish_facts(results: pd.DataFrame) -> pd.DataFrame:
    """Per stage, from its stored result: finishers, riders in the top 10 on
    the winner's time, and the finish type that follows.

    bunch   6+ of the top 10 on the winner's time
    select  at most 3 (solo, break or small group)
    mixed   in between
    NaN     no times stored for the top 10

    Only ever used for stages that lie in the past of the stage being
    predicted.
    """
    fin = results[results["status"] == "finished"]
    top = fin[fin["position"] <= 10]
    facts = pd.DataFrame({
        "n_fin": fin.groupby("stage_id").size(),
        "n_zero_top10": top[top["time_seconds"] == 0].groupby("stage_id").size(),
        "n_timed_top10": top[top["time_seconds"].notna()].groupby("stage_id").size(),
    })
    facts["n_zero_top10"] = facts["n_zero_top10"].fillna(0)
    timed = facts["n_timed_top10"].fillna(0) >= 3
    facts["finish_type"] = np.select(
        [~timed, facts["n_zero_top10"] >= 6, facts["n_zero_top10"] <= 3],
        [None, "bunch", "select"], default="mixed")
    facts.loc[~timed, "finish_type"] = None
    return facts


def race_sprint_history(stages: pd.DataFrame) -> pd.DataFrame:
    """Share of bunch finishes in earlier editions of each stage's race.

    ``stages`` needs id, race_slug, date, profile_type and finish_type (NaN
    where no result is stored).  Returns ``race_bunch_rate_prev`` (NaN without
    an earlier edition) and ``race_prev_stages``, indexed by stage id.  Time
    trials are not counted.
    """
    st = stages[["id", "race_slug", "date", "profile_type", "finish_type"]].copy()
    st["base"] = race_base(st["race_slug"])
    st["year"] = pd.to_datetime(st["date"], errors="coerce").dt.year
    road = st[~st["profile_type"].isin(["itt", "ttt"]) & st["finish_type"].notna()]
    per_year = road.groupby(["base", "year"]).agg(
        n=("id", "size"), bunch=("finish_type", lambda s: (s == "bunch").sum())).reset_index()
    editions = st[["base", "year"]].drop_duplicates().dropna()
    pairs = editions.merge(per_year, on="base", suffixes=("", "_prev"))
    pairs = pairs[pairs["year_prev"] < pairs["year"]]
    prev = pairs.groupby(["base", "year"]).agg(race_prev_stages=("n", "sum"), _bunch=("bunch", "sum")).reset_index()
    prev["race_bunch_rate_prev"] = prev["_bunch"] / prev["race_prev_stages"]
    out = st.merge(prev[["base", "year", "race_prev_stages", "race_bunch_rate_prev"]],
                   on=["base", "year"], how="left")
    out["race_prev_stages"] = out["race_prev_stages"].fillna(0.0)
    return out.set_index("id")[["race_bunch_rate_prev", "race_prev_stages"]]


def _field_rank(base: pd.DataFrame, col: str) -> pd.Series:
    """Percentile of ``col`` among the stage's starters (1.0 = highest)."""
    return base.groupby("stage_id")[col].rank(pct=True, na_option="keep")


def _field_gap(base: pd.DataFrame, col: str, top: int = 10) -> pd.Series:
    """``col`` minus the mean of the stage's ``top`` highest values: how far a
    rider sits below (or above) the level of today's favourites."""
    ranked = base.groupby("stage_id")[col].rank(ascending=False, method="first")
    level = base[col].where(ranked <= top).groupby(base["stage_id"]).transform("mean")
    return base[col] - level


_PROFILE_DTYPE = pd.CategoricalDtype(PROFILE_TYPES)
_SURFACE_DTYPE = pd.CategoricalDtype(["road", "cobbled", "gravel"])
_FINISH_DTYPE = pd.CategoricalDtype(["bunch", "select", "mixed"])


def _history_pairs(targets: pd.DataFrame, hist: pd.DataFrame) -> pd.DataFrame:
    """One row per (target rider-stage, result of that rider dated before the stage).

    ``targets`` needs rider_id, stage_id, stage_date; ``hist`` rider_id and
    hist_date.  Every other column of either frame is carried along.  Rows of
    a rider are a prefix of his date-sorted results, so the pairs are built
    from index ranges instead of a rider-by-rider cross join.
    """
    hist = hist.sort_values(["rider_id", "hist_date"], kind="stable").reset_index(drop=True)
    riders = pd.Index(np.sort(hist["rider_id"].unique()))
    span = 1_000_000  # more days than any date since 1970
    h_day = hist["hist_date"].to_numpy().astype("datetime64[D]").astype(np.int64)
    h_key = riders.get_indexer(hist["rider_id"]).astype(np.int64) * span + h_day
    t_code = riders.get_indexer(targets["rider_id"]).astype(np.int64)
    t_day = targets["stage_date"].to_numpy().astype("datetime64[D]").astype(np.int64)
    lo = np.searchsorted(h_key, t_code * span, side="left")
    hi = np.searchsorted(h_key, t_code * span + t_day, side="left")
    k = np.where(t_code >= 0, hi - lo, 0)
    rep = np.repeat(np.arange(len(targets)), k)
    idx = np.repeat(lo, k) + (np.arange(int(k.sum())) - np.repeat(np.cumsum(k) - k, k))
    out = {c: targets[c].array.take(rep) for c in targets.columns}
    for c in hist.columns:
        if c != "rider_id":
            out[c] = hist[c].array.take(idx)
    joined = pd.DataFrame(out)
    joined["_day_delta"] = (joined["stage_date"] - joined["hist_date"]).dt.days.astype(np.int32)
    return joined


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


def attach_stage_outcome(base: pd.DataFrame, stage_clf="disk") -> pd.DataFrame:
    """Add the stage classifier's p_sprint / p_breakaway / p_gc and their
    interactions with rider skills.

    ``stage_clf`` is ``"disk"`` (the classifier saved by ``main.py train``), a
    bundle from ``model.stage_classifier`` (walk-forward evaluation passes one
    trained only on stages before the test window), or None (columns are NaN
    and the main model learns to ignore them).
    """
    from model.stage_classifier import load as _load_clf, predict_proba as _clf_proba

    base = base.drop(columns=["p_sprint", "p_breakaway", "p_gc"], errors="ignore")
    bundle = _load_clf() if isinstance(stage_clf, str) and stage_clf == "disk" else stage_clf
    if bundle is not None and not base.empty:
        stage_meta = base.drop_duplicates("stage_id").set_index("stage_id")
        proba = _clf_proba(stage_meta, bundle)
        base = base.join(proba, on="stage_id")
    else:
        log.warning("stage classifier skipped (not trained or empty base) — p_sprint/p_breakaway/p_gc set to NaN")
        base["p_sprint"] = np.nan
        base["p_breakaway"] = np.nan
        base["p_gc"] = np.nan

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

    # Finish-type skill expected for this stage: sprint record weighted by the
    # chance of a bunch finish, small-group record by the rest.
    if "bunch_top10_rate_730d" in base.columns:
        ps = base["p_sprint"].fillna(0.5)
        base["exp_type_top10"] = (ps * base["bunch_top10_rate_730d"].fillna(0)
                                  + (1 - ps) * base["select_top10_rate_730d"].fillna(0))
        base["exp_type_pts"] = ps * base["bunch_pts_730d"].fillna(0) + (1 - ps) * base["select_pts_730d"].fillna(0)
        base["exp_type_top10_frank"] = _field_rank(base, "exp_type_top10")
        base["exp_type_pts_frank"] = _field_rank(base, "exp_type_pts")

        # Rating for the expected kind of finish (time-trial rating in a time
        # trial); a rider without one of that kind falls back to his overall.
        overall = base["elo_all"]
        road = ps * base["elo_bunch"].fillna(overall) + (1 - ps) * base["elo_select"].fillna(overall)
        base["elo_exp"] = road.where(base["is_itt"] != 1, base["elo_itt"].fillna(overall))
        base["elo_exp_frank"] = _field_rank(base, "elo_exp")
        base["elo_exp_gap"] = _field_gap(base, "elo_exp")
        base["team_rank_elo_exp"] = base.groupby(["stage_id", "team_key"])["elo_exp"].rank(
            ascending=False, method="min")
    return base


# Most (target row, earlier result) pairs handled in one piece.  The pairs
# frame is the memory peak of a feature build (about 0.4 GB per million pairs);
# riders are processed in groups that stay under this bound.
MAX_PAIRS_PER_CHUNK = 4_000_000


def _rider_chunks(targets: pd.DataFrame, hist: pd.DataFrame, max_pairs: int) -> list[np.ndarray]:
    """Groups of rider ids whose (target rows × results) products add up to
    about ``max_pairs`` at most (a rider above the bound stays in one group)."""
    n_targets = targets.groupby("rider_id").size()
    n_hist = hist.groupby("rider_id").size().reindex(n_targets.index).fillna(0)
    cost = (n_targets * n_hist).sort_index()
    group = (cost.cumsum() // max(max_pairs, 1)).astype(int)
    return [g.index.to_numpy() for _, g in cost.groupby(group)]


def _history_features(targets: pd.DataFrame, hist: pd.DataFrame) -> pd.DataFrame:
    """Rolling features of each target row from that rider's earlier results.

    One row per (target rider-stage, earlier result of that rider) is built
    (``_history_pairs``); every feature is a masked group-by over that frame.
    Returns one row per target that has any earlier result, indexed by
    (rider_id, stage_id).
    """
    joined = _history_pairs(targets, hist)
    dd = joined["_day_delta"].to_numpy()
    # TTT results reflect team performance, not individual ability — exclude
    # them from general rolling form so they don't inflate/distort rider metrics.
    not_ttt = (joined["hist_profile"] != "ttt").to_numpy()

    frames: list[pd.DataFrame] = []

    def add(mask, **aggs) -> None:
        """Group-by aggregates of the history rows under ``mask``, one row per target."""
        cols = sorted({src for src, _ in aggs.values()})
        sub = joined.loc[mask, KEY + cols] if mask is not None else joined[KEY + cols]
        frames.append(sub.groupby(KEY, sort=False).agg(**aggs))

    add(not_ttt & (dd <= 30), avg_pos_30d=("position", "mean"), top10_rate_30d=("is_top10", "mean"),
        races_last_30d=("hist_stage_id", "count"))
    add(not_ttt & (dd <= 60), avg_pos_60d=("position", "mean"))
    add(not_ttt & (dd <= 90), avg_pos_90d=("position", "mean"), top10_rate_90d=("is_top10", "mean"),
        win_rate_90d=("is_win", "mean"), dnf_rate_90d=("is_dnf", "mean"),
        pts_90d=("pts", "sum"), pct_mean_90d=("pct", "mean"))
    add(not_ttt, _last_race=("hist_date", "max"), n_results_all=("hist_stage_id", "size"))

    # ── profile affinity ──────────────────────────────────────────────────────
    prof = {pt: (joined["hist_profile"] == pt).to_numpy() for pt in ("mountain", "flat", "hilly", "itt")}
    add(prof["mountain"], mountain_avg_pos=("position", "mean"))
    add(prof["flat"], flat_avg_pos=("position", "mean"))
    add(prof["hilly"], hilly_avg_pos=("position", "mean"))
    # ITT win rate (all-time) — highly distinctive for pure TT specialists
    add(prof["itt"], tt_avg_pos=("position", "mean"), tt_win_rate=("is_win", "mean"))

    # ── profile-specific rolling features ─────────────────────────────────────
    for pt, name in (("hilly", "hilly"), ("mountain", "mountain"), ("flat", "flat")):
        add(prof[pt] & (dd <= 30), **{f"{name}_avg_pos_30d": ("position", "mean")})
        add(prof[pt] & (dd <= 90), **{f"{name}_avg_pos_90d": ("position", "mean"),
                                      f"{name}_top10_rate_90d": ("is_top10", "mean")})
    add(prof["itt"] & (dd <= 90), tt_avg_pos_90d=("position", "mean"))
    add(prof["itt"] & (dd <= 365), tt_avg_pos_365d=("position", "mean"), tt_top10_rate_365d=("is_top10", "mean"))

    # ── relevant-stage rolling (profile + surface matched) ─────────────────────
    is_special = joined["target_surface"].isin(["cobbled", "gravel"]).to_numpy()
    surface_match = (~is_special) | (joined["hist_surface"] == joined["target_surface"]).to_numpy()
    rel = (joined["hist_profile"] == joined["target_profile"]).to_numpy() & surface_match
    del is_special, surface_match

    add(rel & (dd <= 30), relevant_avg_pos_30d=("position", "mean"))
    add(rel & (dd <= 90), relevant_avg_pos_90d=("position", "mean"), relevant_top10_rate_90d=("is_top10", "mean"))
    add(rel & (dd <= 365), relevant_avg_pos_365d=("position", "mean"),
        relevant_top10_rate_365d=("is_top10", "mean"))

    # ── fatigue: calendar days raced ──────────────────────────────────────────
    add(dd <= 7, race_days_last_7d=("hist_date", "nunique"))
    add(dd <= 14, race_days_last_14d=("hist_date", "nunique"))

    # ── breakaway propensity ──────────────────────────────────────────────────
    # Top-10s scored while starting the stage >20th on GC come from breakaways,
    # not the favourites' group — a distinct skill the form features miss.
    add(not_ttt & (joined["hist_gc_rank"] > 20).to_numpy() & (dd <= 365),
        break_top10_rate_365d=("is_top10", "mean"), break_top10s_365d=("is_top10", "sum"))

    # ── punch-finish affinity ─────────────────────────────────────────────────
    # Hilly stages with steep final km (>=5%) are classics/puncher territory,
    # distinct from GC-style mountain stages or lumpy breakaway stages.
    add(not_ttt & prof["hilly"] & (joined["hist_gradient"].fillna(0) >= 5.0).to_numpy() & (dd <= 90),
        punch_top10_rate_90d=("is_top10", "mean"), punch_avg_pos_90d=("position", "mean"))

    # ── one-day race form (breakaway proxy) ───────────────────────────────────
    # Riders who succeed in one-day races perform similarly in GT breakaways:
    # both require attacking from a non-GC position and staying away.
    oneday = not_ttt & ~joined["hist_is_stage_race"].to_numpy()
    add(oneday & (dd <= 365), oneday_top10_rate_365d=("is_top10", "mean"))
    add(oneday & (dd <= 90), oneday_avg_pos_90d=("position", "mean"))
    del oneday

    # ══ point-in-time strength, finish-type skills, race history ══════════════

    # Recency-weighted level (half-life 45 days) and points (half-life 120 days)
    w_form = np.power(0.5, dd / 45.0)
    pct = joined["pct"].to_numpy(dtype=np.float64)
    ewm = pd.DataFrame({
        "rider_id": joined["rider_id"].to_numpy()[not_ttt], "stage_id": joined["stage_id"].to_numpy()[not_ttt],
        "_pct_w": (pct * w_form)[not_ttt], "_w": np.where(np.isnan(pct), np.nan, w_form)[not_ttt],
        "_pts_w": (joined["pts"].to_numpy(dtype=np.float64) * np.power(0.5, dd / 120.0))[not_ttt],
    })
    e = ewm.groupby(KEY, sort=False).agg(_pct_w=("_pct_w", "sum"), _w=("_w", "sum"), pts_ewm=("_pts_w", "sum"))
    e["pct_ewm"] = (e["_pct_w"] / e["_w"]).where(e["_w"] > 0)
    frames.append(e[["pct_ewm", "pts_ewm"]])
    del ewm, e, w_form, pct

    add(not_ttt & (dd <= 365),
        pts_365d=("pts", "sum"), pct_mean_365d=("pct", "mean"),
        top3_rate_365d=("is_top3", "mean"), top10_rate_365d=("is_top10", "mean"),
        wins_365d=("is_win", "sum"), best_pos_365d=("position", "min"),
        n_results_365d=("hist_stage_id", "size"), tier_mean_365d=("tier_w", "mean"))
    add(not_ttt & (dd <= 730), pts_730d=("pts", "sum"))

    rel_ind = rel & not_ttt
    add(rel_ind & (dd <= 365), rel_pts_365d=("pts", "sum"), rel_pct_mean_365d=("pct", "mean"))
    add(rel_ind & (dd <= 730),
        rel_pts_730d=("pts", "sum"), rel_top3_rate_730d=("is_top3", "mean"),
        rel_best_pos_730d=("position", "min"), rel_n_730d=("hist_stage_id", "size"))

    bunch = not_ttt & (joined["hist_finish"] == "bunch").to_numpy()
    select = not_ttt & (joined["hist_finish"] == "select").to_numpy()
    add(bunch & (dd <= 730),
        bunch_top10_rate_730d=("is_top10", "mean"), bunch_top3_rate_730d=("is_top3", "mean"),
        bunch_pts_730d=("pts", "sum"), bunch_n_730d=("hist_stage_id", "size"))
    add(bunch & (dd <= 365), bunch_pct_mean_365d=("pct", "mean"))
    add(select & (dd <= 730),
        select_top10_rate_730d=("is_top10", "mean"), select_pts_730d=("pts", "sum"),
        select_n_730d=("hist_stage_id", "size"))
    add(select & (dd <= 365), select_pct_mean_365d=("pct", "mean"))
    add(not_ttt & (joined["hist_gradient"].fillna(0) >= 4.0).to_numpy() & (dd <= 730),
        uphill_top10_rate_730d=("is_top10", "mean"), uphill_pts_730d=("pts", "sum"))
    add(prof["mountain"] & (dd <= 365), mtn_lgap_mean_365d=("lgap", "mean"))

    # Seconds lost per km in time trials (finishers only)
    joined["_gap_km"] = (joined["hist_time"].clip(0, 3600) / joined["hist_distance"].where(joined["hist_distance"] > 0)
                         ).where(joined["is_finished"])
    add(prof["itt"] & (dd <= 730), itt_pts_730d=("pts", "sum"))
    add(prof["itt"] & (dd <= 365), itt_gap_per_km_365d=("_gap_km", "mean"))

    # Earlier editions of this race (Roubaix specialists, home races)
    prev_ed = not_ttt & ((joined["hist_race_base"] == joined["target_race_base"])
                         & (joined["hist_race_year"] < joined["target_race_year"])).to_numpy()
    add(prev_ed,
        race_hist_best_pos=("position", "min"), race_hist_pct_mean=("pct", "mean"),
        race_hist_n=("hist_stage_id", "size"), race_hist_top10s=("is_top10", "sum"),
        race_hist_pts=("pts", "sum"))

    # Earlier stages of this edition: who is contesting what in this race
    cur = not_ttt & (joined["hist_race_id"] == joined["target_race_id"]).to_numpy()
    add(cur, cur_pct_mean=("pct", "mean"), cur_top10s=("is_top10", "sum"),
        cur_n=("hist_stage_id", "size"), cur_pts=("pts", "sum"))
    add(cur & rel, cur_same_pct_mean=("pct", "mean"), cur_same_best_pos=("position", "min"))
    add(cur & bunch, cur_bunch_pct_mean=("pct", "mean"))
    add(cur & select, cur_select_pct_mean=("pct", "mean"))
    last = (joined.loc[cur, KEY + ["hist_date", "hist_stage_id", "position"]]
            .sort_values(["hist_date", "hist_stage_id"], kind="stable")
            .groupby(KEY, sort=False).tail(1).set_index(KEY)["position"].rename("cur_last_pos"))
    frames.append(last.to_frame())

    # ── stages like this one ──────────────────────────────────────────────────
    # Every earlier result weighted by how close its stage was to the target
    # stage: profile score (on a square-root scale, so 20 vs 40 counts like
    # 200 vs 260), for simg_* also the gradient of the final km, and recency.
    # Time trials are compared with time trials, road stages with road stages.
    road_h = ~joined["hist_profile"].isin(["itt", "ttt"]).to_numpy()
    same_kind = np.where((joined["target_profile"] == "itt").to_numpy(), prof["itt"], road_h)
    with np.errstate(invalid="ignore"):
        d_ps = (np.sqrt(joined["hist_ps"].to_numpy(dtype=np.float64))
                - np.sqrt(joined["target_ps"].to_numpy(dtype=np.float64)))
        # an unknown gradient on either side: no gradient weighting for that pair
        d_grad = np.nan_to_num(joined["hist_gradient"].to_numpy(dtype=np.float64)
                               - joined["target_grad"].to_numpy(dtype=np.float64), nan=0.0)
        k_ps = np.exp(-0.5 * (d_ps / SIM_PROFILE_BANDWIDTH) ** 2) * np.power(0.5, dd / SIM_HALF_LIFE_DAYS)
        k_grad = np.exp(-0.5 * (d_grad / SIM_GRADIENT_BANDWIDTH) ** 2)
    top10 = joined["is_top10"].to_numpy(dtype=np.float64)
    pts = joined["pts"].to_numpy(dtype=np.float64)
    pct = joined["pct"].to_numpy(dtype=np.float64)          # NaN for non-finishers: averaged over finishes
    rider_col, stage_col = joined["rider_id"].to_numpy(), joined["stage_id"].to_numpy()
    for name, with_gradient in SIM_KERNELS:
        k = k_ps * k_grad if with_gradient else k_ps
        ok = same_kind & (dd <= SIM_WINDOW_DAYS) & ~np.isnan(k)
        w = pd.DataFrame({
            "rider_id": rider_col[ok], "stage_id": stage_col[ok],
            "_n": k[ok], "_top10": (k * top10)[ok], "_pts": (k * pts)[ok],
            "_pct": (k * pct)[ok], "_pct_w": np.where(np.isnan(pct), np.nan, k)[ok],
        }).groupby(KEY, sort=False).sum()
        frames.append(pd.DataFrame({
            f"{name}_n": w["_n"],
            f"{name}_top10": (w["_top10"] / w["_n"]).where(w["_n"] > 0),
            f"{name}_pts": w["_pts"],
            f"{name}_pct": (w["_pct"] / w["_pct_w"]).where(w["_pct_w"] > 0),
        }))
    del joined
    return pd.concat(frames, axis=1)


def history_features(targets: pd.DataFrame, hist: pd.DataFrame,
                     max_pairs: int | None = None) -> pd.DataFrame:
    """``_history_features`` computed rider group by rider group to bound memory
    (``max_pairs`` defaults to MAX_PAIRS_PER_CHUNK)."""
    parts = []
    for riders in _rider_chunks(targets, hist, max_pairs or MAX_PAIRS_PER_CHUNK):
        parts.append(_history_features(targets[targets["rider_id"].isin(riders)],
                                       hist[hist["rider_id"].isin(riders)]))
    return pd.concat(parts) if len(parts) > 1 else parts[0]


def build_features(
    cutoff_date: str | None = None,
    race_slug: str | None = None,
    gender: str = "men",
    stage_clf="disk",
    entries: pd.DataFrame | None = None,
    hide_route: bool = False,
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
    stage_clf : see ``attach_stage_outcome``.
    entries : start-list rows for stages that have no result yet, with columns
        stage_id, rider_id and optionally bib, team_slug.  They become target
        rows (``top10`` NaN) built by the same code as historic stages, and
        never count as history.
    hide_route : build the target rows as if PCS had not published their
        routes yet (no profile score, final-km gradient or elevation; the
        profile icon and the distance stay).  Earlier stages keep theirs.
        For measuring what an early prediction is worth.
    """
    log.info("loading data from DB …")
    with get_conn() as conn:
        results = pd.read_sql(
            "SELECT id, stage_id, rider_id, position, status, time_seconds, bib, points_pcs, team_slug FROM results",
            conn,
        )
        stages = pd.read_sql(
            "SELECT id, race_id, stage_num, date, distance_km, elevation_m, profile_type, surface, gradient_final_km, profile_score FROM stages",
            conn,
        )
        races = pd.read_sql(
            "SELECT id, pcs_slug, name, year, is_stage_race, gender, class FROM races",
            conn,
        )
        riders = pd.read_sql(
            "SELECT id, name, pcs_rank, speciality, weight_kg, height_cm, dob FROM riders",
            conn,
        )

    if entries is not None and len(entries):
        extra = entries.copy()
        for col in ("bib", "team_slug"):
            if col not in extra.columns:
                extra[col] = np.nan if col == "bib" else None
        extra = extra[~extra["stage_id"].isin(results["stage_id"].unique())]
        extra = extra.assign(id=-1, position=np.nan, status=ENTRY_STATUS, time_seconds=np.nan, points_pcs=np.nan)
        results = pd.concat([results, extra[results.columns]], ignore_index=True)

    # ── filter by gender ──────────────────────────────────────────────────────
    races = races[races["gender"].fillna("men") == gender]

    # ── basic prep ────────────────────────────────────────────────────────────
    races["tier"] = races["class"].map(race_tier)
    stages = stages.merge(
        races[["id", "name", "pcs_slug", "is_stage_race", "tier"]].rename(
            columns={"id": "race_id", "name": "race_name", "pcs_slug": "race_slug"}
        ),
        on="race_id",
    )
    stages["date"] = pd.to_datetime(stages["date"], errors="coerce")
    stages = stages.dropna(subset=["date"])

    # A stage stored as 'mountain' with no profile score and no distance never
    # had its page read: the label is a stage-list parsing default (Tour of
    # Belgium sprint stages, Chrono des Nations), and 58% of them end in a
    # bunch sprint.  Its profile is unknown until the stage page is scraped.
    untrusted = (
        (stages["profile_type"] == "mountain")
        & stages["profile_score"].isna()
        & (stages["distance_km"].fillna(0) == 0)
    )
    stages.loc[untrusted, "profile_type"] = None

    # stage_num_norm: stage number / total stages in race
    stage_counts = stages.groupby("race_id")["id"].count().rename("race_stage_count")
    stages = stages.join(stage_counts, on="race_id")
    stages["stage_num_norm"] = stages["stage_num"].fillna(1) / stages["race_stage_count"]

    # prev_stage profile: sort by (race_id, date), shift within each race
    stages_sorted = stages.sort_values(["race_id", "date"])
    prev = stages_sorted.groupby("race_id")["profile_type"].shift(1)
    stages["prev_profile_type"] = prev.values

    # How each stage was decided, and how sprint-prone its race has been.
    facts = stage_finish_facts(results)
    stages = stages.join(facts, on="id")
    stages = stages.join(race_sprint_history(stages), on="id")
    stages["race_base_code"] = pd.factorize(race_base(stages["race_slug"]))[0]
    stages["ps"] = stages["profile_score"]        # carried onto every result row
    stages["race_year"] = stages["date"].dt.year

    results["is_entry"]    = results["status"] == ENTRY_STATUS
    results["is_finished"] = results["status"] == "finished"
    results["is_dnf"]      = results["status"].isin(["dnf", "dns", "otl", "dsq"])
    results["is_top10"]    = (results["position"] <= 10) & results["position"].notna()
    results["is_top3"]     = (results["position"] <= 3) & results["position"].notna()
    results["is_win"]      = results["position"] == 1

    # GC standing before each stage (stage races only; NaN elsewhere)
    gc = _gc_state(results, stages)
    results = results.merge(gc, on=["stage_id", "rider_id"], how="left")

    # Elo-style ratings as they stood before each stage
    results = results.merge(compute_ratings(results, stages), on=["stage_id", "rider_id"], how="left")

    # Join stage date onto results (needed for rolling)
    results = results.merge(
        stages[["id", "date", "profile_type", "surface", "gradient_final_km", "is_stage_race",
                "n_fin", "finish_type", "ps", "tier", "race_id", "race_base_code", "race_year",
                "distance_km"]].rename(columns={"id": "stage_id", "distance_km": "hist_distance"}),
        on="stage_id",
    )

    # Field-relative finishing level (1 = winner, 0 = last finisher), PCS
    # points (blank = none scored) and the log time gap to the winner.
    finished_pos = results["is_finished"] & results["position"].notna()
    results["pct"] = (1 - (results["position"] - 1) / (results["n_fin"] - 1).clip(lower=1)).where(finished_pos).clip(0, 1)
    results["pts"] = results["points_pcs"].fillna(0.0).where(~results["is_entry"], 0.0)
    results["lgap"] = np.log1p(results["time_seconds"].clip(0, 3600)).where(finished_pos)
    results["tier_w"] = results["tier"].map(TIER_WEIGHT)

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
                        "profile_score", "race_bunch_rate_prev", "race_prev_stages"]].rename(
            columns={"id": "stage_id", "date": "stage_date", "profile_type": "stage_profile"}
        ),
        on="stage_id",
    )
    if base.empty:
        log.warning("no result or start-list rows for the target stages")
        return pd.DataFrame()
    base["top10"] = base["is_top10"].astype(float).where(~base["is_entry"])
    if hide_route:
        for col in ("ps", "profile_score", "gradient_final_km", "elevation_m"):
            base[col] = np.nan
    # A route PCS has not published (or that could not be read) is replaced by
    # the typical stage of its profile icon; route_known says which rows that is.
    # The stage classifier is scored on these filled rows (attach_stage_outcome)
    # but fitted on the stages as stored (model.stage_classifier.label_stages,
    # unknown = NaN): a stand-in stage is meant to be read as a typical one,
    # not as one of the few stored stages without a route.
    base["route_known"] = base["profile_score"].notna()
    for fact in ("profile_score", "gradient_final_km"):
        typical = base["stage_profile"].map({k: v[fact] for k, v in ROUTE_DEFAULTS.items()})
        base[fact] = base[fact].fillna(typical)
    typical_climb = base["stage_profile"].map({k: v["elevation_per_km"] for k, v in ROUTE_DEFAULTS.items()})
    no_elevation = base["elevation_m"].isna()
    base["elevation_m"] = base["elevation_m"].fillna(typical_climb * base["distance_km"].where(base["distance_km"] > 0))
    # Climbing per km.  Where the elevation was unknown and there is no distance
    # either, the typical value stands in directly.
    climb = base["elevation_m"] / base["distance_km"].replace(0, np.nan)
    base["elevation_per_km"] = climb.where(~no_elevation, climb.fillna(typical_climb))

    # Attach team GC stats via bib-based team_id (already on base from results)
    base["team_id"] = (base["bib"] // 10).where(base["bib"].notna())
    base = base.merge(_team_gc, on=["stage_id", "team_id"], how="left")

    # ── rolling features over each rider's earlier results ────────────────────
    log.info("computing rolling features …")

    hist = results.loc[~results["is_entry"]].rename(columns={
        "date": "hist_date", "stage_id": "hist_stage_id",
        "profile_type": "hist_profile", "surface": "hist_surface",
        "gc_rank_before": "hist_gc_rank",
        "gradient_final_km": "hist_gradient",
        "is_stage_race": "hist_is_stage_race",
        "finish_type": "hist_finish", "race_id": "hist_race_id",
        "race_base_code": "hist_race_base", "race_year": "hist_race_year",
        "time_seconds": "hist_time", "ps": "hist_ps",
    })[["rider_id", "hist_date", "hist_stage_id", "position", "is_finished", "is_dnf", "is_top10",
        "is_top3", "is_win", "hist_profile", "hist_surface", "hist_gc_rank", "hist_gradient",
        "hist_is_stage_race", "pct", "pts", "lgap", "tier_w", "hist_finish", "hist_race_id",
        "hist_race_base", "hist_race_year", "hist_time", "hist_distance", "hist_ps"]].copy()
    typical_ps = {label: facts["profile_score"] for label, facts in ROUTE_DEFAULTS.items()}
    hist["hist_ps"] = hist["hist_ps"].fillna(hist["hist_profile"].map(typical_ps))
    hist["hist_profile"] = hist["hist_profile"].astype(_PROFILE_DTYPE)
    hist["hist_surface"] = hist["hist_surface"].fillna("road").astype(_SURFACE_DTYPE)
    hist["hist_finish"] = hist["hist_finish"].astype(_FINISH_DTYPE)
    hist["hist_is_stage_race"] = hist["hist_is_stage_race"].fillna(0).astype(bool)
    for col in ("position", "hist_gc_rank", "hist_gradient", "pct", "pts", "lgap", "tier_w",
                "hist_time", "hist_distance", "hist_ps"):
        hist[col] = hist[col].astype(np.float32)
    for col in ("hist_stage_id", "hist_race_id", "hist_race_base", "hist_race_year"):
        hist[col] = hist[col].astype(np.int32)

    targets = base[KEY + ["stage_date", "stage_profile", "surface", "race_id", "race_base_code",
                          "race_year", "ps", "gradient_final_km"]].drop_duplicates(KEY).rename(columns={
        "stage_profile": "target_profile", "surface": "target_surface", "race_id": "target_race_id",
        "race_base_code": "target_race_base", "race_year": "target_race_year",
        "ps": "target_ps", "gradient_final_km": "target_grad"})
    targets["target_ps"] = targets["target_ps"].fillna(targets["target_profile"].map(typical_ps))
    for col in ("target_ps", "target_grad"):
        targets[col] = targets[col].astype(np.float32)
    targets["target_profile"] = targets["target_profile"].astype(_PROFILE_DTYPE)
    targets["target_surface"] = targets["target_surface"].fillna("road").astype(_SURFACE_DTYPE)
    for col in ("target_race_id", "target_race_base", "target_race_year"):
        targets[col] = targets[col].astype(np.int32)

    base = base.join(history_features(targets, hist), on=KEY)
    del hist, targets
    base["days_since_last_race"] = (base["stage_date"] - base["_last_race"]) / np.timedelta64(1, "D")
    base = base.drop(columns=["_last_race"])
    base["break_top10s_365d"] = base["break_top10s_365d"].fillna(0)

    for col in ("pts_90d", "pts_365d", "pts_730d", "pts_ewm", "rel_pts_365d", "rel_pts_730d",
                "bunch_pts_730d", "select_pts_730d", "uphill_pts_730d", "itt_pts_730d",
                "race_hist_pts", "cur_pts", "wins_365d", "n_results_365d", "n_results_all",
                "rel_n_730d", "bunch_n_730d", "select_n_730d", "race_hist_n", "race_hist_top10s",
                "cur_n", "cur_top10s"):
        base[col] = base[col].fillna(0.0)

    # ── team role ─────────────────────────────────────────────────────────────
    # Team = bib decade (the start list's team where no bib is assigned yet).
    team_key = base["team_id"].astype("object").where(base["team_id"].notna(), base["team_slug"])
    base["_team"] = team_key.astype(str).where(team_key.notna())
    tg = base.groupby(["stage_id", "_team"])
    base["team_size"] = tg["rider_id"].transform("size")
    base["team_rank_pts"] = tg["pts_365d"].rank(ascending=False, method="min")
    base["team_rank_rel"] = tg["rel_pts_730d"].rank(ascending=False, method="min")
    base["team_best_pts"] = tg["pts_365d"].transform("max")
    base["team_pts_sum"] = tg["pts_365d"].transform("sum")
    base["team_pts_share"] = (base["pts_365d"] / base["team_pts_sum"]).where(base["team_pts_sum"] > 0)
    base["bib_is_leader"] = (base["bib"] % 10 == 1).astype(float).where(base["bib"].notna())
    base["team_rank_elo"] = tg["elo_all"].rank(ascending=False, method="min")
    base = base.rename(columns={"_team": "team_key"})

    for col in ("elo_all", "elo_itt", "elo_mountain", "elo_cobbled"):
        base[f"{col}_frank"] = _field_rank(base, col)
    base["elo_all_gap"] = _field_gap(base, "elo_all")
    for col in ("sim_pct", "sim_pts", "simg_pct"):
        base[f"{col}_frank"] = _field_rank(base, col)

    # ── standing in today's field ─────────────────────────────────────────────
    for col in FIELD_RANK_SOURCES:
        base[f"{col}_frank"] = _field_rank(base, col)
    base["field_size"] = base.groupby("stage_id")["rider_id"].transform("size")
    top_pts = (base.sort_values("pts_365d", ascending=False).groupby("stage_id").head(10)
               .groupby("stage_id")["pts_365d"].mean().rename("field_pts_top10"))
    base = base.join(top_pts, on="stage_id")
    base["tier_code"] = base["tier"].map(TIER_CODE)

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

    base = attach_stage_outcome(base, stage_clf)

    log.info("features built: %d rows, %d columns", len(base), len(base.columns))
    return base
