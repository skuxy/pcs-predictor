"""
Elo-style rider ratings, updated stage by stage.

Rolling averages treat 15th in a WorldTour sprint and 15th in a .1 race alike.
A rating moves by how a rider did against the riders who were actually there:
beating a strong field is worth more than beating a weak one.

Each stage is scored among its finishers with positions capped at
``POSITION_CAP`` (the peloton rolling in minutes down is not racing for 87th
place, so everyone beyond the cap ties).  A rider's score is the share of the
field he beat; his expected score follows from the rating differences.

Several ratings are kept, each updated only by the stages of its kind:

  all       every individual stage
  bunch     stages that ended in a bunch sprint
  select    stages decided by a small group or solo (time trials excluded)
  itt       individual time trials
  mountain  stages with a mountain profile
  cobbled   cobbled and gravel stages

The value attached to a (rider, stage) row is the rating *before* that stage's
day, so it only reflects earlier results.
"""
import numpy as np
import pandas as pd

RATING_KINDS = ["all", "bunch", "select", "itt", "mountain", "cobbled"]
RATING_COLS = [f"elo_{k}" for k in RATING_KINDS]

START = 1500.0
K_FACTOR = 60.0
POSITION_CAP = 25


def _kinds_for_stage(profile, surface, finish_type) -> list[str]:
    """Which ratings a stage with these properties updates."""
    if profile == "ttt":
        return []
    kinds = ["all"]
    if profile == "itt":
        kinds.append("itt")
    else:
        if finish_type == "bunch":
            kinds.append("bunch")
        elif finish_type in ("select", "mixed"):
            kinds.append("select")
    if profile == "mountain":
        kinds.append("mountain")
    if surface in ("cobbled", "gravel"):
        kinds.append("cobbled")
    return kinds


def _stage_update(ratings: np.ndarray, positions: np.ndarray, k: float, cap: int) -> np.ndarray:
    """Rating change for each finisher of one stage."""
    n = len(positions)
    if n < 2:
        return np.zeros(n)
    capped = np.minimum(positions, cap)
    worse = (capped[None, :] > capped[:, None]).sum(axis=1)
    tied = (capped[None, :] == capped[:, None]).sum(axis=1) - 1
    score = (worse + 0.5 * tied) / (n - 1)
    diff = ratings[None, :] - ratings[:, None]
    expected = (1.0 / (1.0 + np.power(10.0, diff / 400.0))).sum(axis=1) - 0.5
    expected /= n - 1
    return k * (score - expected)


def compute_ratings(results: pd.DataFrame, stages: pd.DataFrame, k: float = K_FACTOR,
                    cap: int = POSITION_CAP) -> pd.DataFrame:
    """Pre-stage ratings for every (stage, rider) row of ``results``.

    ``results`` needs stage_id, rider_id, position, status; ``stages`` id,
    date, profile_type, surface, finish_type.  Rows without a finishing
    position (DNF, start-list rows of stages still to come) get a rating but
    never change one.  Returns stage_id, rider_id and one ``elo_<kind>``
    column per kind; a rider without an earlier stage of that kind has NaN.
    """
    st = stages[["id", "date", "profile_type", "surface", "finish_type"]].copy()
    st["date"] = pd.to_datetime(st["date"], errors="coerce")
    st = st.dropna(subset=["date"]).set_index("id")
    res = results[["stage_id", "rider_id", "position", "status"]]
    res = res[res["stage_id"].isin(st.index)].copy()
    res["date"] = res["stage_id"].map(st["date"])

    rider_ids, rider_code = np.unique(res["rider_id"].to_numpy(), return_inverse=True)
    res["_r"] = rider_code
    n_riders = len(rider_ids)
    rating = {kind: np.full(n_riders, START) for kind in RATING_KINDS}
    seen = {kind: np.zeros(n_riders, dtype=bool) for kind in RATING_KINDS}

    out_stage, out_rider = [], []
    out_vals = {kind: [] for kind in RATING_KINDS}

    res = res.sort_values(["date", "stage_id"], kind="stable")
    for _, day in res.groupby("date", sort=True):
        # Record every row's rating before any of the day's stages is applied.
        idx = day["_r"].to_numpy()
        out_stage.append(day["stage_id"].to_numpy())
        out_rider.append(day["rider_id"].to_numpy())
        for kind in RATING_KINDS:
            out_vals[kind].append(np.where(seen[kind][idx], rating[kind][idx], np.nan))

        deltas = []
        for stage_id, grp in day.groupby("stage_id", sort=False):
            fin = grp[(grp["status"] == "finished") & grp["position"].notna()]
            if len(fin) < 2:
                continue
            meta = st.loc[stage_id]
            fidx = fin["_r"].to_numpy()
            pos = fin["position"].to_numpy(dtype=float)
            for kind in _kinds_for_stage(meta["profile_type"], meta["surface"], meta["finish_type"]):
                deltas.append((kind, fidx, _stage_update(rating[kind][fidx], pos, k, cap)))
        for kind, fidx, delta in deltas:
            np.add.at(rating[kind], fidx, delta)
            seen[kind][fidx] = True

    out = pd.DataFrame({"stage_id": np.concatenate(out_stage), "rider_id": np.concatenate(out_rider)})
    for kind in RATING_KINDS:
        out[f"elo_{kind}"] = np.concatenate(out_vals[kind])
    return out
