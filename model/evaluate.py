"""
Walk-forward evaluation: how accurate would the predictor have been on past races?

Scoring the model on disk on a past race says little: it has usually been
trained on that very race.  Here every test window is predicted by models (and
a stage classifier) fitted only on stages that finished before the window
opened, so the numbers are what a user would have seen on the day.  The saved
predictions also serve ``main.py backtest`` and the UI's backtest tab.

Features are point-in-time by construction (``features.builder`` only looks at
results dated before each stage), so they are built once and re-used by every
fold; only the models are refitted.

Usage
─────
  python main.py evaluate                       # men, 2025-01-01 → last result
  python main.py evaluate --gender women --folds 2025-01-01,2025-07-01,2026-01-01,2026-07-01
"""
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from features.builder import race_tier

log = logging.getLogger(__name__)

# Start of each test window; a window ends where the next one starts.  Each
# model is trained on everything before its window.
DEFAULT_FOLDS = (
    "2025-01-01", "2025-04-01", "2025-07-01",
    "2026-01-01", "2026-04-01", "2026-07-01",
)

# Stage columns copied onto every prediction row for the breakdowns.
META_COLS = ["stage_id", "rider_id", "rider_name", "stage_date", "race_slug", "race_name",
             "profile_type", "is_stage_race", "position", "status", "top10"]


# ── per-stage metrics ─────────────────────────────────────────────────────────

def stage_scores(pred: pd.DataFrame, score_col: str = "score") -> pd.DataFrame:
    """One row of ranking metrics per stage.

    ``pred`` needs ``stage_id``, ``position`` (NaN for non-finishers) and the
    score column (higher = more likely to finish at the front).  Stages
    without a top-10 finisher (no result stored) are dropped.

    hits10        how many of the 10 highest-scored riders finished top 10
    p_at_10       hits10 / min(10, riders who finished top 10)
    hits3         how many of the 3 highest-scored riders finished on the podium
    winner_rank   where the model ranked the actual winner (1 = called it)
    auc           P(a top-10 finisher is scored above a rider outside the top 10)
    ndcg10        position-weighted quality of the predicted top 10 (1 = exact order)
    """
    d = pred[["stage_id", "position", score_col]].copy()
    d["score"] = d[score_col].astype(float)
    d["actual10"] = (d["position"] <= 10).astype(int)
    d["actual3"] = (d["position"] <= 3).astype(int)
    g = d.groupby("stage_id", sort=False)

    # Ties are broken by row order, not by the true result.
    d["rk"] = g["score"].rank(method="first", ascending=False)
    d["rk_avg"] = g["score"].rank(method="average", ascending=True)

    d["hit10"] = d["actual10"] * (d["rk"] <= 10)
    d["hit3"] = d["actual3"] * (d["rk"] <= 3)
    d["win_rk"] = d["rk"].where(d["position"] == 1)

    gain = (11 - d["position"]).clip(lower=0).fillna(0.0)
    d["dcg"] = (gain / np.log2(1 + d["rk"])).where(d["rk"] <= 10, 0.0)
    d["idcg"] = (gain / np.log2(1 + d["position"])).where(d["position"] <= 10, 0.0)
    d["pos_rank_sum"] = d["rk_avg"] * d["actual10"]

    out = d.groupby("stage_id", sort=False).agg(
        n_riders=("score", "size"),
        n_top10=("actual10", "sum"),
        hits10=("hit10", "sum"),
        hits3=("hit3", "sum"),
        winner_rank=("win_rk", "min"),
        dcg=("dcg", "sum"),
        idcg=("idcg", "sum"),
        pos_rank_sum=("pos_rank_sum", "sum"),
    )
    out = out[out["n_top10"] > 0].copy()
    out["p_at_10"] = out["hits10"] / out["n_top10"].clip(upper=10)
    n_pos, n_neg = out["n_top10"], out["n_riders"] - out["n_top10"]
    out["auc"] = ((out["pos_rank_sum"] - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)).where(n_neg > 0)
    out["ndcg10"] = (out["dcg"] / out["idcg"]).where(out["idcg"] > 0)
    out["win_top1"] = (out["winner_rank"] == 1).astype(float).where(out["winner_rank"].notna())
    out["win_top3"] = (out["winner_rank"] <= 3).astype(float).where(out["winner_rank"].notna())
    out["win_top10"] = (out["winner_rank"] <= 10).astype(float).where(out["winner_rank"].notna())
    return out.drop(columns=["dcg", "idcg", "pos_rank_sum"])


