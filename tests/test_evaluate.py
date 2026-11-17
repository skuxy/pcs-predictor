"""Walk-forward evaluation metrics (model/evaluate.py)."""
import numpy as np
import pandas as pd
import pytest

from model.evaluate import (
    paired_difference, probability_metrics, race_tier, stage_scores, summarise, walk_forward,
)


def _stage(stage_id, scores, positions):
    return pd.DataFrame({"stage_id": stage_id, "score": scores, "position": positions})


def test_stage_scores_perfect_ranking():
    n = 30
    pred = _stage(1, np.arange(n, 0, -1, dtype=float), np.arange(1, n + 1, dtype=float))
    s = stage_scores(pred).loc[1]
    assert s["hits10"] == 10 and s["p_at_10"] == 1.0
    assert s["hits3"] == 3
    assert s["winner_rank"] == 1 and s["win_top1"] == 1.0
    assert s["auc"] == pytest.approx(1.0)
    assert s["ndcg10"] == pytest.approx(1.0)


def test_stage_scores_reversed_ranking():
    n = 30
    pred = _stage(1, np.arange(1, n + 1, dtype=float), np.arange(1, n + 1, dtype=float))
    s = stage_scores(pred).loc[1]
    assert s["hits10"] == 0 and s["hits3"] == 0
    assert s["winner_rank"] == n
    assert s["auc"] == pytest.approx(0.0)
    assert s["win_top10"] == 0.0


def test_stage_scores_counts_hits_and_ignores_non_finishers():
    # model's top 10 = riders 0..9; riders 0-3 finished 1st-4th, 4-9 did not finish,
    # the rest of the top 10 came from riders 10-15
    positions = [1, 2, 3, 4] + [np.nan] * 6 + [5, 6, 7, 8, 9, 10] + list(range(11, 25))
    pred = _stage(7, np.arange(30, 0, -1, dtype=float), positions)
    s = stage_scores(pred).loc[7]
    assert s["hits10"] == 4 and s["p_at_10"] == pytest.approx(0.4)
    assert s["hits3"] == 3
    assert s["n_top10"] == 10


def test_stage_without_a_result_is_dropped_and_small_fields_are_normalised():
    no_result = _stage(1, [3.0, 2.0, 1.0], [np.nan, np.nan, np.nan])
    small = _stage(2, [5.0, 4.0, 3.0, 2.0, 1.0], [1, 2, 3, np.nan, np.nan])   # only 3 finishers
    s = stage_scores(pd.concat([no_result, small]))
    assert list(s.index) == [2]
    assert s.loc[2, "p_at_10"] == 1.0


def test_summarise_by_group():
    a = _stage(1, np.arange(20, 0, -1, dtype=float), np.arange(1, 21, dtype=float))
    b = _stage(2, np.arange(1, 21, dtype=float), np.arange(1, 21, dtype=float))
    st = stage_scores(pd.concat([a, b]))
    st["profile_type"] = ["flat", "hilly"]
    overall = summarise(st)
    assert overall.loc["all", "stages"] == 2
    assert overall.loc["all", "p_at_10"] == pytest.approx(0.5)
    by = summarise(st, "profile_type")
    assert by.loc["flat", "p_at_10"] == 1.0 and by.loc["hilly", "p_at_10"] == 0.0


def test_probability_metrics_on_a_calibrated_forecast():
    rng = np.random.default_rng(0)
    p = rng.uniform(0, 1, 20000)
    y = rng.uniform(0, 1, 20000) < p
    pred = pd.DataFrame({"stage_id": 1, "prob": p, "position": np.where(y, 1, 50)})
    m = probability_metrics(pred)
    assert m["ece"] < 0.02
    assert m["brier"] == pytest.approx(1 / 6, abs=0.01)
    assert len(m["reliability"]) == 10


