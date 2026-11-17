"""Generate top-10 probability predictions for a given race/stage."""
import json
import logging
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

from features.builder import build_features
from db.database import fill_stage_meta, get_conn
from model.train import model_paths
from scraper.races import fetch_stage_meta
from scraper.startlist import fetch_startlist
from scraper.utils import pages_newer_than

log = logging.getLogger(__name__)


def _load_model(path: Path):
    if path.exists():
        with open(path, "rb") as f:
            return pickle.load(f)
    return None


def startlist_entries(race_slug: str, cutoff_date: str) -> tuple[pd.DataFrame, dict]:
    """Start-list rows for the stages of a race that have no result yet.

    Returns ``(entries, names)``: one row per (rider, stage dated on or after
    ``cutoff_date`` without a stored result) with stage_id, rider_id, bib and
    team_slug, ready for ``build_features(entries=...)``; and rider_id → name
    for the riders on the list.  A rider the database has never seen gets a
    negative id (no history: his form features are empty).  In a race that is
    under way, riders who did not finish the last completed stage are left out.
    Both are empty when every stage has a result or PCS has no start list.
    """
    empty = pd.DataFrame(columns=["stage_id", "rider_id", "bib", "team_slug"])
    with get_conn() as conn:
        race = conn.execute("SELECT id FROM races WHERE pcs_slug = ?", (race_slug,)).fetchone()
        if race is None:
            log.warning("race not found in DB: %s", race_slug)
            return empty, {}
        open_stages = [r["id"] for r in conn.execute(
            """SELECT s.id FROM stages s
               WHERE s.race_id = ? AND s.date >= ?
                 AND NOT EXISTS (SELECT 1 FROM results x WHERE x.stage_id = s.id)
               ORDER BY s.date""",
            (race["id"], cutoff_date),
        ).fetchall()]
        if not open_stages:
            return empty, {}

        starters = fetch_startlist(race_slug)
        if not starters:
            log.warning("no start list for %s", race_slug)
            return empty, {}

        riders = pd.read_sql("SELECT id, pcs_slug, name FROM riders", conn)
        last_done = conn.execute(
            """SELECT s.id FROM stages s
               WHERE s.race_id = ? AND s.date < ?
                 AND EXISTS (SELECT 1 FROM results x WHERE x.stage_id = s.id)
               ORDER BY s.date DESC LIMIT 1""",
            (race["id"], cutoff_date),
        ).fetchone()
        still_racing = None
        if last_done is not None:
            still_racing = {r["rider_id"] for r in conn.execute(
                "SELECT rider_id FROM results WHERE stage_id = ? AND status = 'finished'", (last_done["id"],))}

    by_slug = dict(zip(riders["pcs_slug"], riders["id"]))
    by_name = dict(zip(riders["name"].str.lower(), riders["id"]))
    rows, names, unknown = [], {}, 0
    for st in starters:
        rider_id = by_slug.get(st["slug"])
        if rider_id is None:
            rider_id = by_name.get((st.get("name") or "").lower())
        if rider_id is None:
            unknown += 1
            rider_id = -unknown
        elif still_racing is not None and rider_id not in still_racing:
            continue
        names[rider_id] = st.get("name")
        rows.append({"rider_id": rider_id, "bib": st.get("bib"),
                     "team_slug": st.get("team_slug") or st.get("team")})
    if still_racing is not None:
        log.info("start list: %d of %d riders still in the race", len(rows), len(starters))
    if not rows:
        return empty, {}
    one = pd.DataFrame(rows)
    one["bib"] = pd.to_numeric(one["bib"], errors="coerce")
    entries = pd.concat([one.assign(stage_id=sid) for sid in open_stages], ignore_index=True)
    return entries[["stage_id", "rider_id", "bib", "team_slug"]], names


# What a stage page says about the route.  The calendar scrape stores the
# profile icon and (sometimes) the distance; the rest arrives with the result
# unless it is asked for before the race.
ROUTE_FACTS = ("distance_km", "elevation_m", "profile_type", "gradient_final_km", "profile_score",
               "departure", "arrival")


