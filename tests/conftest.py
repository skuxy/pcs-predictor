"""Shared fixtures: a small synthetic season database for feature / prediction tests."""
import sqlite3

import numpy as np
import pytest

from db import database


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """No test may reach PCS.  A fetch that a test forgot to fake fails at
    once instead of quietly asking the real site (and passing)."""
    from scraper import results, utils

    def refuse(*args, **kwargs):
        # pytest.fail is not an Exception subclass: no `except Exception` in the code under test swallows it
        pytest.fail("a test tried to fetch from the network: fake the fetcher it calls")

    monkeypatch.setattr(utils.SESSION, "get", refuse)
    monkeypatch.setattr(utils, "fetch_with_playwright", refuse)
    monkeypatch.setattr(results, "fetch_with_playwright", refuse)


def _build_synthetic_db(path: str, seed: int = 7) -> dict:
    """Three seasons of three races for 30 riders in 6 teams of 5.

    alpha  one-day, flat          (bunch finishes)
    beta   4 stages: flat, hilly, mountain, itt
    gamma  one-day, hilly, cobbled (small-group finishes)

    Rider strength is fixed (rider 1 strongest) with noise, so form features
    carry signal.  Returns ids the tests need.
    """
    database.init_db(path)
    rng = np.random.default_rng(seed)
    n_riders = 30
    conn = sqlite3.connect(path)
    for i in range(1, n_riders + 1):
        conn.execute("INSERT INTO riders (id, pcs_slug, name) VALUES (?,?,?)", (i, f"rider-{i}", f"Rider {i}"))

    stage_id = 0
    race_id = 0
    stage_ids = {}
    for year in (2024, 2025, 2026):
        plan = [
            ("alpha", "1.Pro", 0, [(f"{year}-03-10", "flat", "road")]),
            ("beta", "2.UWT", 1, [(f"{year}-05-0{d}", p, "road") for d, p in
                                  ((1, "flat"), (2, "hilly"), (3, "mountain"), (4, "itt"))]),
            ("gamma", "1.UWT", 0, [(f"{year}-09-15", "hilly", "cobbled")]),
        ]
        for name, cls, is_stage, stages in plan:
            race_id += 1
            slug = f"race/{name}/{year}"
            conn.execute(
                "INSERT INTO races (id, pcs_slug, name, year, start_date, end_date, class, is_stage_race, gender) "
                "VALUES (?,?,?,?,?,?,?,?, 'men')",
                (race_id, slug, name.title(), year, stages[0][0], stages[-1][0], cls, is_stage))
            for num, (date, profile, surface) in enumerate(stages, 1):
                stage_id += 1
                st_slug = f"{slug}/stage-{num}" if is_stage else slug
                conn.execute(
                    "INSERT INTO stages (id, race_id, stage_num, pcs_slug, date, distance_km, elevation_m, "
                    "profile_type, surface, gradient_final_km, profile_score) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (stage_id, race_id, num if is_stage else None, st_slug, date, 150.0, 1500, profile, surface,
                     6.0 if profile == "mountain" else 0.5, 200 if profile == "mountain" else 20))
                stage_ids[(name, year, num)] = stage_id
                strength = np.arange(n_riders) + rng.normal(0, 6, n_riders)
                order = np.argsort(strength)
                bunch = profile == "flat"
                for pos, ridx in enumerate(order, 1):
                    rider = int(ridx) + 1
                    bib = (ridx // 5) * 10 + (ridx % 5) + 1
                    dnf = pos > 27
                    gap = 0 if (bunch or pos == 1) else pos * 7
                    conn.execute(
                        "INSERT INTO results (stage_id, rider_id, position, status, time_seconds, points_pcs, bib, team_slug) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (stage_id, rider, None if dnf else pos, "dnf" if dnf else "finished",
                         None if dnf else gap, max(0, 30 - 3 * pos) if pos <= 10 else None, int(bib),
                         f"team-{ridx // 5}"))
    conn.commit()
    conn.close()
    return {"stage_ids": stage_ids, "n_riders": n_riders}


@pytest.fixture
def season_db(tmp_path, monkeypatch):
    """Synthetic database wired into features.builder / model.predict / model.stage_classifier."""
    path = str(tmp_path / "season.db")
    info = _build_synthetic_db(path)
    info["path"] = path

    def _conn():
        return database.get_conn(path)

    import features.builder as builder
    import model.predict as predict
    import model.stage_classifier as stage_classifier
    monkeypatch.setattr(builder, "get_conn", _conn)
    monkeypatch.setattr(predict, "get_conn", _conn)
    monkeypatch.setattr(stage_classifier, "get_conn", _conn)
    monkeypatch.setattr(database, "DB_PATH", path, raising=False)
    info["conn_factory"] = _conn
    return info
