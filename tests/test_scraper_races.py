"""Tests for race/stage parsing helpers (legacy and live Oct 2026 markup)."""
import pytest

import scraper.races as races_mod
from scraper.races import (
    _parse_date_range, _parse_float, _parse_int, _parse_race_list, _parse_stage_date,
    _parse_stage_list_table, _resolve_stage_dates, _stage_options,
    fetch_race_stages, normalize_race_href, stage_meta_from_soup, gender_from_category, _detect_gender,
)
from scraper.utils import soup


class TestParseDateRange:
    def test_stage_race(self):
        start, end = _parse_date_range("16.01 - 21.01", 2024)
        assert start == "2024-01-16"
        assert end == "2024-01-21"

    def test_one_day_race(self):
        start, end = _parse_date_range("28.01", 2024)
        assert start == "2024-01-28"
        assert end == "2024-01-28"

    def test_december(self):
        start, end = _parse_date_range("07.12 - 15.12", 2023)
        assert start == "2023-12-07"
        assert end == "2023-12-15"


class TestParseStageDate:
    def test_standard(self):
        assert _parse_stage_date("16/01", 2024) == "2024-01-16"

    def test_single_digit_day(self):
        assert _parse_stage_date("03/07", 2024) == "2024-07-03"

    def test_none_year(self):
        result = _parse_stage_date("16/01", None)
        assert result is not None and result.endswith("-01-16")

    def test_invalid_returns_none(self):
        assert _parse_stage_date("not-a-date", 2024) is None


class TestParseNumeric:
    def test_float_with_km(self):
        assert _parse_float("144km") == 144.0

    def test_float_decimal(self):
        assert _parse_float("141.6") == 141.6

    def test_float_comma(self):
        assert _parse_float("141,6") == 141.6

    def test_int_with_spaces(self):
        assert _parse_int("3 200") == 3200

    def test_empty_returns_none(self):
        assert _parse_float("") is None
        assert _parse_int("") is None


# ── Live PCS markup (Oct 2026) ────────────────────────────────────────────────


class TestNormalizeRaceHref:
    def test_hyphen_stage_link_with_known_race(self):
        assert normalize_race_href("race/tour-de-france-2026-stage-1", "race/tour-de-france/2026") \
            == "race/tour-de-france/2026/stage-1"

    def test_split_stage_and_prologue(self):
        race = "race/bene-ladies-tour/2026"
        assert normalize_race_href("race/bene-ladies-tour-2026-stage-3a", race) == race + "/stage-3a"
        assert normalize_race_href("race/bene-ladies-tour-2026-prologue", race) == race + "/prologue"

    def test_name_with_digits(self):
        assert normalize_race_href("race/kampioenschap-van-vlaanderen1-2026-result",
                                   "race/kampioenschap-van-vlaanderen1/2026") \
            == "race/kampioenschap-van-vlaanderen1/2026/result"

    def test_without_race_hint(self):
        assert normalize_race_href("race/il-lombardia-2026-result") == "race/il-lombardia/2026/result"
        assert normalize_race_href("/race/tour-down-under/2024/gc") == "race/tour-down-under/2024/gc"


