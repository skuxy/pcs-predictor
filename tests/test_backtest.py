"""Tests for model/backtest.py metric calculations."""
from io import StringIO
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from model.backtest import backtest


def _make_df(stages: list[dict]) -> pd.DataFrame:
    """
    Build a synthetic predict_race() output DataFrame.
    Each stage entry: {date, rider_id, rider_name, top10_prob, top10, position, profile_type}
    """
    rows = []
    for s in stages:
        rows.append({
            "stage_date":   s["date"],
            "race_name":    s.get("name", "test stage"),
            "rider_id":     s["rider_id"],
            "rider_name":   s["rider_name"],
            "top10_prob":   s["top10_prob"],
            "top10":        s["top10"],
            "position":     s.get("position", None),
            "profile_type": s.get("profile_type", "flat"),
        })
    return pd.DataFrame(rows)


def _run_backtest(df, top_n=10):
    """Call backtest() with a mocked predict_race and capture stdout."""
    with patch("model.backtest.predict_race", return_value=df):
        buf = StringIO()
        with patch("builtins.print", side_effect=lambda *a, **kw: buf.write(" ".join(str(x) for x in a) + "\n")):
            backtest("race/fake/2024", "2024-01-01", top_n=top_n)
    return buf.getvalue()


class TestPrecisionRecall:
    def _one_stage(self, n_riders: int = 20, actual_top10: list[int] | None = None):
        """
        Build a single-stage DataFrame for n_riders.
        Riders are ordered by predicted probability (highest = rider_id 0).
        actual_top10: list of rider_ids who actually finished top-10.
                      Defaults to riders 0-9 if not given.
        All riders get a finish position so dropna(subset=["top10","position"]) keeps them.
        """
        if actual_top10 is None:
            actual_top10 = list(range(10))
        actual_set = set(actual_top10)
        # Assign finish positions: actual top-10 riders in order first, rest after
        fin_pos = {}
        for i, rid in enumerate(actual_top10):
            fin_pos[rid] = i + 1
        next_pos = len(actual_top10) + 1
        rows = []
        for rank in range(n_riders):
            rid = rank
            if rid not in fin_pos:
                fin_pos[rid] = next_pos
                next_pos += 1
            rows.append({
                "date": "2024-05-01", "rider_id": rid, "rider_name": f"Rider {rid}",
                "top10_prob": 1.0 - rank * (0.9 / n_riders),
                "top10": float(rid in actual_set),
                "position": fin_pos[rid],
            })
        return _make_df(rows)

    def test_perfect_precision(self):
        # Default: top-10 predicted (riders 0-9) == actual top-10
        df = self._one_stage()
        output = _run_backtest(df, top_n=10)
        assert "precision=1.00" in output

    def test_zero_precision(self):
        # Predicted top-10: riders 0-9; actual top-10: riders 10-19
        df = self._one_stage(n_riders=20, actual_top10=list(range(10, 20)))
        output = _run_backtest(df, top_n=10)
        assert "precision=0.00" in output

    def test_half_precision(self):
        # Predicted top-10: riders 0-9; actual top-10: riders 5-14 → 5 hits
        df = self._one_stage(n_riders=20, actual_top10=list(range(5, 15)))
        output = _run_backtest(df, top_n=10)
        assert "precision=0.50" in output

    def test_recall_eight_of_ten(self):
        # Predicted top-10: riders 0-9; actual top-10: riders 0-7 + 15,16 → 8 hits
        # precision = 8/10 = 0.80; recall = 8/10 = 0.80
        df = self._one_stage(n_riders=20, actual_top10=list(range(8)) + [15, 16])
        output = _run_backtest(df, top_n=10)
        assert "precision=0.80" in output
        assert "recall=0.80" in output


class TestGcLeaderboard:
    def test_most_frequent_rider_appears_first(self):
        # Contador: top-1 prediction on 3 stages (3 appearances)
        # Cavendish: top-1 on 1 stage, outside top-1 on 2 stages (1 appearance with top_n=1)
        stages = []
        for d in ["2024-05-01", "2024-05-02", "2024-05-03"]:
            stages.append({"date": d, "rider_id": 1, "rider_name": "Contador",
                           "top10_prob": 0.9, "top10": 1.0, "position": 1})
            stages.append({"date": d, "rider_id": 2, "rider_name": "Cavendish",
                           "top10_prob": 0.5, "top10": 0.0, "position": 5})
        df = _make_df(stages)
        output = _run_backtest(df, top_n=1)  # top_n=1 so only the highest-prob rider per stage
        gc_section = output.split("GC LEADERBOARD", 1)[-1]
        assert "Contador"  in gc_section, "Contador (3 top-1 appearances) should be in GC leaderboard"
        assert "Cavendish" not in gc_section, "Cavendish (0 top-1 appearances) should not appear"

    def test_empty_predictions_no_crash(self):
        df = pd.DataFrame(columns=["stage_date", "race_name", "rider_id", "rider_name",
                                   "top10_prob", "top10", "position", "profile_type"])
        with patch("model.backtest.predict_race", return_value=df):
            # Should not raise
            backtest("race/fake/2024", "2024-01-01")


class TestSummaryMetrics:
    def test_auc_present_in_output(self):
        rows = []
        for i in range(20):
            rows.append({
                "date": "2024-05-01", "rider_id": i, "rider_name": f"R{i}",
                "top10_prob": 1.0 - i * 0.04, "top10": float(i < 10),
                "position": i + 1,
            })
        df = _make_df(rows)
        output = _run_backtest(df)
        assert "Overall AUC" in output
        assert "Avg precision@10" in output

    def test_profile_breakdown_shown_for_multiple_stages(self):
        rows = []
        for i in range(20):
            rows.append({
                "date": "2024-05-01", "rider_id": i, "rider_name": f"R{i}",
                "top10_prob": 1.0 - i * 0.04, "top10": float(i < 10),
                "position": i + 1, "profile_type": "mountain",
            })
        for i in range(20):
            rows.append({
                "date": "2024-05-02", "rider_id": i, "rider_name": f"R{i}",
                "top10_prob": 1.0 - i * 0.04, "top10": float(i < 10),
                "position": i + 1, "profile_type": "flat",
            })
        df = _make_df(rows)
        output = _run_backtest(df)
        assert "Precision@10 by profile type" in output
