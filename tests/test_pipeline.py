"""End to end on a synthetic season: train, predict from a start list, backtest, evaluate."""
import json
import sqlite3

import numpy as np
import pandas as pd
import pytest

import model.evaluate as evaluate
import model.predict as predict
import model.stage_classifier as stage_classifier
import model.train as train
from model.predict import SOURCE_SEEN, SOURCE_UNSEEN, SOURCE_WALK_FORWARD, coherent_probabilities
from model.train import TARGETS, training_rows


@pytest.fixture
def workspace(season_db, tmp_path, monkeypatch):
    """Synthetic DB plus a private model directory (never the repo's model/)."""
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    monkeypatch.setattr(train, "MODEL_DIR", model_dir)
    monkeypatch.setattr(evaluate, "EVAL_DIR", model_dir)
    monkeypatch.setattr(stage_classifier, "CLASSIFIER_PATH", model_dir / "stage_classifier.pkl")
    monkeypatch.setattr(evaluate, "_race_classes", lambda: pd.read_sql(
        "SELECT pcs_slug AS race_slug, class AS race_class FROM races", sqlite3.connect(season_db["path"])))
    season_db["model_dir"] = model_dir
    return season_db


def test_training_rows_skip_team_time_trials_and_unridden_stages():
    df = pd.DataFrame({
        "top10":        [1.0, 0.0, 0.0, np.nan, 1.0],
        "position":     [1,   12,  np.nan, np.nan, 2],
        "profile_type": ["flat", "flat", "flat", "flat", "ttt"],
    })
    rows, y = training_rows(df, "top10")
    assert list(rows.index) == [0, 1, 2]             # start-list row and the TTT row are gone
    assert y.tolist() == [1, 0, 0]                   # a DNF is "not top 10"
    assert training_rows(df, "win")[1].tolist() == [1, 0, 0]
    assert set(TARGETS) == {"top10", "top3", "win"}


def test_coherent_probabilities():
    df = pd.DataFrame({
        "stage_id":   [1, 1, 1, 1, 2, 2],
        "win_prob":   [0.30, 0.10, 0.05, 0.05, 0.9, 0.9],
        "top3_prob":  [0.20, 0.50, 0.40, 0.10, 0.9, 0.9],
        "top10_prob": [0.60, 0.40, 0.90, 0.30, 0.7, 0.95],
    })
    out = coherent_probabilities(df)
    sums = out.groupby("stage_id")[["win_prob", "top3_prob"]].sum()
    assert sums.loc[1, "win_prob"] == pytest.approx(1.0)
    assert sums.loc[2, "win_prob"] == pytest.approx(1.0)
    # the log-odds shift keeps the order of the riders within a stage
    assert out[out.stage_id == 1]["win_prob"].rank().tolist() == df[df.stage_id == 1]["win_prob"].rank().tolist()
    # a stage whose probabilities already add up is left as it is
    exact = pd.DataFrame({"stage_id": [5] * 4, "win_prob": [0.4, 0.3, 0.2, 0.1]})
    assert coherent_probabilities(exact)["win_prob"].tolist() == pytest.approx([0.4, 0.3, 0.2, 0.1], abs=1e-6)
    assert (out["win_prob"] <= out["top3_prob"] + 1e-12).all()
    assert (out["top3_prob"] <= out["top10_prob"] + 1e-12).all()
    assert (out[["win_prob", "top3_prob", "top10_prob"]] <= 1.0).all().all()
    # a two-rider field has two podium places, not three
    assert out.loc[out.stage_id == 2, "top3_prob"].sum() == pytest.approx(2.0, abs=1e-6)
    assert df["win_prob"].tolist() == [0.30, 0.10, 0.05, 0.05, 0.9, 0.9]      # input untouched