# Shown with a prediction whose stage has no profile score in the database
# (the sizes: `main.py evaluate --hide-route`, results/accuracy_2026-11.md).
ROUTE_UNKNOWN_NOTE = ("This stage's profile (profile score, final-km gradient, elevation) is not in the database: "
                      "it is predicted as a typical stage of its kind, at about 0.4 fewer top-10 hits (0.65 on "
                      "hilly stages). For a stage still to come, run again once PCS has published the route.")


def _route_from_pcs(row) -> dict:
    """What the page of an open stage says about its route, limited to what
    may be stored over the row ``row`` of the stages table."""
    slug = row["pcs_slug"]
    with pages_newer_than(row["date"]):
        facts = fetch_stage_meta(slug)
        one_day = not any(part in slug for part in ("/stage-", "/prologue"))
        if one_day and facts.get("profile_score") is None and facts.get("gradient_final_km") is None:
            for key, value in fetch_stage_meta(f"{slug}/result").items():
                if facts.get(key) is None:
                    facts[key] = value
    # 0 is a real profile score and a real gradient, but not a distance
    facts = {k: v for k in ROUTE_FACTS
             if (v := facts.get(k)) is not None and not (k == "distance_km" and v == 0)}
    # A time trial is known from the stage's name in the stage list ("(ITT)",
    # "(TTT)", "Prologue").  Before the stage its page has no "Won how" and
    # shows a road icon, which must not replace that label.
    if row["profile_type"] in ("itt", "ttt"):
        facts.pop("profile_type", None)
    # 'mountain' with no profile score and no distance is the stage-list
    # default, "profile unknown" to the feature builder for as long as the
    # distance is missing: a distance is not stored without the icon (or the
    # score) that tells what the stage is.
    unread = row["profile_type"] == "mountain" and row["profile_score"] is None and not row["distance_km"]
    if unread and "profile_type" not in facts and "profile_score" not in facts:
        facts.pop("distance_km", None)
    return facts


def refresh_open_stage_routes(stage_ids) -> int:
    """Ask PCS for the route of stages still to be ridden that lack part of it.

    The models lean on a stage's profile score, the gradient of its final km
    and its elevation; without them a stage is predicted as a typical stage
    of its profile icon (features.builder.ROUTE_DEFAULTS), about 0.4 top-10
    hits worse.  For each stage in ``stage_ids`` missing one of these, the
    stage page is fetched afresh (``/result`` too for a one-day race) and
    what it says is written to the stages table; a fact PCS does not show yet
    stays empty.  Returns the number of stages that gained a fact.  A page
    that cannot be fetched, or a database that cannot be written (another
    command holds it), is logged and skipped: the prediction goes ahead.
    """
    ids = [int(i) for i in stage_ids]
    if not ids:
        return 0
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT id, pcs_slug, date, distance_km, elevation_m, gradient_final_km, profile_score, profile_type
                FROM stages WHERE id IN ({','.join('?' * len(ids))})
                  AND (profile_score IS NULL OR gradient_final_km IS NULL OR elevation_m IS NULL
                       OR COALESCE(distance_km, 0) = 0)""", ids).fetchall()
    updated = 0
    for row in rows:
        slug = row["pcs_slug"]
        try:
            facts = _route_from_pcs(row)
            gained = [k for k in ("profile_score", "gradient_final_km", "elevation_m", "distance_km")
                      if k in facts and (row[k] is None or (k == "distance_km" and not row[k]))]
            if not gained:
                log.info("route of %s: PCS has nothing new", slug)
                continue
            with get_conn() as conn:
                fill_stage_meta(conn, row["id"], facts, overwrite=True)
        except Exception as exc:                     # neither the network nor a locked database may break a prediction
            log.warning("route of %s could not be refreshed: %s", slug, exc)
            continue
        updated += 1
        log.info("route of %s: stored %s", slug, ", ".join(f"{k}={facts[k]}" for k in gained))
    return updated


def predict_race(race_slug: str, cutoff_date: str, gender: str = "men") -> pd.DataFrame:
    """
    Predict top-10 probabilities for all starters in a race.

    Stages with a stored result are predicted from that result's riders (a
    backtest); stages still to be ridden from the PCS start list.  Both go
    through ``features.builder.build_features``, so an upcoming stage gets
    exactly the features the model was trained on.  The route of the open
    stages (profile score, final-km gradient, elevation) is fetched first
    where the database lacks it.  Team time trials are left out.
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

    entries, names = startlist_entries(race_slug, cutoff_date)
    if len(entries):
        # PCS answered for the start list: get the route of the open stages too
        refresh_open_stage_routes(entries["stage_id"].unique())
    log.info("building features for %s (gender=%s, %d start-list rows) …", race_slug, gender, len(entries))
    df = build_features(cutoff_date=cutoff_date, race_slug=race_slug, gender=gender,
                        entries=entries if len(entries) else None)
    # A team time trial is not an individual result (a rider's position there
    # is his team's): the models are neither trained nor scored on it.
    ttt = df["profile_type"] == "ttt" if not df.empty else None
    if ttt is not None and ttt.any():
        log.info("skipping %d team time trial stage(s)", df.loc[ttt, "stage_id"].nunique())
        df = df[~ttt]
    if df.empty:
        return pd.DataFrame()
    if names:
        df["rider_name"] = df["rider_name"].fillna(df["rider_id"].map(names))

    X = df[feature_cols]
    df["top10_prob"] = model.predict_proba(X)[:, 1]
    if win_model is not None:
        df["win_prob"] = win_model.predict_proba(X)[:, 1]
    if top3_model is not None:
        df["top3_prob"] = top3_model.predict_proba(X)[:, 1]
    df = coherent_probabilities(df)
    df["predicted_top10"] = (df["top10_prob"] >= 0.5).astype(int)

    return df.sort_values(["stage_date", "top10_prob"], ascending=[True, False])


