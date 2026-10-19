"""Weekly UCI team-points ledger, jumps and chart payload (analysis.team_points)
plus the report CLI (scripts/team_points_report.py), on a small synthetic DB
built with the contract schema (db.uci_points)."""
import hashlib
import json
import sqlite3
import sys
import types
from datetime import date, timedelta

import pandas as pd
import pytest

from analysis import team_points as tp
from db.uci_points import ensure_uci_points_schema

RANKING = "rankings/me/ranking-2026-2028"

TEAMS = [  # rank, slug, name, class, official points
    (1, "team-alpha-2026", "UAE Team Emirates - XRG", "WT", 4400.0),
    (2, "team-beta-2026", "Red Bull - BORA - hansgrohe", "WT", 2100.0),
    (3, "team-gamma-2026", "Bardiani CSF 7 Saber", "PRT", 765.0),
    (4, "team-delta-2026", "Team Novo Nordisk", "PRT", 10.0),
]


def _result(slug, name, day, race, label, kind, pos, pts, url, race_slug=None, cls="1.UWT",
            status="finished"):
    return (2026, slug, name, day, race, race_slug, label, kind, cls, pos, status, pts, None, url)


def build_db(path):
    """Synthetic season: see the module docstring of each test for the numbers used."""
    conn = sqlite3.connect(path)
    ensure_uci_points_schema(conn)
    conn.executemany(
        "INSERT INTO uci_team_totals (ranking, ranking_date, rank, team_slug, team_name, team_class, points) "
        "VALUES (?, '2026-08-14', ?, ?, ?, ?, ?)",
        [(RANKING, r, s, n, c, p) for r, s, n, c, p in TEAMS])
    # the scraper's reconciliation pseudo-ranking (team pages' season totals) must not be
    # picked as the listing; it supplies breakdown_total (here = the 'points counted' sums)
    page_totals = {"team-alpha-2026": 4509.0, "team-beta-2026": 2130.0, "team-gamma-2026": 765.0,
                   "team-delta-2026": 10.0}
    conn.executemany(
        "INSERT INTO uci_team_totals (ranking, ranking_date, rank, team_slug, team_name, team_class, points) "
        "VALUES ('team-breakdown/ranking-2026-2028', '2026-08-15', ?, ?, ?, ?, ?)",
        [(5 - r, s, n, c, page_totals[s]) for r, s, n, c, p in TEAMS])
    bd = [  # team, rider, name (as PCS lists it), nth, counted, not counted, sanction
        ("team-alpha-2026", "tadej-pogacar", "Tadej Pogačar", 1, 3435.0, None, 25.0),
        ("team-alpha-2026", "isaac-del-toro", "Isaac del Toro", 2, 770.0, None, 30.0),
        ("team-alpha-2026", "jan-mover", "Jan Mover", 3, 100.0, None, None),
        ("team-beta-2026", "mathieu-van-der-poel", "VAN DER POEL Mathieu", 1, 2080.0, None, None),
        ("team-beta-2026", "jan-mover", "Jan Mover", 2, 50.0, None, None),
        ("team-gamma-2026", "giulio-climber", "CLIMBER Giulio", 1, 765.0, None, None),
    ]
    for k in range(1, 21):  # 20 domestiques scoring k points each (k = 1..20)
        counted = k >= 4
        bd.append(("team-alpha-2026", f"dom-{k:02d}", f"DOM{k:02d} Rider", 24 - k,
                   float(k) if counted else None, None if counted else float(k), None))
    conn.executemany(
        "INSERT INTO uci_team_breakdown (season, team_slug, team_name, team_class, rider_slug, rider_name, "
        "nth_best, points_counted, points_not_counted, sanction_points) VALUES (2026, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(t, dict((s, n) for _, s, n, _, _ in TEAMS)[t], "WT", r, n, nth, c, nc, sp)
         for t, r, n, nth, c, nc, sp in bd])
    pog, dt, jm = "Tadej Pogačar", "Isaac del Toro", "Jan Mover"
    rows = [
        _result("tadej-pogacar", pog, "2026-03-21", "Milano-Sanremo", "", "oneday", 1, 800.0,
                "race/milano-sanremo-2026-result", "race/milano-sanremo/2026"),
        _result("tadej-pogacar", pog, "2026-04-05", "Ronde van Vlaanderen ME", "", "oneday", 1, 800.0,
                "race/ronde-van-vlaanderen-2026-result", "race/ronde-van-vlaanderen/2026"),
        _result("tadej-pogacar", pog, "2026-07-18", "Tour de France", "Stage 14", "stage", 1, 210.0,
                "race/tour-de-france-2026-stage-14", "race/tour-de-france/2026", "2.UWT"),
        _result("tadej-pogacar", pog, "2026-07-19", "Tour de France", "Stage 15", "stage", 2, 150.0,
                "race/tour-de-france-2026-stage-15", "race/tour-de-france/2026", "2.UWT"),
        _result("tadej-pogacar", pog, "2026-07-21", "Tour de France", "Stage 16 (ITT)", "stage", 30, None,
                "race/tour-de-france-2026-stage-16", "race/tour-de-france/2026", "2.UWT"),
        _result("tadej-pogacar", pog, "2026-07-21", "Tour de France", "Leader jersey after Stage 16 (ITT)",
                "other", 1, 25.0, "race/tour-de-france-2026-stage-16", "race/tour-de-france/2026", "2.UWT",
                "leader"),
        _result("tadej-pogacar", pog, "2026-07-22", "Tour de France", "Leader jersey after Stage 17",
                "other", 1, 25.0, "race/tour-de-france-2026-stage-17", "race/tour-de-france/2026", "2.UWT",
                "leader"),
        _result("tadej-pogacar", pog, "2026-07-26", "Tour de France", "General classification", "gc", 1,
                1300.0, "race/tour-de-france-2026-gc", "race/tour-de-france/2026", "2.UWT"),
        _result("tadej-pogacar", pog, "2026-07-26", "Tour de France", "Mountains classification", "kom", 2,
                150.0, "race/tour-de-france-2026-kom", "race/tour-de-france/2026", "2.UWT"),
        _result("isaac-del-toro", dt, "2025-10-25", "National Championships Mexico ME - Road Race", "",
                "oneday", 1, 100.0, "race/nc-mexico-2025-result", "race/nc-mexico/2025", "NC"),
        _result("isaac-del-toro", dt, "2026-07-26", "Tour de France", "General classification", "gc", 3,
                700.0, "race/tour-de-france-2026-gc", "race/tour-de-france/2026", "2.UWT"),
        _result("jan-mover", jm, "2026-02-01", "Dev Race", "", "oneday", 1, 40.0,
                "race/dev-race-2026-result", "race/dev-race/2026", "1.2"),
        _result("jan-mover", jm, "2026-04-12", "Paris-Roubaix", "", "oneday", 3, 100.0,
                "race/paris-roubaix-2026-result", "race/paris-roubaix/2026"),
        _result("jan-mover", jm, "2026-07-10", "Tour de France", "Stage 7", "stage", 2, 50.0,
                "race/tour-de-france-2026-stage-7", "race/tour-de-france/2026", "2.UWT"),
        _result("mathieu-van-der-poel", "VAN DER POEL Mathieu", "2026-03-21", "Milano-Sanremo", "", "oneday",
                2, 640.0, "race/milano-sanremo-2026-result", "race/milano-sanremo/2026"),
        _result("mathieu-van-der-poel", "VAN DER POEL Mathieu", "2026-04-05", "Ronde van Vlaanderen ME", "",
                "oneday", 2, 640.0, "race/ronde-van-vlaanderen-2026-result", "race/ronde-van-vlaanderen/2026"),
        _result("mathieu-van-der-poel", "VAN DER POEL Mathieu", "2026-04-12", "Paris-Roubaix", "", "oneday",
                1, 800.0, "race/paris-roubaix-2026-result", "race/paris-roubaix/2026"),
        _result("giulio-climber", "CLIMBER Giulio", "2026-04-20", "Tour of the Alps", "Stage 1", "stage", 1,
                25.0, "race/tour-of-the-alps-2026-stage-1", "race/tour-of-the-alps/2026", "2.Pro"),
        _result("giulio-climber", "CLIMBER Giulio", "2026-04-21", "Tour of the Alps", "Stage 2", "stage", 5,
                5.0, "race/tour-of-the-alps-2026-stage-2", "race/tour-of-the-alps/2026", "2.Pro"),
        _result("giulio-climber", "CLIMBER Giulio", "2026-04-22", "Tour of the Alps", "Stage 3", "stage", 1,
                25.0, "race/tour-of-the-alps-2026-stage-3", "race/tour-of-the-alps/2026", "2.Pro"),
        _result("giulio-climber", "CLIMBER Giulio", "2026-04-24", "Tour of the Alps", "General classification",
                "gc", 1, 300.0, "race/tour-of-the-alps-2026-gc", "race/tour-of-the-alps/2026", "2.Pro"),
        _result("giulio-climber", "CLIMBER Giulio", "2026-04-24", "Tour of the Alps", "Points classification",
                "points", 1, 10.0, "race/tour-of-the-alps-2026-points", "race/tour-of-the-alps/2026", "2.Pro"),
        _result("giulio-climber", "CLIMBER Giulio", "2026-05-31", "Giro d'Italia", "General classification",
                "gc", 2, 400.0, "race/giro-d-italia-2026-gc", "race/giro-d-italia/2026", "2.UWT"),
    ]
    for k in range(1, 21):
        rows.append(_result(f"dom-{k:02d}", f"Dom{k:02d} Rider", "2026-05-10", "Dom Race", "", "oneday",
                            21 - k, float(k), f"race/dom-race-2026-result", "race/dom-race/2026", "1.1"))
    conn.executemany(
        "INSERT INTO rider_uci_results (season, rider_slug, rider_name, date, race_name, race_slug, "
        "result_label, result_kind, race_class, position, status, points_uci, points_pcs, result_url) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.executemany(
        "INSERT INTO rider_uci_penalties (season, rider_slug, date, race_name, penalty_points, fine, article) "
        "VALUES (2026, ?, ?, ?, ?, '500', 'littering')",
        [("tadej-pogacar", "2026-04-05", "Ronde van Vlaanderen ME", 25.0),
         ("isaac-del-toro", "2026-08-12", "Vuelta a Burgos | Stage 1", 30.0)])
    conn.executemany(
        "INSERT INTO rider_team_spells (season, rider_slug, team_slug, team_name, start_date, end_date) "
        "VALUES (2026, ?, ?, ?, ?, ?)",
        [("jan-mover", "dev-team-2026", "Dev Team", None, "2026-02-28"),
         ("jan-mover", "team-alpha-2026", "UAE Team Emirates - XRG", "2026-03-01", "2026-05-31"),
         ("jan-mover", "team-beta-2026", "Red Bull - BORA - hansgrohe", "2026-06-01", None),
         ("tadej-pogacar", "team-alpha-2026", "UAE Team Emirates - XRG", None, None)])
    conn.commit()
    conn.close()