# Trimmed from race/tour-de-france/2026/gc/stages ("Stage winners and leaders").
TDF_STAGE_LIST = """
<table class="basic"><tr><th class="w6">Date</th><th class="w2"></th><th class="w43 mb_w90">Stage</th>
<th class="w24 mb_w100">Stage winner</th><th class="w24 mb_w100">Leader after stage</th></tr>
<tr class=""><td>07/04</td><td><span class="icon profile p3 mg_rp4"></span></td><td class="mb_w90">
<a href="race/tour-de-france-2026-stage-1">Stage 1 (TTT) | Barcelona - Barcelona</a></td>
<td class="mb_w100"><a href="tour-de-france-2026-gc"></a></td>
<td class="mb_w100"><span class="flag dk"></span> <a href="rider/jonas-vingegaard">VINGEGAARD Jonas</a></td></tr>
<tr class=""><td>07/06</td><td><span class="icon profile p5 mg_rp4"></span></td><td class="mb_w90">
<a href="race/tour-de-france-2026-stage-3">Stage 3 | Granollers - Les Angles</a></td>
<td><a href="rider/tadej-pogacar">POGAČAR Tadej</a></td><td><a href="rider/tadej-pogacar">POGAČAR Tadej</a></td></tr>
<tr class=""><td>07/21</td><td><span class="icon profile p1 mg_rp4"></span></td><td class="mb_w90">
<a href="race/tour-de-france-2026-stage-16">Stage 16 (ITT) | Évian-les-Bains - Thonon-les-Bains</a></td>
<td></td><td></td></tr>
<tr class=""><td>07/26</td><td><span class="icon profile p1 mg_rp4"></span></td><td class="mb_w90">
<a href="race/tour-de-france-2026-stage-21">Stage 21 | Paris - Paris</a></td>
<td><a href="rider/mathieu-van-der-poel">VAN DER POEL Mathieu</a></td><td></td></tr>
</table>"""

BENE_STAGE_LIST = """
<table class="basic"><tr><th>Date</th><th></th><th>Stage</th><th>Stage winner</th><th>Leader after stage</th></tr>
<tr><td>07/15</td><td><span class="icon profile p1 mg_rp4"></span></td>
<td><a href="race/bene-ladies-tour-2026-prologue">Prologue | IJzendijke - IJzendijke</a></td><td></td><td></td></tr>
<tr><td>07/18</td><td><span class="icon profile p1 mg_rp4"></span></td>
<td><a href="race/bene-ladies-tour-2026-stage-3a">Stage 3a (ITT) | Maaseik - Maaseik</a></td><td></td><td></td></tr>
<tr><td>07/18</td><td><span class="icon profile p2 mg_rp4"></span></td>
<td><a href="race/bene-ladies-tour-2026-stage-3b">Stage 3b | Maaseik - Maaseik</a></td><td></td><td></td></tr>
</table>"""


class TestStageListPage:
    def test_tdf_rows(self):
        table = soup(TDF_STAGE_LIST).find("table")
        stages = _parse_stage_list_table(table, "race/tour-de-france/2026", 2026)
        assert [s["pcs_slug"] for s in stages] == [
            "race/tour-de-france/2026/stage-1", "race/tour-de-france/2026/stage-3",
            "race/tour-de-france/2026/stage-16", "race/tour-de-france/2026/stage-21"]
        # MM/DD on the live page: 07/04 is the 4th of July
        assert [s["date"] for s in stages] == ["2026-07-04", "2026-07-06", "2026-07-21", "2026-07-26"]
        assert [s["profile_type"] for s in stages] == ["ttt", "mountain", "itt", "flat"]
        assert stages[1]["departure"] == "Granollers" and stages[1]["arrival"] == "Les Angles"
        assert [s["stage_num"] for s in stages] == [1, 2, 3, 4]   # ordinal position
        assert all(s["distance_km"] is None for s in stages)   # no KM column on this page

    def test_prologue_and_split_stages(self):
        table = soup(BENE_STAGE_LIST).find("table")
        stages = _parse_stage_list_table(table, "race/bene-ladies-tour/2026", 2026)
        assert [(s["pcs_slug"].rsplit("/", 1)[1], s["profile_type"], s["date"]) for s in stages] == [
            ("prologue", "itt", "2026-07-15"), ("stage-3a", "itt", "2026-07-18"),
            ("stage-3b", "hilly", "2026-07-18")]