def test_paired_difference():
    a = pd.DataFrame({"p_at_10": [0.4, 0.5, 0.6]}, index=[1, 2, 3])
    b = pd.DataFrame({"p_at_10": [0.5, 0.6, 0.7, 0.9]}, index=[1, 2, 3, 4])
    d = paired_difference(a, b)
    assert d["stages"] == 3
    assert d["mean_diff"] == pytest.approx(0.1)
    assert d["ci95"][0] == pytest.approx(0.1) and d["ci95"][1] == pytest.approx(0.1)


def test_race_tier():
    assert race_tier("1.UWT") == "WorldTour" and race_tier("2.Pro") == "ProSeries" and race_tier("1.1") == ".1"


def test_walk_forward_trains_only_on_earlier_stages(monkeypatch):
    import model.evaluate as ev
    monkeypatch.setattr(ev, "_race_classes", lambda: pd.DataFrame({"race_slug": ["race/a/2025"], "race_class": ["1.UWT"]}))
    dates = pd.to_datetime(["2024-06-01", "2024-12-01", "2025-02-01", "2025-05-01", "2025-08-01"])
    rows = []
    for sid, d in enumerate(dates, 1):
        for rider in range(12):
            rows.append({"stage_id": sid, "rider_id": rider, "rider_name": f"r{rider}", "stage_date": d,
                         "race_slug": "race/a/2025", "race_name": "A", "profile_type": "ttt" if sid == 4 else "flat",
                         "is_stage_race": 0, "position": rider + 1, "status": "finished",
                         "top10": float(rider < 10), "x": float(-rider)})
    df = pd.DataFrame(rows)
    seen = []

    def fitter(train, test, cols):
        assert train["stage_date"].max() < test["stage_date"].min()
        seen.append((train["stage_id"].nunique(), sorted(test["stage_id"].unique())))
        return test["x"].to_numpy()

    pred = walk_forward(df, ["x"], fitter, folds=("2025-01-01", "2025-07-01"), stage_clf=False)
    # the team time trial (stage 4) is not scored, but stays in the training data of the next fold
    assert seen == [(2, [3]), (4, [5])]
    assert sorted(pred["stage_id"].unique()) == [3, 5]
    assert set(pred["fold"]) == {"2025-01-01", "2025-07-01"}
    assert (pred["tier"] == "WorldTour").all()
    assert stage_scores(pred)["p_at_10"].eq(1.0).all()


def test_walk_forward_can_score_a_differently_built_test_frame(monkeypatch):
    """test_df: the models are fitted on df, the test rows come from the other
    frame (the same stages built as if their routes were not published)."""
    import model.evaluate as ev
    monkeypatch.setattr(ev, "_race_classes", lambda: pd.DataFrame({"race_slug": ["race/a/2025"], "race_class": ["1.UWT"]}))
    rows = []
    for sid, d in enumerate(pd.to_datetime(["2024-06-01", "2025-02-01"]), 1):
        for rider in range(12):
            rows.append({"stage_id": sid, "rider_id": rider, "rider_name": f"r{rider}", "stage_date": d,
                         "race_slug": "race/a/2025", "race_name": "A", "profile_type": "flat", "is_stage_race": 0,
                         "position": rider + 1, "status": "finished", "top10": float(rider < 10), "x": float(-rider)})
    df = pd.DataFrame(rows)
    other = df.assign(x=df["x"] * -1)                    # the other frame ranks the riders the wrong way round

    def fitter(train, test, cols):
        assert (train["x"] <= 0).all()                   # trained on df ...
        return test["x"].to_numpy()                      # ... scored on test_df
    same = walk_forward(df, ["x"], fitter, folds=("2025-01-01",), stage_clf=False)
    swapped = walk_forward(df, ["x"], fitter, folds=("2025-01-01",), stage_clf=False, test_df=other)
    assert stage_scores(same)["winner_rank"].iloc[0] == 1
    assert stage_scores(swapped)["winner_rank"].iloc[0] == 12