@pytest.fixture(scope="module")
def db_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("tp") / "uci.db"
    build_db(path)
    return path


@pytest.fixture()
def conn(db_path):
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    yield c
    c.close()


@pytest.fixture(scope="module")
def payload(db_path):
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return tp.build_chart_payload(c, season=2026, top_n=3, generated_at="2026-08-14 12:00")
    finally:
        c.close()


def _team(payload, slug):
    return next(t for t in payload["teams"] if t["key"] == slug)


# ---------------------------------------------------------------------------
# names
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,slug,display,surname", [
    ("POGAČAR Tadej", "tadej-pogacar", "Tadej Pogačar", "Pogačar"),
    ("VAN DER POEL Mathieu", "mathieu-van-der-poel", "Mathieu van der Poel", "van der Poel"),
    ("DEL TORO Isaac", "isaac-del-toro", "Isaac del Toro", "del Toro"),
    ("DE LIE Arnaud", "arnaud-de-lie", "Arnaud De Lie", "De Lie"),
    ("DE LA CRUZ David", "david-de-la-cruz", "David de la Cruz", "de la Cruz"),
    ("O'CONNOR Ben", "ben-o-connor", "Ben O'Connor", "O'Connor"),
    ("MCNULTY Brandon", "brandon-mcnulty", "Brandon McNulty", "McNulty"),
    ("GROßSCHARTNER Felix", "felix-grossschartner", "Felix Großschartner", "Großschartner"),
    ("SKJELMOSE JENSEN Mattias", "mattias-skjelmose-jensen", "Mattias Skjelmose Jensen", "Skjelmose Jensen"),
    ("MOLANO JUAN SEBASTIÁN", "juan-sebastian-molano", "Juan Sebastián Molano", "Molano"),  # all caps: slug decides
    ("Isaac del Toro", None, "Isaac del Toro", "del Toro"),
    ("Mathieu van der Poel", None, "Mathieu van der Poel", "van der Poel"),
    ("Juan Sebastián Molano", None, "Juan Sebastián Molano", "Molano"),
])
def test_split_rider_name(raw, slug, display, surname):
    assert tp.split_rider_name(raw, slug) == (display, surname)
    assert tp.display_name(raw, slug) == display


@pytest.mark.parametrize("raw,clean", [
    ("Ronde van Vlaanderen ME", "Ronde van Vlaanderen"),
    ("World Championships ME - Road Race", "World Championships Road Race"),
    ("Tour de France (2.UWT)", "Tour de France"),
    ("Tour de France | Stage 4", "Tour de France"),
    ("La Vuelta Ciclista a España", "Vuelta a España"),
    ("Grand Prix Cycliste de Québec", "GP Québec"),
    ("GP de Fourmies / La Voix du Nord", "GP de Fourmies"),
    ("National Championships Norway ME - Road Race", "Norway nationals"),
    ("National Championships Norway ME - ITT", "Norway nationals ITT"),
    ("In Flanders Fields From Middelkerke to Wevelgem", "Gent-Wevelgem"),
    # place-to-place names keep their hyphen; disciplines / secondary titles do not
    ("Milano - Torino", "Milano-Torino"),
    ("Paris - Tours Elite", "Paris-Tours"),
    ("Kuurne - Brussel - Kuurne", "Kuurne-Brussel-Kuurne"),
    ("Binche - Chimay - Binche / Mémorial Frank Vandenbroucke", "Binche-Chimay-Binche"),
    ("Veenendaal - Veenendaal ME", "Veenendaal-Veenendaal"),
    ("Tour Auvergne - Rhône-Alpes", "Tour Auvergne-Rhône-Alpes"),
    ("Asian Games ME - ITT", "Asian Games ITT"),
    ("World Championships - Mixed Relay TTT", "World Championships Mixed Relay TTT"),
    ("Circuito del Porto - Trofeo Arvedi", "Circuito del Porto Trofeo Arvedi"),
    # age categories stay visible: a U23 / junior title is not the elite one
    ("National Championships Hungary MU - Road Race", "Hungary U23 nationals"),
    ("National Championships Hungary MU - ITT", "Hungary U23 nationals ITT"),
    ("National Championships Hungary ME - Road Race", "Hungary nationals"),
    ("World Championships MU - Road Race", "World Championships U23 Road Race"),
    ("World Championships WU - ITT", "World Championships U23 ITT"),
    ("World Championships MJ - Road Race", "World Championships Junior Road Race"),
    ("National Championships Belgium MJ - Road Race", "Belgium junior nationals"),
    ("Boucles de la Mayenne - Crédit Mutuel", "Boucles de la Mayenne"),     # "Mutuel" is no MU marker
])
def test_clean_race_name(raw, clean):
    assert tp.clean_race_name(raw) == clean


@pytest.mark.parametrize("name,slug,short", [
    ("UAE Team Emirates - XRG", None, "UAE"),
    ("Pinarello Q36.5 Pro Cycling Team", None, "Pinarello-Q36.5"),  # listing spelling, no hyphen
    ("Team Visma | Lease a Bike", None, "Visma"),
    (None, "team-jayco-alula-2026", "Jayco"),
    ("Team Novo Nordisk", None, "Novo Nordisk"),        # heuristic
    ("Bardiani CSF 7 Saber", None, "Bardiani CSF"),
    ("Team Flanders - Baloise", None, "Flanders"),
])
def test_team_short_name(name, slug, short):
    assert tp.team_short_name(name, slug) == short


def test_ranking_name_prefers_official_listing(conn):
    assert tp.ranking_name(conn, 2026) == RANKING
    listing = tp.load_listing(conn, 2026)
    assert list(listing["team_slug"]) == [t[1] for t in TEAMS] and listing["points"].iloc[0] == 4400


def test_breakdown_total_prefers_team_page_total(tmp_path):
    build_db(tmp_path / "w.db")
    conn = sqlite3.connect(tmp_path / "w.db")
    assert tp.load_breakdown_page_totals(conn, 2026)["team-alpha-2026"] == 4509.0
    p = tp.build_chart_payload(conn, season=2026, top_n=3, generated_at="x")
    assert _team(p, "team-alpha-2026")["breakdown_total"] == 4509
    assert not any("team page total differs" in n for n in p["meta"]["coverage"]["notes"])
    # PCS prints a negative counted value as '-': its page total is 10 below the counted sum
    conn.execute("UPDATE uci_team_totals SET points = 4499 WHERE ranking LIKE 'team-breakdown/%' "
                 "AND team_slug = 'team-alpha-2026'")
    p = tp.build_chart_payload(conn, season=2026, top_n=3, generated_at="x")
    assert _team(p, "team-alpha-2026")["breakdown_total"] == 4499
    assert p["meta"]["coverage"]["reconciliation"]["teams_exact"] == 2
    note = next(n for n in p["meta"]["coverage"]["notes"] if "team page total differs" in n)
    assert "UAE -10" in note
    conn.execute("DELETE FROM uci_team_totals WHERE ranking LIKE 'team-breakdown/%'")
    assert tp.load_breakdown_page_totals(conn, 2026) == {}
    p = tp.build_chart_payload(conn, season=2026, top_n=3, generated_at="x")
    assert _team(p, "team-alpha-2026")["breakdown_total"] == 4509     # falls back to the counted sum


def test_ordinal_and_weeks():
    assert [tp.ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, 103)] == \
        ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd", "103rd"]
    assert tp.week_start("2026-04-05") == "2026-03-30"     # Sunday -> its Monday
    assert tp.week_start("2026-03-30") == "2026-03-30"
    assert tp.week_axis("2025-12-31", "2026-01-12") == ["2025-12-29", "2026-01-05", "2026-01-12"]


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------

def test_ledger_columns_and_names(conn):
    led = tp.load_points_ledger(conn, 2026)
    assert list(led.columns) == tp.LEDGER_COLUMNS
    vdp = led[led["rider_slug"] == "mathieu-van-der-poel"]
    assert set(vdp["rider_name"]) == {"Mathieu van der Poel"}
    assert set(vdp["rider_short"]) == {"van der Poel"}
    assert set(led.loc[led["rider_slug"] == "giulio-climber", "rider_name"]) == {"Giulio Climber"}
    # zero/NULL-point rows are not events; race names are cleaned
    assert not ((led["rider_slug"] == "tadej-pogacar") & (led["result_label"] == "Stage 16 (ITT)")).any()
    assert "Ronde van Vlaanderen" in set(led["race_name"])
    assert (led["week_start"] == led["date"].map(tp.week_start)).all()