class TestResolveStageDates:
    def test_month_day_detected_from_day_above_12(self):
        assert _resolve_stage_dates(["04/28", "05/03"], 2026) == ["2026-04-28", "2026-05-03"]

    def test_day_month_detected_from_day_above_12(self):
        assert _resolve_stage_dates(["28/04", "03/05"], 2026) == ["2026-04-28", "2026-05-03"]

    def test_ambiguous_prefers_compact_non_decreasing_reading(self):
        # MM/DD: 3-5 June (2 days). DD/MM would be 6 Mar, 6 Apr, 6 May.
        assert _resolve_stage_dates(["06/03", "06/04", "06/05"], 2026) == [
            "2026-06-03", "2026-06-04", "2026-06-05"]
        # DD/MM: 3-5 June. MM/DD would be 6 Mar, 6 Apr, 6 May.
        assert _resolve_stage_dates(["03/06", "04/06", "05/06"], 2026) == [
            "2026-06-03", "2026-06-04", "2026-06-05"]

    def test_single_ambiguous_date_uses_live_month_day_order(self):
        assert _resolve_stage_dates(["07/04"], 2026) == ["2026-07-04"]

    def test_unparseable(self):
        assert _resolve_stage_dates([None, "x"], 2026) == [None, None]


# Trimmed from a live stage page: the info block is a flat run of label divs.
STAGE4_INFO = """
<div class="lineh16"><div class="bold mr5" style="display: inline-block;">Date</div>
<div class="" style="display: inline-block;">7 July 2026</div><br/>
<div class="bold mr5" style="display: inline-block;">Classification:</div><div>2.UWT</div><br/>
<div class="bold mr5" style="display: inline-block;">Race category:</div><div>ME</div><br/>
<div class="bold mr5" style="display: inline-block;">Distance:</div><div class="mr3">181.9</div><div>km</div><br/>
<div class="bold mr5" style="display: inline-block;">Points scales: </div><div>GT.A.Stage</div><br/>
<div class="bold mr5" style="display: inline-block;">Parcours type: </div>
<div><span class="icon profile p2 mg_rp4"></span></div><div>Hills, flat finish</div><br/>
<div class="bold mr5" style="display: inline-block;">Profile score:</div><div class="mr5">108</div><div></div><br/>
<div class="bold mr5" style="display: inline-block;">Gradient final km: </div><div>0.8%</div><br/>
<div></div><div class="bold mr5" style="display: inline-block;">Vertical meters: </div><div>2784</div><br/>
<div class="bold mr5" style="display: inline-block;">Departure: </div><div><a href="location/carcassonne">Carcassonne</a></div><br/>
<div class="bold mr5" style="display: inline-block;">Arrival: </div><div><a href="location/foix">Foix</a></div><br/>
<div class="bold mr5" style="display: inline-block;">Won how: </div><div>Sprint of 10 riders</div><br/>
</div>"""


class TestStageMeta:
    def test_stage_info_block(self):
        meta = stage_meta_from_soup(soup(STAGE4_INFO))
        assert meta == {
            "date": "2026-07-07", "distance_km": 181.9, "elevation_m": 2784, "profile_type": "hilly",
            "gradient_final_km": 0.8, "profile_score": 108, "departure": "Carcassonne",
            "arrival": "Foix", "race_class": "2.UWT", "points_scale": "GT.A.Stage", "race_category": "ME"}

    def test_race_category_gives_the_gender(self):
        # Tour El Salvador is in the men's calendar circuit with no marker in
        # its name; its pages say "Race category: WE"
        html = STAGE4_INFO.replace("<div>ME</div>", "<div>WE</div>")
        assert gender_from_category(stage_meta_from_soup(soup(html))["race_category"]) == "women"
        assert gender_from_category("ME") == "men" and gender_from_category("MU") == "men"
        assert gender_from_category(None) is None and gender_from_category("") is None

    def test_profile_score_with_final_part(self):
        html = ('<div class="lineh16"><div class="bold mr5">Profile score:</div><div class="mr5">316</div>'
                '<div class="mr3">(final: </div><div>139</div><div>)</div><br/></div>')
        assert stage_meta_from_soup(soup(html))["profile_score"] == 316

    def test_ttt_from_points_scale(self):
        html = STAGE4_INFO.replace("GT.A.Stage", "GT.A.TTT")
        assert stage_meta_from_soup(soup(html))["profile_type"] == "ttt"

    def test_itt_from_won_how(self):
        html = STAGE4_INFO.replace("Sprint of 10 riders", "Time Trial")
        assert stage_meta_from_soup(soup(html))["profile_type"] == "itt"


