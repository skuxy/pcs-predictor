"""Tests for the UCI team-ranking scraper (scraper/uci_points.py), its storage
(db/uci_points.py) and the CLI (scripts/scrape_uci_points.py).

Every HTML snippet is trimmed from the live PCS markup of October 2026.
"""
import importlib.util
import pathlib
import sqlite3
from collections import defaultdict

import pytest

from scraper import uci_points as up
from scraper.utils import soup
from db.uci_points import (ensure_uci_points_schema, upsert_team_totals, upsert_team_breakdown,
                           replace_rider_results, replace_rider_penalties, replace_rider_spells,
                           upsert_team_recent_points)

ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("scrape_uci_points", ROOT / "scripts" / "scrape_uci_points.py")
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)


# ---------------------------------------------------------------------------
# live-markup snippets
# ---------------------------------------------------------------------------

LISTING = """
<h1>UCI World Ranking</h1>
<form action="rankings.php"><span class="inputCont"><select name="date">
  <option value="2026-10-18">2026-10-18</option></select></span></form>
<table class="basic"><thead><tr>
<th class="w9">#</th><th class="w9 cu600 fs10">Prev.</th><th class="w9 fs10">Diff.</th>
<th class="w36">Team</th><th class="w9 cu600">Class</th><th class="w9">Points</th><th class="w18 cu600"></th>
</tr></thead><tbody>
<tr class=""><td>1</td><td class="cu600 fs10"></td><td class="fs10">-</td><td><span class="flag ae"></span> <a href="team/uae-team-emirates-xrg-2026">UAE Team Emirates - XRG</a></td><td class="cu600">WT</td><td><a href="team/uae-team-emirates-xrg-2026/results/ranking-2026-2028">30607</a></td><td class="cu600"><div class="valuebar"><div class="w80 bg green4 left"></div><div class="title l80">30607</div></div></td></tr>
<tr class=""><td>13</td><td class="cu600 fs10"></td><td class="fs10">-</td><td><span class="flag ch"></span> <a href="team/pinarello-q365-pro-cycling-team-2026">Pinarello Q36.5 Pro Cycling Team</a></td><td class="cu600">PRT</td><td><a href="team/pinarello-q365-pro-cycling-team-2026/results/ranking-2026-2028">10722</a></td><td class="cu600"></td></tr>
</tbody></table>
"""

BREAKDOWN = """
<h1>UAE Team Emirates - XRG <span class="hideIfMobile">(WT)</span></h1>
<table class="basic"><thead><tr><th class="w50">Season</th><th class="w50">Points</th></tr></thead><tbody>
<tr class=""><td>2026</td><td>30982.1</td></tr>
<tr class="sum"><td class=""></td><td class="">30982.1</td></tr></tbody></table>
<table class="basic"><thead><tr>
<th class="w11">Season</th><th class="w44">Rider</th><th class="w11">Nth best rider for team</th>
<th class="w11">Points counted</th><th class="w11">Not counted</th><th class="w11">Sanction points</th>
</tr></thead><tbody>
<tr class=""><td>2026</td><td><span class="flag si"></span> <a href="rider/tadej-pogacar">POGAČAR Tadej</a></td><td>1</td><td>9331.75</td><td>-</td><td>25</td></tr>
<tr class=""><td>2026</td><td><span class="flag at"></span> <a href="rider/felix-grossschartner">GROßSCHARTNER Felix</a></td><td>18</td><td>374.32</td><td>-</td><td>-</td></tr>
<tr class=""><td>2026</td><td><span class="flag pt"></span> <a href="rider/rui-oliveira">OLIVEIRA Rui</a></td><td>24</td><td>-</td><td>164</td><td>30</td></tr>
<tr class="sum"><td class=""></td><td class=""></td><td class=""></td><td class="">30982.1</td><td class=""></td><td class="">140</td></tr>
</tbody></table>
"""

RESULTS_HEAD = """
<h1 translate="no">Tadej Pogačar</h1>
<form action="rider.php"><input type="hidden" name="id" value="194619"><input type="hidden" name="sportid" value="1"></form>
<table class="basic"><thead><tr>
<th class="w3">#</th><th class="w9">Date</th><th class="w5">Result</th><th class="w37">Race</th>
<th class="w9 cu600">Class</th><th class="w9 cu600">KMs</th><th class="w9">PCS points</th>
<th class="w9 cu600">UCI points</th><th class="w9">Vert. mtr</th></tr></thead><tbody>
"""
RESULTS_TAIL = """<tr class="sum"><td></td><td></td><td></td><td></td><td></td><td>14030</td><td>7617</td><td>17916.75</td><td>216768</td></tr>
</tbody></table>"""


def _res_row(n, d, res, race, href, cls="2.UWT", pcs="-", uci="-"):
    return (f'<tr class=""><td>{n}</td><td>{d}</td><td>{res}</td><td><span class="flag fr"></span> '
            f'<a href="{href}">{race}</a></td><td class="cu600">{cls}</td><td class="cu600">150</td>'
            f'<td>{pcs}</td><td class="cu600">{uci}</td><td>1000</td></tr>')


RESULTS = RESULTS_HEAD + "".join([
    _res_row(1, "2026-08-29", "DNF", "La Vuelta Ciclista a España | Stage 8", "race/vuelta-a-espana-2026-stage-8"),
    _res_row(2, "2026-08-24", "NR", "La Vuelta Ciclista a España | Stage 3", "race/vuelta-a-espana-2026-stage-3"),
    _res_row(3, "2026-07-26", "2", "Tour de France | Mountains classification", "race/tour-de-france-2026-kom", pcs="25", uci="150"),
    _res_row(4, "2026-07-26", "6", "Tour de France | Points classification", "race/tour-de-france-2026-points"),
    _res_row(5, "2026-07-26", "1", "Tour de France | General classification", "race/tour-de-france-2026-gc", pcs="500", uci="1300"),
    _res_row(6, "2026-07-26", "30", "Tour de France | Stage 21", "race/tour-de-france-2026-stage-21"),
    _res_row(7, "2026-07-09", "1", "Tour de France | Stage 6", "race/tour-de-france-2026-stage-6", pcs="100", uci="210"),
    _res_row(8, "2026-07-04", "3", "Tour de France | Stage 1 (TTT)", "race/tour-de-france-2026-stage-1", pcs="30", uci="13.75"),
    _res_row(9, "2026-04-28", "6", "Tour de Romandie | Prologue", "race/tour-de-romandie-2026-prologue", pcs="7", uci="15"),
    _res_row(10, "2026-04-05", "1", "Ronde van Vlaanderen ME", "race/ronde-van-vlaanderen-2026-result", cls="1.UWT",
             pcs="275", uci='800 <span class="red fs10">-25</span>'),
    _res_row(11, "2025-10-11", "1", "Il Lombardia", "race/il-lombardia-2025-result", cls="1.UWT", pcs="275", uci="800"),
]) + RESULTS_TAIL

RANKING = """
<h1 translate="no">Tadej Pogačar</h1>
<form action="rider.php"><input type="hidden" name="id" value="194619"><input name="date" type="text" value="2026-10-18"/></form>
<table class="basic"><thead><tr><th class="w14">Date start</th><th class="w57">Race</th><th class="w14">Class</th><th class="w14">Points</th></tr></thead><tbody>
<tr class=""><td>2026-07-04</td><td><span class="flag fr"></span> <a href="race/tour-de-france-2026-gc">Tour de France</a></td><td>2.UWT</td><td>1748.75</td></tr>
<tr class=""><td>2026-04-05</td><td><span class="flag be"></span> <a href="race/ronde-van-vlaanderen-2026-result">Ronde van Vlaanderen ME</a></td><td>1.UWT</td><td>800</td></tr>
<tr class=""><td>2025-10-25</td><td><span class="flag mx"></span> <a href="race/nc-mexico-2025-result">National Championships Mexico ME - Road Race</a></td><td>NC</td><td>100</td></tr>
<tr class=""><td></td><td><a href="race/">Sanctions</a></td><td></td><td>-25</td></tr>
<tr class="sum"><td class=""></td><td class=""></td><td class=""></td><td class="">2623.75</td></tr>
</tbody></table>
<table class="basic"><thead><tr><th class="w22">Date</th><th class="w56">Race</th><th class="w11">Result</th><th class="w11 pnts2">Points</th></tr></thead><tbody>
<tr class=""><td>2026-07-26</td><td><span class="flag fr"></span> <a href="race/">Tour de France | Mountains classification</a></td><td>2</td><td class="pnts2">150</td></tr>
<tr class=""><td>2026-07-26</td><td><span class="flag fr"></span> <a href="race/">Tour de France | General classification</a></td><td>1</td><td class="pnts2">1300</td></tr>
<tr class=""><td>2026-07-09</td><td><span class="flag fr"></span> <a href="race/">Tour de France | Stage 6</a></td><td>1</td><td class="pnts2">210</td></tr>
<tr class=""><td>2026-07-09</td><td><span class="flag fr"></span> <a href="race/">Tour de France | General classification</a></td><td>1</td><td class="pnts2">25</td></tr>
<tr class=""><td>2026-07-04</td><td><span class="flag fr"></span> <a href="race/">Tour de France | Stage 1 (TTT)</a></td><td>3</td><td class="pnts2">13.75</td></tr>
<tr class=""><td>2026-04-05</td><td><span class="flag be"></span> <a href="race/">Ronde van Vlaanderen ME</a></td><td>1</td><td class="pnts2">800</td></tr>
<tr class=""><td>2026-04-05</td><td><a href="race/">Sanctions</a></td><td></td><td class="pnts2">-25</td></tr>
<tr class=""><td>2025-10-25</td><td><span class="flag mx"></span> <a href="race/">National Championships Mexico ME - Road Race</a></td><td>1</td><td class="pnts2">100</td></tr>
<tr class="sum"><td class=""></td><td class=""></td><td class=""></td><td class="pnts2">2573.75</td></tr>
</tbody></table>
"""

