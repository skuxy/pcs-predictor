"""Tests for model/stage_classifier.py."""
import numpy as np
import pandas as pd
import pytest
from sklearn.ensemble import HistGradientBoostingClassifier

from model.stage_classifier import _label, _compute_gc_ranks, predict_proba, STAGE_FEATURES


class TestLabel:
    def _row(self, profile_type, n_zero=0, gc_rank=np.nan, is_stage=1):
        return pd.Series({
            "profile_type": profile_type,
            "n_zero_time": n_zero,
            "gc_rank_before": gc_rank,
            "is_stage_race": is_stage,
        })

    def test_itt_always_gc(self):
        assert _label(self._row("itt", n_zero=10)) == "gc"

    def test_ttt_always_gc(self):
        assert _label(self._row("ttt", n_zero=10)) == "gc"

    def test_sprint_when_six_same_time(self):
        assert _label(self._row("flat", n_zero=6)) == "sprint"

    def test_sprint_when_more_than_six_same_time(self):
        assert _label(self._row("mountain", n_zero=9)) == "sprint"

    def test_gc_when_winner_in_top15(self):
        assert _label(self._row("mountain", n_zero=1, gc_rank=5)) == "gc"

    def test_gc_at_boundary_rank15(self):
        assert _label(self._row("hilly", n_zero=0, gc_rank=15)) == "gc"

    def test_breakaway_when_winner_outside_top15(self):
        assert _label(self._row("mountain", n_zero=0, gc_rank=16)) == "breakaway"

    def test_oneday_flat_sprint(self):
        assert _label(self._row("flat", n_zero=0, gc_rank=np.nan, is_stage=0)) == "sprint"

    def test_oneday_mountain_breakaway(self):
        assert _label(self._row("mountain", n_zero=0, gc_rank=np.nan, is_stage=0)) == "breakaway"

    def test_stage1_no_gc_rank_flat(self):
        # Stage 1 of a stage race has no GC rank yet — fall back to profile
        assert _label(self._row("flat", n_zero=0, gc_rank=np.nan, is_stage=1)) == "sprint"

    def test_stage1_no_gc_rank_mountain(self):
        assert _label(self._row("mountain", n_zero=0, gc_rank=np.nan, is_stage=1)) == "breakaway"

    def test_sprint_threshold_five_not_triggered(self):
        # n_zero = 5 is below the sprint threshold of 6
        assert _label(self._row("flat", n_zero=5, gc_rank=20, is_stage=1)) == "breakaway"