PROB_COLS = {"win_prob": 1, "top3_prob": 3, "top10_prob": 10}


def _shift_to_total(p: np.ndarray, group: np.ndarray, total: np.ndarray) -> np.ndarray:
    """Add one constant per group to the log-odds of ``p`` so that each
    group's probabilities sum to its ``total`` (given per row)."""
    q = np.clip(np.asarray(p, dtype=float), 1e-6, 1 - 1e-6)
    z = np.log(q / (1 - q))
    codes, _ = pd.factorize(group)
    n_groups = codes.max() + 1 if len(codes) else 0
    target = np.zeros(n_groups)
    target[codes] = total
    lo, hi = np.full(n_groups, -30.0), np.full(n_groups, 30.0)
    for _ in range(60):                       # bisection: the sum grows with the shift
        mid = (lo + hi) / 2
        sums = np.bincount(codes, 1 / (1 + np.exp(-(z + mid[codes]))), minlength=n_groups)
        low = sums < target
        lo, hi = np.where(low, mid, lo), np.where(low, hi, mid)
    return 1 / (1 + np.exp(-(z + ((lo + hi) / 2)[codes])))


def coherent_probabilities(df: pd.DataFrame) -> pd.DataFrame:
    """Make the probabilities of a stage consistent with each other.

    Each model is calibrated on its own, so on a given stage P(win) need not
    add up to one winner.  Per stage, the log-odds of every rider are shifted
    by one constant so that P(win) sums to 1, P(podium) to 3 and P(top 10) to
    10 (fewer in a field smaller than that), and then win ≤ podium ≤ top 10 is
    enforced for every rider.  The shift keeps the order of the riders.

    On the 2026 walk-forward stages this beat both the raw probabilities and
    a proportional rescaling on Brier score and log loss (P(top 10): log loss
    0.18827 raw, 0.18816 rescaled, 0.18745 shifted); the rescaling also
    pushed favourites too high (riders given 0.8–0.9 finished top 10 77% of
    the time), which the shift does not.
    """
    df = df.copy()
    size = df.groupby("stage_id")["stage_id"].transform("size").to_numpy()
    stage = df["stage_id"].to_numpy()
    for col, places in PROB_COLS.items():
        if col in df.columns and len(df):
            df[col] = _shift_to_total(df[col].to_numpy(), stage, np.minimum(places, size))
    ordered = [c for c in PROB_COLS if c in df.columns]        # win, podium, top 10
    for narrow, wide in zip(ordered[:-1], ordered[1:]):
        df[wide] = np.maximum(df[wide], df[narrow])
    return df


