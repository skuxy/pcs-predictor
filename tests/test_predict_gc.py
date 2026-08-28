"""Regression tests for GC-snapshot / bib handling in predictions.

_gc_snapshot() (model/predict.py) groups riders into teams via `bib // 10`,
reading it off the DataFrame returned by db.database._load_results(). That
loader must keep selecting `bib` — dropping it makes any prediction for a
race with at least one completed stage crash with KeyError('bib').
"""
import sqlite3

import pandas as pd

from db.database import _load_results
from model.predict import _gc_snapshot


SCHEMA = """
CREATE TABLE results (
    id INTEGER PRIMARY KEY,
    stage_id INTEGER NOT NULL,
    rider_id INTEGER NOT NULL,
    position INTEGER,
    status TEXT DEFAULT 'finished',
    time_seconds INTEGER,
    points_pcs INTEGER,
    points_uci INTEGER,
    bib INTEGER
);
"""


def test_load_results_includes_bib_column():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO results (stage_id, rider_id, position, status, time_seconds, bib) "
        "VALUES (1, 10, 1, 'finished', 100, 1)"
    )
    conn.commit()

    df = _load_results(conn)
    assert "bib" in df.columns
    assert df.iloc[0]["bib"] == 1


def test_gc_snapshot_groups_riders_by_team_via_bib():
    completed_stage_rows = [{"id": 1}, {"id": 2}]
    results_df = pd.DataFrame([
        {"stage_id": 1, "rider_id": 10, "status": "finished", "time_seconds": 100, "bib": 1},
        {"stage_id": 1, "rider_id": 20, "status": "finished", "time_seconds": 105, "bib": 21},
        {"stage_id": 2, "rider_id": 10, "status": "finished", "time_seconds": 100, "bib": 1},
        {"stage_id": 2, "rider_id": 20, "status": "finished", "time_seconds": 130, "bib": 21},
    ])

    gc_deficit, gc_rank, team_min_deficit, team_best_rank, rider_team = _gc_snapshot(
        completed_stage_rows, results_df
    )

    assert rider_team[10] == 0    # bib 1 // 10
    assert rider_team[20] == 2    # bib 21 // 10
    assert gc_deficit[10] == 0.0  # leader
    assert gc_deficit[20] > 0.0
    assert team_min_deficit[0] == 0.0
    assert team_best_rank[0] == 1
