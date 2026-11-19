"""main.py wiring for the team UCI points commands and init_db's UCI-points schema."""
import sqlite3
import types

import pytest

import main
from db.database import init_db

UCI_TABLES = {"uci_team_totals", "uci_team_breakdown", "rider_uci_results",
              "rider_uci_penalties", "rider_team_spells"}


def _tables(path):
    with sqlite3.connect(path) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_init_db_creates_uci_points_tables(tmp_path):
    db = tmp_path / "c.db"
    init_db(str(db))
    init_db(str(db))  # idempotent
    assert UCI_TABLES <= _tables(db)
    assert {"results", "stages", "races", "riders"} <= _tables(db)


@pytest.mark.parametrize("cmd,script", [("scrape-uci-points", "scrape_uci_points"),
                                        ("team-points", "team_points_report")])
def test_passthrough_commands_forward_argv(monkeypatch, cmd, script):
    seen = {}

    def fake_load(name):
        seen["name"] = name
        return types.SimpleNamespace(run=lambda argv: seen.setdefault("argv", argv) and 0)

    monkeypatch.setattr(main, "_load_script", fake_load)
    main.main([cmd, "--season", "2026", "--help", "-v"])
    assert seen == {"name": script, "argv": ["--season", "2026", "--help", "-v"]}


def test_passthrough_nonzero_exit_code(monkeypatch):
    monkeypatch.setattr(main, "_load_script", lambda name: types.SimpleNamespace(run=lambda argv: 2))
    with pytest.raises(SystemExit) as exc:
        main.main(["scrape-uci-points", "--season", "2026"])
    assert exc.value.code == 2


def test_other_commands_still_reject_unknown_args():
    with pytest.raises(SystemExit) as exc:
        main.main(["init", "--bogus"])
    assert exc.value.code == 2


def test_load_script_imports_by_path():
    mod = main._load_script("team_points_report")
    assert callable(mod.run)
    mod = main._load_script("scrape_uci_points")
    assert callable(mod.run)


def test_team_points_end_to_end_missing_tables(tmp_path, capsys):
    """team-points on a DB without the UCI tables exits 1 and leaves the DB untouched."""
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()
    with pytest.raises(SystemExit) as exc:
        main.main(["team-points", "--db", str(db), "--season", "2026"])
    assert exc.value.code == 1
    assert "no uci_team_totals" in capsys.readouterr().err
    assert _tables(db) == set()


def test_scrape_elevation_replaces_the_unread_mountain_default(tmp_path, monkeypatch):
    """A stage that only came from the stage list is stored as 'mountain' with
    distance 0; the backfill takes its profile from the stage page.  A label
    that was read from a page is never overwritten."""
    import argparse
    from db import database
    import scraper.races as races

    path = str(tmp_path / "c.db")
    init_db(path)
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO races (id, pcs_slug, name, year, start_date, end_date, class, is_stage_race) "
                     "VALUES (1, 'race/tour-of-belgium/2026', 'Tour of Belgium', 2026, '2026-06-17', '2026-06-18', '2.Pro', 1)")
        conn.execute("INSERT INTO stages (id, race_id, stage_num, pcs_slug, date, distance_km, elevation_m, profile_type) "
                     "VALUES (1, 1, 1, 'race/tour-of-belgium/2026/stage-1', '2026-06-17', 0, 500, 'mountain')")
        conn.execute("INSERT INTO stages (id, race_id, stage_num, pcs_slug, date, distance_km, elevation_m, profile_type) "
                     "VALUES (2, 1, 2, 'race/tour-of-belgium/2026/stage-2', '2026-06-18', 181.5, 2900, 'mountain')")
        conn.commit()

    from contextlib import contextmanager

    @contextmanager
    def _conn(db_path=path):
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr(database, "get_conn", _conn)
    monkeypatch.setattr(races, "fetch_stage_elevation", lambda slug: None)
    monkeypatch.setattr(races, "fetch_race_stages", lambda slug: [])
    monkeypatch.setattr(races, "fetch_result_meta",
                        lambda slug: {"gradient_final_km": 0.4, "profile_score": 12, "profile_type": "flat"})
    monkeypatch.setattr(races, "fetch_stage_meta", lambda slug: {})       # the distance backfill: nothing on the page

    main.cmd_scrape_elevation(argparse.Namespace())

    with sqlite3.connect(path) as conn:
        rows = dict(conn.execute("SELECT id, profile_type FROM stages"))
        scores = dict(conn.execute("SELECT id, profile_score FROM stages"))
    assert rows == {1: "flat", 2: "mountain"}       # stage 2 had a distance: its label was read from a page
    assert scores == {1: 12, 2: 12}