def model_info(gender: str = "men") -> dict:
    """The metrics file written with the top-10 model ({} if none)."""
    _, _, metrics_path = model_paths(gender)
    return json.loads(metrics_path.read_text()) if metrics_path.exists() else {}


def walkforward_race(race_slug: str, gender: str = "men") -> pd.DataFrame:
    """Saved out-of-sample predictions for a past race (empty if the race is
    not covered by the last ``main.py evaluate`` run), joined with its results."""
    from model.evaluate import load_walkforward

    wf = load_walkforward(gender)
    if wf is None:
        return pd.DataFrame()
    with get_conn() as conn:
        rows = pd.read_sql(
            """SELECT s.id AS stage_id, s.date AS stage_date, s.profile_type, s.surface,
                      r.name AS race_name, r.pcs_slug AS race_slug,
                      x.rider_id, x.position, x.status, ri.name AS rider_name
               FROM races r
               JOIN stages s ON s.race_id = r.id
               JOIN results x ON x.stage_id = s.id
               JOIN riders ri ON ri.id = x.rider_id
               WHERE r.pcs_slug = ? AND r.gender = ?""",
            conn, params=(race_slug, gender),
        )
    df = rows.merge(wf, on=["stage_id", "rider_id"], how="inner")
    if df.empty:
        return df
    df["stage_date"] = pd.to_datetime(df["stage_date"])
    df["top10"] = (df["position"] <= 10).astype(float)
    df["is_cobbled"] = (df["surface"] == "cobbled").astype(int)
    df["is_gravel"] = (df["surface"] == "gravel").astype(int)
    df = coherent_probabilities(df)            # the file holds the models' raw outputs
    return df.sort_values(["stage_date", "top10_prob"], ascending=[True, False])


# Where a backtest's predictions come from, most trustworthy first.
SOURCE_WALK_FORWARD = "walk-forward"   # models refitted on earlier stages only
SOURCE_UNSEEN = "unseen"               # current model, race after its training cutoff
SOURCE_SEEN = "seen"                   # current model was trained on this race


def backtest_predictions(race_slug: str, gender: str = "men",
                         cutoff_date: str | None = None) -> tuple[pd.DataFrame, str]:
    """Predictions for a past race and how far they can be trusted.

    Uses the saved walk-forward predictions when the race is covered by them;
    otherwise the current model, flagged ``seen`` when the race lies before
    the model's training cutoff (the model has then learnt from the very
    result it is asked to predict, and the score flatters it).
    """
    df = walkforward_race(race_slug, gender)
    if not df.empty:
        return df, SOURCE_WALK_FORWARD

    with get_conn() as conn:
        race = conn.execute("SELECT start_date FROM races WHERE pcs_slug = ?", (race_slug,)).fetchone()
    start = race["start_date"] if race is not None else None
    df = predict_race(race_slug, cutoff_date or start or "1900-01-01", gender=gender)
    trained_to = model_info(gender).get("train_cutoff")
    seen = bool(start and trained_to and start < trained_to)
    return df, SOURCE_SEEN if seen else SOURCE_UNSEEN


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
        if "route_known" in group.columns and not group["route_known"].all():
            print(f"  {ROUTE_UNKNOWN_NOTE}")
        print(f"{'='*65}")
        print(f"  {'Rank':>4}  {'Rider':<30}  {'P(top10)':>8}  {'Actual':>8}")
        print(f"  {'-'*55}")
        for rank, (_, row) in enumerate(group.head(top_n).iterrows(), 1):
            actual = int(row["position"]) if pd.notna(row.get("position")) else "-"
            print(f"  {rank:>4}  {str(row['rider_name']):<30}  {row['top10_prob']:>8.3f}  {str(actual):>8}")
        print()