PROFILE = """
<h1 translate="no">Tadej Pogačar</h1>
<ul class="rdrSeasonNav2 left fix"><li class="tab cur"><a class="" data-id="2026" href="?x=1&amp;id=194619&amp;date1=2026-01-01&amp;date2=2026-12-31&amp;sportid=1">2026</a></li>
<li class="tab"><a class="" data-id="2025" href="?x=1">2025</a></li></ul>
<table class="rdrResults"><thead><tr><th class="date"><br/>Date</th><th class="result">Result</th><th class="gc_rnk"></th><th class="res_icon"></th><th class="race">Race</th><th class="jersey hide"></th><th class="worn"></th><th class="distance">Distance</th><th class="pcs">Points<br/>PCS</th><th class="uci">Points<br/>UCI</th><th class="more"></th></tr></thead><tbody>
<tr class="main"><td>04.07 › 26.07</td><td></td><td></td><td></td><td><span class="flag fr"></span> <a href="race/tour-de-france-2026-stage-1"><span>Tour de France</span> (2.UWT)</a></td><td></td><td></td><td></td><td></td><td><a class="fs11" href="rider-in-race/tadej-pogacar/tour-de-france">more</a></td></tr>
<tr class="stage"><td></td><td>1</td><td></td><td><span class="svg_shirt w14 mx4" data-color="yellow"></span></td><td><a href="race/tour-de-france-2026-gc">General classification</a></td><td></td><td></td><td>500</td><td>1300</td><td></td></tr>
<tr class="stage"><td>26.07</td><td>30</td><td>1</td><td></td><td><a href="race/tour-de-france-2026-stage-21"><span class="imob">21</span><span class="idesk">Stage 21</span> - Paris › Paris</a></td><td><span class="svg_shirt w14 mx4" data-color="yellow"></span></td><td>88.7</td><td></td><td> <span class="gc_pnt">+1300</span></td><td></td></tr>
<tr class="stage"><td>09.07</td><td>1</td><td>1</td><td></td><td><a href="race/tour-de-france-2026-stage-6"><span class="imob">6</span><span class="idesk">Stage 6</span> - Pau › Gavarnie-Gèdre</a></td><td><span class="svg_shirt w14 mx4" data-color="yellow"></span></td><td>186.2</td><td>100</td><td>210 <span class="gc_pnt">+25</span></td><td></td></tr>
<tr class="main"><td>05.04</td><td>1</td><td></td><td></td><td><span class="flag be"></span> <a href="race/ronde-van-vlaanderen-2026-result"><span>Ronde van Vlaanderen ME</span> (1.UWT)</a></td><td></td><td>278.2</td><td>275</td><td>800 <font class="red fs10">-25</font></td><td><a class="fs11" href="rider-in-race/tadej-pogacar/ronde-van-vlaanderen">more</a></td></tr>
</tbody></table>
<ul class="rdr-teams2">
<li class="main"><div class="season">2027</div><div class="name"><a href="team/xds-astana-team-2027">XDS Astana Team</a> (WT)<span class="fs10"></span></div></li>
<li class="main"><div class="season">2026</div><div class="name"><a href="team/xds-astana-team-2026">XDS Astana Team</a> (WT)<span class="fs10"> as from 01/08</span></div></li>
<li class="main"><div class="season">2026</div><div class="name"><a href="team/netcompany-ineos-2026">Netcompany INEOS</a> (WT)<span class="fs10"> until 31/07</span></div></li>
<li class="main old"><div class="season">2024</div><div class="name"><a href="team/burgos-bh-2024">Burgos - BH</a> (PRT)<span class="fs10"></span></div></li>
</ul>
"""

FINES = """
<table class="basic"><thead><tr><th class="w7">Date</th><th class="w28">Race</th><th class="w7">Penalty points</th><th class="w7">Fine</th><th class="w9 fs10">Other penalties</th><th class="w42">Article</th></tr></thead><tbody>
<tr class=""><td>2026-04-05</td><td><span class="flag be"></span> <a href="race/ronde-van-vlaanderen-2026-result">Ronde van Vlaanderen ME</a></td><td>25</td><td>500</td><td class="fs10"> </td><td>2.12.007 8.3 | Disposing of waste outside of litter zones.</td></tr>
<tr class=""><td>2026-03-07</td><td><a href="race/strade-bianche-2026-result">Strade Bianche</a></td><td>-</td><td>200</td><td class="fs10"> </td><td>2.12.007 8.1 | Failing to respect the instructions</td></tr>
<tr class=""><td>2023-08-06</td><td><a href="race/world-championship-2023-result">World Championships ME - Road Race</a></td><td>15</td><td>500</td><td class="fs10"> </td><td>2.12.007 8.1</td></tr>
<tr class="sum"><td class=""></td><td class=""></td><td class="">40</td><td class="">1200</td><td class="fs10"></td><td class=""></td></tr>
</tbody></table>
"""

RECENT = """
<table class="basic"><thead><tr><th>Date</th><th>UCI Points</th><th>(Indirect)</th></tr></thead><tbody>
<tr class=""><td>2026-10-18</td><td>722</td><td>-</td></tr>
<tr class=""><td>2026-09-27</td><td>1565</td><td>1415</td></tr>
<tr class="sum"><td></td><td>2287</td><td></td></tr>
</tbody></table>
"""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def test_licence_cycle():
    assert up.licence_cycle(2026) == "2026-2028"
    assert up.licence_cycle(2028) == "2026-2028"
    assert up.licence_cycle(2025) == "2023-2025"
    assert up.team_ranking_path(2026) == "rankings/me/ranking-2026-2028"


@pytest.mark.parametrize("text,expected", [
    ("9331.75", 9331.75), ("30607", 30607.0), ("-", None), ("", None), (" 0.71 ", 0.71),
])
def test_parse_number(text, expected):
    assert up.parse_number(text) == expected


@pytest.mark.parametrize("href,year,expected", [
    ("race/tour-de-france-2026-stage-6", 2026, "race/tour-de-france/2026"),
    ("race/milano-sanremo-2026-result", 2026, "race/milano-sanremo/2026"),
    ("race/tour-de-france-2026-gc", 2026, "race/tour-de-france/2026"),
    ("race/nc-mexico-2025-result", 2025, "race/nc-mexico/2025"),
    ("race/tour-de-france/2026/stage-6", 2026, "race/tour-de-france/2026"),
    ("race/gp-2000-2026-result", 2026, "race/gp-2000/2026"),
    ("team/uae-2026", 2026, None),
])
def test_race_slug_from_href(href, year, expected):
    assert up.race_slug_from_href(href, year) == expected


@pytest.mark.parametrize("label,kind", [
    ("", "oneday"), ("Stage 6", "stage"), ("Stage 1 (TTT)", "stage"), ("Prologue", "stage"),
    ("General classification", "gc"), ("Points classification", "points"),
    ("Mountains classification", "kom"), ("Youth classification", "youth"),
    ("Teams classification", "other"),
])
def test_result_kind(label, kind):
    assert up.result_kind(label) == kind


def test_parse_position():
    assert up.parse_position("1") == (1, "finished")
    assert up.parse_position("DNF") == (None, "dnf")
    assert up.parse_position("OTL") == (None, "otl")
    assert up.parse_position("NR") == (None, "nr")


def test_split_points_cell_penalty_span_is_not_concatenated():
    # The old parser read '800 -25' as 80025.
    td = soup('<table><tr><td class="cu600">800 <span class="red fs10">-25</span></td></tr></table>').td
    assert up.split_points_cell(td) == (800.0, 25.0, None)
    td = soup('<table><tr><td>15 <font class="red fs10">-25</font></td></tr></table>').td
    assert up.split_points_cell(td) == (15.0, 25.0, None)


def test_split_points_cell_gc_bonus():
    td = soup('<table><tr><td>180 <span class="gc_pnt">+20</span></td></tr></table>').td
    assert up.split_points_cell(td) == (180.0, None, 20.0)
    td = soup('<table><tr><td> <span class="gc_pnt">+25</span></td></tr></table>').td
    assert up.split_points_cell(td) == (None, None, 25.0)
    td = soup('<table><tr><td class="cu600">-</td></tr></table>').td
    assert up.split_points_cell(td) == (None, None, None)


def test_display_name():
    assert up.display_name("POGAČAR Tadej") == "Tadej Pogačar"
    assert up.display_name("GROßSCHARTNER Felix") == "Felix Großschartner"
    assert up.display_name("LAENGEN Vegard Stake") == "Vegard Stake Laengen"


# ---------------------------------------------------------------------------
# official pages
# ---------------------------------------------------------------------------