def test_scrape_elevation_fills_a_missing_distance(tmp_path, monkeypatch):
    """A stage stored before PCS showed its distance keeps 0 once it has
    results (scrape skips it).  scrape-elevation takes the distance from the
    stage page (the /result page for a one-day race).  The stage-list default
    label is recognised by its missing distance, so it gets a distance only
    together with the profile its page shows."""
    import argparse
    import datetime
    from contextlib import contextmanager
    from db import database
    import scraper.races as races
    from scraper import utils

    far = (datetime.date.today() + datetime.timedelta(days=main.ROUTE_HORIZON_DAYS + 5)).isoformat()
    path = str(tmp_path / "d.db")
    init_db(path)
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO races (id, pcs_slug, name, year, start_date, end_date, class, is_stage_race) VALUES "
                     "(1, 'race/tour-de-suisse/2026', 'Tour de Suisse', 2026, '2026-06-17', '2026-06-21', '2.UWT', 1), "
                     "(2, 'race/gp-de-plumelec/2026', 'GP de Plumelec', 2026, '2026-05-09', '2026-05-09', '1.Pro', 0)")
        stage = ("INSERT INTO stages (id, race_id, stage_num, pcs_slug, date, distance_km, elevation_m, "
                 "gradient_final_km, profile_score, profile_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
        conn.executemany(stage, [
            (1, 1, 1, "race/tour-de-suisse/2026/stage-1", "2026-06-17", 0, 2446, 1.9, 202, "hilly"),      # route read, distance missed
            (2, 1, 2, "race/tour-de-suisse/2026/stage-2", "2026-06-18", 0, 100, 0.0, None, "mountain"),   # default label, page shows no profile
            (3, 1, 3, "race/tour-de-suisse/2026/stage-3", "2026-06-19", 0, 100, 0.0, None, "mountain"),   # default label, page shows it
            (4, 2, 1, "race/gp-de-plumelec/2026", "2026-05-09", 0, 2433, 6.2, 126, "hilly"),              # one-day race
            (5, 1, 5, "race/tour-de-suisse/2026/stage-5", far, 0, 100, 0.0, 30, "flat"),                  # too far ahead
            (6, 1, 4, "race/tour-de-suisse/2026/stage-4", "2026-06-20", 10.5, 202, 0.9, 10, "itt"),       # has a distance
        ])
        conn.commit()

    @contextmanager
    def _conn(db_path=path):
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    pages = {
        "race/tour-de-suisse/2026/stage-1": {"distance_km": 171.2, "profile_type": "mountain"},
        "race/tour-de-suisse/2026/stage-2": {"distance_km": 150.0, "profile_type": None},
        "race/tour-de-suisse/2026/stage-3": {"distance_km": 160.0, "profile_type": "flat"},
        "race/gp-de-plumelec/2026": {"distance_km": None},
        "race/gp-de-plumelec/2026/result": {"distance_km": 198.5, "profile_type": "hilly"},
    }
    seen = []

    def fake_meta(slug):
        seen.append((slug, utils._stale_through))
        return dict(pages.get(slug, {}))

    monkeypatch.setattr(database, "get_conn", _conn)
    monkeypatch.setattr(races, "fetch_stage_elevation", lambda slug: None)
    monkeypatch.setattr(races, "fetch_race_stages", lambda slug: [])
    monkeypatch.setattr(races, "fetch_result_meta", lambda slug: {})
    monkeypatch.setattr(races, "fetch_stage_meta", fake_meta)

    main.cmd_scrape_elevation(argparse.Namespace())

    with sqlite3.connect(path) as conn:
        got = {i: (d, p) for i, d, p in conn.execute("SELECT id, distance_km, profile_type FROM stages")}
    assert got[1] == (171.2, "hilly")           # the stored label was read from a page: kept
    assert got[2] == (0, "mountain")            # no profile on the page: still recognisable as unread
    assert got[3] == (160.0, "flat")
    assert got[4] == (198.5, "hilly")
    assert got[5] == (0, "flat") and got[6] == (10.5, "itt")
    assert [slug for slug, _ in seen] == [
        "race/tour-de-suisse/2026/stage-1", "race/tour-de-suisse/2026/stage-2", "race/tour-de-suisse/2026/stage-3",
        "race/gp-de-plumelec/2026", "race/gp-de-plumelec/2026/result"]
    assert dict(seen)["race/tour-de-suisse/2026/stage-1"] == "2026-06-17"      # a copy from before the stage is not used