# The race overview of a stage race renders the final stage page; its stage
# <select> has hyphen-style option values.
OVERVIEW_WITH_SELECT = """
<html><body>
<select onchange="window.location.href=this.value; ">
<option value="race/tour-de-france-2026-gc">ME</option>
<option value="race/tour-de-france-femmes-2026-gc">WE</option></select>
<select onchange="window.location.href=this.value; ">
<option value="race/tour-de-france-2026-stage-1">Stage 1 (TTT) | Barcelona - Barcelona</option>
<option value="race/tour-de-france-2026-stage-3">Stage 3 | Granollers - Les Angles</option>
<option value="race/tour-de-france-2026-gc">General classification</option>
<option value="race/tour-de-france-2026-points">Points classification</option></select>
<select class="resultFilter"><option value="1253">UAE Team Emirates - XRG</option></select>
</body></html>"""

ONE_DAY_PAGE = """
<html><body>
<select><option value="race/il-lombardia-2026-result">ME</option>
<option value="race/piccolo-giro-di-lombardia-2026-result">MU</option></select>
<select><option value="race/il-lombardia-2027-result">2027</option>
<option value="race/il-lombardia-2026-result">2026</option></select>
<div class="lineh16"><div class="bold mr5">Date</div><div>10 October 2026</div><br/>
<div class="bold mr5">Distance:</div><div class="mr3">239</div><div>km</div><br/>
<div class="bold mr5">Parcours type: </div><div><span class="icon profile p4 mg_rp4"></span></div>
<div>Mountains, flat finish</div><br/>
<div class="bold mr5">Profile score:</div><div class="mr5">316</div><div class="mr3">(final: </div><div>139</div><div>)</div><br/>
<div class="bold mr5">Gradient final km: </div><div>0.0%</div><br/>
<div class="bold mr5">Vertical meters: </div><div>4662</div><br/>
<div class="bold mr5">Departure: </div><div><a href="location/bergamo">Bergamo</a></div><br/>
<div class="bold mr5">Arrival: </div><div><a href="location/como">Como</a></div><br/>
</div></body></html>"""


def test_stage_options_from_overview_select():
    opts = _stage_options(soup(OVERVIEW_WITH_SELECT), "race/tour-de-france/2026")
    assert opts == [("race/tour-de-france/2026/stage-1", "Stage 1 (TTT) | Barcelona - Barcelona"),
                    ("race/tour-de-france/2026/stage-3", "Stage 3 | Granollers - Les Angles")]
    assert _stage_options(soup(ONE_DAY_PAGE), "race/il-lombardia/2026") == []


def _fake_fetch(pages):
    calls = []

    def fetch(url, use_cache=True):
        calls.append(url)
        return pages.get(url)
    return fetch, calls


def test_fetch_race_stages_live_stage_race(monkeypatch):
    base = "https://www.procyclingstats.com/"
    fetch, calls = _fake_fetch({
        base + "race/tour-de-france/2026": OVERVIEW_WITH_SELECT,
        base + "race/tour-de-france/2026/gc/stages": f"<html><body>{TDF_STAGE_LIST}</body></html>",
    })
    monkeypatch.setattr(races_mod, "fetch", fetch)
    stages = fetch_race_stages("race/tour-de-france/2026")
    assert len(stages) == 4 and stages[0]["date"] == "2026-07-04"
    assert calls == [base + "race/tour-de-france/2026", base + "race/tour-de-france/2026/gc/stages"]