def test_transfer_spells_split_points_by_date(conn):
    led, excluded = tp.load_points_ledger(conn, 2026, return_excluded=True)
    jm = led[led["rider_slug"] == "jan-mover"].set_index("date")
    assert jm.loc["2026-04-12", "team_slug"] == "team-alpha-2026"   # spell 1 Mar - 31 May
    assert jm.loc["2026-07-10", "team_slug"] == "team-beta-2026"    # spell from 1 Jun
    assert jm.loc["2026-04-12", "team_name"] == "UAE Team Emirates - XRG"
    # the February result was for a team outside the ranking: not in any team total
    assert "2026-02-01" not in jm.index
    out = excluded[excluded["rider_slug"] == "jan-mover"]
    assert list(out["team_slug"]) == ["dev-team-2026"] and out["points"].sum() == 40


def test_single_breakdown_team_without_spells(conn):
    led = tp.load_points_ledger(conn, 2026)
    assert set(led.loc[led["rider_slug"] == "isaac-del-toro", "team_slug"]) == {"team-alpha-2026"}
    assert set(led.loc[led["rider_slug"] == "giulio-climber", "team_slug"]) == {"team-gamma-2026"}


def test_penalties_are_negative_rows(conn):
    led = tp.load_points_ledger(conn, 2026)
    pens = led[led["result_kind"] == "penalty"].set_index("rider_slug")
    assert len(pens) == 2
    p = pens.loc["tadej-pogacar"]
    assert p["points"] == -25 and p["date"] == "2026-04-05" and p["week_start"] == "2026-03-30"
    assert p["race_name"] == "Ronde van Vlaanderen"
    assert p["race_slug"] == "race/ronde-van-vlaanderen/2026"   # borrowed from his result row
    assert p["team_slug"] == "team-alpha-2026"
    q = pens.loc["isaac-del-toro"]
    assert q["points"] == -30 and q["result_label"] == "Penalty (Stage 1)" and q["race_name"] == "Vuelta a Burgos"
    assert pd.isna(q["race_slug"])


def test_ledger_totals_match_breakdown(conn):
    led = tp.load_points_ledger(conn, 2026)
    per = led.groupby(["team_slug", "rider_slug"])["points"].sum()
    assert per[("team-alpha-2026", "tadej-pogacar")] == pytest.approx(3435.0)   # 3460 - 25 sanction
    assert per[("team-alpha-2026", "isaac-del-toro")] == pytest.approx(770.0)   # incl. Oct 2025 nationals
    assert per[("team-alpha-2026", "jan-mover")] == 100 and per[("team-beta-2026", "jan-mover")] == 50


def test_rider_ledger_ignores_team(conn):
    rl = tp.load_rider_ledger(conn, "jan-mover", 2026)
    assert rl["points"].sum() == 190            # 40 (dev team) + 100 + 50
    assert list(rl["team_slug"]) == ["dev-team-2026", "team-alpha-2026", "team-beta-2026"]
    assert tp.load_rider_ledger(conn, "nobody", 2026).empty


# ---------------------------------------------------------------------------
# weekly series
# ---------------------------------------------------------------------------

def test_weeks_contiguous_from_first_scoring_week(conn):
    led = tp.load_points_ledger(conn, 2026)
    s = tp.weekly_team_series(led, teams=[t[1] for t in TEAMS])
    weeks = s["weeks"]
    assert weeks[0] == "2025-10-20"          # del Toro's Mexican nationals, 25 Oct 2025
    assert weeks[-1] == "2026-08-10"         # last event: 12 Aug 2026 sanction
    days = [date.fromisoformat(w) for w in weeks]
    assert all(d.weekday() == 0 for d in days)
    assert all(b - a == timedelta(days=7) for a, b in zip(days, days[1:]))
    assert list(s["gains"].index) == weeks and list(s["cumulative"].columns) == [t[1] for t in TEAMS]
    assert (s["cumulative"]["team-delta-2026"] == 0).all()


def test_cumulative_monotone_except_penalty_weeks(conn):
    led = tp.load_points_ledger(conn, 2026)
    s = tp.weekly_team_series(led, teams=[t[1] for t in TEAMS])
    pen_weeks = {(t, w) for t, w in zip(led.loc[led["result_kind"] == "penalty", "team_slug"],
                                         led.loc[led["result_kind"] == "penalty", "week_start"])}
    cum = s["cumulative"]
    drops = []
    for team in cum.columns:
        d = cum[team].diff().fillna(cum[team].iloc[0])
        for w, v in d.items():
            if v < 0:
                assert (team, w) in pen_weeks
                drops.append((team, w))
    assert drops == [("team-alpha-2026", "2026-08-10")]   # del Toro's sanction alone that week
    assert cum["team-alpha-2026"].iloc[-1] == pytest.approx(4515.0)
    assert (s["gains"].sum() == cum.iloc[-1]).all()


def test_rank_by_week_ties_share_better_rank(conn):
    led = tp.load_points_ledger(conn, 2026)
    s = tp.weekly_team_series(led, teams=[t[1] for t in TEAMS])
    first = s["rank"].iloc[0]
    assert first["team-alpha-2026"] == 1 and first["team-beta-2026"] == 2 and first["team-delta-2026"] == 2
    wk = s["weeks"].index("2026-04-06")      # beta's Roubaix win: beta 2080 passes alpha 1775
    assert s["rank"].iloc[wk]["team-beta-2026"] == 1


def test_best20_counts_twenty_best_riders(conn):
    led = tp.load_points_ledger(conn, 2026)
    s = tp.weekly_team_series(led, teams=[t[1] for t in TEAMS])
    b = s["best20"]["team-alpha-2026"]
    # alpha: 23 riders; the three weakest domestiques (1+2+3 pts) drop out
    assert b.iloc[-1] == pytest.approx(4515.0 - 6.0)
    assert b.iloc[-1] == pytest.approx(4509.0)
    wk = s["weeks"].index("2026-05-04")      # domestiques score on 10 May
    # season to date: Pogačar 1575, del Toro 100, Mover 100 + 17 best domestiques (4..20 = 204)
    assert b.iloc[wk] == pytest.approx(1575 + 100 + 100 + 204)
    assert b.iloc[wk - 1] == pytest.approx(1775)
    # best-N per team counts only points attributed to that team (Mover's 50 for beta)
    assert s["best20"]["team-beta-2026"].iloc[-1] == pytest.approx(2130.0)


def test_best_n_evolves_week_by_week():
    rows = pd.DataFrame([
        {"date": "2026-03-02", "team_slug": "t", "rider_slug": "a", "points": 10.0},
        {"date": "2026-03-02", "team_slug": "t", "rider_slug": "b", "points": 5.0},
        {"date": "2026-03-09", "team_slug": "t", "rider_slug": "c", "points": 7.0},
        {"date": "2026-03-16", "team_slug": "t", "rider_slug": "b", "points": 4.0},
    ])
    rows["week_start"] = rows["date"].map(tp.week_start)
    s = tp.weekly_team_series(rows, teams=["t"], best_n=2)
    assert list(s["best20"]["t"]) == [15.0, 17.0, 19.0]    # a+b, a+c, a+b(9)
    assert list(s["cumulative"]["t"]) == [15.0, 22.0, 26.0]


def test_best_n_sanction_before_teammates_score():
    """A rider without any points item yet is not a '0' that pushes a sanctioned
    team-mate's negative net out of the best N (PCS lists only riders with items)."""
    rows = pd.DataFrame([
        {"date": "2026-01-20", "team_slug": "t", "rider_slug": "s", "points": -15.0},   # sanction
        {"date": "2026-01-27", "team_slug": "t", "rider_slug": "a", "points": 50.0},
        {"date": "2026-02-03", "team_slug": "t", "rider_slug": "b", "points": 30.0},
        {"date": "2026-02-03", "team_slug": "t", "rider_slug": "c", "points": 20.0},
    ])
    rows["week_start"] = rows["date"].map(tp.week_start)
    s = tp.weekly_team_series(rows, teams=["t"], best_n=2)
    assert list(s["cumulative"]["t"]) == [-15.0, 35.0, 85.0]
    # week 1: only s listed -> -15; week 2: a, s -> 35; week 3: a + b (s, c drop out)
    assert list(s["best20"]["t"]) == [-15.0, 35.0, 80.0]
    assert all(b <= a for a, b in zip(s["cumulative"]["t"], s["best20"]["t"]))


# ---------------------------------------------------------------------------
# jumps
# ---------------------------------------------------------------------------

def _rows(*specs):
    """Ledger rows: (rider_slug, short, race, label, kind, position, points[, status])."""
    out = []
    for spec in specs:
        slug, short, race, label, kind, pos, pts = spec[:7]
        status = spec[7] if len(spec) > 7 else "finished"
        out.append({"date": "2026-07-26", "week_start": "2026-07-20", "team_slug": "t", "team_name": "T",
                    "rider_slug": slug, "rider_name": short.capitalize() + " Full", "rider_short": short,
                    "race_name": race, "race_slug": "race/" + race.lower().replace(" ", "-") + "/2026",
                    "result_label": label, "result_kind": kind, "race_class": "2.UWT",
                    "position": pos, "status": status, "points": float(pts)})
    df = pd.DataFrame(out, columns=tp.LEDGER_COLUMNS)
    df["position"] = df["position"].astype("Int64")
    return df