def test_parse_team_ranking_listing():
    rows = up.parse_team_ranking_listing(LISTING)
    assert rows == [
        {"rank": 1, "team_slug": "uae-team-emirates-xrg-2026", "team_name": "UAE Team Emirates - XRG",
         "team_class": "WT", "points": 30607.0, "ranking_date": "2026-10-18"},
        {"rank": 13, "team_slug": "pinarello-q365-pro-cycling-team-2026",
         "team_name": "Pinarello Q36.5 Pro Cycling Team", "team_class": "PRT", "points": 10722.0,
         "ranking_date": "2026-10-18"},
    ]


def test_parse_team_breakdown_meta():
    meta = up.parse_team_breakdown_meta(BREAKDOWN)
    assert meta == {"team_name": "UAE Team Emirates - XRG", "team_class": "WT",
                    "season_totals": {2026: 30982.1}, "sanction_total": 140.0}


def test_parse_team_breakdown_rows():
    rows = up.parse_team_breakdown(BREAKDOWN, 2026)
    assert [r["rider_slug"] for r in rows] == ["tadej-pogacar", "felix-grossschartner", "rui-oliveira"]
    pog, gross, rui = rows
    assert pog["points_counted"] == 9331.75 and pog["points_not_counted"] is None
    assert pog["sanction_points"] == 25.0 and pog["nth_best"] == 1
    assert pog["rider_name"] == "Tadej Pogačar" and pog["rider_name_raw"] == "POGAČAR Tadej"
    assert gross["points_counted"] == 374.32 and gross["sanction_points"] is None
    assert rui["points_counted"] is None and rui["points_not_counted"] == 164.0
    assert rui["sanction_points"] == 30.0
    assert pog["team_name"] == "UAE Team Emirates - XRG" and pog["team_class"] == "WT"
    assert up.parse_team_breakdown(BREAKDOWN, 2027) == []


def test_parse_team_recent_points():
    assert up.parse_team_recent_points(RECENT) == [
        {"date": "2026-10-18", "points": 722.0, "indirect_points": 0.0},
        {"date": "2026-09-27", "points": 1565.0, "indirect_points": 1415.0},
    ]


# ---------------------------------------------------------------------------
# rider results
# ---------------------------------------------------------------------------

def _by_label(rows):
    return {(r["date"], r["result_label"]): r for r in rows}


def test_parse_rider_results_rows():
    p = up.parse_rider_results(RESULTS, 2026)
    assert p["rider_name"] == "Tadej Pogačar" and p["rider_id"] == "194619"
    assert p["n_rows"] == 11 and p["has_older"] is False  # nothing before 2025-10-01
    rows = p["rows"]
    # the 2025 Lombardia row (2025-10-11) is kept as a candidate previous-October
    # row; merge_season_rows cuts it at the calibrated season start
    assert len(rows) == 11
    assert [r["date"] for r in rows if r["date"] < "2026-01-01"] == ["2025-10-11"]
    by = _by_label(rows)
    gc = by[("2026-07-26", "General classification")]
    assert gc["result_kind"] == "gc" and gc["position"] == 1 and gc["points_uci"] == 1300.0
    assert gc["points_pcs"] == 500.0 and gc["race_name"] == "Tour de France"
    assert gc["race_slug"] == "race/tour-de-france/2026" and gc["result_url"] == "race/tour-de-france-2026-gc"
    assert gc["race_class"] == "2.UWT"
    assert by[("2026-07-26", "Mountains classification")]["result_kind"] == "kom"
    assert by[("2026-07-26", "Points classification")]["points_uci"] is None
    assert by[("2026-07-04", "Stage 1 (TTT)")]["points_uci"] == 13.75
    assert by[("2026-04-28", "Prologue")]["result_kind"] == "stage"
    dnf = by[("2026-08-29", "Stage 8")]
    assert dnf["status"] == "dnf" and dnf["position"] is None and dnf["points_uci"] is None
    assert by[("2026-08-24", "Stage 3")]["status"] == "nr"
    ronde = by[("2026-04-05", "")]
    assert ronde["result_kind"] == "oneday" and ronde["points_uci"] == 800.0  # not 80025
    assert ronde["race_name"] == "Ronde van Vlaanderen ME" and ronde["race_class"] == "1.UWT"


def test_parse_rider_results_keeps_previous_october_on():
    # NSN 2026: the 2025-10-25 Israeli ITT championship counts for the 2026
    # UCI season and is missing from the world-ranking page; the results page
    # is its only source.  Rows before 1 October belong to the previous season.
    page = RESULTS_HEAD + "".join([
        _res_row(1, "2026-02-01", "3", "Tour of Oman | Stage 2", "race/tour-of-oman-2026-stage-2", uci="10"),
        _res_row(2, "2025-10-25", "1", "National Championships Israel ME - ITT",
                 "race/nc-israel-itt-2025-result", cls="NC", uci="50"),
        _res_row(3, "2025-09-20", "1", "Old Race", "race/old-race-2025-result", cls="1.1", uci="40"),
    ]) + RESULTS_TAIL
    p = up.parse_rider_results(page, 2026)
    assert p["has_older"] is True
    assert [(r["date"], r["points_uci"]) for r in p["rows"]] == [("2026-02-01", 10.0), ("2025-10-25", 50.0)]
    rows, diag = up.merge_season_rows(p["rows"], {"items": [], "races": [], "total": None}, 2026,
                                      season_start="2025-10-22")
    assert sum(r["points_uci"] or 0 for r in rows) == 60.0
    assert diag["unlisted"][-1]["race_name"] == "National Championships Israel ME - ITT"


def _full_page(n_rows, start_day=1, year=2026):
    rows = "".join(_res_row(i, f"{year}-{1 + (start_day + i) // 28:02d}-{1 + (start_day + i) % 28:02d}",
                            "5", f"Race {start_day + i}", f"race/race-{start_day + i}-{year}-result",
                            cls="1.1", uci="3")
                   for i in range(n_rows))
    return RESULTS_HEAD + rows + RESULTS_TAIL


def test_fetch_rider_season_results_single_page(monkeypatch):
    calls = []
    monkeypatch.setattr(up, "fetch", lambda url, use_cache=True: calls.append(url) or RESULTS)
    res = up.fetch_rider_season_results("tadej-pogacar", 2026)
    assert len(calls) == 1 and calls[0].endswith("rider/tadej-pogacar/results")
    assert res["pages"] == 1 and len(res["rows"]) == 11 and res["rider_id"] == "194619"
    assert res["complete"] is True


def test_fetch_rider_season_results_paginates_until_short_page(monkeypatch):
    # The default page is 100 rows all in 2026 -> switch to the season-filtered
    # form endpoint and page by offset until a page has fewer than 100 rows.
    late_2025 = RESULTS_HEAD + "".join([
        _res_row(1, "2025-11-02", "2", "Late Race", "race/late-race-2025-result", cls="1.1", uci="20"),
        _res_row(2, "2025-09-20", "1", "Old Race", "race/old-race-2025-result", cls="1.1", uci="40"),
    ]) + RESULTS_TAIL
    pages = {   # first match wins: the previous-season page before "offset=0&"
        "rider/busy-rider/results": _full_page(100),
        "xseason=2025&offset=0&": late_2025,
        "offset=0&": _full_page(100),
        "offset=100&": _full_page(30, start_day=101),
    }
    calls = []

    def fake_fetch(url, use_cache=True):
        calls.append(url)
        for key, html in pages.items():
            if key in url:
                return html
        raise AssertionError(f"unexpected fetch {url}")

    monkeypatch.setattr(up, "fetch", fake_fetch)
    res = up.fetch_rider_season_results("busy-rider", 2026)
    assert len(calls) == 4
    assert "rider.php?id=194619&sportid=1&p=results&s=&xseason=2026&offset=0" in calls[1]
    assert "xseason=2026&offset=100" in calls[2]
    # the season filter is the calendar year: late-2025 rows come from 2025's page
    assert "xseason=2025&offset=0" in calls[3]
    assert len(res["rows"]) == 131 and res["pages"] == 4 and res["complete"] is True
    assert [r["date"] for r in res["rows"] if r["date"] < "2026-01-01"] == ["2025-11-02"]


def test_fetch_rider_season_results_flags_partial_rows(monkeypatch):
    def fake_fetch(url, use_cache=True):
        if "rider/busy-rider/results" in url:
            return _full_page(100)
        return None   # every form page fails
    monkeypatch.setattr(up, "fetch", fake_fetch)
    res = up.fetch_rider_season_results("busy-rider", 2026)
    assert res["complete"] is False and len(res["rows"]) == 100


# ---------------------------------------------------------------------------
# world-ranking breakdown + merge
# ---------------------------------------------------------------------------

def test_parse_rider_uci_ranking():
    wr = up.parse_rider_uci_ranking(RANKING)
    assert wr["rider_id"] == "194619" and wr["date"] == "2026-10-18"
    assert wr["total"] == 2623.75  # first table's sum row
    assert wr["sanctions"] == [{"date": "2026-04-05", "points": 25.0}]
    assert len(wr["items"]) == 7
    leader = [i for i in wr["items"] if i["date"] == "2026-07-09" and i["label"] == "General classification"]
    assert leader and leader[0]["points"] == 25.0 and leader[0]["position"] == 1
    nc = [r for r in wr["races"] if r["race_class"] == "NC"][0]
    assert nc["race_url"] == "race/nc-mexico-2025-result" and nc["date_start"] == "2025-10-25"
    assert all(r["race_name"] != "Sanctions" for r in wr["races"])