SUMMARY_COLS = ["p_at_10", "hits3", "win_top1", "win_top3", "win_top10", "winner_rank_median", "auc", "ndcg10"]


def summarise(stages: pd.DataFrame, by: str | list[str] | None = None) -> pd.DataFrame:
    """Average the per-stage metrics, overall or per group (stage-weighted)."""
    def _agg(s: pd.DataFrame) -> pd.Series:
        return pd.Series({
            "stages": len(s),
            "p_at_10": s["p_at_10"].mean(),
            "hits3": s["hits3"].mean(),
            "win_top1": s["win_top1"].mean(),
            "win_top3": s["win_top3"].mean(),
            "win_top10": s["win_top10"].mean(),
            "winner_rank_median": s["winner_rank"].median(),
            "auc": s["auc"].mean(),
            "ndcg10": s["ndcg10"].mean(),
        })

    if by is None:
        return _agg(stages).to_frame("all").T
    return stages.groupby(by).apply(_agg, include_groups=False)


def probability_metrics(pred: pd.DataFrame, prob_col: str = "prob", bins: int = 10) -> dict:
    """Brier score, log loss, and a reliability table for top-10 probabilities."""
    p = pred[prob_col].astype(float).clip(1e-6, 1 - 1e-6)
    y = (pred["position"] <= 10).astype(float)
    edges = np.linspace(0, 1, bins + 1)
    bucket = pd.cut(p, edges, include_lowest=True)
    table = pd.DataFrame({"p": p, "y": y}).groupby(bucket, observed=True).agg(
        n=("y", "size"), predicted=("p", "mean"), actual=("y", "mean"))
    ece = float((table["n"] * (table["predicted"] - table["actual"]).abs()).sum() / table["n"].sum())
    per_stage_sum = pred.assign(_p=p).groupby("stage_id")["_p"].sum()
    return {
        "brier": float(((p - y) ** 2).mean()),
        "log_loss": float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean()),
        "ece": ece,
        "mean_prob_sum_per_stage": float(per_stage_sum.mean()),
        "reliability": [
            {"bin": f"{max(idx.left, 0):.1f}–{idx.right:.1f}", "n": int(r.n),
             "predicted": round(float(r.predicted), 4), "actual": round(float(r.actual), 4)}
            for idx, r in table.iterrows()
        ],
    }


def paired_difference(a: pd.DataFrame, b: pd.DataFrame, col: str = "p_at_10",
                      n_boot: int = 2000, seed: int = 0) -> dict:
    """Mean of (b − a) over the stages both score, with a bootstrap 95% interval."""
    both = a[[col]].join(b[[col]], lsuffix="_a", rsuffix="_b", how="inner").dropna()
    diff = (both[f"{col}_b"] - both[f"{col}_a"]).to_numpy()
    rng = np.random.default_rng(seed)
    boots = rng.choice(diff, size=(n_boot, len(diff)), replace=True).mean(axis=1)
    return {"stages": int(len(diff)), "mean_diff": float(diff.mean()),
            "ci95": (float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5)))}


# ── walk-forward ──────────────────────────────────────────────────────────────

def _race_classes() -> pd.DataFrame:
    from db.database import get_conn
    with get_conn() as conn:
        return pd.read_sql("SELECT pcs_slug AS race_slug, class AS race_class FROM races", conn)


