"""Tests for rider profile parsing."""
import pytest
from scraper.riders import _current_team, _parse_dob_parts, _parse_speciality, _parse_float, _parse_rider
from bs4 import BeautifulSoup


class TestParseDobParts:
    def test_standard(self):
        assert _parse_dob_parts(["21st", "September", "1998", "(", "27", ")"]) == "1998-09-21"

    def test_no_parentheses(self):
        assert _parse_dob_parts(["3rd", "January", "2000"]) == "2000-01-03"

    def test_single_digit_day(self):
        assert _parse_dob_parts(["1st", "May", "1985", "(", "40", ")"]) == "1985-05-01"

    def test_age_not_mistaken_for_day(self):
        # Age "27" should not overwrite day "21"
        result = _parse_dob_parts(["21st", "September", "1998", "(", "27", ")"])
        assert result.endswith("-21")

    def test_missing_data_returns_none(self):
        assert _parse_dob_parts(["September", "1998"]) is None

    def test_empty_returns_none(self):
        assert _parse_dob_parts([]) is None


class TestParseSpeciality:
    def _make_soup(self, items: list[tuple[str, int]]) -> BeautifulSoup:
        """Build a minimal PCS-style ul.pps.list from (label, score) pairs."""
        lis = ""
        for label, score in items:
            lis += f"""
            <li>
              <div class="xbar"><div class="valuebar"><div class="w{score} bg left"></div></div></div>
              <div class="xvalue ac">{score * 100}</div>
              <div class="xtitle"><a href="#">{label}</a></div>
            </li>"""
        html = f'<ul class="pps list">{lis}</ul>'
        return BeautifulSoup(html, "lxml")

    def test_climber_wins(self):
        s = self._make_soup([("GC", 85), ("Sprint", 20), ("TT", 40)])
        assert _parse_speciality(s) == "gc"

    def test_sprinter_wins(self):
        s = self._make_soup([("Sprint", 92), ("GC", 30), ("Hills", 25)])
        assert _parse_speciality(s) == "sprinter"

    def test_classics_wins(self):
        s = self._make_soup([("Onedayraces", 90), ("GC", 60)])
        assert _parse_speciality(s) == "classics"

    def test_tt_wins(self):
        s = self._make_soup([("TT", 88), ("GC", 70)])
        assert _parse_speciality(s) == "tt"

    def test_empty_returns_none(self):
        s = BeautifulSoup("<div></div>", "lxml")
        assert _parse_speciality(s) is None


class TestParseFloat:
    def test_integer_string(self):
        assert _parse_float("66") == 66.0

    def test_decimal_string(self):
        assert _parse_float("1.76") == 1.76

    def test_with_unit(self):
        assert _parse_float("66 kg") == 66.0

    def test_empty_returns_none(self):
        assert _parse_float("") is None

    def test_non_numeric_returns_none(self):
        assert _parse_float("abc") is None


# ── Live PCS rider page (Oct 2026), trimmed ──────────────────────────────────
# The site-wide "Teams" menu links every WorldTeam (XDS Astana first): the old
# fallback read that menu, so every rider got 'XDS Astana Team'.
_NAV = """
<span class="mob_topnav"><ul class="topnav"><li><a class="menu" href="teams.php">Teams</a>
<ul><li><a href="team/xds-astana-team-2026">XDS Astana Team</a></li>
<li><a href="team/uno-x-mobility-2026">Uno-X Mobility</a></li>
<li><a href="team/uae-team-emirates-xrg-2026">UAE Team Emirates - XRG</a></li></ul></li></ul></span>"""

_TITLE = """<div class="page-title"><div class="title"><span class="flag si w32"></span>
<h1 translate="no">Tadej Pogačar</h1></div><span class="arrow">»</span>
<div class="subtitle"><h2 translate="no">UAE Team Emirates - XRG</h2></div></div>"""

_INFO = """
<div class="borderbox left w65"><div class=""> <div class="lineh14">
<div class="bold mr5" style="display: inline-block;">Name:</div><div class="mr5">Tadej  Pogačar </div><br>
<div class="bold mr5" style="display: inline-block;">Date of birth:</div>
<div class="mr4" style="display: inline-block;">21st</div>
<div class="mr4" style="display: inline-block;">September 1998 (28)</div><br>
<div class="" style="display: inline-block;"></div>
<div class="bold mr5" style="display: inline-block;">Nationality:</div>
<div class="" style="display: inline-block;"><span class="flag si"></span> </div>
<div class="" style="display: inline-block;"><a href="nation/slovenia">Slovenia</a></div><br>
<div class="bold mr5" style="display: inline-block;">Weight: </div><div class="mr4">66</div>
<div class="mr10">kg</div><div class="bold mr5" style="display: inline-block;">Height: </div>
<div class="mr4">1.76</div><div class=""> m</div><br>
<div class="bold mr5" style="display: inline-block;">Place of birth:</div>
<div class=""><a href="location/klanec">Klanec</a></div></div></div>
<div class=""><div class="bold">Specialties</div></div>
<ul class="pps list">
<li><div class="xbar"><div class="valuebar"><div class="w90 bg green2 left"></div></div></div>
<div class="xvalue ac">9983</div><div class="xtitle"><a href="?type=oneday">Oneday</a></div></li>
<li><div class="xbar"><div class="valuebar"><div class="w75 bg red left"></div></div></div>
<div class="xvalue ac">8344</div><div class="xtitle"><a href="?type=gc">GC</a></div></li>
<li><div class="xbar"></div><div class="xvalue ac">11052</div><div class="xtitle"><a href="?type=climber">Climber</a></div></li>
</ul>
<ul class="list horizontal">
<li class=""><div class="mr3"><a href="rankings/me/individual">PCS Ranking</a></div>
<div class="bg grey2 bold pad1_5 rounded2 mr5">1</div></li>
<li class=""><div class="mr3"><a href="rankings/me/uci-individual">UCI World</a></div>
<div class="bg grey2 bold pad1_5 rounded2 mr5">2</div></li></ul>
<ul class="list"><li class=""><div class="bold mr5">Visits:</div><div class="mr5"><span>15301</span></div>
<div>this week</div></li></ul>
</div>"""

