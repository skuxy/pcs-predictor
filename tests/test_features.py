"""Tests for GC-state feature computation in features/builder.py."""
import numpy as np
import pandas as pd
import pytest

from features.builder import _gc_state, FEATURE_COLS


def _stages():
    # race 1: three-stage race; race 2: one-day race
    return pd.DataFrame({
        "id":            [101, 102, 103, 201],
        "race_id":       [1, 1, 1, 2],
        "date":          ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-05"],
        "is_stage_race": [1, 1, 1, 0],
    })


def _results():
    # time_seconds is the gap behind the stage winner
    rows = [
        # stage 1: r10 and r11 same time, r12 +30s
        (101, 10, 0.0, "finished"),
        (101, 11, 0.0, "finished"),
        (101, 12, 30.0, "finished"),
        # stage 2: r11 loses 60s
        (102, 10, 0.0, "finished"),
        (102, 11, 60.0, "finished"),
        (102, 12, 0.0, "finished"),
        # stage 3: results irrelevant, only the "before" state matters
        (103, 10, 0.0, "finished"),
        (103, 11, 0.0, "finished"),
        (103, 12, 0.0, "finished"),
        # one-day race: must not appear in GC output
        (201, 10, 0.0, "finished"),
    ]
    return pd.DataFrame(rows, columns=["stage_id", "rider_id", "time_seconds", "status"])


def test_gc_state_first_stage_has_no_rows():
    gc = _gc_state(_results(), _stages())
    assert not (gc["stage_id"] == 101).any()


def test_gc_state_one_day_race_excluded():
    gc = _gc_state(_results(), _stages())
    assert not (gc["stage_id"] == 201).any()


def test_gc_state_before_stage_2():
    gc = _gc_state(_results(), _stages()).set_index(["stage_id", "rider_id"])
    # after stage 1: r10=0, r11=0, r12=+30s
    assert gc.loc[(102, 10), "gc_deficit_min"] == 0.0
    assert gc.loc[(102, 11), "gc_deficit_min"] == 0.0
    assert gc.loc[(102, 12), "gc_deficit_min"] == pytest.approx(0.5)
    assert gc.loc[(102, 10), "gc_rank_before"] == 1
    assert gc.loc[(102, 11), "gc_rank_before"] == 1
    assert gc.loc[(102, 12), "gc_rank_before"] == 3


def test_gc_state_before_stage_3_accumulates():
    gc = _gc_state(_results(), _stages()).set_index(["stage_id", "rider_id"])
    # after stages 1+2: r10=0, r11=60s, r12=30s
    assert gc.loc[(103, 10), "gc_deficit_min"] == 0.0
    assert gc.loc[(103, 11), "gc_deficit_min"] == pytest.approx(1.0)
    assert gc.loc[(103, 12), "gc_deficit_min"] == pytest.approx(0.5)
    assert gc.loc[(103, 10), "gc_rank_before"] == 1
    assert gc.loc[(103, 11), "gc_rank_before"] == 3
    assert gc.loc[(103, 12), "gc_rank_before"] == 2


def test_gc_state_missing_time_does_not_break_accumulation():
    results = _results()
    results.loc[
        (results["stage_id"] == 102) & (results["rider_id"] == 12), "time_seconds"
    ] = np.nan
    gc = _gc_state(results, _stages()).set_index(["stage_id", "rider_id"])
    # r12's missing stage-2 time contributes 0; stage-1 gap remains
    assert gc.loc[(103, 12), "gc_deficit_min"] == pytest.approx(0.5)
    assert gc.loc[(103, 11), "gc_deficit_min"] == pytest.approx(1.0)


def test_new_features_registered():
    for col in ["gc_deficit_min", "gc_rank_before",
                "break_top10_rate_365d", "break_top10s_365d"]:
        assert col in FEATURE_COLS
