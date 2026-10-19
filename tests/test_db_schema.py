"""Schema migration + result-storage tests (team columns, UCI sanctions) and
the scrape-missing / repair-points flows on a temporary DB (no network)."""
import argparse
import sqlite3
from contextlib import contextmanager

import pytest

import main
from db import database
from db.database import (
    fill_result_teams, fill_stage_meta, find_implausible_uci_stages, init_db, insert_result,
    repair_stage_points, uci_points_anomalies, upsert_stage,
    find_null_gap_stages, find_stages_disagreeing_with_rider_results,
)

# The results/stages/races/riders tables as they were before team_slug /
# team_name / uci_penalty existed (db/schema.sql up to Oct 2026).
OLD_SCHEMA = """
CREATE TABLE riders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, pcs_slug TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
    nationality TEXT, dob TEXT, team TEXT, pcs_rank INTEGER, speciality TEXT,
    weight_kg REAL, height_cm REAL, updated_at TEXT DEFAULT (datetime('now')));
CREATE TABLE races (
    id INTEGER PRIMARY KEY AUTOINCREMENT, pcs_slug TEXT NOT NULL, name TEXT NOT NULL,
    year INTEGER NOT NULL, start_date TEXT, end_date TEXT, class TEXT, country TEXT,
    is_stage_race INTEGER DEFAULT 0, gender TEXT DEFAULT 'men', UNIQUE(pcs_slug));
CREATE TABLE stages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, race_id INTEGER NOT NULL REFERENCES races(id),
    stage_num INTEGER, pcs_slug TEXT UNIQUE NOT NULL, date TEXT, distance_km REAL,
    elevation_m INTEGER, profile_type TEXT, surface TEXT DEFAULT 'road', departure TEXT,
    arrival TEXT, gpx_path TEXT);
CREATE TABLE results (
    id INTEGER PRIMARY KEY AUTOINCREMENT, stage_id INTEGER NOT NULL REFERENCES stages(id),
    rider_id INTEGER NOT NULL REFERENCES riders(id), position INTEGER,
    status TEXT DEFAULT 'finished', time_seconds INTEGER, points_pcs INTEGER,
    points_uci INTEGER, bib INTEGER, UNIQUE(stage_id, rider_id));
INSERT INTO riders (pcs_slug, name) VALUES ('tadej-pogacar', 'Pogačar Tadej');
INSERT INTO races (pcs_slug, name, year, start_date, end_date, class, is_stage_race)
    VALUES ('race/ronde-van-vlaanderen/2026', 'Ronde van Vlaanderen', 2026, '2026-04-05', '2026-04-05', '1.UWT', 0);
INSERT INTO stages (race_id, pcs_slug, date, distance_km)
    VALUES (1, 'race/ronde-van-vlaanderen/2026', '2026-04-05', 272.0);
INSERT INTO results (stage_id, rider_id, position, status, time_seconds, points_pcs, points_uci, bib)
    VALUES (1, 1, 1, 'finished', 0, 275, 80025, 1);
"""


def _cols(path, table):
    with sqlite3.connect(path) as conn:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


@pytest.fixture
def old_db(tmp_path):
    path = str(tmp_path / "old.db")
    with sqlite3.connect(path) as conn:
        conn.executescript(OLD_SCHEMA)
    return path


@pytest.fixture
def new_db(tmp_path):
    path = str(tmp_path / "new.db")
    init_db(path)
    return path