def test_fetch_race_stages_falls_back_to_select_options(monkeypatch):
    base = "https://www.procyclingstats.com/"
    fetch, _ = _fake_fetch({base + "race/tour-de-france/2026": OVERVIEW_WITH_SELECT})
    monkeypatch.setattr(races_mod, "fetch", fetch)
    stages = fetch_race_stages("race/tour-de-france/2026")
    assert [(s["pcs_slug"], s["stage_num"], s["date"], s["profile_type"]) for s in stages] == [
        ("race/tour-de-france/2026/stage-1", 1, None, "ttt"),
        ("race/tour-de-france/2026/stage-3", 2, None, None)]


def test_fetch_race_stages_live_one_day(monkeypatch):
    base = "https://www.procyclingstats.com/"
    fetch, calls = _fake_fetch({base + "race/il-lombardia/2026": ONE_DAY_PAGE})
    monkeypatch.setattr(races_mod, "fetch", fetch)
    (stage,) = fetch_race_stages("race/il-lombardia/2026")
    assert stage["pcs_slug"] == "race/il-lombardia/2026" and stage["stage_num"] is None
    assert (stage["date"], stage["distance_km"], stage["elevation_m"], stage["profile_type"]) == (
        "2026-10-10", 239.0, 4662, "mountain")
    assert (stage["profile_score"], stage["gradient_final_km"]) == (316, 0.0)
    assert (stage["departure"], stage["arrival"]) == ("Bergamo", "Como")
    assert calls == [base + "race/il-lombardia/2026"]   # info complete: no /result request


def test_race_list_accepts_hyphen_style_links():
    html = """<table class="basic"><thead><tr><th>Date</th><th>Date</th><th>Race</th><th>Winner</th>
    <th>Class</th></tr></thead><tbody>
    <tr><td>06.07 - 26.07</td><td>06.07</td><td><span class="flag fr"></span>
    <a href="race/tour-de-france-2026-gc">Tour de France</a></td><td></td><td>2.UWT</td></tr>
    <tr><td>10.10</td><td>10.10</td><td><span class="flag it"></span>
    <a href="race/il-lombardia/2026/result">Il Lombardia</a></td><td></td><td>1.UWT</td></tr>
    </tbody></table>"""
    races = list(_parse_race_list(html, 2026, ["2.UWT", "1.UWT"]))
    assert [r["pcs_slug"] for r in races] == ["race/tour-de-france/2026", "race/il-lombardia/2026"]
    assert races[0]["is_stage_race"] == 1 and races[1]["is_stage_race"] == 0


# ── Renamed races ────────────────────────────────────────────────────────────
EMPTY_PCS_PAGE = "<html><head></head><body></body></html>"   # PCS answer for an unknown slug

CALENDAR_2026_C1 = """<html><head><title>UCI Cycling calendar 2026</title></head><body>
<table class="basic"><thead><tr><th>Date</th><th>Date</th><th>Race</th><th>Winner</th><th>Class</th></tr></thead><tbody>
<tr><td class="cu500">07.06 - 14.06</td><td class="hide cs500">07.06</td><td><span class="flag fr"></span>
<a href="race/tour-auvergne-rhone-alpes-2026-gc">Tour Auvergne - Rhône-Alpes</a></td><td></td><td>2.UWT</td></tr>
<tr><td class="cu500">07.06 - 14.06</td><td class="hide cs500">07.06</td><td><span class="flag ch"></span>
<a href="race/tour-de-suisse-2026-gc">Tour de Suisse</a></td><td></td><td>2.UWT</td></tr>
<tr><td class="cu500">04.07 - 26.07</td><td class="hide cs500">04.07</td><td><span class="flag fr"></span>
<a href="race/tour-de-france-2026-gc">Tour de France</a></td><td></td><td>2.UWT</td></tr>
</tbody></table></body></html>"""