class TestComputeGcRanks:
    def _make_data(self):
        """
        Mini 3-stage race: riders A=1, B=2, C=3.
        Stage 1: A wins in 3600s, B 3620s, C 3640s  (no GC rank before stage 1)
        Stage 2: B wins  -> gc_rank_before: A=1 (3600), B=2 (3620), C=3 (3640)
        Stage 3: C wins  -> cumulative before: A=7220, B=7240, C=7260 -> A=1, B=2, C=3
        """
        stages = pd.DataFrame([
            {"id": 10, "race_id": 1, "date": "2024-01-01", "is_stage_race": 1},
            {"id": 11, "race_id": 1, "date": "2024-01-02", "is_stage_race": 1},
            {"id": 12, "race_id": 1, "date": "2024-01-03", "is_stage_race": 1},
        ])
        results = pd.DataFrame([
            # stage 1
            {"id": 1, "stage_id": 10, "rider_id": 1, "position": 1, "status": "finished", "time_seconds": 3600},
            {"id": 2, "stage_id": 10, "rider_id": 2, "position": 2, "status": "finished", "time_seconds": 3620},
            {"id": 3, "stage_id": 10, "rider_id": 3, "position": 3, "status": "finished", "time_seconds": 3640},
            # stage 2
            {"id": 4, "stage_id": 11, "rider_id": 2, "position": 1, "status": "finished", "time_seconds": 3600},
            {"id": 5, "stage_id": 11, "rider_id": 1, "position": 2, "status": "finished", "time_seconds": 3620},
            {"id": 6, "stage_id": 11, "rider_id": 3, "position": 3, "status": "finished", "time_seconds": 3640},
            # stage 3
            {"id": 7, "stage_id": 12, "rider_id": 3, "position": 1, "status": "finished", "time_seconds": 3600},
            {"id": 8, "stage_id": 12, "rider_id": 1, "position": 2, "status": "finished", "time_seconds": 3620},
            {"id": 9, "stage_id": 12, "rider_id": 2, "position": 3, "status": "finished", "time_seconds": 3640},
        ])
        return results, stages

    def test_stage1_has_no_gc_rank(self):
        results, stages = self._make_data()
        gc = _compute_gc_ranks(results, stages)
        # Stage 1 winner (rider 1) should not appear because _n_before == 0
        assert 10 not in gc["stage_id"].values

    def test_stage2_winner_gc_rank(self):
        results, stages = self._make_data()
        gc = _compute_gc_ranks(results, stages)
        row = gc[gc["stage_id"] == 11].iloc[0]
        # Stage 2 winner is rider 2; before stage 2, rider 1 leads (3600s), rider 2 is 2nd
        assert row["gc_rank_before"] == 2.0

    def test_stage3_winner_gc_rank(self):
        results, stages = self._make_data()
        gc = _compute_gc_ranks(results, stages)
        row = gc[gc["stage_id"] == 12].iloc[0]
        # Stage 3 winner is rider 3; cumulative before: A=7220, B=7240, C=7260 -> C is 3rd
        assert row["gc_rank_before"] == 3.0

    def test_returns_correct_columns(self):
        results, stages = self._make_data()
        gc = _compute_gc_ranks(results, stages)
        assert set(gc.columns) >= {"stage_id", "gc_rank_before"}

    def test_empty_results_returns_empty(self):
        results, stages = self._make_data()
        # Filter results to empty
        empty_results = results[results["stage_id"] == 9999]
        gc = _compute_gc_ranks(empty_results, stages)
        assert gc.empty


class TestPredictProba:
    def _bundle(self):
        X = pd.DataFrame([
            {f: 0.0 for f in STAGE_FEATURES},
            {f: 1.0 for f in STAGE_FEATURES},
            {f: 0.5 for f in STAGE_FEATURES},
        ])
        y = ["sprint", "gc", "breakaway"]
        clf = HistGradientBoostingClassifier(max_iter=10, random_state=0)
        clf.fit(X, y)
        return {"model": clf, "labels": clf.classes_.tolist()}

    def test_output_columns(self):
        bundle = self._bundle()
        X = pd.DataFrame([{f: 0.0 for f in STAGE_FEATURES}])
        out = predict_proba(X, bundle)
        assert list(out.columns) == ["p_sprint", "p_breakaway", "p_gc"]

    def test_probabilities_sum_to_one(self):
        bundle = self._bundle()
        X = pd.DataFrame([{f: v for f, v in zip(STAGE_FEATURES, range(len(STAGE_FEATURES)))}])
        out = predict_proba(X, bundle)
        assert abs(out.iloc[0].sum() - 1.0) < 1e-6

    def test_missing_class_filled_with_zero(self):
        # Train with only two classes — third should be zero-filled
        X = pd.DataFrame([{f: float(i) for f in STAGE_FEATURES} for i in range(4)])
        y = ["sprint", "gc", "sprint", "gc"]
        clf = HistGradientBoostingClassifier(max_iter=10, random_state=0)
        clf.fit(X, y)
        bundle = {"model": clf, "labels": clf.classes_.tolist()}
        out_cols = predict_proba(pd.DataFrame([{f: 0.0 for f in STAGE_FEATURES}]), bundle).columns
        assert "p_breakaway" in out_cols