def _conn(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


# ── migration ────────────────────────────────────────────────────────────────

def test_upgrade_old_schema_adds_columns_and_keeps_data(old_db):
    assert "team_slug" not in _cols(old_db, "results")
    init_db(old_db)
    init_db(old_db)                       # idempotent
    cols = _cols(old_db, "results")
    assert {"team_slug", "team_name", "uci_penalty"} <= set(cols)
    assert {"gradient_final_km", "profile_score"} <= set(_cols(old_db, "stages"))
    with _conn(old_db) as conn:
        row = conn.execute("SELECT * FROM results").fetchone()
    assert (row["points_uci"], row["points_pcs"], row["bib"]) == (80025, 275, 1)
    assert row["team_slug"] is None and row["uci_penalty"] is None


def test_fresh_schema_has_columns(new_db):
    assert {"team_slug", "team_name", "uci_penalty"} <= set(_cols(new_db, "results"))


def test_insert_result_on_upgraded_db_and_coalesce(old_db):
    init_db(old_db)
    with _conn(old_db) as conn:
        # Re-scrape with the fixed parser: points split, team known.
        insert_result(conn, {"stage_id": 1, "rider_id": 1, "position": 1, "status": "finished",
                             "time_seconds": 0, "points_pcs": 275, "points_uci": 800, "bib": 1,
                             "team_slug": "uae-team-emirates-xrg-2026",
                             "team_name": "UAE Team Emirates - XRG", "uci_penalty": 25})
        # A later write without team data keeps the stored team + penalty.
        insert_result(conn, {"stage_id": 1, "rider_id": 1, "position": 1, "status": "finished",
                             "time_seconds": 0, "points_pcs": 275, "points_uci": 800, "bib": 1})
        row = conn.execute("SELECT * FROM results").fetchone()
    assert (row["points_uci"], row["uci_penalty"]) == (800, 25)
    assert (row["team_slug"], row["team_name"]) == ("uae-team-emirates-xrg-2026", "UAE Team Emirates - XRG")


def test_fractional_ttt_points_are_kept(new_db):
    with _conn(new_db) as conn:
        conn.execute("INSERT INTO riders (pcs_slug, name) VALUES ('jonas-vingegaard', 'Vingegaard Jonas')")
        conn.execute("INSERT INTO races (pcs_slug, name, year) VALUES ('race/tdf/2026', 'TdF', 2026)")
        sid = upsert_stage(conn, {"race_id": 1, "stage_num": 1, "pcs_slug": "race/tdf/2026/stage-1",
                                  "date": "2026-07-04", "distance_km": 19.6, "elevation_m": None,
                                  "profile_type": "ttt", "surface": "road", "departure": None,
                                  "arrival": None, "gpx_path": None})
        insert_result(conn, {"stage_id": sid, "rider_id": 1, "position": 1, "status": "finished",
                             "time_seconds": 0, "points_pcs": 50, "points_uci": 26.25, "bib": None})
        assert conn.execute("SELECT points_uci FROM results").fetchone()[0] == 26.25


def test_fill_result_teams_touches_only_null_team_fields(old_db):
    init_db(old_db)
    with _conn(old_db) as conn:
        n = fill_result_teams(conn, 1, 1, "uae-team-emirates-xrg-2026", "UAE Team Emirates - XRG", 25)
        assert n == 1
        assert fill_result_teams(conn, 1, 1, "other-2026", "Other", 50) == 0   # already filled
        row = conn.execute("SELECT * FROM results").fetchone()
    assert (row["team_slug"], row["uci_penalty"]) == ("uae-team-emirates-xrg-2026", 25)
    assert (row["points_uci"], row["position"], row["time_seconds"]) == (80025, 1, 0)  # untouched


def test_upsert_stage_never_erases_known_values(old_db):
    init_db(old_db)
    with _conn(old_db) as conn:
        upsert_stage(conn, {"race_id": 1, "stage_num": None, "pcs_slug": "race/ronde-van-vlaanderen/2026",
                            "date": None, "distance_km": None, "elevation_m": None,
                            "profile_type": "hilly", "surface": "cobbled", "departure": None,
                            "arrival": None, "gpx_path": None})
        row = conn.execute("SELECT * FROM stages").fetchone()
    assert (row["date"], row["distance_km"], row["profile_type"]) == ("2026-04-05", 272.0, "hilly")


def test_fill_stage_meta_only_fills_nulls(old_db):
    init_db(old_db)
    with _conn(old_db) as conn:
        fill_stage_meta(conn, 1, {"date": "1999-01-01", "distance_km": 1.0, "elevation_m": 2187,
                                  "profile_type": "hilly", "profile_score": 260, "gradient_final_km": 0.4,
                                  "departure": "Antwerpen", "arrival": "Oudenaarde"})
        row = conn.execute("SELECT * FROM stages").fetchone()
    assert (row["date"], row["distance_km"]) == ("2026-04-05", 272.0)          # kept
    assert (row["elevation_m"], row["profile_score"], row["arrival"]) == (2187, 260, "Oudenaarde")


# ── UCI points anomalies ─────────────────────────────────────────────────────

@pytest.mark.parametrize("rows,ttt,expected", [
    ([(1, "finished", 800), (2, "finished", 640), (3, "finished", None)], False, set()),
    ([(1, "finished", 80025), (2, "finished", 640)], False, {"above_cap"}),             # '800 -25'
    ([(1, "finished", 210), (12, "finished", 20), (13, "finished", 1525)], False,
     {"above_cap", "non_monotonic"}),                                                     # '15 -25'
    ([(1, "finished", 60), (2, "finished", 40), (11, "finished", None), (140, "finished", 25)], False,
     {"after_gap"}),                                                                      # bare '-25'
    ([(1, "finished", 250), (None, "dnf", 10)], False, {"non_finisher"}),
    ([(1, "finished", 13.75), (2, "finished", 26.25)], True, set()),                      # TTT shares
    ([(13, "finished", 10), (14, "finished", 10), (15, "finished", 6)], False, set()),    # ties are fine
    # Paris-Nice 2026 stage 3 (TTT) stored from the GC tab: one leader with 10, no shares
    ([(1, "finished", 10)] + [(p, "finished", None) for p in range(2, 30)], True, {"ttt_shares_missing"}),
    ([(p, "finished", 8.57) for p in range(1, 8)] + [(p, "finished", None) for p in range(8, 30)],
     True, set()),
    # La Vuelta Femenina 2023 stage 1 (a TTT stored as 'flat' from the GC tab):
    # only the leader's 8 points among ~150 finishers
    ([(1, "finished", 8)] + [(p, "finished", None) for p in range(2, 30)], False, {"single_scorer"}),
    ([(1, "finished", 8)] + [(p, "finished", None) for p in range(2, 20)], False, set()),  # small field
    ([(p, "finished", None) for p in range(1, 30)], False, set()),        # neutralised stage
])
def test_uci_points_anomalies(rows, ttt, expected):
    assert uci_points_anomalies(rows, is_ttt=ttt) == expected


def _seed_season(path):
    """Two races: a clean one and the Ronde with the corrupted '800 -25' row."""
    with _conn(path) as conn:
        for slug in ("tadej-pogacar", "mathieu-van-der-poel", "gianni-moscon", "edward-planckaert"):
            conn.execute("INSERT INTO riders (pcs_slug, name) VALUES (?, ?)", (slug, slug))
        conn.execute("""INSERT INTO races (pcs_slug, name, year, start_date, end_date, class, is_stage_race, gender)
                        VALUES ('race/ronde-van-vlaanderen/2026', 'Ronde', 2026, '2026-04-05', '2026-04-05', '1.UWT', 0, 'men'),
                               ('race/gp-clean/2026', 'GP Clean', 2026, '2026-04-12', '2026-04-12', '1.1', 0, 'men'),
                               ('race/women-race/2026', 'Women Race', 2026, '2026-04-12', '2026-04-12', '1.WWT', 0, 'women'),
                               ('race/stage-race/2026', 'Stage Race', 2026, '2026-05-01', '2026-05-02', '2.1', 1, 'men')""")
        conn.execute("""INSERT INTO stages (race_id, pcs_slug, date) VALUES
                        (1, 'race/ronde-van-vlaanderen/2026', '2026-04-05'),
                        (2, 'race/gp-clean/2026', '2026-04-12'),
                        (3, 'race/women-race/2026', '2026-04-12'),
                        (4, 'race/stage-race/2026/stage-1', '2026-05-01'),
                        (4, 'race/stage-race/2026/stage-2', '2026-05-02')""")
        rows = [
            (1, 1, 1, "finished", 0, 275, 80025),       # Pogačar: '800 -25' glued
            (1, 2, 2, "finished", 34, 200, 640),
            (1, 3, 140, "finished", 600, None, 25),     # Moscon: bare '-25' stored as points
            (2, 2, 1, "finished", 0, 125, 125),
            (4, 1, 1, "finished", 0, 50, 14),           # stage 1 scraped, stage 2 missing
        ]
        conn.executemany("""INSERT INTO results (stage_id, rider_id, position, status, time_seconds,
                            points_pcs, points_uci) VALUES (?, ?, ?, ?, ?, ?, ?)""", rows)


RONDE_PARSED = [
    {"rider_slug": "tadej-pogacar", "points_uci": 800, "uci_penalty": 25,
     "team_slug": "uae-team-emirates-xrg-2026", "team_name": "UAE Team Emirates - XRG"},
    {"rider_slug": "mathieu-van-der-poel", "points_uci": 640, "uci_penalty": None,
     "team_slug": "alpecin-premier-tech-2026", "team_name": "Alpecin - Premier Tech"},
    {"rider_slug": "gianni-moscon", "points_uci": None, "uci_penalty": 25,
     "team_slug": "red-bull-bora-hansgrohe-2026", "team_name": "Red Bull - BORA - hansgrohe"},
    {"rider_slug": "somebody-new", "points_uci": None, "uci_penalty": None,
     "team_slug": "x-2026", "team_name": "X"},
]


def test_find_and_repair_corrupted_stage(new_db):
    _seed_season(new_db)
    with _conn(new_db) as conn:
        suspects = find_implausible_uci_stages(conn, 2026)
        assert [s["pcs_slug"] for s in suspects] == ["race/ronde-van-vlaanderen/2026"]
        assert "above_cap" in suspects[0]["reasons"]

        dry = repair_stage_points(conn, suspects[0]["stage_id"], RONDE_PARSED, dry_run=True)
        assert len(dry["changed"]) == 2
        assert conn.execute("SELECT MAX(points_uci) FROM results").fetchone()[0] == 80025  # untouched

        rep = repair_stage_points(conn, suspects[0]["stage_id"], RONDE_PARSED)
        assert sorted(rep["changed"]) == [("gianni-moscon", 25, None, None, 25),
                                          ("tadej-pogacar", 80025, 800, None, 25)]
        assert rep["teams_filled"] == 3 and rep["unmatched_page"] == 1 and rep["unmatched_db"] == []
        got = {r[0]: tuple(r[1:]) for r in conn.execute(
            """SELECT rd.pcs_slug, x.points_uci, x.uci_penalty, x.team_slug, x.position
               FROM results x JOIN riders rd ON rd.id = x.rider_id WHERE x.stage_id = 1""")}
        assert got["tadej-pogacar"] == (800, 25, "uae-team-emirates-xrg-2026", 1)
        assert got["gianni-moscon"] == (None, 25, "red-bull-bora-hansgrohe-2026", 140)
        assert find_implausible_uci_stages(conn, 2026) == []


# ── CLI flows with fake fetchers ─────────────────────────────────────────────

@pytest.fixture
def cli_db(tmp_path, monkeypatch):
    path = str(tmp_path / "cli.db")
    init_db(path)
    _seed_season(path)

    @contextmanager
    def conn_cm(db_path=path):
        with database.get_conn(path) as conn:
            yield conn

    monkeypatch.setattr(main, "get_conn", conn_cm)
    monkeypatch.setattr(main, "init_db", lambda *a, **k: init_db(path))
    # Never touch the network / HTML cache from tests: every page "exists",
    # and no race is renamed unless a test says so.
    monkeypatch.setattr(main, "fetch", lambda url, use_cache=True: "<html><body>page</body></html>")
    monkeypatch.setattr(main, "find_race_alias", lambda *a, **k: None)
    monkeypatch.setattr(main, "_ALIASES", {})
    return path


def _row(slug, pos, pts=None, pen=None, team="team-a-2026"):
    return {"rider_slug": slug, "rider_name": slug, "position": pos, "status": "finished",
            "time_seconds": 0, "points_pcs": None, "points_uci": pts, "uci_penalty": pen,
            "bib": None, "team_slug": team, "team_name": team.upper()}


def test_scrape_missing_picks_up_partial_races(cli_db, monkeypatch):
    stage_lists = {
        "race/stage-race/2026": [
            {"pcs_slug": "race/stage-race/2026/stage-1", "stage_num": 1, "date": "2026-05-01",
             "distance_km": None, "elevation_m": None, "profile_type": "flat", "surface": "road",
             "departure": None, "arrival": None, "gpx_path": None},
            {"pcs_slug": "race/stage-race/2026/stage-2", "stage_num": 2, "date": "2026-05-02",
             "distance_km": None, "elevation_m": None, "profile_type": "hilly", "surface": "road",
             "departure": None, "arrival": None, "gpx_path": None}],
    }
    fetched = []
    monkeypatch.setattr(main, "fetch_race_stages", lambda slug: stage_lists.get(slug, []))
    monkeypatch.setattr(main, "fetch_stage_meta", lambda slug: {"distance_km": 150.5, "elevation_m": 1200})

    def fake_results(slug):
        fetched.append(slug)
        return [_row("tadej-pogacar", 1, 14), _row("new-rider", 2, 5, team="team-b-2026")]
    monkeypatch.setattr(main, "fetch_stage_results", fake_results)

    args = argparse.Namespace(year=2026, gender="men", races=None, min_results=1, backfill_teams=False)
    main.cmd_scrape_missing(args)

    # Only the missing stage of the partially scraped race is fetched; the
    # women's race (gender filter) and complete races are left alone.
    assert fetched == ["race/stage-race/2026/stage-2"]
    with _conn(cli_db) as conn:
        rows = conn.execute("""SELECT rd.pcs_slug, x.team_slug, x.points_uci FROM results x
                               JOIN riders rd ON rd.id = x.rider_id JOIN stages s ON s.id = x.stage_id
                               WHERE s.pcs_slug = 'race/stage-race/2026/stage-2' ORDER BY x.position""").fetchall()
        assert [tuple(r) for r in rows] == [("tadej-pogacar", "team-a-2026", 14), ("new-rider", "team-b-2026", 5)]
        stage = conn.execute("SELECT distance_km, elevation_m FROM stages WHERE pcs_slug = ?",
                             ("race/stage-race/2026/stage-2",)).fetchone()
        assert tuple(stage) == (150.5, 1200)
        # existing rider profile row is not overwritten by a stub
        assert conn.execute("SELECT name FROM riders WHERE pcs_slug = 'tadej-pogacar'").fetchone()[0] == "tadej-pogacar"
        assert conn.execute("SELECT COUNT(*) FROM stages").fetchone()[0] == 5


def test_scrape_missing_backfill_teams_only_fills_team_columns(cli_db, monkeypatch):
    monkeypatch.setattr(main, "fetch_race_stages", lambda slug: [])
    pages = {"race/gp-clean/2026": [_row("mathieu-van-der-poel", 1, 999, 25, team="alpecin-premier-tech-2026")]}
    monkeypatch.setattr(main, "fetch_stage_results", lambda slug: pages.get(slug, []))
    args = argparse.Namespace(year=2026, gender=None, races="race/gp-clean/2026", min_results=1,
                              backfill_teams=True)
    main.cmd_scrape_missing(args)
    with _conn(cli_db) as conn:
        row = conn.execute("SELECT * FROM results WHERE stage_id = 2").fetchone()
        assert (row["team_slug"], row["team_name"], row["uci_penalty"]) == (
            "alpecin-premier-tech-2026", "ALPECIN-PREMIER-TECH-2026", 25)
        assert row["points_uci"] == 125          # other columns untouched
        # races outside --races were not touched
        assert conn.execute("SELECT team_slug FROM results WHERE stage_id = 1 AND rider_id = 1").fetchone()[0] is None


def test_repair_points_command(cli_db, monkeypatch):
    calls = []

    positions = {"tadej-pogacar": 1, "mathieu-van-der-poel": 2, "gianni-moscon": 140, "somebody-new": 150}

    def fake_results(slug):
        calls.append(slug)
        return [dict(_row(r["rider_slug"], positions[r["rider_slug"]]), **r) for r in RONDE_PARSED]
    monkeypatch.setattr(main, "fetch_stage_results", fake_results)
    main.cmd_repair_points(argparse.Namespace(year=2026, gender=None, races=None, dry_run=False))
    assert calls == ["race/ronde-van-vlaanderen/2026"]
    with _conn(cli_db) as conn:
        assert conn.execute("SELECT points_uci, uci_penalty FROM results WHERE stage_id = 1 AND rider_id = 1"
                            ).fetchone()[:] == (800, 25)
    calls.clear()
    main.cmd_repair_points(argparse.Namespace(year=2026, gender=None, races=None, dry_run=False))
    assert calls == []                      # nothing left to repair


def test_repair_points_stores_ttt_shares_for_a_gc_tab_stage(cli_db, monkeypatch):
    # A team time trial stored from the GC tab under a road profile: one
    # 8-point leader among the finishers.  repair-points flags it
    # (single_scorer), takes the TTT page's shares and positions, and marks
    # the stage 'ttt'; a DNS on the TTT page loses his finish.
    riders = [f"rider-{i:02d}" for i in range(1, 26)]
    with _conn(cli_db) as conn:
        conn.executemany("INSERT INTO riders (pcs_slug, name) VALUES (?, ?)", [(r, r) for r in riders])
        conn.execute("""INSERT INTO races (pcs_slug, name, year, start_date, end_date, class, is_stage_race,
                        gender) VALUES ('race/ttt-race/2026', 'TTT race', 2026, '2026-06-01', '2026-06-03',
                        '2.WWT', 1, 'women')""")
        race_id = conn.execute("SELECT id FROM races WHERE pcs_slug = 'race/ttt-race/2026'").fetchone()[0]
        conn.execute("INSERT INTO stages (race_id, pcs_slug, date, profile_type) VALUES (?, ?, ?, 'flat')",
                     (race_id, "race/ttt-race/2026/stage-1", "2026-06-01"))
        stage_id = conn.execute("SELECT id FROM stages WHERE pcs_slug = 'race/ttt-race/2026/stage-1'"
                                ).fetchone()[0]
        for i, r in enumerate(riders, 1):
            conn.execute("""INSERT INTO results (stage_id, rider_id, position, status, time_seconds, points_uci)
                            SELECT ?, id, ?, 'finished', 0, ? FROM riders WHERE pcs_slug = ?""",
                         (stage_id, i, 8 if i == 1 else None, r))
        conn.commit()

    def fake_results(slug):
        assert slug == "race/ttt-race/2026/stage-1"
        rows = []
        for i, r in enumerate(riders, 1):
            row = dict(_row(r, i, 7.14 if i <= 7 else 0), team_time_trial=True, time_seconds=i)
            if r == "rider-25":
                row.update(position=None, status="dns", time_seconds=None)
            rows.append(row)
        return rows
    monkeypatch.setattr(main, "fetch_stage_results", fake_results)
    with _conn(cli_db) as conn:
        flagged = {s["pcs_slug"]: s["reasons"] for s in find_implausible_uci_stages(conn, 2026)}
    assert flagged["race/ttt-race/2026/stage-1"] == ["single_scorer"]
    main.cmd_repair_points(argparse.Namespace(year=2026, gender="women", races=None, dry_run=False))
    with _conn(cli_db) as conn:
        assert conn.execute("SELECT profile_type FROM stages WHERE id = ?", (stage_id,)).fetchone()[0] == "ttt"
        got = conn.execute("""SELECT COUNT(*), SUM(points_uci = 7.14) FROM results WHERE stage_id = ?""",
                           (stage_id,)).fetchone()
        assert tuple(got) == (25, 7)
        dns = conn.execute("""SELECT position, status, time_seconds FROM results x JOIN riders rd
                              ON rd.id = x.rider_id WHERE x.stage_id = ? AND rd.pcs_slug = 'rider-25'""",
                           (stage_id,)).fetchone()
        assert tuple(dns) == (None, "dns", None)
        assert find_implausible_uci_stages(conn, 2026, "women") == []


def test_init_db_creates_rider_alias_table(new_db):
    with _conn(new_db) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(rider_slug_aliases)")]
    assert cols == ["old_slug", "new_slug", "evidence", "merged_at", "valid_until"]
    init_db(new_db)                                  # idempotent


def _stage(slug, date=None):
    return {"pcs_slug": slug, "stage_num": None, "date": date, "distance_km": None,
            "elevation_m": None, "profile_type": None, "surface": "road", "departure": None,
            "arrival": None, "gpx_path": None}


@pytest.mark.parametrize("fetched", [
    [],                                                             # PCS gave nothing
    [_stage("race/stage-race/2026")],                               # stage race parsed as one-day
    [_stage("race/stage-race-2026-stage-1"), _stage("race/stage-race-2026-stage-2")],  # foreign slug format
])
def test_stage_list_falls_back_to_stored_stages(cli_db, fetched):
    stages = main._checked_stage_list(4, "race/stage-race/2026", fetched)
    assert [s["pcs_slug"] for s in stages] == ["race/stage-race/2026/stage-1", "race/stage-race/2026/stage-2"]


def test_stage_list_drops_dates_outside_race_window(cli_db):
    stages = main._checked_stage_list(4, "race/stage-race/2026", [
        _stage("race/stage-race/2026/stage-1", "2026-05-01"),
        _stage("race/stage-race/2026/stage-2", "2026-02-05")])     # misread day/month
    assert [s["date"] for s in stages] == ["2026-05-01", None]


def test_renamed_race_is_fetched_from_alias_into_stored_stages(cli_db, monkeypatch):
    # race/stage-race/2026 was renamed on PCS to race/new-name/2026; its stored
    # stages must be filled from the new URLs, without creating new stage rows.
    empty = "<html><head></head><body></body></html>"
    monkeypatch.setattr(main, "fetch", lambda url, use_cache=True: empty if "stage-race" in url else "<html><body>x</body></html>")
    monkeypatch.setattr(main, "find_race_alias", lambda slug, *a, **k: "race/new-name/2026")
    monkeypatch.setattr(main, "_ALIASES", {})

    def fake_stages(slug):
        if slug != "race/new-name/2026":
            return []
        return [_stage("race/new-name/2026/stage-1", "2026-05-01"), _stage("race/new-name/2026/stage-2", "2026-05-02")]
    fetched = []

    def fake_results(slug):
        fetched.append(slug)
        return [_row("tadej-pogacar", 1, 14)]
    monkeypatch.setattr(main, "fetch_race_stages", fake_stages)
    monkeypatch.setattr(main, "fetch_stage_results", fake_results)
    monkeypatch.setattr(main, "fetch_stage_meta", lambda slug: {})

    main._scrape_race_results(4, "race/stage-race/2026", skip_riders=True)
    assert fetched == ["race/new-name/2026/stage-2"]            # stage-1 already had results
    with _conn(cli_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM stages").fetchone()[0] == 5
        n = conn.execute("""SELECT COUNT(*) FROM results x JOIN stages s ON s.id = x.stage_id
                            WHERE s.pcs_slug = 'race/stage-race/2026/stage-2'""").fetchone()[0]
        assert n == 1
    assert main._fetch_slug("race/stage-race/2026/stage-1", 4, "race/stage-race/2026") == "race/new-name/2026/stage-1"


def test_fill_stage_meta_overwrite_takes_page_values_but_never_erases(old_db):
    init_db(old_db)
    with _conn(old_db) as conn:
        fill_stage_meta(conn, 1, {"date": "2026-04-05", "distance_km": 260.5, "elevation_m": None,
                                  "profile_type": "hilly", "arrival": "Oudenaarde"}, overwrite=True)
        row = conn.execute("SELECT * FROM stages").fetchone()
    assert (row["distance_km"], row["profile_type"], row["arrival"]) == (260.5, "hilly", "Oudenaarde")
    assert row["date"] == "2026-04-05"


def test_scraped_stage_meta_overwrites_provisional_route(cli_db, monkeypatch):
    monkeypatch.setattr(main, "fetch_race_stages", lambda slug: [])
    monkeypatch.setattr(main, "fetch_stage_results", lambda slug: [_row("tadej-pogacar", 1, 14)])
    # The page says the stage was shortened; its date is bogus (outside the race window).
    monkeypatch.setattr(main, "fetch_stage_meta",
                        lambda slug: {"distance_km": 88.7, "departure": "Paris", "date": "2026-12-31"})
    with _conn(cli_db) as conn:
        conn.execute("UPDATE stages SET distance_km = 130, departure = 'Thoiry' WHERE id = 5")
    main._scrape_race_results(4, "race/stage-race/2026", skip_riders=True)
    with _conn(cli_db) as conn:
        row = conn.execute("SELECT distance_km, departure, date FROM stages WHERE id = 5").fetchone()
    assert tuple(row) == (88.7, "Paris", "2026-05-02")


def test_repair_refreshes_positions_with_points(new_db):
    # Amstel Gold Race Women 2026: PCS swapped 20th/21st after the first
    # scrape.  Copying only the points onto the stale order made the pair
    # non-monotonic; the repair now takes the page's positions too.
    with _conn(new_db) as conn:
        conn.execute("INSERT INTO riders (pcs_slug, name) VALUES ('fisher-black', 'F'), ('chabbey', 'C')")
        conn.execute("""INSERT INTO races (pcs_slug, name, year, start_date, end_date, class, is_stage_race, gender)
                        VALUES ('race/amstel-gold-race-we/2026', 'Amstel', 2026, '2026-04-19', '2026-04-19',
                                '1.WWT', 0, 'women')""")
        conn.execute("INSERT INTO stages (race_id, pcs_slug, date) VALUES (1, 'race/amstel-gold-race-we/2026', '2026-04-19')")
        conn.executemany("""INSERT INTO results (stage_id, rider_id, position, status, time_seconds, points_uci)
                            VALUES (1, ?, ?, 'finished', ?, ?)""", [(1, 20, 5, 30), (2, 21, 5, 20)])
        page = [_row("chabbey", 20, 30), _row("fisher-black", 21, 20)]
        for r in page:
            r["time_seconds"] = 7
        rep = repair_stage_points(conn, 1, page)
        assert sorted(rep["moved"]) == [("chabbey", 21, 20, "finished", "finished"),
                                        ("fisher-black", 20, 21, "finished", "finished")]
        assert rep["retimed"] == 2
        got = conn.execute("""SELECT rd.pcs_slug, x.position, x.points_uci, x.time_seconds FROM results x
                              JOIN riders rd ON rd.id = x.rider_id ORDER BY x.position""").fetchall()
        assert [tuple(r) for r in got] == [("chabbey", 20, 30, 7), ("fisher-black", 21, 20, 7)]
        assert find_implausible_uci_stages(conn, 2026) == []


def _seed_refresh(path):
    with _conn(path) as conn:
        for table in ("results", "stages", "races", "riders"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM sqlite_sequence")
        conn.executemany("INSERT INTO riders (pcs_slug, name) VALUES (?, ?)", [
            ("winner", "Winner A"), ("second", "Second B"), ("thomas-pidcock", "Thomas  Pidcock"),
            ("gone", "Gone C"), ("itt-rider", "Itt D")])
        conn.execute("""INSERT INTO races (pcs_slug, name, year, start_date, end_date, class, is_stage_race, gender)
                        VALUES ('race/r/2026', 'R', 2026, '2026-03-01', '2026-03-02', '2.1', 1, 'men')""")
        conn.executemany("INSERT INTO stages (race_id, pcs_slug, date, profile_type) VALUES (1, ?, ?, ?)", [
            ("race/r/2026/stage-1", "2026-03-01", "itt"), ("race/r/2026/stage-2", "2026-03-02", "flat")])
        conn.executemany("""INSERT INTO results (stage_id, rider_id, position, status, time_seconds, points_uci)
                            VALUES (?, ?, ?, ?, ?, ?)""", [
            (1, 1, 1, "finished", 0, 20), (1, 2, 2, "finished", None, 10), (1, 5, 3, "finished", None, 5),
            (2, 1, 1, "finished", 0, 20), (2, 2, 2, "finished", 0, 10), (2, 3, 3, "finished", 0, 5),
            (2, 4, 4, "finished", 0, None)])
        # rider pages (scoring rows only): stage 1 agrees; stage 2 order was
        # corrected (second/third swapped)
        conn.executemany("""INSERT INTO rider_uci_results (season, rider_slug, date, race_slug, result_label,
                            result_kind, position, status, points_uci, result_url)
                            VALUES (2026, ?, ?, 'race/r/2026', ?, 'stage', ?, 'finished', ?, ?)""", [
            ("winner", "2026-03-01", "Stage 1 (ITT)", 1, 20, "race/r-2026-stage-1"),
            ("second", "2026-03-01", "Stage 1 (ITT)", 2, 10, "race/r-2026-stage-1"),
            ("winner", "2026-03-02", "Stage 2", 1, 20, "race/r-2026-stage-2"),
            ("second", "2026-03-02", "Stage 2", 3, 5, "race/r-2026-stage-2")])


def test_refresh_selectors(cli_db):
    _seed_refresh(cli_db)
    with _conn(cli_db) as conn:
        nulls = find_null_gap_stages(conn, 2026)
        assert [(s["pcs_slug"], s["reasons"]) for s in nulls] == [("race/r/2026/stage-1", ["null_gaps(2)"])]
        mism = find_stages_disagreeing_with_rider_results(conn, 2026)
        assert [(s["pcs_slug"], s["reasons"]) for s in mism] == [
            ("race/r/2026/stage-2", ["positions_differ", "uci_points_differ"])]


def _seed_mismatch(path):
    """Stored stages vs rider pages that still hold their zero-point rows (as
    the scraper stored them before rider_uci_results became scoring-only).

    Riders: a, b scraped (pages through 2026-04-01), c never scraped, d a
    partial scrape (complete = 0), e scraped but his pages end 2026-03-02."""
    with _conn(path) as conn:
        conn.executemany("INSERT INTO riders (pcs_slug, name) VALUES (?, ?)",
                         [(s, s.upper()) for s in ("a", "b", "c", "d", "e")])
        conn.executemany("""INSERT INTO races (pcs_slug, name, year, start_date, end_date, class, is_stage_race,
                            gender) VALUES (?, ?, 2026, ?, ?, '2.1', 1, 'men')""", [
            ("race/r/2026", "R", "2026-03-01", "2026-03-20"), ("race/old-name/2026", "Old", "2026-04-01", "2026-04-01")])
        stages = {1: "2026-03-01", 2: "2026-03-02", 3: "2026-03-03", 4: "2026-03-04", 5: "2026-03-05",
                  6: "2026-03-20"}
        conn.executemany("INSERT INTO stages (race_id, pcs_slug, date, profile_type) VALUES (1, ?, ?, 'flat')",
                         [(f"race/r/2026/stage-{n}", d) for n, d in stages.items()])
        conn.execute("INSERT INTO stages (race_id, pcs_slug, date, profile_type) "
                     "VALUES (2, 'race/old-name/2026/stage-1', '2026-04-01', 'flat')")
        rid = {s: i for i, s in enumerate("abcde", 1)}
        stored = [  # stage id, rider, position, points
            (1, "a", 1, 20), (1, "b", 2, 10), (1, "c", 3, None),
            (2, "a", 1, 20), (2, "b", 2, 10),             # b's page: 2nd, but no points
            (3, "a", 11, None), (3, "b", 12, None),       # order outside the points corrected
            (4, "c", 1, 20), (4, "a", 2, 10), (4, "b", 3, 5),
            (5, "d", 1, 20), (5, "a", 2, 10),             # d's page for stage 5 failed
            (6, "e", 1, 20),                              # after e's pages were fetched
            (7, "a", 1, 10), (7, "b", 2, None),           # renamed race, a's page: 2nd
        ]
        conn.executemany("INSERT INTO results (stage_id, rider_id, position, status, points_uci) "
                         "VALUES (?, ?, ?, 'finished', ?)", [(s, rid[r], p, pts) for s, r, p, pts in stored])
        page = [  # rider, stage, position, points
            ("a", 1, 1, 20), ("b", 1, 2, 10), ("a", 2, 1, 20), ("b", 2, 2, None),
            ("a", 3, 12, None), ("b", 3, 11, None), ("a", 4, 2, 10), ("b", 4, 3, 5), ("a", 5, 2, 10),
            ("d", 1, 9, None), ("e", 1, 7, None), ("e", 2, 8, None)]
        rows = [(r, stages[n], "race/r/2026", f"Stage {n}", p, pts, f"race/r-2026-stage-{n}")
                for r, n, p, pts in page]
        rows += [("a", "2026-04-01", "race/new-name/2026", "Stage 1", 2, 10, "race/new-name-2026-stage-1"),
                 ("b", "2026-04-01", "race/new-name/2026", "Stage 1", 1, None, "race/new-name-2026-stage-1")]
        conn.executemany("""INSERT INTO rider_uci_results (season, rider_slug, rider_name, date, race_slug,
                            result_label, result_kind, position, status, points_uci, result_url)
                            VALUES (2026, ?, 'X', ?, ?, ?, 'stage', ?, 'finished', ?, ?)""", rows)
        conn.execute("""INSERT INTO rider_uci_scrapes (season, rider_slug, rows_seen, scoring_rows, first_date,
                        last_date, complete) VALUES (2026, 'd', 1, 0, '2026-03-01', '2026-03-01', 0)""")


def _mismatches(path):
    with _conn(path) as conn:
        return [(s["pcs_slug"], s["reasons"]) for s in find_stages_disagreeing_with_rider_results(conn, 2026)]


def test_uci_mismatch_same_with_and_without_zero_point_rows(new_db):
    from db.uci_points import prune_zero_point_results
    _seed_mismatch(new_db)
    before = _mismatches(new_db)
    assert before == [("race/r/2026/stage-2", ["uci_points_differ"]),
                      ("race/r/2026/stage-3", ["positions_differ"]),
                      ("race/old-name/2026/stage-1", ["positions_differ"])]
    with _conn(new_db) as conn:
        assert prune_zero_point_results(conn, 2026, penalty_race=lambda *a: (None, None))["deleted"] == 7
    # stage 2: b has no row any more — a scraped rider with no row scored 0;
    # stage 3: only non-scoring places moved, no rows left to compare (accepted);
    # the renamed race is still matched with a single scorer on the page
    assert _mismatches(new_db) == [("race/r/2026/stage-2", ["uci_points_differ"]),
                                   ("race/old-name/2026/stage-1", ["positions_differ"])]


def test_uci_mismatch_missing_row_judged_only_for_covered_riders(new_db):
    from db.uci_points import prune_zero_point_results
    _seed_mismatch(new_db)
    with _conn(new_db) as conn:
        prune_zero_point_results(conn, 2026, penalty_race=lambda *a: (None, None))
        # c (never scraped), d (partial), e (stage after his pages) are not judged:
        # stages 4, 5, 6 are not flagged.  Once e's pages reach stage 6 he is.
        conn.execute("UPDATE rider_uci_scrapes SET last_date = '2026-03-20' WHERE rider_slug = 'e'")
    assert ("race/r/2026/stage-6", ["uci_points_differ"]) in _mismatches(new_db)
    with _conn(new_db) as conn:  # his points are on the page: agreement
        conn.execute("""INSERT INTO rider_uci_results (season, rider_slug, date, race_slug, result_label,
                        result_kind, position, status, points_uci, result_url) VALUES (2026, 'e', '2026-03-20',
                        'race/r/2026', 'Stage 6', 'stage', 1, 'finished', 20, 'race/r-2026-stage-6')""")
        # d's scrape record says partial: even a later stage is not judged
        conn.execute("UPDATE rider_uci_scrapes SET last_date = '2026-12-31' WHERE rider_slug = 'd'")
    assert [s for s, _ in _mismatches(new_db)] == ["race/r/2026/stage-2", "race/old-name/2026/stage-1"]


def test_refresh_stages_command_syncs_rows(cli_db, monkeypatch):
    _seed_refresh(cli_db)
    pages = {
        "race/r/2026/stage-1": [_row("winner", 1, 20), dict(_row("second", 2, 10), time_seconds=28),
                                dict(_row("itt-rider", 3, 5), time_seconds=64)],
        # PCS re-slugged Pidcock, corrected the order, and "gone" is no longer listed
        "race/r/2026/stage-2": [_row("winner", 1, 20), dict(_row("tom-pidcock", 2, 10), rider_name="PIDCOCK Tom"),
                                _row("second", 3, 5), _row("new-rider", 4)],
    }
    fetched = []

    def fake_results(slug):
        fetched.append(slug)
        return [dict(r) for r in pages[slug]]
    monkeypatch.setattr(main, "fetch_stage_results", fake_results)
    # stage 2's page says it was a team time trial; the stored profile was 'flat'
    monkeypatch.setattr(main, "fetch_stage_meta", lambda slug: {"profile_type": "ttt", "distance_km": 23.8}
                        if slug.endswith("stage-2") else {"profile_type": "flat", "distance_km": 30.0})
    main.cmd_refresh_stages(argparse.Namespace(year=2026, gender=None, races=None, null_gaps=True,
                                               uci_mismatch=True, stages=None, dry_run=False))
    assert fetched == ["race/r/2026/stage-1", "race/r/2026/stage-2"]
    with _conn(cli_db) as conn:
        assert [tuple(r) for r in conn.execute("SELECT profile_type, distance_km FROM stages ORDER BY id")] == [
            ("itt", 30.0), ("ttt", 23.8)]   # NULLs filled; only a time-trial profile overrides
    with _conn(cli_db) as conn:
        def rows(stage):
            return [tuple(r) for r in conn.execute(
                """SELECT rd.pcs_slug, x.position, x.time_seconds, x.points_uci FROM results x
                   JOIN riders rd ON rd.id = x.rider_id WHERE x.stage_id = ? ORDER BY x.position""", (stage,))]
        assert rows(1) == [("winner", 1, 0, 20), ("second", 2, 28, 10), ("itt-rider", 3, 64, 5)]
        assert rows(2) == [("winner", 1, 0, 20), ("thomas-pidcock", 2, 0, 10), ("second", 3, 0, 5),
                           ("new-rider", 4, 0, None)]
        assert conn.execute("SELECT COUNT(*) FROM riders WHERE pcs_slug = 'tom-pidcock'").fetchone()[0] == 0
        assert find_null_gap_stages(conn, 2026) == []
        assert find_stages_disagreeing_with_rider_results(conn, 2026) == []


def test_refresh_dup_positions_turns_annulled_finishes_into_dsq(cli_db, monkeypatch):
    """Giro 2023 stage 2: Baudin's struck 157th was stored as a finish next to
    Kirsch's 157th.  The selector finds such stages (DB only); the refresh
    stores the annulled row as dsq.  A tie PCS prints itself stays."""
    from db.database import find_duplicate_position_stages
    _seed_refresh(cli_db)
    with _conn(cli_db) as conn:
        conn.execute("UPDATE results SET position = 2 WHERE stage_id = 2 AND rider_id = 3")   # annulled
        conn.execute("UPDATE results SET position = 2, time_seconds = 7 WHERE stage_id = 1 AND rider_id = 5")
        conn.execute("UPDATE results SET time_seconds = 7 WHERE stage_id = 1 AND rider_id = 2")   # ITT tie
        conn.execute("INSERT INTO stages (race_id, pcs_slug, date, profile_type) "
                     "VALUES (1, 'race/r/2026/stage-3', '2026-03-03', 'ttt')")
        conn.executemany("INSERT INTO results (stage_id, rider_id, position, status) VALUES (3, ?, 1, 'finished')",
                         [(1,), (2,)])   # a team time trial: never selected
        conn.commit()
        assert [(s["pcs_slug"], s["reasons"]) for s in find_duplicate_position_stages(conn, 2026)] == [
            ("race/r/2026/stage-1", ["duplicate_positions(1)"]), ("race/r/2026/stage-2", ["duplicate_positions(1)"])]
    pages = {"race/r/2026/stage-1": [_row("winner", 1, 20), dict(_row("second", 2, 10), time_seconds=7),
                                     dict(_row("itt-rider", 2, 5), time_seconds=7)],
             "race/r/2026/stage-2": [_row("winner", 1, 20), _row("second", 2, 10),
                                     dict(_row("thomas-pidcock", None), status="dsq", time_seconds=None),
                                     _row("gone", 3)]}
    monkeypatch.setattr(main, "fetch_stage_results", lambda slug: [dict(r) for r in pages[slug]])
    monkeypatch.setattr(main, "fetch_stage_meta", lambda slug: {})
    main.cmd_refresh_stages(argparse.Namespace(year=2026, gender=None, races=None, null_gaps=False,
                                               uci_mismatch=False, dup_positions=True, stages=None, dry_run=False))
    with _conn(cli_db) as conn:
        got = conn.execute("""SELECT rd.pcs_slug, x.position, x.status FROM results x JOIN riders rd
                              ON rd.id = x.rider_id WHERE x.stage_id = 2 ORDER BY rd.id""").fetchall()
        assert [tuple(r) for r in got] == [("winner", 1, "finished"), ("second", 2, "finished"),
                                           ("thomas-pidcock", None, "dsq"), ("gone", 3, "finished")]
        # only the tie PCS prints itself is left
        assert [s["pcs_slug"] for s in find_duplicate_position_stages(conn, 2026)] == ["race/r/2026/stage-1"]


def _seed_gender(path):
    """Tour El Salvador stored as 'men' (calendar circuit 16, no marker in the
    name): its riders ride women's races elsewhere."""
    with _conn(path) as conn:
        conn.execute("""INSERT INTO races (pcs_slug, name, year, start_date, end_date, class, is_stage_race, gender)
                        VALUES ('race/vuelta-el-salvador/2026', 'Tour El Salvador', 2026, '2026-01-17',
                                '2026-01-21', '2.1', 1, 'men')""")
        sal = conn.execute("SELECT id FROM races WHERE pcs_slug = 'race/vuelta-el-salvador/2026'").fetchone()[0]
        conn.execute("INSERT INTO stages (race_id, pcs_slug, date) VALUES (?, 'race/vuelta-el-salvador/2026/stage-1', "
                     "'2026-01-17')", (sal,))
        st = conn.execute("SELECT id FROM stages WHERE race_id = ?", (sal,)).fetchone()[0]
        women = conn.execute("SELECT id FROM stages WHERE pcs_slug = 'race/women-race/2026'").fetchone()[0]
        for i in range(6):
            conn.execute("INSERT INTO riders (pcs_slug, name) VALUES (?, ?)", (f"w{i}", f"W {i}"))
            rid = conn.execute("SELECT id FROM riders WHERE pcs_slug = ?", (f"w{i}",)).fetchone()[0]
            conn.execute("INSERT INTO results (stage_id, rider_id, position, status) VALUES (?, ?, ?, 'finished')",
                         (st, rid, i + 1))
            if i < 4:   # four of six also rode a women's race; two locals did not
                conn.execute("INSERT INTO results (stage_id, rider_id, position, status) "
                             "VALUES (?, ?, ?, 'finished')", (women, rid, i + 1))
        conn.commit()
    return sal


def test_race_gender_suspects_and_correction(cli_db, monkeypatch):
    from db.database import find_race_gender_suspects
    sal = _seed_gender(cli_db)
    with _conn(cli_db) as conn:
        got = find_race_gender_suspects(conn, 2026)
    assert [(r["pcs_slug"], r["gender"], r["other_gender_riders"], r["riders"]) for r in got] == [
        ("race/vuelta-el-salvador/2026", "men", 4, 6)]   # the men's races of the seed are not suspects
    pages = []
    monkeypatch.setattr(main, "fetch_stage_meta", lambda slug: pages.append(slug) or {"race_category": "WE"})
    main.main(["race-gender", "--year", "2026", "--dry-run"])
    with _conn(cli_db) as conn:
        assert conn.execute("SELECT gender FROM races WHERE id = ?", (sal,)).fetchone()[0] == "men"
    main.main(["race-gender", "--year", "2026"])
    assert pages[-1] == "race/vuelta-el-salvador/2026/stage-1"
    with _conn(cli_db) as conn:
        assert conn.execute("SELECT gender FROM races WHERE id = ?", (sal,)).fetchone()[0] == "women"
        assert find_race_gender_suspects(conn, 2026) == []
    # a men's race whose page says ME is left alone, even with --all
    monkeypatch.setattr(main, "fetch_stage_meta", lambda slug: {"race_category": "ME"}
                        if "women" not in slug and "salvador" not in slug else {"race_category": "WE"})
    main.main(["race-gender", "--year", "2026", "--all"])
    with _conn(cli_db) as conn:
        assert dict(conn.execute("SELECT pcs_slug, gender FROM races").fetchall())["race/gp-clean/2026"] == "men"


def test_scrape_sets_race_gender_from_the_page(cli_db, monkeypatch):
    sal = _seed_gender(cli_db)
    with _conn(cli_db) as conn:
        conn.execute("INSERT INTO stages (race_id, pcs_slug, date) VALUES (?, 'race/vuelta-el-salvador/2026/stage-2', "
                     "'2026-01-18')", (sal,))
    stage = {"pcs_slug": "race/vuelta-el-salvador/2026/stage-2", "stage_num": 2, "date": "2026-01-18",
             "distance_km": None, "elevation_m": None, "profile_type": "flat", "surface": "road",
             "departure": None, "arrival": None, "gpx_path": None}
    monkeypatch.setattr(main, "fetch_race_stages", lambda slug: [stage])
    monkeypatch.setattr(main, "fetch_stage_results", lambda slug: [_row("w0", 1)])
    monkeypatch.setattr(main, "fetch_stage_meta", lambda slug: {"race_category": "WE", "distance_km": 120.0})
    main._scrape_race_results(sal, "race/vuelta-el-salvador/2026", skip_riders=True)
    with _conn(cli_db) as conn:
        assert conn.execute("SELECT gender FROM races WHERE id = ?", (sal,)).fetchone()[0] == "women"


def test_rescrape_keeps_the_gender_the_page_set(cli_db, monkeypatch):
    """A second `scrape` skips the stages that have results, so it never reads
    the page again: the calendar row's guess ('men', no marker in 'Tour El
    Salvador') must not overwrite the gender the first run took from it."""
    sal = _seed_gender(cli_db)
    calendar_row = {"pcs_slug": "race/vuelta-el-salvador/2026", "name": "Tour El Salvador", "year": 2026,
                    "start_date": "2026-01-17", "end_date": "2026-01-22", "class": "2.1", "country": "sv",
                    "is_stage_race": 1, "gender": "men"}
    stage = {"pcs_slug": "race/vuelta-el-salvador/2026/stage-1", "stage_num": 1, "date": "2026-01-17",
             "distance_km": None, "elevation_m": None, "profile_type": "flat", "surface": "road",
             "departure": None, "arrival": None, "gpx_path": None}
    with _conn(cli_db) as conn:   # the seeded stage has results: let the first run scrape it
        conn.execute("DELETE FROM results WHERE stage_id = (SELECT id FROM stages WHERE pcs_slug = ?)",
                     (stage["pcs_slug"],))
    pages = []
    monkeypatch.setattr(main, "iter_races", lambda years, circuits=None: iter([dict(calendar_row)]))
    monkeypatch.setattr(main, "fetch_race_stages", lambda slug: [dict(stage)])
    monkeypatch.setattr(main, "fetch_stage_results", lambda slug: [_row("w0", 1)])
    monkeypatch.setattr(main, "fetch_stage_meta", lambda slug: pages.append(slug) or {"race_category": "WE"})
    args = argparse.Namespace(years="2026", gender="men", skip_riders=True)
    for _ in range(2):
        main.cmd_scrape(args)
        with _conn(cli_db) as conn:
            row = conn.execute("SELECT gender, end_date FROM races WHERE id = ?", (sal,)).fetchone()
        assert tuple(row) == ("women", "2026-01-22")   # other calendar fields still update
    assert pages == [stage["pcs_slug"]]                 # the second run read no page


def test_upsert_race_sets_gender_on_insert_only(new_db):
    row = {"pcs_slug": "race/gp-oetingen/2026", "name": "GP Oetingen", "year": 2026, "start_date": "2026-03-01",
           "end_date": "2026-03-01", "class": "1.2", "country": "be", "is_stage_race": 0, "gender": "men"}
    with _conn(new_db) as conn:
        rid = database.upsert_race(conn, row)
        assert conn.execute("SELECT gender FROM races WHERE id = ?", (rid,)).fetchone()[0] == "men"
        conn.execute("UPDATE races SET gender = 'women' WHERE id = ?", (rid,))   # the page said WE
        assert database.upsert_race(conn, {**row, "class": "1.1"}) == rid
        assert tuple(conn.execute("SELECT gender, class FROM races WHERE id = ?", (rid,)).fetchone()) == \
            ("women", "1.1")
        conn.execute("UPDATE races SET gender = NULL WHERE id = ?", (rid,))      # unknown: the guess fills it
        database.upsert_race(conn, {**row, "gender": "women"})
        assert conn.execute("SELECT gender FROM races WHERE id = ?", (rid,)).fetchone()[0] == "women"


def test_refresh_never_prunes_on_a_short_page(cli_db, monkeypatch):
    _seed_refresh(cli_db)
    monkeypatch.setattr(main, "fetch_stage_results", lambda slug: [_row("winner", 1, 20)])
    monkeypatch.setattr(main, "fetch_stage_meta", lambda slug: {})
    main.cmd_refresh_stages(argparse.Namespace(year=2026, gender=None, races=None, null_gaps=False,
                                               uci_mismatch=False, stages="race/r/2026/stage-2", dry_run=False))
    with _conn(cli_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM results WHERE stage_id = 2").fetchone()[0] == 4


def test_race_alias_ignores_failed_fetch(cli_db, monkeypatch):
    # A failed fetch (None) is not PCS's empty "unknown slug" page: no alias lookup.
    looked = []
    monkeypatch.setattr(main, "fetch", lambda url, use_cache=True: None)
    monkeypatch.setattr(main, "find_race_alias", lambda *a, **k: looked.append(a) or "race/gp-clean/2026")
    assert main._race_alias(4, "race/stage-race/2026") is None
    assert looked == [] and 4 not in main._ALIASES


def test_race_alias_rejects_another_stored_race(cli_db, monkeypatch):
    # GP Lillers' page is empty; the calendar's same-class same-day race is
    # one we store under its own slug — a different race, never an alias.
    monkeypatch.setattr(main, "fetch", lambda url, use_cache=True: "<html><head></head><body></body></html>")
    monkeypatch.setattr(main, "find_race_alias", lambda *a, **k: "race/gp-clean/2026")
    assert main._race_alias(4, "race/stage-race/2026") is None