def walk_forward(
    df: pd.DataFrame,
    feature_cols: list[str],
    fitter,
    folds=DEFAULT_FOLDS,
    end: str | None = None,
    stage_clf: bool = True,
    extra_cols: tuple[str, ...] = (),
    test_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Predict every stage on or after ``folds[0]`` with a model that only saw
    earlier stages.

    ``fitter(train_df, test_df, feature_cols)`` returns one score per test row
    (stored as ``score``) or a dict of named score columns.  With
    ``stage_clf`` the stage outcome classifier is refitted per fold on stages
    before the window and its p_sprint / p_breakaway / p_gc columns replaced.

    ``test_df``: take the test rows of every window from this frame instead —
    the same stages built differently (``build_features(hide_route=True)``);
    the models are still fitted on ``df``.

    Returns one row per (rider, stage) with the stage metadata, ``score`` and
    ``fold``.  Team time trials are not scored (their "position" is the team's).
    """
    from features.builder import attach_stage_outcome
    from model.stage_classifier import fit_bundle

    df = df.copy()
    df["stage_date"] = pd.to_datetime(df["stage_date"])
    if test_df is not None:
        test_df = test_df.copy()
        test_df["stage_date"] = pd.to_datetime(test_df["stage_date"])
    starts = [pd.Timestamp(f) for f in folds]
    stop_all = pd.Timestamp(end) if end else df["stage_date"].max() + pd.Timedelta(days=1)
    out = []
    for i, start in enumerate(starts):
        stop = starts[i + 1] if i + 1 < len(starts) else stop_all
        bundle = fit_bundle(start.strftime("%Y-%m-%d")) if stage_clf else None
        fold_df = attach_stage_outcome(df, bundle) if stage_clf else df
        if test_df is None:
            fold_test = fold_df
        else:
            fold_test = attach_stage_outcome(test_df, bundle) if stage_clf else test_df
        train = fold_df[fold_df["stage_date"] < start]
        test = fold_test[(fold_test["stage_date"] >= start) & (fold_test["stage_date"] < stop)]
        test = test[test["profile_type"] != "ttt"]
        if train.empty or test.empty:
            log.warning("fold %s: train %d rows, test %d rows — skipped", start.date(), len(train), len(test))
            continue
        log.info("fold %s → %s: train %d rows / %d stages, test %d rows / %d stages",
                 start.date(), stop.date(), len(train), train["stage_id"].nunique(),
                 len(test), test["stage_id"].nunique())
        scores = fitter(train, test, feature_cols)
        keep = [c for c in META_COLS if c in test.columns] + [c for c in extra_cols if c in test.columns]
        part = test[keep].copy()
        if isinstance(scores, dict):
            for name, values in scores.items():
                part[name] = np.asarray(values, dtype=float)
        else:
            part["score"] = np.asarray(scores, dtype=float)
        part["fold"] = start.strftime("%Y-%m-%d")
        out.append(part)
    pred = pd.concat(out, ignore_index=True)
    pred = pred.merge(_race_classes(), on="race_slug", how="left")
    pred["tier"] = pred["race_class"].map(race_tier)
    return pred


def stage_frame(pred: pd.DataFrame, score_col: str = "score", podium_col: str | None = None,
                win_col: str | None = None) -> pd.DataFrame:
    """Per-stage metrics joined with the stage's metadata for breakdowns.

    The top-10 metrics rank riders by ``score_col``.  When the model also
    gives P(podium) / P(win), the podium hits are counted on the riders ranked
    by ``podium_col`` and the winner's rank is read from ``win_col``: each
    question is answered by the probability trained for it.
    """
    meta_cols = [c for c in ["stage_id", "stage_date", "race_slug", "race_name", "profile_type",
                             "is_stage_race", "race_class", "tier", "fold"] if c in pred.columns]
    meta = pred.drop_duplicates("stage_id")[meta_cols].set_index("stage_id")
    out = stage_scores(pred, score_col)
    if podium_col and podium_col in pred.columns:
        out["hits3"] = stage_scores(pred, podium_col)["hits3"]
    if win_col and win_col in pred.columns:
        win = stage_scores(pred, win_col)
        for col in ("winner_rank", "win_top1", "win_top3", "win_top10"):
            out[col] = win[col]
    out = out.join(meta)
    out["profile_type"] = out["profile_type"].fillna("unknown")
    out["year"] = pd.to_datetime(out["stage_date"]).dt.year
    out["race_type"] = np.where(out["is_stage_race"].fillna(0).astype(int) == 1, "stage race", "one-day")
    return out


def report(pred: pd.DataFrame, score_col: str = "score", prob: bool = True,
           podium_col: str | None = None, win_col: str | None = None) -> dict:
    """Summary tables (overall and by terrain, tier, race type, season) as a
    JSON-ready dict, plus calibration when the score is a probability."""
    st = stage_frame(pred, score_col, podium_col, win_col)

    def _table(by=None):
        t = summarise(st, by).round(4)
        return json.loads(t.reset_index(names="group").to_json(orient="records"))

    out = {
        "stages": int(len(st)),
        "rows": int(len(pred)),
        "first_stage": str(pd.to_datetime(st["stage_date"]).min().date()),
        "last_stage": str(pd.to_datetime(st["stage_date"]).max().date()),
        "random_p_at_10": round(float((st["n_top10"].clip(upper=10) / st["n_riders"]).mean()), 4),
        "overall": _table()[0],
        "by_profile": _table("profile_type"),
        "by_tier": _table("tier"),
        "by_race_type": _table("race_type"),
        "by_year": _table("year"),
        "hits10_distribution": {int(k): int(v) for k, v in st["hits10"].value_counts().sort_index().items()},
    }
    if prob:
        out["calibration"] = probability_metrics(pred, score_col)
    return out


def format_report(rep: dict, title: str = "Walk-forward evaluation") -> str:
    """Plain-text rendering of ``report()`` for the CLI."""
    cols = [("stages", "stages", "{:.0f}"), ("p_at_10", "p@10", "{:.3f}"), ("hits3", "podium@3", "{:.2f}"),
            ("win_top1", "win#1", "{:.3f}"), ("win_top3", "win≤3", "{:.3f}"), ("win_top10", "win≤10", "{:.3f}"),
            ("winner_rank_median", "win rank", "{:.0f}"), ("auc", "AUC", "{:.3f}"), ("ndcg10", "nDCG@10", "{:.3f}")]

    def _rows(title_, rows):
        lines = [f"\n  {title_:<14}" + "".join(f"{h:>10}" for _, h, _ in cols), "  " + "-" * (14 + 10 * len(cols))]
        for r in rows:
            cells = "".join(f"{(fmt.format(r[k]) if r.get(k) is not None else '—'):>10}" for k, _, fmt in cols)
            lines.append(f"  {str(r['group']):<14}{cells}")
        return lines

    lines = [f"{'=' * 106}", f"  {title}",
             f"  {rep['stages']} stages, {rep['rows']:,} rider-stage rows, {rep['first_stage']} → {rep['last_stage']}"
             f"   (random p@10 = {rep['random_p_at_10']:.3f})", f"{'=' * 106}"]
    lines += _rows("overall", [rep["overall"]])
    lines += _rows("terrain", rep["by_profile"])
    lines += _rows("tier", rep["by_tier"])
    lines += _rows("race type", rep["by_race_type"])
    lines += _rows("season", rep["by_year"])
    if rep.get("baselines"):
        lines += _rows("simple ranking", [{**b, "group": b.get("short", b["group"])} for b in rep["baselines"]])
        lines += ["  (" + "; ".join(f"{b.get('short', '')}: {b['group']}" for b in rep["baselines"]) + ")"]
    cal = rep.get("calibration")
    if cal:
        lines += ["", f"  probabilities: Brier {cal['brier']:.4f}  log loss {cal['log_loss']:.4f}  "
                      f"calibration error {cal['ece']:.4f}  sum of P(top10) per stage {cal['mean_prob_sum_per_stage']:.1f}",
                  f"  {'P(top10) bin':<16}{'riders':>9}{'predicted':>11}{'actual':>9}"]
        for b in cal["reliability"]:
            lines.append(f"  {b['bin']:<16}{b['n']:>9}{b['predicted']:>11.3f}{b['actual']:>9.3f}")
    return "\n".join(lines) + "\n"


EVAL_DIR = Path("model")


def evaluation_path(gender: str = "men") -> Path:
    return EVAL_DIR / ("evaluation.json" if gender == "men" else f"evaluation_{gender}.json")


def walkforward_path(gender: str = "men") -> Path:
    return EVAL_DIR / ("walkforward.csv.gz" if gender == "men" else f"walkforward_{gender}.csv.gz")


# Simple rankings the model has to beat, as (short label, label, feature column).
BASELINES = [
    ("form 90d", "Top-10 rate, last 90 days", "top10_rate_90d"),
    ("points 365d", "PCS points, last 365 days", "pts_365d"),
    ("Elo", "Elo rating (all stages)", "elo_all"),
]


def load_walkforward(gender: str = "men") -> pd.DataFrame | None:
    """Out-of-sample predictions saved by ``evaluate`` (stage_id, rider_id,
    fold and one probability column per target, as the models gave them:
    apply ``model.predict.coherent_probabilities`` per stage), or None."""
    path = walkforward_path(gender)
    if not path.exists():
        return None
    return pd.read_csv(path)


def evaluate(gender: str = "men", folds=DEFAULT_FOLDS, targets=("top10", "top3", "win"),
             save: bool = True, hide_route: bool = False) -> dict:
    """Walk-forward evaluation of the production pipeline.

    Builds the features once, refits the stage classifier and one calibrated
    model per target for every fold, and scores each test stage with models
    that never saw it.  The summary describes the probabilities as
    ``predict_race`` hands them out (made coherent per stage).  With ``save``
    it goes to ``model/evaluation[_women].json`` and the models' raw per-rider
    outputs to ``model/walkforward[_women].csv.gz`` (the UI's backtest reads
    them).

    ``hide_route`` scores the test stages as if PCS had not published their
    routes yet (no profile score, final-km gradient or elevation: what an
    early prediction has to work with).  Nothing is saved in that mode.
    """
    import datetime

    from features.builder import build_features, FEATURE_COLS
    from model.predict import coherent_probabilities
    from model.train import HGB_PARAMS, TARGETS, fit_model, training_rows

    targets = [t for t in TARGETS if t in targets]
    if "top10" not in targets:
        raise ValueError("the top10 target is required")
    df = build_features(gender=gender, stage_clf=None)
    if df.empty:
        raise RuntimeError("no features built — run the scraper first")
    test_df = build_features(gender=gender, stage_clf=None, hide_route=True) if hide_route else None
    save = save and not hide_route

    def fitter(train, test, cols):
        out = {}
        for t in targets:
            rows, y = training_rows(train, t)
            model = fit_model(rows[cols], y, rows["stage_id"].values)
            out[f"{t}_prob"] = model.predict_proba(test[cols])[:, 1]
        return out

    raw = walk_forward(df, FEATURE_COLS, fitter, folds, extra_cols=tuple(c for _, _, c in BASELINES),
                       test_df=test_df)
    pred = coherent_probabilities(raw)         # what predict_race hands out
    rep = report(pred, "top10_prob",
                 podium_col="top3_prob" if "top3" in targets else None,
                 win_col="win_prob" if "win" in targets else None)
    rep["baselines"] = []
    for short, label, col in BASELINES:
        ranked = pred.assign(_s=pred[col].fillna(-np.inf))
        row = summarise(stage_frame(ranked, "_s")).round(4).iloc[0].to_dict()
        rep["baselines"].append({"group": label, "short": short, **row})
    rep["config"] = {
        "gender": gender,
        "folds": list(folds),
        "targets": targets,
        "n_features": len(FEATURE_COLS),
        "hide_route": hide_route,
        "model": {"type": "HistGradientBoostingClassifier × 5, isotonic-calibrated", **HGB_PARAMS},
        "generated": datetime.date.today().isoformat(),
    }
    if save:
        EVAL_DIR.mkdir(exist_ok=True)
        evaluation_path(gender).write_text(json.dumps(rep, indent=1))
        # The models' own outputs; readers apply coherent_probabilities.
        cols = ["stage_id", "rider_id", "fold"] + [f"{t}_prob" for t in targets]
        raw[cols].round(5).to_csv(walkforward_path(gender), index=False)
        log.info("saved %s and %s", evaluation_path(gender), walkforward_path(gender))
    return rep