def test_is_missing_page():
    assert races_mod.is_missing_page(EMPTY_PCS_PAGE)
    assert races_mod.is_missing_page("")
    assert not races_mod.is_missing_page(CALENDAR_2026_C1)


def test_fetch_race_stages_empty_pcs_page(monkeypatch):
    fetch, _ = _fake_fetch({"https://www.procyclingstats.com/race/dauphine/2026": EMPTY_PCS_PAGE})
    monkeypatch.setattr(races_mod, "fetch", fetch)
    assert fetch_race_stages("race/dauphine/2026") == []    # no bogus one-day pseudo-stage


def test_find_race_alias_by_dates_class_and_name(monkeypatch):
    base = "https://www.procyclingstats.com/races.php?year=2026&circuit="
    fetch, calls = _fake_fetch({base + "1&class=": CALENDAR_2026_C1})
    monkeypatch.setattr(races_mod, "fetch", fetch)
    # Two 2.UWT races on 7-14 June: the name decides.
    assert races_mod.find_race_alias("race/dauphine/2026", "2026-06-07", "2026-06-14", "2.UWT",
                                     "Tour Auvergne - Rhône-Alpes") == "race/tour-auvergne-rhone-alpes/2026"
    assert races_mod.find_race_alias("race/dauphine/2026", "2026-06-07", "2026-06-14", "2.UWT",
                                     "Critérium du Dauphiné") is None          # ambiguous → no guess
    assert races_mod.find_race_alias("race/x/2026", "2026-07-04", "2026-07-26", "2.UWT") == "race/tour-de-france/2026"
    assert races_mod.find_race_alias("race/x/2026", "2026-07-05", "2026-07-26", "2.UWT") is None
    assert all(u.startswith(base) for u in calls)


def test_find_race_alias_none_when_stored_slug_still_listed(monkeypatch):
    # A race whose own slug is still in the calendar was not renamed (its page
    # merely failed): the same-class race on the same dates is not an alias.
    base = "https://www.procyclingstats.com/races.php?year=2026&circuit="
    fetch, _ = _fake_fetch({base + "1&class=": CALENDAR_2026_C1})
    monkeypatch.setattr(races_mod, "fetch", fetch)
    assert races_mod.find_race_alias("race/tour-auvergne-rhone-alpes/2026", "2026-06-07", "2026-06-14",
                                     "2.UWT", "Tour Auvergne - Rhône-Alpes") is None


def test_is_missing_page_failed_fetch_is_not_missing():
    assert not races_mod.is_missing_page(None)


def test_detect_gender_from_calendar_row():
    # names and slugs of the women's races the men's circuits list (Oct 2026 DB)
    assert _detect_gender("Vuelta CV Feminas", "1.Pro") == "women"
    assert _detect_gender("La Choralis Fourmies Féminine", "1.Pro") == "women"
    assert _detect_gender("Setmana Ciclista-Volta Comunitat Valenciana Fèmines", "2.Pro") == "women"
    assert _detect_gender("Clasica de Almeria", "1.Pro", "race/clasica-de-almeria-we/2026") == "women"
    assert _detect_gender("Veenendaal - Veenendaal", "1.Pro", "race/veenendaal-veenendaal-classic-we/2025") == "women"
    assert _detect_gender("Strade Bianche", "1.WWT") == "women"
    # no marker anywhere: only the result pages' race category tells
    assert _detect_gender("Tour El Salvador", "2.1", "race/vuelta-el-salvador/2026") == "men"
    # men's races stay men's ('we' only as a whole slug token)
    assert _detect_gender("Tour de Pologne", "2.UWT", "race/tour-de-pologne/2026") == "men"
    assert _detect_gender("Gran Premio Industria", "1.1", "race/gp-industria-artigianato-weiler/2026") == "men"