def _label(df, subject="team"):
    gain = float(df["points"].sum())
    contributors, groups = tp.contributors_for(df, gain)
    return tp.jump_label(groups, gain, subject=subject), contributors


def test_label_one_day_win_and_place():
    assert _label(_rows(("p", "Pogačar", "Ronde van Vlaanderen", "", "oneday", 1, 800),
                        ("p", "Pogačar", "Ronde van Vlaanderen", "Penalty", "penalty", None, -25)))[0] == \
        "Pogačar wins Ronde van Vlaanderen"
    assert _label(_rows(("v", "van der Poel", "Paris-Roubaix", "", "oneday", 2, 640)))[0] == \
        "Van der Poel 2nd at Paris-Roubaix"
    label, contrib = _label(_rows(("p", "Pogačar", "Ronde van Vlaanderen", "", "oneday", 1, 800),
                                  ("p", "Pogačar", "Ronde van Vlaanderen", "Penalty", "penalty", None, -25)))
    assert contrib == [{"rider": "Pogačar Full", "race": "Ronde van Vlaanderen",
                        "detail": "1st, penalty -25", "points": 775.0}]


def test_label_stage_race_forms():
    gc_and_wins = _rows(("v", "Vingegaard", "Tour de France", "General classification", "gc", 2, 1040),
                        ("v", "Vingegaard", "Tour de France", "Stage 4", "stage", 1, 210),
                        ("v", "Vingegaard", "Tour de France", "Stage 15", "stage", 1, 210),
                        ("v", "Vingegaard", "Tour de France", "Stage 16 (ITT)", "stage", 3, 120))
    label, contrib = _label(gc_and_wins)
    assert label == "Vingegaard: Tour de France GC 2nd + 2 stage wins"
    assert contrib[0]["detail"] == "GC 2nd, stages 4 & 15 1st, stage 16 3rd"
    assert _label(gc_and_wins, subject="rider")[0] == "Tour de France: GC 2nd + 2 stage wins"
    gc_only = _rows(("g", "Gall", "Giro d'Italia", "General classification", "gc", 2, 1040),
                    ("g", "Gall", "Giro d'Italia", "Stage 9", "stage", 7, 40),
                    ("g", "Gall", "Giro d'Italia", "Leader jersey after Stage 9", "other", 1, 20, "leader"))
    label, contrib = _label(gc_only)
    assert label == "Gall 2nd at Giro d'Italia"
    assert contrib[0]["detail"] == "GC 2nd, stage 9 7th, leader 1 day"
    gc_win = _rows(("p", "Pogačar", "Tour de Suisse", "General classification", "gc", 1, 500),
                   ("p", "Pogačar", "Tour de Suisse", "Mountains classification", "kom", 2, 30))
    assert _label(gc_win)[0] == "Pogačar wins Tour de Suisse"
    assert _label(gc_win, subject="rider")[0] == "Wins Tour de Suisse"
    gc_kom = _rows(("c", "Carapaz", "Tour de France", "General classification", "gc", 8, 300),
                   ("c", "Carapaz", "Tour de France", "Mountains classification", "kom", 1, 200))
    assert _label(gc_kom)[0] == "Carapaz: Tour de France GC 8th + KOM 1st"
    wins = _rows(("m", "Merlier", "Tour de France", "Stage 2", "stage", 1, 210),
                 ("m", "Merlier", "Tour de France", "Stage 5", "stage", 1, 210),
                 ("m", "Merlier", "Tour de France", "Stage 6", "stage", 1, 210),
                 ("m", "Merlier", "Tour de France", "Stage 3", "stage", 4, 90))
    label, contrib = _label(wins)
    assert label == "Merlier: 3 stage wins at Tour de France"
    assert contrib[0]["detail"] == "stages 2, 5 & 6 1st, stage 3 4th"
    assert _label(wins, subject="rider")[0] == "3 stage wins at Tour de France"
    one = _rows(("j", "Philipsen", "Tour de France", "Stage 17", "stage", 1, 210))
    assert _label(one)[0] == "Philipsen: stage 17 win at Tour de France"
    many = _rows(*[("p", "Pogačar", "Tour de Romandie", f"Stage {k}", "stage", 1, 60) for k in (1, 2, 4, 5)],
                 ("p", "Pogačar", "Tour de Romandie", "General classification", "gc", 1, 500))
    label, contrib = _label(many, subject="rider")
    assert label == "Tour de Romandie: GC 1st + 4 stage wins"
    assert contrib[0]["detail"] == "GC 1st, 4 stage wins"


def test_label_multi_rider_and_multi_race():
    same_race = _rows(("a", "Almeida", "Vuelta a España", "General classification", "gc", 3, 500),
                      ("b", "Ayuso", "Vuelta a España", "General classification", "gc", 5, 400),
                      ("c", "Soler", "Vuelta a España", "Stage 4", "stage", 1, 180),
                      ("d", "Wellens", "GP Québec", "", "oneday", 9, 60))
    label, contrib = _label(same_race)
    # Soler's stage win (16% of the week) is named too
    assert label == "Vuelta a España: Almeida GC 3rd, Ayuso GC 5th; Soler stage 4 win + 1 more rider"
    # contributors stop once they cover 75% of the gain (855 of 1140): 500 + 400
    assert [c["points"] for c in contrib] == [500.0, 400.0]
    spread = _rows(("a", "Almeida", "Vuelta a España", "Stage 4", "stage", 2, 300),
                   ("b", "Wellens", "GP Québec", "", "oneday", 1, 300),
                   ("c", "Soler", "Coppa Sabatini", "", "oneday", 1, 250),
                   ("d", "Bjerg", "Chrono des Nations", "", "oneday", 3, 150))
    label, contrib = _label(spread)
    # every win worth >= 10% of the week is named (at most two extra)
    assert label == ("Almeida: Vuelta a España stage 4 2nd; Wellens wins GP Québec; "
                     "Soler wins Coppa Sabatini + 1 more race")
    assert len(contrib) == 3                       # 300 + 300 + 250 = 850 >= 750
    leading = _rows(("d", "del Toro", "UAE Tour", "General classification", "gc", 1, 500),
                    ("a", "Almeida", "Volta ao Algarve", "General classification", "gc", 3, 200),
                    ("b", "Christen", "Vuelta a Andalucía", "General classification", "gc", 4, 140),
                    ("c", "Wellens", "Clásica Jaén", "", "oneday", 1, 125))
    assert _label(leading)[0] == ("Del Toro wins UAE Tour; Almeida 3rd at Volta ao Algarve; "
                                  "Wellens wins Clásica Jaén + 1 more race")
    assert _label(leading, subject="rider")[0] == "Wins UAE Tour; Volta ao Algarve & wins Clásica Jaén + 1 more"
    two_races = _rows(("a", "Almeida", "Vuelta a España", "Stage 4", "stage", 2, 300),
                      ("b", "del Toro", "GP Québec", "", "oneday", 5, 250),
                      ("c", "Soler", "Vuelta a España", "Stage 5", "stage", 3, 50))
    assert _label(two_races)[0] == "Almeida: Vuelta a España stage 4 2nd; del Toro 5th at GP Québec + 1 more rider"
    worlds = _rows(("m", "McNulty", "World Championships Road Race", "", "oneday", 1, 700),
                   ("d", "del Toro", "World Championships Road Race", "", "oneday", 5, 410),
                   ("w", "Wellens", "World Championships Road Race", "", "oneday", 20, 100),
                   ("x", "Other", "GP Québec", "", "oneday", 9, 60))
    assert _label(worlds)[0] == "World Championships Road Race: McNulty 1st, del Toro 5th + 2 more riders"
    one_rider = _rows(("g", "Groenewegen", "Bredene Koksijde Classic", "", "oneday", 1, 200),
                      ("g", "Groenewegen", "GP Monseré", "", "oneday", 1, 200),
                      ("x", "Other", "Some Race", "", "oneday", 5, 50))
    assert _label(one_rider)[0] == "Groenewegen wins Bredene Koksijde Classic + GP Monseré"


def test_label_low_gc_gives_way_to_stage_win_or_jersey():
    stage_win = _rows(("j", "Johannessen", "Vuelta a España", "General classification", "gc", 26, 40),
                      ("j", "Johannessen", "Vuelta a España", "Stage 21", "stage", 1, 180))
    assert _label(stage_win)[0] == "Johannessen: stage 21 win at Vuelta a España"
    # GC 8th (top 10) stays in the headline next to the wins / jersey
    assert _label(_rows(("c", "Carapaz", "Tour de France", "General classification", "gc", 8, 300),
                        ("c", "Carapaz", "Tour de France", "Stage 18", "stage", 1, 210)))[0] == \
        "Carapaz: Tour de France GC 8th + 1 stage win"
    vuelta = _rows(("b", "Buitrago", "Vuelta a España", "General classification", "gc", 17, 100),
                   ("b", "Buitrago", "Vuelta a España", "Mountains classification", "kom", 1, 250),
                   ("o", "Omrzel", "Vuelta a España", "General classification", "gc", 7, 340),
                   ("o", "Omrzel", "Vuelta a España", "Stage 12", "stage", 1, 0.0),
                   ("m", "Miquel", "Vuelta a España", "Stage 9", "stage", 2, 150))
    label, contrib = _label(vuelta)
    assert label == "Vuelta a España: Buitrago KOM 1st, Omrzel GC 7th & stage 12 win + 1 more rider"
    assert contrib[0]["detail"] == "GC 17th, KOM 1st"          # the detail keeps every result