def test_merge_season_rows_adds_leader_and_previous_year_items():
    results = up.parse_rider_results(RESULTS, 2026)["rows"]
    wr = up.parse_rider_uci_ranking(RANKING)
    rows, diag = up.merge_season_rows(results, wr, 2026, "Tadej Pogačar", season_start="2025-10-20")
    by = _by_label(rows)
    leader = by[("2026-07-09", "Leader jersey after Stage 6")]
    assert leader["status"] == "leader" and leader["result_kind"] == "other"
    assert leader["points_uci"] == 25.0 and leader["result_url"] == "race/tour-de-france-2026-stage-6"
    assert leader["race_slug"] == "race/tour-de-france/2026"
    # the final day's GC item matches the GC row instead of becoming a leader row
    assert sum(1 for r in rows if r["status"] == "leader") == 1
    nc = by[("2025-10-25", "")]
    assert nc["race_class"] == "NC" and nc["points_uci"] == 100.0
    assert nc["result_url"] == "race/nc-mexico-2025-result" and nc["race_slug"] == "race/nc-mexico/2025"
    # Romandie prologue (15) has points on the results page but not in the ranking
    assert diag["unlisted"] == [{"date": "2026-04-28", "race_name": "Tour de Romandie",
                                 "result_label": "Prologue", "points_uci": 15.0}]
    assert diag["points_changed"] == [] and diag["outside_season"] == []
    assert len(rows) == len([r for r in results if r["date"] >= "2025-10-20"]) + 2


def test_merge_final_gc_from_race_end_date_with_stale_results_page():
    # --refresh re-fetched the ranking page on a later day; the cached results
    # page stops at stage 5.  The GC item on the race's last day is the final
    # GC, not leader-jersey points.
    stale = [{"rider_name": "X", "date": "2026-10-18", "race_name": "Tour of Guangxi",
              "race_slug": "race/tour-of-guangxi/2026", "result_label": "Stage 5", "result_kind": "stage",
              "race_class": "2.UWT", "position": 3, "status": "finished", "points_uci": 30.0,
              "points_pcs": None, "result_url": "race/tour-of-guangxi-2026-stage-5"}]

    def item(day, label, pts, pos=1):
        return {"date": day, "text": f"Tour of Guangxi | {label}", "race_name": "Tour of Guangxi",
                "label": label, "position": pos, "status": "finished", "points": pts}
    wr = {"total": None, "sanctions": [],
          "races": [{"date_start": "2026-10-14", "race_name": "Tour of Guangxi",
                     "race_url": "race/tour-of-guangxi-2026-gc", "race_class": "2.UWT", "points": 340}],
          "items": [item("2026-10-18", "Stage 5", 30.0, 3), item("2026-10-18", "General classification", 10.0),
                    item("2026-10-19", "Stage 6", 0.0, 12), item("2026-10-19", "General classification", 300.0)]}
    rows, _ = up.merge_season_rows(stale, wr, 2026, "X", "2025-10-22",
                                   race_end_dates={"race/tour-of-guangxi/2026": "2026-10-19"})
    by = {(r["date"], r["result_kind"], r["status"]): r for r in rows}
    assert by[("2026-10-18", "other", "leader")]["points_uci"] == 10.0
    final = by[("2026-10-19", "gc", "finished")]
    assert final["points_uci"] == 300.0 and final["result_label"] == "General classification"
    assert not any(r["status"] == "leader" and r["date"] == "2026-10-19" for r in rows)


def test_merge_skips_items_before_season_start():
    results = up.parse_rider_results(RESULTS, 2026)["rows"]
    rows, diag = up.merge_season_rows(results, up.parse_rider_uci_ranking(RANKING), 2026)
    assert all(r["date"] >= "2026-01-01" for r in rows)
    assert diag["outside_season"] == [{"date": "2025-10-25", "points": 100.0,
                                       "text": "National Championships Mexico ME - Road Race"}]


def _wr(items, sanctions=()):
    return {"items": [{"date": d, "points": p} for d, p in items],
            "sanctions": [{"date": d, "points": p} for d, p in sanctions]}


def test_calibrate_season_start():
    # Japan Cup 2025-10-19 belongs to 2025; the Mexican championships of
    # 2025-10-23 to 2026 -> the season starts the day after 2025-10-19.
    samples = [
        (100.0, _wr([("2026-03-01", 100), ("2025-10-19", 150)])),
        (250.0, _wr([("2026-05-01", 200), ("2025-10-23", 50)])),
        (75.0, _wr([("2026-05-01", 100)], sanctions=[("2026-05-01", 25)])),
    ]
    start, scores = up.calibrate_season_start(samples, 2026)
    assert start == "2025-10-20"
    assert scores == {"2025-10-19": 2, "2025-10-23": 3, "2026-01-01": 2}


def test_merge_takes_ranking_points_when_pages_disagree():
    results = up.parse_rider_results(RESULTS, 2026)["rows"]
    wr = up.parse_rider_uci_ranking(RANKING)
    wr["items"][2] = {**wr["items"][2], "points": 200.0}  # Stage 6 210 -> 200
    rows, diag = up.merge_season_rows(results, wr, 2026)
    assert _by_label(rows)[("2026-07-09", "Stage 6")]["points_uci"] == 200.0
    assert diag["points_changed"][0]["results_page"] == 210.0


# ---------------------------------------------------------------------------
# profile + fines
# ---------------------------------------------------------------------------

def test_parse_rider_profile_season_leader_points_and_history():
    p = up.parse_rider_profile_season(PROFILE, 2026)
    assert p["rider_name"] == "Tadej Pogačar" and p["season_tab"] == 2026
    # +25 after stage 6 counts; the final day's +1300 repeats the GC award
    assert [(r["date"], r["points_uci"], r["result_label"]) for r in p["leader_rows"]] == [
        ("2026-07-09", 25.0, "Leader jersey after Stage 6")]
    assert p["leader_rows"][0]["race_class"] == "2.UWT"
    assert p["sanctions"] == [{"date": "2026-04-05", "race_name": "Ronde van Vlaanderen ME",
                               "penalty_points": 25.0}]
    hist = [h for h in p["team_history"] if h["season"] == 2026]
    assert [(h["team_slug"], h["note"]) for h in hist] == [
        ("xds-astana-team-2026", "as from 01/08"), ("netcompany-ineos-2026", "until 31/07")]


def test_parse_rider_profile_other_season_has_no_leader_rows():
    assert up.parse_rider_profile_season(PROFILE, 2025)["leader_rows"] == []


def test_parse_fines_penalties_keeps_season_point_penalties():
    rows = up.parse_fines_penalties(FINES, 2026)
    assert rows == [{"date": "2026-04-05", "race_name": "Ronde van Vlaanderen ME",
                     "race_url": "race/ronde-van-vlaanderen-2026-result", "penalty_points": 25.0,
                     "fine": "500", "article": "2.12.007 8.3 | Disposing of waste outside of litter zones."}]


# ---------------------------------------------------------------------------
# spells
# ---------------------------------------------------------------------------

def test_spells_from_history_transfer_dates():
    hist = up.parse_rider_profile_season(PROFILE, 2026)["team_history"]
    spells = cli.spells_from_history(hist, 2026, [])
    assert spells == [
        {"team_slug": "netcompany-ineos-2026", "team_name": "Netcompany INEOS",
         "start_date": None, "end_date": "2026-07-31"},
        {"team_slug": "xds-astana-team-2026", "team_name": "XDS Astana Team",
         "start_date": "2026-08-01", "end_date": None},
    ]
    assert cli.team_for_date(spells, "2026-07-31") == "netcompany-ineos-2026"
    assert cli.team_for_date(spells, "2026-08-01") == "xds-astana-team-2026"