_TEAMS = """
<span class="table-cont"><ul class="rdr-teams2">
<li class="showifmob combiLine"><div class="season">2027-2032</div><div class="name2">
<a class="blue" href="team/uae-team-emirates-xrg-2027">UAE Team Emirates - XRG</a> (WT)</div></li>
<li class="main hideifmob"><div class="season">2027</div><div class="name">
<a href="team/uae-team-emirates-xrg-2027">UAE Team Emirates - XRG</a> (WT)</div></li>
<li class="main"><div class="season">2026</div><div class="name">
<a href="team/uae-team-emirates-xrg-2026">UAE Team Emirates - XRG</a> (WT)</div></li>
<li class="main old"><div class="season">2018</div><div class="name">
<a href="team/ljubljana-gusto-xaurum-2018">Ljubljana Gusto Xaurum</a></div></li>
</ul></span>"""


def _page(*parts) -> BeautifulSoup:
    return BeautifulSoup("<html><body>" + "".join(parts) + "</body></html>", "lxml")


class TestParseRiderLive:
    def test_all_profile_fields(self):
        data = _parse_rider(_page(_NAV, _TITLE, _INFO, _TEAMS), "tadej-pogacar")
        assert data == {
            "pcs_slug": "tadej-pogacar",
            "name": "Tadej Pogačar",
            "nationality": "Slovenia",
            "dob": "1998-09-21",
            "team": "UAE Team Emirates - XRG",
            "pcs_rank": 1,
            "speciality": "climber",
            "weight_kg": 66.0,
            "height_cm": 176.0,
        }

    def test_team_never_comes_from_site_menu(self):
        data = _parse_rider(_page(_NAV, "<h1>Some Rider</h1>", _INFO), "some-rider")
        assert data["team"] is None

    def test_team_from_history_when_subtitle_missing(self):
        title = _TITLE.replace('<div class="subtitle"><h2 translate="no">UAE Team Emirates - XRG</h2></div>', "")
        teams = (_TEAMS.replace('href="team/uae-team-emirates-xrg-2026">UAE Team Emirates - XRG',
                                'href="team/uae-team-emirates-xrg-2026">UAE 2026')
                       .replace('href="team/uae-team-emirates-xrg-2027">UAE Team Emirates - XRG',
                                'href="team/uae-team-emirates-xrg-2027">UAE future'))
        page = _page(_NAV, title, _INFO, teams)
        # the latest season that has started, not the future contract rows
        assert _current_team(page, season=2026) == "UAE 2026"
        assert _current_team(page, season=2027) == "UAE future"
        assert _parse_rider(page, "tadej-pogacar")["team"] in {"UAE 2026", "UAE future"}

    def test_dob_with_age_in_one_div(self):
        assert _parse_dob_parts("21st September 1998 (28)".split()) == "1998-09-21"


class TestParseRiderLegacy:
    def test_legacy_list_markup(self):
        html = """
        <h1>Tadej Pogačar</h1>
        <ul class="list"><li><div class="bold mr5">Date of birth:</div><div class="mr3">21st</div>
        <div class="mr3">September</div><div class="mr3">1998</div><div>(</div><div>27</div><div>)</div></li>
        <li><div class="bold mr5">Nationality:</div><div><span class="flag si"></span></div>
        <div><a href="nation/slovenia">Slovenia</a></div></li>
        <li><div class="bold mr5">Weight:</div><div class="mr3">66</div><div class="mr10">kg</div>
        <div class="bold mr5">Height:</div><div class="mr3">1.76</div><div>m</div></li>
        <li><div class="bold mr5">Team:</div><div><a href="team/uae-team-emirates-2025">UAE Team Emirates</a></div></li>
        <li><div class="bold mr5">PCS ranking:</div><div>1</div></li></ul>"""
        data = _parse_rider(_page(_NAV, html), "tadej-pogacar")
        assert (data["dob"], data["nationality"], data["weight_kg"], data["height_cm"]) == (
            "1998-09-21", "Slovenia", 66.0, 176.0)
        assert data["team"] == "UAE Team Emirates"
        assert data["pcs_rank"] == 1