def test_final_week_counts_earlier_stage_wins():
    week = _rows(("p", "Pogačar", "Tour de France", "General classification", "gc", 1, 1300),
                 ("p", "Pogačar", "Tour de France", "Stage 19", "stage", 1, 210))
    earlier = _rows(("p", "Pogačar", "Tour de France", "Stage 3", "stage", 1, 210),
                    ("p", "Pogačar", "Tour de France", "Stage 10", "stage", 1, 210),
                    ("p", "Pogačar", "Tour de Suisse", "Stage 2", "stage", 1, 100),   # other race
                    ("x", "Other", "Tour de France", "Stage 4", "stage", 1, 210))      # other rider
    gain = float(week["points"].sum())
    contrib, groups = tp.contributors_for(week, gain, history=earlier)
    assert tp.jump_label(groups, gain) == "Pogačar: Tour de France GC 1st + 3 stage wins"
    assert tp.jump_label(groups, gain, subject="rider") == "Tour de France: GC 1st + 3 stage wins"
    assert contrib[0]["detail"] == "GC 1st, stage 19 1st; also won stages 3 & 10 earlier"
    assert contrib[0]["points"] == 1510.0                       # points stay the week's
    # a mid-race week (no final classification) describes only that week
    mid = _rows(("p", "Pogačar", "Tour de France", "Stage 14", "stage", 1, 210))
    contrib, groups = tp.contributors_for(mid, 210.0, history=earlier)
    assert tp.jump_label(groups, 210.0) == "Pogačar: stage 14 win at Tour de France"
    assert contrib[0]["detail"] == "stage 14 1st"


def test_label_same_rider_twice_and_minor_placings():
    # not dominant (53%): the rider's two results, then the rest
    pid = _rows(("p", "T. Pidcock", "Il Lombardia", "", "oneday", 6, 280),
                ("p", "T. Pidcock", "Tre Valli Varesine", "", "oneday", 2, 170),
                ("h", "Hermans", "Gran Piemonte", "", "oneday", 5, 150),
                ("w", "Wright", "Gran Piemonte", "", "oneday", 6, 130),
                ("z", "Zana", "Coppa Bernocchi", "", "oneday", 9, 120))
    assert _label(pid)[0] == ("T. Pidcock 6th at Il Lombardia & 2nd at Tre Valli Varesine; "
                              "Hermans 5th at Gran Piemonte + 1 more race")
    # one rider >= 60% across races: both results, a win first
    stage = _rows(("p", "Pidcock", "Il Lombardia", "", "oneday", 6, 280),
                  ("p", "Pidcock", "Tour of Guangxi", "Stage 3", "stage", 1, 150),
                  ("x", "Other", "Gran Piemonte", "", "oneday", 5, 200))
    assert _label(stage)[0] == "Pidcock 6th at Il Lombardia & stage 3 win at Tour of Guangxi"
    seixas = _rows(("s", "Seixas", "Liège-Bastogne-Liège", "", "oneday", 2, 640),
                   ("s", "Seixas", "La Flèche Wallonne", "", "oneday", 1, 500),
                   ("x", "Other", "Some Race", "", "oneday", 5, 100))
    assert _label(seixas)[0] == "Seixas wins La Flèche Wallonne & 2nd at Liège-Bastogne-Liège"
    # no top-3 placing: the best stage placing still names the result
    minor = _rows(("m", "Milesi", "Giro d'Italia", "Stage 4", "stage", 5, 100),
                  ("m", "Milesi", "Giro d'Italia", "Stage 9", "stage", 8, 40))
    assert _label(minor)[0] == "Milesi: Giro d'Italia stage 4 5th"


def test_label_regressions_2026_review():
    # Visma, week of 2026-03-09: Paris-Nice is 64% of the week, but its second
    # rider (Armirail, 34) is tiny; Jorgenson's Tirreno GC 2nd (460) is named.
    visma = _rows(("v", "Vingegaard", "Paris-Nice", "General classification", "gc", 1, 500),
                  ("v", "Vingegaard", "Paris-Nice", "Stage 4", "stage", 1, 102),
                  ("v", "Vingegaard", "Paris-Nice", "Stage 7", "stage", 1, 102),
                  ("a", "Armirail", "Paris-Nice", "General classification", "gc", 19, 34),
                  ("c", "Campenaerts", "Paris-Nice", "Stage 2", "stage", 5, 34),
                  ("k", "Kelderman", "Paris-Nice", "Stage 6", "stage", 6, 30),
                  ("j", "Jorgenson", "Tirreno-Adriatico", "General classification", "gc", 2, 460))
    assert _label(visma)[0] == ("Paris-Nice: Vingegaard GC 1st & 2 stage wins; "
                                "Jorgenson 2nd at Tirreno-Adriatico + 3 more riders")
    # Soudal, 2026-04-06: Merlier's Scheldeprijs win, not Van Gestel's 20th place
    soudal = _rows(("s", "Stuyven", "Paris-Roubaix", "", "oneday", 3, 400),
                   ("g", "Van Gestel", "Paris-Roubaix", "", "oneday", 20, 50),
                   ("x", "Other", "Paris-Roubaix", "", "oneday", 15, 100),
                   ("m", "Merlier", "Scheldeprijs", "", "oneday", 1, 250))
    assert _label(soudal)[0] == "Paris-Roubaix: Stuyven 3rd; Merlier wins Scheldeprijs + 2 more riders"
    # NSN, 2026-06-15: Askey's Baloise Belgium Tour GC 4th outranks Kogut's nationals 2nd
    nsn = _rows(("r", "Raisberg", "Israel nationals", "", "oneday", 1, 150),
                ("k", "Kogut", "Israel nationals", "", "oneday", 2, 100),
                ("e", "Einhorn", "Israel nationals", "", "oneday", 3, 70),
                ("a", "Askey", "Baloise Belgium Tour", "General classification", "gc", 4, 135))
    assert _label(nsn)[0] == "Israel nationals: Raisberg 1st; Askey 4th at Baloise Belgium Tour + 2 more riders"
    # Alpecin, 2026-07-20: van der Poel's Champs-Élysées win is not dropped
    alpecin = _rows(("p", "Philipsen", "Tour de France", "Stage 17", "stage", 1, 210),
                    ("p", "Philipsen", "Tour de France", "Stage 18", "stage", 3, 120),
                    ("p", "Philipsen", "Tour de France", "Points classification", "points", 2, 120),
                    ("v", "van der Poel", "Tour de France", "Stage 21", "stage", 1, 180),
                    ("x", "Other", "Some Race", "", "oneday", 9, 50))
    assert _label(alpecin)[0] == "Philipsen: stage 17 win at Tour de France; van der Poel stage 21 win"
    # Pinarello, 2026-03-16: Pidcock's Milano-Torino win next to his Sanremo 2nd
    pin = _rows(("p", "T. Pidcock", "Milano-Sanremo", "", "oneday", 2, 640),
                ("p", "T. Pidcock", "Milano-Torino", "", "oneday", 1, 250),
                ("x", "Other", "Some Race", "", "oneday", 9, 100))
    assert _label(pin)[0] == "T. Pidcock 2nd at Milano-Sanremo & wins Milano-Torino"
    # Movistar, 2026-05-11: two wins (250) outrank a bigger single placing (140)
    mov = _rows(("m", "Milesi", "Giro d'Italia", "Stage 5", "stage", 4, 140),
                ("b", "Barrenetxea", "Tour du Finistère", "", "oneday", 1, 125),
                ("b", "Barrenetxea", "Boucles de l'Aulne", "", "oneday", 1, 125),
                ("x", "Other", "Some Race", "", "oneday", 9, 60))
    assert _label(mov)[0] == ("Barrenetxea wins Tour du Finistère + Boucles de l'Aulne; "
                              "Milesi: Giro d'Italia stage 5 4th + 1 more race")


def test_label_extra_win_of_an_earlier_named_rider_keeps_his_name():
    """MBH Bank, week of 2026-06-22: Takács (named first) also won the U23 ITT.
    Appended after Peák's phrase as ' & wins ...' it read as Peák's win; it
    must carry Takács's surname.  The tail counts only riders not named."""
    mbh = _rows(("t", "Takács", "Hungary nationals", "", "oneday", 1, 100),
                ("p", "Peák", "Hungary nationals ITT", "", "oneday", 1, 50),
                ("p", "Peák", "Hungary nationals", "", "oneday", 5, 40),
                ("t", "Takács", "Hungary U23 nationals ITT", "", "oneday", 1, 25),
                ("d", "Dina", "Hungary nationals", "", "oneday", 9, 5),
                ("b", "Budziński", "Poland nationals", "", "oneday", 7, 3),
                ("v", "Valent", "Hungary nationals", "", "oneday", 11, 3))
    label = _label(mbh)[0]
    assert label == ("Hungary nationals: Takács 1st; Peák wins Hungary nationals ITT; "
                     "Takács wins Hungary U23 nationals ITT + 3 more riders")
    assert "Peák wins Hungary nationals ITT & " not in label
    # the last-named rider's further win still chains with ' & '
    last = _rows(("t", "Takács", "Hungary nationals", "", "oneday", 1, 100),
                 ("p", "Peák", "Hungary nationals ITT", "", "oneday", 1, 50),
                 ("p", "Peák", "Hungary U23 nationals ITT", "", "oneday", 1, 25),
                 ("d", "Dina", "Hungary nationals", "", "oneday", 9, 20))
    assert _label(last)[0] == ("Hungary nationals: Takács 1st; Peák wins Hungary nationals ITT & "
                               "wins Hungary U23 nationals ITT + 1 more rider")
    # two riders' phrases, then a win of the FIRST one: surname again, not ' & '
    two = _rows(("a", "Almeida", "Vuelta a España", "Stage 4", "stage", 1, 300),
                ("b", "Wellens", "GP Québec", "", "oneday", 1, 280),
                ("a", "Almeida", "Coppa Sabatini", "", "oneday", 1, 120),
                ("a", "Almeida", "Tour of Britain", "Stage 2", "stage", 1, 110),
                ("c", "Soler", "Chrono des Nations", "", "oneday", 3, 200))
    assert _label(two)[0] == ("Almeida wins Coppa Sabatini & stage 4 win at Vuelta a España; "
                              "Wellens wins GP Québec; Almeida: stage 2 win at Tour of Britain + 1 more race")
    # only a named rider's further minor result is left: '+ 1 more result'
    minor = _rows(("t", "Takács", "Hungary nationals", "", "oneday", 1, 100),
                  ("p", "Peák", "Hungary nationals ITT", "", "oneday", 1, 50),
                  ("p", "Peák", "Hungary nationals", "", "oneday", 5, 40))
    assert _label(minor)[0] == "Hungary nationals: Takács 1st; Peák wins Hungary nationals ITT + 1 more result"


