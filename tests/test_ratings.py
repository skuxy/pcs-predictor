"""Elo-style ratings (features/ratings.py)."""
import numpy as np
import pandas as pd
import pytest

from features.ratings import START, _kinds_for_stage, _stage_update, compute_ratings


def test_stage_update_is_zero_sum_and_rewards_the_front():
    delta = _stage_update(np.full(6, START), np.array([1, 2, 3, 4, 5, 6], dtype=float), k=60, cap=25)
    assert delta.sum() == pytest.approx(0.0, abs=1e-9)
    assert delta[0] > delta[1] > delta[4] > delta[5]
    assert delta[0] > 0 > delta[5]


def test_stage_update_caps_the_back_of_the_field():
    pos = np.arange(1, 41, dtype=float)
    delta = _stage_update(np.full(40, START), pos, k=60, cap=25)
    # everyone at or beyond the cap ties
    assert np.allclose(delta[24:], delta[24])
    assert delta[23] > delta[24]


def test_beating_a_strong_field_is_worth_more():
    pos = np.array([1, 2, 3, 4], dtype=float)
    weak = _stage_update(np.array([1500, 1400, 1400, 1400.0]), pos, k=60, cap=25)[0]
    strong = _stage_update(np.array([1500, 1700, 1700, 1700.0]), pos, k=60, cap=25)[0]
    assert strong > weak > 0


def test_kinds_for_stage():
    assert _kinds_for_stage("ttt", "road", None) == []
    assert _kinds_for_stage("itt", "road", "select") == ["all", "itt"]
    assert _kinds_for_stage("flat", "road", "bunch") == ["all", "bunch"]
    assert _kinds_for_stage("mountain", "road", "select") == ["all", "select", "mountain"]
    assert _kinds_for_stage("hilly", "cobbled", "mixed") == ["all", "select", "cobbled"]
    assert _kinds_for_stage("hilly", "road", None) == ["all"]


def _frames():
    stages = pd.DataFrame({
        "id": [1, 2, 3, 4],
        "date": ["2026-03-01", "2026-03-02", "2026-03-02", "2026-03-05"],
        "profile_type": ["flat", "flat", "hilly", "flat"],
        "surface": ["road"] * 4,
        "finish_type": ["bunch", "bunch", "select", None],
    })
    rows = []
    for stage in (1, 2, 3):
        for pos, rider in enumerate((10, 11, 12), 1):
            rows.append((stage, rider, pos, "finished"))
    rows.append((2, 13, None, "dnf"))
    # stage 4 has not been ridden: start-list rows only
    rows += [(4, 10, None, "startlist"), (4, 12, None, "startlist"), (4, 99, None, "startlist")]
    return pd.DataFrame(rows, columns=["stage_id", "rider_id", "position", "status"]), stages


def test_ratings_are_pre_stage_values():
    results, stages = _frames()
    r = compute_ratings(results, stages).set_index(["stage_id", "rider_id"])
    # nobody has a rating before the first stage
    assert r.loc[1, "elo_all"].isna().all()
    # stages 2 and 3 are the same day: both see only stage 1
    assert r.loc[(2, 10), "elo_all"] == r.loc[(3, 10), "elo_all"]
    assert r.loc[(2, 10), "elo_all"] > START > r.loc[(2, 12), "elo_all"]
    # the select rating has no earlier stage of its kind on day 2
    assert np.isnan(r.loc[(3, 10), "elo_select"])
    # a DNF row gets a rating but had none yet
    assert np.isnan(r.loc[(2, 13), "elo_all"])


def test_start_list_rows_read_ratings_without_changing_them():
    results, stages = _frames()
    r = compute_ratings(results, stages).set_index(["stage_id", "rider_id"])
    # rider 10 won three times: clearly above rider 12 before the open stage
    assert r.loc[(4, 10), "elo_all"] > r.loc[(4, 12), "elo_all"]
    assert r.loc[(4, 10), "elo_bunch"] > START
    assert r.loc[(4, 10), "elo_select"] > START
    # a rider with no results has no rating
    assert np.isnan(r.loc[(4, 99), "elo_all"])
    # dropping the start-list rows leaves the other rows' ratings unchanged
    without = compute_ratings(results[results["status"] != "startlist"], stages).set_index(["stage_id", "rider_id"])
    pd.testing.assert_frame_equal(r.loc[without.index], without)