def test_train_predict_and_backtest(workspace, monkeypatch):
    train.train(train_cutoff="2026-09-01", gender="men", target="all", importances=False)
    model_dir = workspace["model_dir"]
    for name in ("trained_model.pkl", "trained_model_top3.pkl", "trained_model_win.pkl",
                 "feature_names.json", "metrics.json"):
        assert (model_dir / name).exists(), name
    metrics = json.loads((model_dir / "metrics.json").read_text())
    assert metrics["train_cutoff"] == "2026-09-01"
    assert metrics["last_training_stage"] == "2026-05-04"
    assert "val_auc" not in metrics

    # gamma 2026 (15 Sep) is after the cutoff: an honest backtest with the model on disk
    df, source = predict.backtest_predictions("race/gamma/2026", "men")
    assert source == SOURCE_UNSEEN
    assert len(df) == workspace["n_riders"]
    assert {"top10_prob", "top3_prob", "win_prob"} <= set(df.columns)
    assert df["win_prob"].sum() == pytest.approx(1.0, abs=1e-6)
    assert (df["win_prob"] <= df["top3_prob"] + 1e-9).all() and (df["top3_prob"] <= df["top10_prob"] + 1e-9).all()
    # the strongest riders are ranked ahead of the weakest
    ranked = df.sort_values("top10_prob", ascending=False)["rider_id"].tolist()
    assert np.mean(ranked[:10]) < np.mean(ranked[-10:])

    # alpha 2026 (10 Mar) is before the cutoff: flagged as seen in training
    _, source = predict.backtest_predictions("race/alpha/2026", "men")
    assert source == SOURCE_SEEN

    # an upcoming race is predicted from its start list with the same code path
    with sqlite3.connect(workspace["path"]) as conn:
        conn.execute("INSERT INTO races (id, pcs_slug, name, year, start_date, end_date, class, is_stage_race, gender) "
                     "VALUES (100, 'race/alpha/2027', 'Alpha', 2027, '2027-03-10', '2027-03-10', '1.Pro', 0, 'men')")
        conn.execute("INSERT INTO stages (id, race_id, pcs_slug, date, distance_km, profile_type, surface) "
                     "VALUES (1000, 100, 'race/alpha/2027', '2027-03-10', 150, 'flat', 'road')")
        conn.commit()
    starters = [{"slug": f"rider-{i}", "name": f"Rider {i}", "team": None, "team_slug": f"team-{(i - 1) // 5}",
                 "bib": ((i - 1) // 5) * 10 + (i - 1) % 5 + 1} for i in range(1, 31)]
    starters.append({"slug": "new-kid", "name": "KID New", "team": None, "team_slug": "team-9", "bib": 91})
    monkeypatch.setattr(predict, "fetch_startlist", lambda slug: starters)
    up = predict.predict_race("race/alpha/2027", "2027-03-01", gender="men")
    assert len(up) == 31 and up["top10"].isna().all()
    assert up["rider_name"].notna().all() and "KID New" in set(up["rider_name"])
    assert up["win_prob"].sum() == pytest.approx(1.0, abs=1e-6)
    by_rider = up.set_index("rider_id")["top10_prob"]
    assert by_rider.loc[[1, 2, 3]].mean() > by_rider.loc[[28, 29, 30]].mean()


def test_evaluate_walk_forward_saves_summary_and_predictions(workspace):
    rep = evaluate.evaluate(gender="men", folds=("2025-09-01", "2026-04-01"), targets=("top10",), save=True)
    # fold 1: gamma 2025, alpha 2026; fold 2: beta 2026 (the ITT counts, no TTT here) and gamma 2026
    assert rep["stages"] == 2 + 5
    assert 0 <= rep["overall"]["p_at_10"] <= 1
    assert rep["overall"]["p_at_10"] > rep["random_p_at_10"]
    assert {b["group"] for b in rep["baselines"]} == {label for _, label, _ in evaluate.BASELINES}
    assert rep["config"]["folds"] == ["2025-09-01", "2026-04-01"]
    assert "calibration" in rep and len(rep["by_profile"]) >= 3
    text = evaluate.format_report(rep)
    assert "p@10" in text and "terrain" in text

    saved = json.loads(evaluate.evaluation_path("men").read_text())
    assert saved["stages"] == rep["stages"]
    wf = evaluate.load_walkforward("men")
    assert list(wf.columns) == ["stage_id", "rider_id", "fold", "top10_prob"]
    assert wf["stage_id"].nunique() == 7
    assert set(wf["fold"]) == {"2025-09-01", "2026-04-01"}

    # a covered race is backtested from the saved out-of-sample predictions
    df, source = predict.backtest_predictions("race/gamma/2026", "men")
    assert source == SOURCE_WALK_FORWARD
    assert len(df) == workspace["n_riders"] and df["top10_prob"].between(0, 1).all()
    assert set(df["position"].dropna()) <= set(range(1, 31))


def test_predict_race_leaves_team_time_trials_out(workspace):
    train.train(train_cutoff="2026-01-01", gender="men", target="top10", importances=False)
    with sqlite3.connect(workspace["path"]) as conn:
        conn.execute("UPDATE stages SET profile_type = 'ttt' WHERE id = ?",
                     (workspace["stage_ids"][("beta", 2026, 1)],))
        conn.commit()
    df = predict.predict_race("race/beta/2026", "2026-05-01", gender="men")
    assert df["stage_id"].nunique() == 3
    assert (df["profile_type"] != "ttt").all()