def test_team_time_trial_win_is_not_a_stage_win():
    # INEOS, Paris-Nice 2026: stage 3 was a TTT the team won (8.57 per rider)
    ineos = _rows(("v", "Vauquelin", "Paris-Nice", "General classification", "gc", 4, 300),
                  ("v", "Vauquelin", "Paris-Nice", "Stage 3 (TTT)", "stage", 1, 8.57),
                  ("g", "Godon", "Paris-Nice", "Stage 7", "stage", 1, 60),
                  ("g", "Godon", "Paris-Nice", "Stage 3 (TTT)", "stage", 1, 8.57),
                  ("t", "Tarling", "Paris-Nice", "Stage 3 (TTT)", "stage", 1, 8.57))
    label, contrib = _label(ineos)
    assert "stage 3 win" not in label and "2 stage wins" not in label
    assert label == "Vauquelin 4th at Paris-Nice; Godon stage 7 win"
    assert contrib[0]["detail"] == "GC 4th, stage 3 TTT 1st"
    groups = tp.contributors_for(ineos, float(ineos["points"].sum()))[1]
    assert [g["detail"] for g in groups if g["rider_slug"] == "g"] == ["stage 7 1st, stage 3 TTT 1st"]
    alone = _rows(("t", "Tarling", "Paris-Nice", "Stage 3 (TTT)", "stage", 1, 8.57))
    assert _label(alone)[0] == "Tarling: Paris-Nice TTT win"


def test_contributors_capped_at_four():
    df = _rows(*[(f"r{k}", f"R{k}", f"Race {k}", "", "oneday", 1, 100) for k in range(8)])
    contrib, groups = tp.contributors_for(df, 800.0)
    assert len(contrib) == 4 and len(groups) == 8


def test_detect_jumps_threshold_cap_and_fallback():
    weeks = tp.week_axis("2026-01-05", "2026-06-29")
    gains = pd.Series(0.0, index=weeks)
    rows = []
    big = {weeks[2]: 900, weeks[5]: 850, weeks[8]: 800, weeks[11]: 780, weeks[14]: 760, weeks[17]: 740,
           weeks[20]: 720, weeks[23]: 700}
    for i, w in enumerate(weeks):
        g = big.get(w, 50.0)
        gains[w] = g
        rows.append({"date": w, "week_start": w, "team_slug": "t", "team_name": "T", "rider_slug": f"r{i}",
                     "rider_name": f"Rider {i}", "rider_short": f"R{i}", "race_name": f"Race {i}",
                     "race_slug": f"race/r{i}/2026", "result_label": "", "result_kind": "oneday",
                     "race_class": "1.1", "position": 1, "status": "finished", "points": g})
    led = pd.DataFrame(rows, columns=tp.LEDGER_COLUMNS)
    total = float(gains.sum())
    jumps = tp.detect_jumps(gains, led, total)
    assert len(jumps) == 6                                   # 8 qualify, capped at 6, largest kept
    assert [j["gain"] for j in sorted(jumps, key=lambda j: -j["gain"])] == [900, 850, 800, 780, 760, 740]
    assert [j["week"] for j in jumps] == sorted(j["week"] for j in jumps)   # chronological
    j = jumps[0]
    assert j["week_index"] == weeks.index(j["week"])
    assert j["share_of_season"] == pytest.approx(round(900 / total, 4))
    assert j["label"] == "R2 wins Race 2"
    assert set(j) == {"week", "week_index", "gain", "share_of_season", "label", "contributors"}
    # flat season: nothing clears 2x the typical week -> the three biggest weeks still annotated
    flat = pd.Series([300.0, 310.0, 290.0, 305.0], index=weeks[:4])
    fl = led.iloc[:4].assign(points=flat.values)
    got = tp.detect_jumps(flat, fl, float(flat.sum()))
    assert [x["week"] for x in got] == [weeks[0], weeks[1], weeks[3]]
    got = tp.detect_jumps(flat, fl, float(flat.sum()), subject="rider")     # riders: at least two
    assert [x["week"] for x in got] == [weeks[1], weeks[3]]
    # never annotate weeks below min_abs, even as fallback
    tiny = pd.Series([20.0, 30.0], index=weeks[:2])
    assert tp.detect_jumps(tiny, led.iloc[:2].assign(points=[20.0, 30.0]), 50.0) == []


def test_weeks_meta_and_axis_start():
    led = _rows(("a", "A", "Tour de France", "Stage 2", "stage", 1, 210),
                ("b", "B", "Tour de France", "Stage 3", "stage", 2, 150),
                ("c", "C", "Tour of Qinghai", "General classification", "gc", 1, 300),
                ("a", "A", "Tour de France", "Penalty", "penalty", None, -25))
    meta = tp.weeks_meta(led, ["2026-07-13", "2026-07-20"])
    # the biggest race of the week over every rider, not only annotated jumps
    assert meta[1] == {"week": "2026-07-20", "top_race": "Tour de France", "top_race_points": 360.0,
                       "races": [{"race": "Tour de France", "points": 360.0},
                                 {"race": "Tour of Qinghai", "points": 300.0}]}
    assert meta[0]["top_race"] is None
    weeks = tp.week_axis("2025-10-20", "2026-02-16")
    gains = pd.DataFrame(0.0, index=weeks, columns=["t", "u"])
    gains.loc["2025-10-20", "t"] = 50.0          # late-2025 points count for 2026
    gains.loc["2026-01-05", "u"] = 5.0           # too small to start the chart
    gains.loc["2026-01-19", "u"] = 300.0
    finals = {"t": 1000.0, "u": 2000.0}
    assert tp.axis_start(weeks, gains, finals, 2026) == "2026-01-12"   # the week before
    assert tp.axis_start(weeks[11:], gains.iloc[11:], finals, 2026) is None  # no 2025 weeks: no trim


def test_page_total_note_explains_ties_and_negative_nets():
    bd = pd.DataFrame([
        {"team_slug": "eus", "rider_slug": "a", "nth_best": 20, "points_counted": 0.71, "points_not_counted": None},
        {"team_slug": "eus", "rider_slug": "b", "nth_best": 21, "points_counted": None, "points_not_counted": 0.71},
        {"team_slug": "kern", "rider_slug": "c", "nth_best": 20, "points_counted": 5.0, "points_not_counted": None},
        {"team_slug": "kern", "rider_slug": "d", "nth_best": 21, "points_counted": None, "points_not_counted": 2.0},
    ])
    assert tp._tie_on_20th(bd, "eus") == 0.71 and tp._tie_on_20th(bd, "kern") is None
    note = tp._page_total_note([("eus", "Euskaltel", 0.71), ("kern", "Kern Pharma", -10.0)], bd)
    assert "Euskaltel +0.71: two riders tie on 20th place and the page total counts both" in note
    assert "Kern Pharma -10: a rider with more sanction points than points" in note
    assert "mid-season" not in note


def test_detect_jumps_keeps_headline_wins():
    # Decathlon 2026: Seixas' Il Lombardia win (a Monument) came in a week
    # smaller than six weeks of placings; a WorldTour GC win (2.UWT) is added
    # while the cap for win weeks (here 7) allows, a 1.1 win never forces in.
    weeks = tp.week_axis("2026-01-05", "2026-06-29")
    gains = pd.Series(0.0, index=weeks)
    rows = []
    spec = {2: (1700, "Race A", "oneday", "1.Pro", 3), 4: (1600, "Race B", "oneday", "1.Pro", 2),
            6: (1500, "Race C", "oneday", "1.Pro", 2), 8: (1400, "Race D", "oneday", "1.Pro", 4),
            10: (1300, "Race E", "oneday", "1.Pro", 5), 12: (1250, "Race F", "oneday", "1.Pro", 2),
            14: (1180, "Il Lombardia", "oneday", "1.UWT", 1),
            16: (900, "Tour de Suisse", "gc", "2.UWT", 1),
            18: (850, "Volta a Portugal", "gc", "2.1", 1),
            20: (600, "Amstel Gold Race", "oneday", "1.UWT", 1)}
    for i, w in enumerate(weeks):
        pts, race, kind, cls, pos = spec.get(i, (40.0, f"Small {i}", "oneday", "1.2", 9))
        gains[w] = pts
        rows.append({"date": w, "week_start": w, "team_slug": "t", "team_name": "T", "rider_slug": f"r{i}",
                     "rider_name": f"Rider {i}", "rider_short": f"R{i}", "race_name": race,
                     "race_slug": f"race/r{i}/2026", "result_label": "General classification" if kind == "gc" else "",
                     "result_kind": kind, "race_class": cls, "position": pos, "status": "finished",
                     "points": float(pts)})
    led = pd.DataFrame(rows, columns=tp.LEDGER_COLUMNS)
    led["position"] = led["position"].astype("Int64")
    jumps = tp.detect_jumps(gains, led, float(gains.sum()), max_with_wins=8)
    got = {weeks.index(j["week"]) for j in jumps}
    assert got == {2, 4, 6, 8, 10, 12, 14, 16}      # 6 by gain + the Monument + one WT GC (cap 8)
    assert [j["label"] for j in jumps if weeks.index(j["week"]) == 14] == ["R14 wins Il Lombardia"]
    assert tp.headline_tier("World Championships Road Race", "WC", "oneday", 1) == 1
    assert tp.headline_tier("World Championships U23 Road Race", "WC", "oneday", 1) == 0
    assert tp.headline_tier("Tour de France", "2.UWT", "gc", 1) == 1
    assert tp.headline_tier("Paris-Nice", "2.UWT", "stage", 1, "Stage 3 (TTT)") == 0