def test_spells_single_team_and_fallback():
    hist = [{"season": 2026, "team_slug": "uae-team-emirates-xrg-2026", "team_name": "UAE", "note": ""}]
    assert cli.spells_from_history(hist, 2026, []) == [
        {"team_slug": "uae-team-emirates-xrg-2026", "team_name": "UAE", "start_date": None, "end_date": None}]
    fb = cli.spells_from_history([], 2026, [{"team_slug": "a-2026", "team_name": "A"}])
    assert fb == [{"team_slug": "a-2026", "team_name": "A", "start_date": None, "end_date": None}]


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_storage_is_idempotent(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    ensure_uci_points_schema(conn)
    ensure_uci_points_schema(conn)  # twice is fine
    listing = up.parse_team_ranking_listing(LISTING)
    breakdown = up.parse_team_breakdown(BREAKDOWN, 2026)
    results = up.parse_rider_results(RESULTS, 2026)["rows"]
    rows, _ = up.merge_season_rows(results, up.parse_rider_uci_ranking(RANKING), 2026,
                                   season_start="2025-10-20")
    pens = up.parse_fines_penalties(FINES, 2026)
    spells = [{"team_slug": "uae-team-emirates-xrg-2026", "team_name": "UAE", "start_date": None,
               "end_date": None}]
    for _ in range(2):
        upsert_team_totals(conn, "rankings/me/ranking-2026-2028", "2026-10-18", listing)
        upsert_team_breakdown(conn, "uae-team-emirates-xrg-2026", "UAE", "WT", breakdown)
        replace_rider_results(conn, 2026, "tadej-pogacar", rows)
        replace_rider_penalties(conn, 2026, "tadej-pogacar", pens)
        replace_rider_spells(conn, 2026, "tadej-pogacar", spells)
        upsert_team_recent_points(conn, 2026, "uae-team-emirates-xrg-2026", up.parse_team_recent_points(RECENT))
        conn.commit()
        assert _count(conn, "uci_team_totals") == 2
        assert _count(conn, "uci_team_breakdown") == 3
        # 12 rows on the pages, 8 worth points: only those are stored
        assert len(rows) == 12 and _count(conn, "rider_uci_results") == 8
        assert _count(conn, "rider_uci_scrapes") == 1
        assert _count(conn, "rider_uci_penalties") == 1
        assert _count(conn, "rider_team_spells") == 1
        assert _count(conn, "uci_team_recent_points") == 2
    assert conn.execute("SELECT COUNT(*) FROM rider_uci_results WHERE points_uci IS NULL "
                        "OR points_uci = 0").fetchone()[0] == 0
    assert conn.execute("SELECT rows_seen, scoring_rows, first_date, last_date, complete "
                        "FROM rider_uci_scrapes").fetchone() == (12, 8, "2025-10-25", "2026-08-29", None)
    pts = conn.execute("SELECT points_uci FROM rider_uci_results WHERE result_kind = 'gc'").fetchone()[0]
    assert pts == 1300.0
    # a refreshed rider replaces his old rows (DNF, NR, KOM 2nd: one scores);
    # an empty result set is "nothing scraped" and deletes nothing
    replace_rider_results(conn, 2026, "tadej-pogacar", rows[:3])
    assert _count(conn, "rider_uci_results") == 1
    replace_rider_results(conn, 2026, "tadej-pogacar", [])
    assert _count(conn, "rider_uci_results") == 1
    assert conn.execute("SELECT rows_seen, last_date FROM rider_uci_scrapes").fetchone() == (3, "2026-08-29")
    # a team page that lost a rider drops him
    upsert_team_breakdown(conn, "uae-team-emirates-xrg-2026", "UAE", "WT", breakdown[:1])
    assert _count(conn, "uci_team_breakdown") == 1


def _zero(day, url, status="finished"):
    return {**_ROW, "date": day, "race_name": "Z", "points_uci": None, "status": status, "result_url": url}


def test_replace_scored_nothing_vs_nothing_scraped(tmp_path):
    conn = sqlite3.connect(tmp_path / "e.db")
    ensure_uci_points_schema(conn)
    old = [{**_ROW, "date": "2026-03-01", "race_name": "A", "points_uci": 40.0, "result_url": "a"}]

    def stored(slug):
        return conn.execute("SELECT COUNT(*) FROM rider_uci_results WHERE rider_slug = ?", (slug,)).fetchone()[0]

    def scrape(slug):
        return conn.execute("SELECT rows_seen, scoring_rows, last_date, complete FROM rider_uci_scrapes "
                            "WHERE rider_slug = ?", (slug,)).fetchone()
    for slug in ("a", "b", "c", "d"):
        replace_rider_results(conn, 2026, slug, old, complete=True)
    # scraped fine, every row worth 0 (PCS re-attributed the points): stale points cleared
    assert replace_rider_results(conn, 2026, "a", [_zero("2026-03-01", "a"), _zero("2026-05-02", "z")],
                                 complete=True) == 0
    assert stored("a") == 0 and scrape("a") == (2, 0, "2026-05-02", 1)
    # scraped fine, no season row at all (his one result was annulled): cleared too
    assert replace_rider_results(conn, 2026, "b", [], complete=True, rider_name="B") == 0
    assert stored("b") == 0 and scrape("b") == (0, 0, None, 1)
    assert conn.execute("SELECT rider_name FROM rider_uci_scrapes WHERE rider_slug = 'b'").fetchone() == ("B",)
    # nothing scraped (page failed: no rows, not complete): stored rows and record untouched
    for complete in (None, False):
        assert replace_rider_results(conn, 2026, "c", [], complete=complete) == 0
        assert stored("c") == 1 and scrape("c") == (1, 1, "2026-03-01", 1)
    # a partial scrape still replaces, but its record says so
    replace_rider_results(conn, 2026, "d", [_zero("2026-06-01", "z")], complete=False)
    assert stored("d") == 0 and scrape("d") == (1, 0, "2026-06-01", 0)
    # zero / NULL points never stored; negative and leader rows are
    replace_rider_results(conn, 2026, "e", [
        _zero("2026-03-01", "n"), {**_zero("2026-03-02", "z0"), "points_uci": 0.0},
        {**_zero("2026-03-03", "neg"), "points_uci": -5.0},
        {**_zero("2026-03-04", "lead", "leader"), "points_uci": 25.0, "result_kind": "other"}], complete=True)
    assert conn.execute("SELECT result_url FROM rider_uci_results WHERE rider_slug = 'e' ORDER BY date"
                        ).fetchall() == [("neg",), ("lead",)]


def test_store_rider_complete_scrape_with_no_points_clears_stale_rows(tmp_path, monkeypatch):
    conn = _store_db(tmp_path)
    replace_rider_results(conn, 2026, "r", [
        {**_ROW, "date": "2026-03-01", "race_name": "A", "points_uci": 100.0, "result_url": "a"}])
    monkeypatch.setattr(up, "fetch_rider_penalties", lambda slug, season: [])
    info = {"name": "R", "official": 0.0, "sanction": 0.0, "teams": [{"team_slug": "t-2026", "team_name": "T"}]}
    # PCS moved the result: the rider's page now shows the race worth 0 points
    pages = {"res": {"rider_name": "R", "rows": [_zero("2026-03-01", "a")], "complete": True},
             "wr": {"total": 0.0, "items": [], "sanctions": [], "races": []}}
    diag = cli.store_rider(conn, 2026, "r", info, pages, "2025-10-22", "none", defaultdict(int), [], {})
    assert "kept_results" not in diag and diag["ledger"] == 0.0
    assert _count(conn, "rider_uci_results") == 0
    assert conn.execute("SELECT rows_seen, complete FROM rider_uci_scrapes").fetchone() == (1, 1)
    # a later run whose ranking page fails keeps that (empty) stored season
    pages = {"res": {"rider_name": "R", "rows": [{**_zero("2026-03-01", "a"), "points_uci": 80.0}],
                     "complete": True}, "wr": None}
    monkeypatch.setattr(up, "fetch_rider_profile_season", lambda slug, season: None)
    diag = cli.store_rider(conn, 2026, "r", info, pages, "2025-10-22", "none", defaultdict(int), [], {})
    assert diag["kept_results"] and _count(conn, "rider_uci_results") == 0
    # a rider never stored whose ranking page fails: rows kept, record marked partial
    diag = cli.store_rider(conn, 2026, "new", info, pages, "2025-10-22", "none", defaultdict(int), [], {})
    assert "kept_results" not in diag
    assert conn.execute("SELECT scoring_rows, complete FROM rider_uci_scrapes WHERE rider_slug = 'new'"
                        ).fetchone() == (1, 0)


def test_store_rider_resolves_penalty_race_from_zero_point_rows(tmp_path, monkeypatch):
    # Unai Ramos: 0 points, a 10-point sanction in a race he scored nothing in.
    # The race comes from his zero-point row, which is not stored.
    conn = _store_db(tmp_path)
    monkeypatch.setattr(up, "fetch_rider_penalties", lambda slug, season: [
        {"date": "2026-04-16", "race_name": "Vuelta a Castilla y León | Stage 2", "penalty_points": 10.0,
         "fine": "200", "article": "x"}])
    rows = [{**_zero("2026-04-16", "race/castilla-y-leon-2026-stage-2"), "race_name": "Vuelta a Castilla y León",
             "race_slug": "race/castilla-y-leon/2026", "race_class": "2.1", "result_label": "Stage 2",
             "result_kind": "stage", "rider_name": "Unai Ramos"}]
    info = {"name": "U", "official": 0.0, "sanction": 10.0, "teams": [{"team_slug": "k-2026", "team_name": "K"}]}
    pages = {"res": {"rider_name": "Unai Ramos", "rows": rows, "complete": True},
             "wr": {"total": 0.0, "items": [], "sanctions": [{"date": "2026-04-16", "points": 10.0}],
                    "races": []}}
    cli.store_rider(conn, 2026, "unai-ramos", info, pages, "2025-10-22", "none", defaultdict(int), [], {})
    assert _count(conn, "rider_uci_results") == 0
    assert conn.execute("SELECT race_slug, race_class, penalty_points FROM rider_uci_penalties").fetchall() == [
        ("race/castilla-y-leon/2026", "2.1", 10.0)]
    assert conn.execute("SELECT rider_name FROM rider_uci_scrapes").fetchone() == ("Unai Ramos",)
    # still a scraped rider for the reconciliation report, although he has no row
    upsert_team_breakdown(conn, "k-2026", "K", "PRT", [
        {"season": 2026, "rider_slug": "unai-ramos", "rider_name": "RAMOS Unai", "nth_best": 30,
         "points_counted": None, "points_not_counted": None, "sanction_points": 10.0}])
    rec = cli.rider_reconciliation(conn, 2026)[0]
    assert rec["scraped"] and rec["penalties"] == 10.0 and rec["diff"] == 0.0


# ---------------------------------------------------------------------------
# --prune-zero / --vacuum (maintenance on a DB stored before scoring-only)
# ---------------------------------------------------------------------------

def _legacy_db(path):
    """A DB as the old scraper left it: zero-point rows stored, no scrape
    records, penalties without their race."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE rider_uci_results (
            season INTEGER NOT NULL, rider_slug TEXT NOT NULL, rider_name TEXT,
            date TEXT NOT NULL, race_name TEXT, race_slug TEXT, result_label TEXT,
            result_kind TEXT NOT NULL, race_class TEXT, position INTEGER, status TEXT,
            points_uci REAL, points_pcs REAL, result_url TEXT NOT NULL,
            fetched_at TEXT DEFAULT (datetime('now')),
            UNIQUE(season, rider_slug, date, result_url, result_label));
        CREATE TABLE rider_uci_penalties (
            season INTEGER NOT NULL, rider_slug TEXT NOT NULL, date TEXT NOT NULL, race_name TEXT,
            penalty_points REAL NOT NULL, fine TEXT, article TEXT,
            UNIQUE(season, rider_slug, date, race_name));""")
    rows = []
    for season in (2025, 2026):
        for i in range(30):
            pts = 10.0 if i % 5 == 0 else (0.0 if i % 7 == 0 else None)
            rows.append((season, "r", "Rider R", f"{season}-05-{i + 1:02d}", "Race A", f"race/a/{season}",
                         f"Stage {i}", "stage", "2.1", i + 1, "finished", pts, f"u{i}"))
        rows.append((season, "zero", "Zero Z", f"{season}-06-01", "Race B", f"race/b/{season}", "",
                     "oneday", "1.1", 50, "finished", None, "b"))
    conn.executemany("INSERT INTO rider_uci_results (season, rider_slug, rider_name, date, race_name, race_slug, "
                     "result_label, result_kind, race_class, position, status, points_uci, result_url) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    # the day before his only (zero-point) race day: nearest row of that race
    conn.execute("INSERT INTO rider_uci_penalties VALUES (2026, 'zero', '2026-05-31', 'Race B ME', 10, '', '')")
    conn.commit()
    conn.close()


def test_prune_zero_keeps_what_the_zero_rows_were_needed_for(tmp_path):
    from db.uci_points import prune_zero_point_results
    db = tmp_path / "legacy.db"
    _legacy_db(db)
    conn = sqlite3.connect(db)
    got = prune_zero_point_results(conn, 2026)
    conn.commit()
    # 2026: 6 scoring rows of 31 (rider r: i % 5 == 0); 2025 untouched
    assert got == {"deleted": 25, "kept": 6, "scrapes_backfilled": 2, "penalties_resolved": 1}
    assert conn.execute("SELECT season, COUNT(*) FROM rider_uci_results GROUP BY season").fetchall() == [
        (2025, 31), (2026, 6)]
    assert conn.execute("SELECT rider_slug, rider_name, rows_seen, scoring_rows, first_date, last_date, complete "
                        "FROM rider_uci_scrapes ORDER BY rider_slug").fetchall() == [
        ("r", "Rider R", 30, 6, "2026-05-01", "2026-05-30", None),
        ("zero", "Zero Z", 1, 0, "2026-06-01", "2026-06-01", None)]
    assert conn.execute("SELECT race_slug, race_class FROM rider_uci_penalties").fetchone() == (
        "race/b/2026", "1.1")
    # idempotent
    assert prune_zero_point_results(conn, 2026) == {"deleted": 0, "kept": 6, "scrapes_backfilled": 0,
                                                    "penalties_resolved": 0}
    # every season
    assert prune_zero_point_results(conn)["deleted"] == 25
    assert conn.execute("SELECT COUNT(*) FROM rider_uci_results").fetchone()[0] == 12
    assert _count(conn, "rider_uci_scrapes") == 4
    # the readers: data_through still reaches the last row seen, the zero rider is still "scraped"
    from analysis.team_points import data_through
    assert data_through(conn, 2026) == "2026-06-01"
    assert cli._scraped_riders(conn, 2026) == {"r", "zero"}
    assert cli._rows_seen(conn, 2026) == 31


def test_cli_prune_zero_and_vacuum(tmp_path, capsys):
    db = tmp_path / "legacy.db"
    _legacy_db(db)
    conn = sqlite3.connect(db)  # pad the file so VACUUM has pages to give back
    conn.execute("CREATE TABLE pad (x TEXT)")
    conn.executemany("INSERT INTO pad VALUES (?)", [("x" * 2000,) for _ in range(500)])
    conn.commit()
    conn.execute("DELETE FROM pad")
    conn.commit()
    conn.close()
    assert cli.run(["--db", str(db), "--prune-zero", "--season", "2026"]) == 0
    out = capsys.readouterr().out
    assert "25 zero-point rows deleted, 6 scoring rows kept" in out and "--vacuum" in out
    size = db.stat().st_size  # deleting frees pages inside the file only
    assert size > 1_000_000
    assert cli.run(["--db", str(db), "--prune-zero", "--all-seasons", "--vacuum"]) == 0
    out = capsys.readouterr().out
    assert "prune-zero (all seasons): 25 zero-point rows deleted" in out and "vacuum:" in out
    assert db.stat().st_size < size / 2
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM rider_uci_results").fetchone()[0] == 12
    assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    conn.close()
    # vacuum alone is a no-op on a compact file; a missing DB is an error, never created
    assert cli.run(["--db", str(db), "--vacuum"]) == 0
    assert cli.run(["--db", str(tmp_path / "none.db"), "--vacuum"]) == 1
    assert not (tmp_path / "none.db").exists()
    with pytest.raises(SystemExit):
        cli.run(["--db", str(db), "--all-seasons"])


# ---------------------------------------------------------------------------
# CLI end to end (fetch served from snippets)
# ---------------------------------------------------------------------------

PAGES = {
    "rankings/me/ranking-2026-2028": LISTING,
    "team/uae-team-emirates-xrg-2026/results/ranking-2026-2028": BREAKDOWN,
    "team/pinarello-q365-pro-cycling-team-2026/results/ranking-2026-2028": BREAKDOWN.replace(
        "rider/tadej-pogacar", "rider/other-rider").replace("rider/felix-grossschartner", "rider/x").replace(
        "rider/rui-oliveira", "rider/y"),
    "latest-uci-points": RECENT,
    "s=uci-world-ranking": RANKING,
    "/results": RESULTS,
    "info/fines-penalties": FINES,
}


def _fake_fetch(fetched):
    def fake(url, use_cache=True):
        fetched.append(url)
        if "rider/x" in url or "rider/y" in url or "rider/other-rider" in url:
            return None  # a failing page must not stop the run
        for key, html in PAGES.items():
            if key in url:
                return html
        if url.rstrip("/").split("/")[-2] == "rider":
            return PROFILE
        return None
    return fake


def test_cli_run_end_to_end_and_rerun(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fetched = []
    monkeypatch.setattr(up, "fetch", _fake_fetch(fetched))
    db = tmp_path / "uci.db"
    report = tmp_path / "rep.json"
    rc = cli.run(["--season", "2026", "--teams", "all", "--db", str(db), "--report", str(report),
                  "--season-start", "2025-10-20"])
    assert rc == 2  # the three pinarello riders' pages fail -> exit 2, run completes
    conn = sqlite3.connect(db)
    counts = {t: _count(conn, t) for t in ("uci_team_totals", "uci_team_breakdown", "rider_uci_results",
                                           "rider_uci_penalties", "rider_team_spells")}
    # 2 listing rows + 2 breakdown-page totals (ranking 'team-breakdown/ranking-2026-2028')
    assert counts["uci_team_totals"] == 4 and counts["uci_team_breakdown"] == 6
    assert counts["rider_uci_penalties"] == 3  # Pogačar, Rui Oliveira, Großschartner share the snippet
    leader = conn.execute("SELECT COUNT(*) FROM rider_uci_results WHERE rider_slug='tadej-pogacar' "
                          "AND status='leader'").fetchone()[0]
    assert leader == 1
    name = conn.execute("SELECT rider_name FROM uci_team_breakdown WHERE rider_slug='tadej-pogacar'").fetchone()[0]
    assert name == "Tadej Pogačar"
    conn.close()
    rc2 = cli.run(["--season", "2026", "--teams", "all", "--db", str(db), "--season-start", "2025-10-20"])
    assert rc2 == 2
    conn = sqlite3.connect(db)
    assert {t: _count(conn, t) for t in counts} == counts  # idempotent
    import json
    rep = json.loads(report.read_text())
    pog = [r for r in rep["riders"] if r["rider_slug"] == "tadej-pogacar"][0]
    # snippet ledger: results 2491.75 + 15 prologue + leader 25 + NC 100 - 25 sanction
    assert pog["leader"] == 25.0 and pog["previous_year"] == 100.0 and pog["penalties"] == 25.0
    assert rep["stats"]["teams"] == 2 and rep["stats"]["leader_rows"] >= 1


def test_spells_winter_transfer_gives_previous_october_points_to_old_team():
    # Chih-Hao Tu: Bahrain - Victorious in 2025, Astemo Utsunomiya Blitzen in 2026;
    # his November 2025 championships count for Bahrain in the 2026 season.
    hist = [{"season": 2026, "team_slug": "astemo-utsunomiya-blitzen-2026",
             "team_name": "Astemo Utsunomiya Blitzen", "note": ""},
            {"season": 2025, "team_slug": "bahrain-victorious-2025", "team_name": "Bahrain - Victorious",
             "note": ""}]
    spells = cli.spells_from_history(hist, 2026, [{"team_slug": "bahrain-victorious-2026",
                                                   "team_name": "Bahrain - Victorious"}], "2025-10-22")
    assert spells == [
        {"team_slug": "bahrain-victorious-2026", "team_name": "Bahrain - Victorious",
         "start_date": "2025-10-22", "end_date": "2025-12-31"},
        {"team_slug": "astemo-utsunomiya-blitzen-2026", "team_name": "Astemo Utsunomiya Blitzen",
         "start_date": "2026-01-01", "end_date": None},
    ]
    assert cli.team_for_date(spells, "2025-11-23") == "bahrain-victorious-2026"
    assert cli.team_for_date(spells, "2026-02-06") == "astemo-utsunomiya-blitzen-2026"
    # same franchise both years -> no extra spell
    same = [{"season": 2026, "team_slug": "uae-2026", "team_name": "UAE", "note": ""},
            {"season": 2025, "team_slug": "uae-2025", "team_name": "UAE", "note": ""}]
    assert cli.spells_from_history(same, 2026, [{"team_slug": "uae-2026", "team_name": "UAE"}],
                                   "2025-10-22") == [
        {"team_slug": "uae-2026", "team_name": "UAE", "start_date": None, "end_date": None}]


def test_same_day_penalties_are_summed(tmp_path):
    # Kuss, TdF 2026 stage 20: two sanctions (25 + 50) on one day in one race.
    conn = sqlite3.connect(tmp_path / "p.db")
    ensure_uci_points_schema(conn)
    rows = [{"date": "2026-07-25", "race_name": "Tour de France | Stage 20", "penalty_points": 25.0,
             "fine": "500", "article": "2.12.007 8.3"},
            {"date": "2026-07-25", "race_name": "Tour de France | Stage 20", "penalty_points": 50.0,
             "fine": "1000", "article": "2.12.007 8.3"}]
    replace_rider_penalties(conn, 2026, "sepp-kuss", rows)
    got = conn.execute("SELECT penalty_points, fine FROM rider_uci_penalties").fetchall()
    assert got == [(75.0, "500 + 1000")]


def test_rider_reconciliation_is_spell_aware(tmp_path):
    # Luca Giaimi: 1 point for the devo team in January, 100 for UAE after joining.
    conn = sqlite3.connect(tmp_path / "r.db")
    ensure_uci_points_schema(conn)
    upsert_team_breakdown(conn, "uae-2026", "UAE", "WT", [
        {"season": 2026, "rider_slug": "luca-giaimi", "rider_name": "Luca Giaimi", "nth_best": 27,
         "points_counted": None, "points_not_counted": 100.0, "sanction_points": None}])
    base = {"race_slug": None, "result_kind": "oneday", "race_class": "1.1", "position": 1,
            "status": "finished", "points_pcs": None, "rider_name": "Luca Giaimi", "result_label": ""}
    replace_rider_results(conn, 2026, "luca-giaimi", [
        {**base, "date": "2026-01-25", "race_name": "Tour of Sharjah", "points_uci": 1.0, "result_url": "a"},
        {**base, "date": "2026-10-18", "race_name": "Chrono des Nations", "points_uci": 100.0, "result_url": "b"}])
    replace_rider_spells(conn, 2026, "luca-giaimi", [
        {"team_slug": "uae-gen-z-2026", "team_name": "Gen Z", "start_date": None, "end_date": "2026-02-26"},
        {"team_slug": "uae-2026", "team_name": "UAE", "start_date": "2026-02-27", "end_date": None}])
    rec = cli.rider_reconciliation(conn, 2026)[0]
    assert rec["ledger"] == 100.0 and rec["other_teams"] == 1.0 and rec["diff"] == 0.0


# ---------------------------------------------------------------------------
# store_rider: never replace stored data with a degraded fallback
# ---------------------------------------------------------------------------

def _store_db(tmp_path):
    conn = sqlite3.connect(tmp_path / "s.db")
    ensure_uci_points_schema(conn)
    return conn


_ROW = {"rider_name": "R", "race_slug": None, "result_label": "", "result_kind": "oneday",
        "race_class": "1.1", "position": 1, "status": "finished", "points_pcs": None}


def test_store_rider_keeps_stored_rows_when_pages_fail(tmp_path, monkeypatch):
    conn = _store_db(tmp_path)
    full = [{**_ROW, "date": "2025-10-25", "race_name": "NC", "points_uci": 50.0, "result_url": "nc"},
            {**_ROW, "date": "2026-03-01", "race_name": "A", "points_uci": 100.0, "result_url": "a"}]
    replace_rider_results(conn, 2026, "r", full)
    replace_rider_penalties(conn, 2026, "r", [{"date": "2026-03-01", "race_name": "A", "penalty_points": 25.0}])
    replace_rider_spells(conn, 2026, "r", [
        {"team_slug": "t1-2026", "team_name": "T1", "start_date": None, "end_date": "2026-06-30"},
        {"team_slug": "t2-2026", "team_name": "T2", "start_date": "2026-07-01", "end_date": None}])
    monkeypatch.setattr(up, "fetch_rider_profile_season", lambda slug, season: None)   # profile fails
    monkeypatch.setattr(up, "fetch_rider_penalties", lambda slug, season: None)        # fines page fails
    info = {"name": "R", "official": 125.0, "sanction": 25.0,
            "teams": [{"team_slug": "t1-2026", "team_name": "T1"}, {"team_slug": "t2-2026", "team_name": "T2"}]}
    # ranking page failed (wr None): only the results page's 2026 row is known
    pages = {"res": {"rider_name": "R", "rows": full[1:], "complete": True}, "wr": None}
    stats, failures = defaultdict(int), []
    diag = cli.store_rider(conn, 2026, "r", info, pages, "2025-10-22", "needed", stats, failures, {})
    assert diag["kept_results"] and diag["kept_penalties"] and diag["kept_spells"]
    assert _count(conn, "rider_uci_results") == 2
    assert conn.execute("SELECT penalty_points FROM rider_uci_penalties").fetchall() == [(25.0,)]
    assert conn.execute("SELECT team_slug, start_date, end_date FROM rider_team_spells ORDER BY team_slug"
                        ).fetchall() == [("t1-2026", None, "2026-06-30"), ("t2-2026", "2026-07-01", None)]
    assert "penalties r" in failures and "profile r" in failures


def _zingle_db(tmp_path):
    """Axel Zingle as stored by a complete scrape: one scoring row (Paris-Nice)
    and a 100-point sanction from E3, a race worth 0 to him (row not stored)."""
    conn = _store_db(tmp_path)
    replace_rider_results(conn, 2026, "axel-zingle", [
        {**_ROW, "date": "2026-03-10", "race_name": "Paris-Nice", "race_slug": "race/paris-nice/2026",
         "race_class": "2.UWT", "points_uci": 60.0, "result_url": "race/paris-nice-2026-stage-3"}],
        complete=True)
    replace_rider_penalties(conn, 2026, "axel-zingle", [
        {"date": "2026-03-27", "race_name": "E3 Saxo Classic ME", "penalty_points": 100.0,
         "race_slug": "race/e3-harelbeke/2026", "race_class": "1.UWT"}])
    return conn


def _zingle_pages(complete: bool, wr: bool = True):
    # a partial results page set: Paris-Nice only, E3 (0 points) on the page that failed
    rows = [{**_ROW, "date": "2026-03-10", "race_name": "Paris-Nice", "race_slug": "race/paris-nice/2026",
             "race_class": "2.UWT", "points_uci": 60.0, "result_url": "race/paris-nice-2026-stage-3"}]
    return {"res": {"rider_name": "Axel Zingle", "rows": rows, "complete": complete},
            "wr": {"total": 60.0, "items": [], "sanctions": [{"date": "2026-03-27", "points": 100.0}],
                   "races": []} if wr else None}


_ZINGLE = {"name": "ZINGLE Axel", "official": -40.0, "sanction": 100.0,
           "teams": [{"team_slug": "visma-2026", "team_name": "Visma"}]}


def _penalty_rows(conn):
    return conn.execute("SELECT date, race_name, penalty_points, race_slug, race_class "
                        "FROM rider_uci_penalties").fetchall()


def test_store_rider_degraded_keeps_the_penalty_race(tmp_path, monkeypatch):
    # results pages partial: the stored (scoring-only) rows are kept and the
    # E3 row the sanction was placed with is on neither side — the race the
    # penalty was stored with must survive, not be overwritten with NULL
    conn = _zingle_db(tmp_path)
    monkeypatch.setattr(up, "fetch_rider_penalties", lambda slug, season: [
        {"date": "2026-03-27", "race_name": "E3 Saxo Classic ME", "penalty_points": 100.0,
         "fine": "", "article": "2.12.007"}])
    diag = cli.store_rider(conn, 2026, "axel-zingle", _ZINGLE, _zingle_pages(complete=False), "2025-10-22",
                           "none", defaultdict(int), [], {})
    assert diag["kept_results"] and "kept_penalties" not in diag
    assert _penalty_rows(conn) == [
        ("2026-03-27", "E3 Saxo Classic ME", 100.0, "race/e3-harelbeke/2026", "1.UWT")]


def test_store_rider_degraded_ranking_fallback_keeps_race_name(tmp_path, monkeypatch):
    # fines page down too: the sanction comes from the ranking page; its race
    # name is the stored one, not the 'Sanctions' placeholder
    conn = _zingle_db(tmp_path)
    monkeypatch.setattr(up, "fetch_rider_penalties", lambda slug, season: None)
    cli.store_rider(conn, 2026, "axel-zingle", _ZINGLE, _zingle_pages(complete=False), "2025-10-22",
                    "none", defaultdict(int), [], {})
    assert _penalty_rows(conn) == [
        ("2026-03-27", "E3 Saxo Classic ME", 100.0, "race/e3-harelbeke/2026", "1.UWT")]


def _duric_db(tmp_path):
    """Đorđe Đurić as stored by a complete scrape: two 25-point sanctions, one
    listed only on the ranking page ('Sanctions'), one on the fines page too."""
    conn = _store_db(tmp_path)
    replace_rider_results(conn, 2026, "dorde-duric", [
        {**_ROW, "date": "2026-05-01", "race_name": "A", "points_uci": 91.0, "result_url": "a"}], complete=True)
    replace_rider_penalties(conn, 2026, "dorde-duric", [
        {"date": "2026-06-02", "race_name": "Sanctions", "penalty_points": 25.0},
        {"date": "2026-09-16", "race_name": "Tour of Taihu Lake | Stage 5", "penalty_points": 25.0,
         "fine": "CHF 200", "article": "2.12.007", "race_slug": "race/tour-of-taihu-lake/2026", "race_class": "2.1"}])
    return conn


_DURIC = {"name": "ĐURIĆ Đorđe", "official": 41.0, "sanction": 50.0,
          "teams": [{"team_slug": "t-2026", "team_name": "T"}]}
_TAIHU = {"date": "2026-09-16", "race_name": "Tour of Taihu Lake | Stage 5", "penalty_points": 25.0,
          "fine": "CHF 200", "article": "2.12.007"}


def _no_ranking(conn):
    rows = conn.execute("SELECT date, race_name, points_uci FROM rider_uci_results").fetchall()
    return {"res": {"rider_name": "Đorđe Đurić", "complete": True, "rows": [
        {**_ROW, "date": d, "race_name": n, "points_uci": p, "result_url": n.lower()} for d, n, p in rows]},
        "wr": None}


@pytest.mark.parametrize("fines", [[_TAIHU], []], ids=["fines-page-subset", "fines-page-empty"])
def test_store_rider_without_ranking_keeps_ranking_only_sanctions(tmp_path, monkeypatch, fines):
    # ranking page down: the fines page does not list the 2026-06-02 sanction
    # (PCS shows it on the ranking page only) — it must not be deleted
    conn = _duric_db(tmp_path)
    before = _penalty_rows(conn)
    monkeypatch.setattr(up, "fetch_rider_penalties", lambda slug, season: [dict(p) for p in fines])
    monkeypatch.setattr(up, "fetch_rider_profile_season", lambda slug, season: None)
    diag = cli.store_rider(conn, 2026, "dorde-duric", _DURIC, _no_ranking(conn), "2025-10-22", "none",
                           defaultdict(int), [], {})
    assert diag["kept_results"] and diag["kept_penalties"]
    assert _penalty_rows(conn) == before
    assert diag["ledger"] == diag["official"] == 41.0


def test_store_rider_without_ranking_keeps_a_riders_only_sanction(tmp_path, monkeypatch):
    # Unai Ramos: his only sanction is on the ranking page; the breakdown's
    # sanction column says nothing either way on this run
    conn = _store_db(tmp_path)
    replace_rider_results(conn, 2026, "unai-ramos", [], complete=True, rider_name="Unai Ramos")
    replace_rider_penalties(conn, 2026, "unai-ramos", [
        {"date": "2026-06-02", "race_name": "Sanctions", "penalty_points": 10.0}])
    pages_read = []
    monkeypatch.setattr(up, "fetch_rider_penalties", lambda slug, season: pages_read.append(slug) or [])
    monkeypatch.setattr(up, "fetch_rider_profile_season", lambda slug, season: None)
    for sanction in (10.0, 0.0):
        diag = cli.store_rider(conn, 2026, "unai-ramos", {**_DURIC, "official": 0.0, "sanction": sanction},
                               {"res": {"rider_name": "Unai Ramos", "rows": [], "complete": True}, "wr": None},
                               "2025-10-22", "none", defaultdict(int), [], {})
        assert diag["kept_penalties"] and diag["ledger"] == -10.0
        assert conn.execute("SELECT date, race_name, penalty_points FROM rider_uci_penalties").fetchall() == [
            ("2026-06-02", "Sanctions", 10.0)]
    assert pages_read == ["unai-ramos", "unai-ramos"]


def test_fines_over_stored_per_day():
    stored = [{"date": "2026-06-02", "race_name": "Sanctions", "penalty_points": 25.0},
              {"date": "2026-07-25", "race_name": "Tour de France | Stage 20", "penalty_points": 75.0,
               "race_slug": "race/tour-de-france/2026", "race_class": "2.UWT"},
              {"date": "2026-09-16", "race_name": "Sanctions", "penalty_points": 25.0}]
    fines = [  # Kuss: the stored 75 is these two summed (one row per day and race)
        {"date": "2026-07-25", "race_name": "Tour de France | Stage 20", "penalty_points": 25.0},
        {"date": "2026-07-25", "race_name": "Tour de France | Stage 20", "penalty_points": 50.0},
        # the placeholder's race, fine and article
        {"date": "2026-09-16", "race_name": "Tour of Taihu Lake | Stage 5", "penalty_points": 25.0,
         "fine": "CHF 200"},
        # imposed since the last scrape
        {"date": "2026-10-13", "race_name": "Il Lombardia", "penalty_points": 15.0}]
    out, added = cli._fines_over_stored(stored, fines)
    assert [(p["date"], p["race_name"], p["penalty_points"]) for p in out] == [
        ("2026-06-02", "Sanctions", 25.0),                          # ranking-only: kept
        ("2026-07-25", "Tour de France | Stage 20", 25.0),
        ("2026-07-25", "Tour de France | Stage 20", 50.0),           # same total: fines rows, not twice
        ("2026-09-16", "Tour of Taihu Lake | Stage 5", 25.0),
        ("2026-10-13", "Il Lombardia", 15.0)]
    assert added == ["2026-10-13"]
    assert sum(p["penalty_points"] for p in out) == 140.0
    # a day the fines page shows less for keeps the stored rows
    out, added = cli._fines_over_stored(stored[1:2], fines[:1])
    assert out == stored[1:2] and added == []


def test_store_rider_degraded_matches_partial_pages_zero_rows(tmp_path, monkeypatch):
    # the partial pages did hold the race (a zero-point row): matched from it
    conn = _store_db(tmp_path)
    replace_rider_results(conn, 2026, "r", [
        {**_ROW, "date": "2026-03-10", "race_name": "A", "points_uci": 60.0, "result_url": "a"}], complete=True)
    monkeypatch.setattr(up, "fetch_rider_penalties", lambda slug, season: [
        {"date": "2026-04-16", "race_name": "Vuelta a Castilla y León | Stage 2", "penalty_points": 10.0}])
    rows = [{**_zero("2026-04-16", "race/castilla-y-leon-2026-stage-2"), "race_name": "Vuelta a Castilla y León",
             "race_slug": "race/castilla-y-leon/2026", "race_class": "2.1", "result_kind": "stage"}]
    pages = {"res": {"rider_name": "R", "rows": rows, "complete": False},
             "wr": {"total": 0.0, "items": [], "sanctions": [{"date": "2026-04-16", "points": 10.0}], "races": []}}
    diag = cli.store_rider(conn, 2026, "r", {**_ZINGLE, "sanction": 10.0}, pages, "2025-10-22", "none",
                           defaultdict(int), [], {})
    assert diag["kept_results"] and _count(conn, "rider_uci_results") == 1
    assert _penalty_rows(conn) == [
        ("2026-04-16", "Vuelta a Castilla y León | Stage 2", 10.0, "race/castilla-y-leon/2026", "2.1")]


def test_store_rider_previous_october_points_stay_with_franchise(tmp_path, monkeypatch):
    # Raisberg: Israel - Premier Tech in 2025, NSN in 2026; PCS counts his
    # 2025-10-25 ITT title (50) for NSN, so a reconciling one-team rider gets
    # no winter spell for the old team even when his profile is read.
    conn = _store_db(tmp_path)
    prof = {"team_history": [
        {"season": 2026, "team_slug": "nsn-cycling-team-2026", "team_name": "NSN", "note": ""},
        {"season": 2025, "team_slug": "israel-premier-tech-2025", "team_name": "IPT", "note": ""}],
        "leader_rows": []}
    monkeypatch.setattr(up, "fetch_rider_profile_season", lambda slug, season: prof)
    rows = [{**_ROW, "date": "2025-10-25", "race_name": "NC Israel ITT", "points_uci": 50.0, "result_url": "nc"},
            {**_ROW, "date": "2026-05-01", "race_name": "A", "points_uci": 158.0, "result_url": "a"}]
    info = {"name": "R", "official": 208.0, "sanction": 0.0,
            "teams": [{"team_slug": "nsn-cycling-team-2026", "team_name": "NSN"}]}
    pages = {"res": {"rider_name": "R", "rows": rows, "complete": True},
             "wr": {"total": 158.0, "items": [], "sanctions": [], "races": []}}
    diag = cli.store_rider(conn, 2026, "nadav-raisberg", info, pages, "2025-10-22", "all",
                           defaultdict(int), [], {})
    assert diag["ledger"] == 208.0
    assert diag["spells"] == [{"team_slug": "nsn-cycling-team-2026", "team_name": "NSN",
                               "start_date": None, "end_date": None}]