# ---------------------------------------------------------------------------
# payload
# ---------------------------------------------------------------------------

def test_payload_schema(payload):
    assert set(payload) == {"meta", "weeks", "weeks_meta", "teams", "riders", "official"}
    meta = payload["meta"]
    assert {"title", "season", "gender", "generated_at", "data_through", "week_convention", "source",
            "metric_default", "coverage", "axis_start"} <= set(meta)
    assert [m["week"] for m in payload["weeks_meta"]] == payload["weeks"]
    sanremo = payload["weeks_meta"][payload["weeks"].index("2026-03-16")]
    assert sanremo["top_race"] == "Milano-Sanremo" and sanremo["top_race_points"] == 1440.0   # 800 + 640
    assert meta["season"] == 2026 and meta["gender"] == "men" and meta["metric_default"] == "all_riders"
    assert meta["generated_at"] == "2026-08-14 12:00" and meta["data_through"] == "2026-08-12"
    assert meta["week_convention"].startswith("ISO weeks, Monday start")
    cov = meta["coverage"]
    assert cov["teams"] == 3 and cov["penalties"] == 2
    assert cov["riders"] == 25                      # 23 alpha (incl. Mover) + van der Poel + Climber
    assert isinstance(cov["results_with_points"], int)
    rec = cov["reconciliation"]
    assert rec["teams_exact"] == 3 and rec["max_abs_team_diff"] == 0
    assert isinstance(rec["note"], str) and all(isinstance(n, str) for n in cov["notes"])
    assert payload["official"] == {"ranking": RANKING, "date": "2026-08-14",
                                   "label": "Official UCI team ranking 2026 (PCS)"}
    n = len(payload["weeks"])
    assert [t["key"] for t in payload["teams"]] == ["team-alpha-2026", "team-beta-2026", "team-gamma-2026"]
    team_keys = {"key", "name", "short", "class", "official_rank", "official_total", "breakdown_total",
                 "final_total", "final_rank", "cumulative", "gains", "rank_by_week", "cumulative_best20",
                 "final_total_best20", "jumps", "top_riders"}
    for t in payload["teams"]:
        assert set(t) == team_keys
        assert len(t["cumulative"]) == len(t["gains"]) == len(t["rank_by_week"]) == len(t["cumulative_best20"]) == n
        assert isinstance(t["official_rank"], int) and isinstance(t["final_rank"], int)
        assert t["class"] in ("WT", "PRT")
        assert 1 <= len(t["jumps"]) <= 6
        for j in t["jumps"]:
            assert payload["weeks"][j["week_index"]] == j["week"]
            assert t["gains"][j["week_index"]] == j["gain"]
            assert {"rider", "race", "detail", "points"} == set(j["contributors"][0])
    json.dumps(payload, allow_nan=False)            # strict JSON (no NaN/inf)


def test_payload_team_values(payload):
    a = _team(payload, "team-alpha-2026")
    assert a["short"] == "UAE" and a["official_total"] == 4400 and a["official_rank"] == 1
    assert a["final_total"] == 4515 and a["final_total_best20"] == 4509 and a["breakdown_total"] == 4509
    assert a["final_rank"] == 1 and a["cumulative"][-1] == 4515
    assert a["top_riders"][0] == {"rider": "Tadej Pogačar", "points": 3435.0}
    labels = [j["label"] for j in a["jumps"]]
    # the final week of a stage race speaks for the whole race: the stage 14
    # win (previous week) is in the label and the detail, the points are the week's
    assert labels == ["Pogačar wins Milano-Sanremo", "Pogačar wins Ronde van Vlaanderen",
                      "Pogačar: Tour de France GC 1st + 1 stage win"]
    tdf = a["jumps"][-1]
    assert tdf["week"] == "2026-07-20" and tdf["gain"] == 2200
    assert tdf["contributors"] == [
        {"rider": "Tadej Pogačar", "race": "Tour de France",
         "detail": "GC 1st, KOM 2nd, leader 2 days; also won stage 14 earlier", "points": 1500.0},
        {"rider": "Isaac del Toro", "race": "Tour de France", "detail": "GC 3rd", "points": 700.0}]
    b = _team(payload, "team-beta-2026")
    assert b["short"] == "Red Bull-Bora" and b["final_total"] == 2130
    assert [j["label"] for j in b["jumps"]] == ["Van der Poel 2nd at Milano-Sanremo",
                                                 "Van der Poel 2nd at Ronde van Vlaanderen",
                                                 "Van der Poel wins Paris-Roubaix"]
    g = _team(payload, "team-gamma-2026")
    assert g["short"] == "Bardiani CSF" and g["class"] == "PRT"
    assert [j["label"] for j in g["jumps"]] == [
        "Climber: Tour of the Alps GC 1st + 2 stage wins + points 1st", "Climber 2nd at Giro d'Italia"]
    # weekly ranks are among all four listing teams
    assert max(max(t["rank_by_week"]) for t in payload["teams"]) <= 4


def test_payload_rider_alone(payload):
    assert len(payload["riders"]) == 1
    r = payload["riders"][0]
    assert {"key", "kind", "name", "short", "team_slug", "team_short", "official_total", "final_total",
            "would_rank", "cumulative", "gains", "rank_by_week", "cumulative_best20", "jumps",
            "top_riders"} <= set(r)
    assert r["key"] == "rider:tadej-pogacar" and r["kind"] == "rider"
    assert r["name"] == "Tadej Pogačar" and r["short"] == "Pogačar"
    assert r["team_slug"] == "team-alpha-2026" and r["team_short"] == "UAE"
    assert r["official_total"] == 3435 and r["final_total"] == 3435
    assert r["top_riders"] is None
    assert r["cumulative_best20"] == r["cumulative"] and len(r["cumulative"]) == len(payload["weeks"])
    assert r["would_rank"] == 2                     # alpha 4515 > 3435 > beta 2130
    assert r["would_rank_official"] == 2            # vs listing 4400 / 2100 / 765 / 10
    i = payload["weeks"].index("2026-03-16")         # Sanremo week: 800 vs alpha 900, beta 640
    assert r["rank_by_week"][i] == 2
    labels = [j["label"] for j in r["jumps"]]
    # the Ronde week (775 after the sanction) is below the gain threshold but a Monument win
    assert labels == ["Wins Milano-Sanremo", "Wins Ronde van Vlaanderen", "Tour de France: GC 1st + 1 stage win"]
    assert r["jumps"][2]["share_of_season"] == pytest.approx(round(1500 / 3435, 4))


def test_rider_alone_counts_points_for_any_team(conn):
    p = tp.build_chart_payload(conn, season=2026, top_n=2, generated_at="x", riders=("jan-mover",))
    r = p["riders"][0]
    assert r["final_total"] == 190                  # dev team 40 + alpha 100 + beta 50
    assert r["official_total"] == 150               # breakdown entries of the listed teams
    assert r["team_slug"] == "team-beta-2026"       # his team at the end of the data
    assert len(p["teams"]) == 2
    assert tp.build_chart_payload(conn, season=2026, top_n=2, generated_at="x", riders=("nobody",))["riders"] == []


def test_payload_without_listing_raises(tmp_path):
    path = tmp_path / "empty.db"
    c = sqlite3.connect(path)
    ensure_uci_points_schema(c)
    with pytest.raises(ValueError):
        tp.build_chart_payload(c, 2026)
    c.close()


def test_listing_snapshot_finds_partial_day():
    rows = []

    def add(day, team, rider, race, pts):
        rows.append({"date": day, "team_slug": team, "rider_slug": rider, "race_name": race, "points": pts})
    add("2026-10-17", "a", "a1", "Race X", 100.0)
    add("2026-10-17", "b", "b1", "Race X", 80.0)
    add("2026-10-18", "a", "a1", "Asia Tour", 30.0)
    add("2026-10-18", "b", "b2", "Asia Tour", 10.0)
    add("2026-10-18", "a", "a2", "Euro Classic", 50.0)
    add("2026-10-18", "b", "b1", "Euro Classic", 40.0)
    led = pd.DataFrame(rows)
    listing = pd.DataFrame({"team_slug": ["a", "b"], "points": [130.0, 90.0]})  # 17th + Asia Tour only
    snap = tp.listing_snapshot(led, listing, "2026-10-18")
    assert snap["date"] == "2026-10-18"
    assert snap["included"] == ["Asia Tour"] and snap["excluded"] == ["Euro Classic"]
    assert snap["teams_matched"] == 2 and snap["matched_full_day"] == 0


def test_markdown_summary(payload):
    md = tp.markdown_summary(payload)
    assert ("| 1 | UAE Team Emirates - XRG | 4,400 | 4,515 | 4,509 | "
            "Pogačar: Tour de France GC 1st + 1 stage win (2026-07-20, +2,200) |") in md
    assert "Pogačar alone: 3,435 pts — would rank 2nd among teams" in md
    assert sum(line.startswith("| ") for line in md.splitlines()) == 1 + 3   # header + three teams


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_cli_end_to_end(db_path, tmp_path, capsys, monkeypatch):
    from scripts import team_points_report as cli
    seen = {}

    def fake_render(payload):
        seen["payload"] = payload
        return "<html><body>chart</body></html>"
    monkeypatch.setitem(sys.modules, "analysis.team_points_chart",
                        types.SimpleNamespace(render_html=fake_render))
    before = _sha(db_path)
    out_json, out_html = tmp_path / "out" / "tp.json", tmp_path / "out" / "tp.html"
    rc = cli.run(["--season", "2026", "--top", "3", "--db", str(db_path), "--out-json", str(out_json),
                  "--out-html", str(out_html), "--generated-at", "2026-08-14 12:00"])
    assert rc == 0
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["meta"]["generated_at"] == "2026-08-14 12:00"
    assert [t["key"] for t in payload["teams"]] == ["team-alpha-2026", "team-beta-2026", "team-gamma-2026"]
    assert payload["riders"][0]["key"] == "rider:tadej-pogacar"
    assert out_html.read_text() == "<html><body>chart</body></html>"
    assert seen["payload"]["meta"]["title"] == payload["meta"]["title"]
    out = capsys.readouterr().out
    assert "| # | Team | Official | Ledger (all riders) | Best-20 | Biggest jump |" in out
    assert "Pogačar alone: 3,435 pts — would rank 2nd among teams" in out
    assert _sha(db_path) == before                  # opened read-only, untouched


def test_cli_rider_flag_and_errors(db_path, tmp_path, capsys):
    from scripts import team_points_report as cli
    rc = cli.run(["--db", str(db_path), "--top", "2", "--rider", "jan-mover", "--rider", "tadej-pogacar",
                  "--out-json", str(tmp_path / "p.json")])
    assert rc == 0
    payload = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))
    assert [r["key"] for r in payload["riders"]] == ["rider:jan-mover", "rider:tadej-pogacar"]
    assert "Mover alone: 190 pts" in capsys.readouterr().out
    assert cli.run(["--db", str(tmp_path / "missing.db")]) == 1
    bare = tmp_path / "bare.db"
    sqlite3.connect(bare).close()
    assert cli.run(["--db", str(bare)]) == 1
    with pytest.raises(sqlite3.OperationalError):
        cli.open_readonly(db_path).execute("CREATE TABLE x (y)")


def test_cli_html_renderer_missing(db_path, tmp_path, capsys, monkeypatch):
    from scripts import team_points_report as cli
    monkeypatch.setitem(sys.modules, "analysis.team_points_chart", None)   # import -> ImportError
    out_json = tmp_path / "p.json"
    rc = cli.run(["--db", str(db_path), "--top", "2", "--out-json", str(out_json),
                  "--out-html", str(tmp_path / "p.html")])
    assert rc == 1
    assert out_json.exists() and not (tmp_path / "p.html").exists()
    captured = capsys.readouterr()
    assert "cannot render HTML" in captured.err and "| # | Team |" in captured.out


# ---------------------------------------------------------------------------
# rider_uci_results holds scoring rows only (db.uci_points): the readers
# must not change when the zero-point rows of an older DB are pruned
# ---------------------------------------------------------------------------

def _with_zero_point_rows(src, dst):
    """Copy of the synthetic DB plus zero-point rows the readers once needed:
    del Toro's only Vuelta a Burgos row (his sanction's race), Pogačar's last
    race day (the "data through" date), and a sanction-only rider whose page
    name (<h1>) only his zero-point row carries."""
    import shutil
    shutil.copy(src, dst)
    conn = sqlite3.connect(dst)
    conn.executemany(
        "INSERT INTO rider_uci_results (season, rider_slug, rider_name, date, race_name, race_slug, "
        "result_label, result_kind, race_class, position, status, points_uci, points_pcs, result_url) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
            _result("isaac-del-toro", "Isaac del Toro", "2026-08-12", "Vuelta a Burgos", "Stage 1", "stage",
                    40, None, "race/vuelta-a-burgos-2026-stage-1", "race/vuelta-a-burgos/2026", "2.Pro"),
            _result("tadej-pogacar", "Tadej Pogačar", "2026-08-20", "Late Race", "", "oneday", 12, None,
                    "race/late-race-2026-result", "race/late-race/2026", "1.1"),
            _result("penny-only", "Penny Only", "2026-05-03", "Gamma GP", "", "oneday", 80, None,
                    "race/gamma-gp-2026-result", "race/gamma-gp/2026", "1.1")])
    conn.execute("INSERT INTO uci_team_breakdown (season, team_slug, team_name, team_class, rider_slug, "
                 "rider_name, nth_best, points_counted, points_not_counted, sanction_points) VALUES "
                 "(2026, 'team-gamma-2026', 'Bardiani CSF 7 Saber', 'PRT', 'penny-only', 'ONLY P', 2, "
                 "NULL, NULL, 10)")
    conn.execute("INSERT INTO rider_uci_penalties (season, rider_slug, date, race_name, penalty_points) "
                 "VALUES (2026, 'penny-only', '2026-05-03', 'Gamma GP', 10)")
    conn.commit()
    conn.close()


def test_readers_unchanged_after_pruning_zero_point_rows(db_path, tmp_path):
    from db.uci_points import prune_zero_point_results
    path = tmp_path / "legacy.db"
    _with_zero_point_rows(db_path, path)

    def read():
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            led = tp.load_points_ledger(c, 2026)
            pens = led[led["result_kind"] == "penalty"].set_index("rider_slug")
            names = tp._rider_names(c, 2026, tp.load_breakdown(c, 2026))
            payload = tp.build_chart_payload(c, season=2026, top_n=3, generated_at="2026-08-21 12:00")
            return led, pens, names, tp.data_through(c, 2026), payload
        finally:
            c.close()
    led0, pens0, names0, through0, payload0 = read()
    assert through0 == "2026-08-20"
    assert pens0.loc["isaac-del-toro", "race_slug"] == "race/vuelta-a-burgos/2026"
    assert pens0.loc["isaac-del-toro", "race_class"] == "2.Pro"
    assert pens0.loc["penny-only", "race_slug"] == "race/gamma-gp/2026"
    assert names0["penny-only"] == ("Penny Only", "Only")

    conn = sqlite3.connect(path)
    got = prune_zero_point_results(conn, 2026)
    conn.commit()
    assert got["deleted"] == 4 and got["penalties_resolved"] == 3
    assert conn.execute("SELECT COUNT(*) FROM rider_uci_results WHERE points_uci IS NULL").fetchone()[0] == 0
    conn.close()

    led1, pens1, names1, through1, payload1 = read()
    assert through1 == through0                       # from rider_uci_scrapes.last_date
    pd.testing.assert_frame_equal(led1, led0)         # penalties keep their race (stored column)
    assert names1 == names0                           # sanction-only rider: name from his scrape record
    assert payload1 == payload0


def test_penalty_race_stored_wins_over_lookup(tmp_path):
    path = tmp_path / "p.db"
    build_db(path)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE rider_uci_penalties SET race_slug = 'race/vuelta-a-burgos/2026', race_class = '2.Pro' "
                 "WHERE rider_slug = 'isaac-del-toro'")
    conn.commit()
    ev = tp.load_events(conn, 2026)
    ev = ev[ev["result_kind"] == "penalty"].set_index("rider_slug")
    assert tuple(ev.loc["isaac-del-toro", ["race_slug", "race_class"]]) == ("race/vuelta-a-burgos/2026", "2.Pro")
    # a penalty without one still borrows the rider's (scoring) row of that race
    assert ev.loc["tadej-pogacar", "race_slug"] == "race/ronde-van-vlaanderen/2026"
    # a DB from before the columns existed (read-only, never migrated) reads the same way
    conn.executescript("""
        CREATE TABLE p2 AS SELECT season, rider_slug, date, race_name, penalty_points, fine, article
                           FROM rider_uci_penalties;
        DROP TABLE rider_uci_penalties; ALTER TABLE p2 RENAME TO rider_uci_penalties;""")
    old = tp.load_events(conn, 2026)
    old = old[old["result_kind"] == "penalty"].set_index("rider_slug")
    assert old.loc["tadej-pogacar", "race_slug"] == "race/ronde-van-vlaanderen/2026"
    assert pd.isna(old.loc["isaac-del-toro", "race_slug"])
    conn.close()


def test_match_penalty_race():
    rows = [("2026-03-27", "E3 Saxo Classic ME", "race/e3-harelbeke/2026", "1.UWT"),
            ("2026-07-04", "Tour de France", "race/tour-de-france/2026", "2.UWT"),
            ("2026-07-25", "Tour de France", "race/tour-de-france/2026", "2.UWT")]
    assert tp.match_penalty_race("E3 Saxo Classic", "2026-03-27", rows) == ("race/e3-harelbeke/2026", "1.UWT")
    assert tp.match_penalty_race("Tour de France | Stage 20", "2026-07-24", rows) == (
        "race/tour-de-france/2026", "2.UWT")   # nearest day of the race
    assert tp.match_penalty_race("Giro d'Italia | Stage 3", "2026-05-11", rows) == (None, None)
    assert tp.match_penalty_race(None, "2026-05-11", []) == (None, None)
