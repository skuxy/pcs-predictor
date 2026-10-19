"""Tests for result-table parsing: time-gap ditto marks, the live (Oct 2026)
header-mapped markup, UCI sanctions, stage-tab selection and team time trials."""
import pytest

import scraper.results as results_mod
from scraper.utils import soup
from scraper.results import (
    _looks_cancelled, _parse_points, _parse_results_table, _parse_ttt_results,
    fetch_stage_results, select_stage_result,
)


def _row(pos, slug, gap):
    return f"""
    <tr>
      <td>{pos}</td>
      <td class="ridername"><a href="rider/{slug}">{slug}</a></td>
      <td class="time ar"><font>{gap}</font></td>
    </tr>"""


def _table(rows):
    return soup(f"<table class='results'>{rows}</table>").find("table")


def test_ditto_mark_carries_previous_gap_not_zero():
    # PCS renders ",," when a rider's gap equals the PREVIOUS rider's gap.
    # Group 1: winner + one rider at 0:00. Group 2: two riders at +2:30.
    rows = (
        _row(1, "winner", "4:53:12")
        + _row(2, "same-time", ",,")
        + _row(3, "chase-a", "2:30")
        + _row(4, "chase-b", ",,")
    )
    res = _parse_results_table(_table(rows))
    gaps = {r["rider_slug"]: r["time_seconds"] for r in res}
    assert gaps["winner"] == 0
    assert gaps["same-time"] == 0
    assert gaps["chase-a"] == 150
    assert gaps["chase-b"] == 150  # ditto == previous rider's gap, not 0


def test_gruppetto_after_leaders():
    rows = (
        _row(1, "winner", "5:01:00")
        + _row(2, "second", "0:05")
        + _row(3, "grup-a", "21:14")
        + _row(4, "grup-b", ",,")
        + _row(5, "grup-c", ",,")
    )
    res = _parse_results_table(_table(rows))
    gaps = {r["rider_slug"]: r["time_seconds"] for r in res}
    assert gaps["grup-a"] == 1274
    assert gaps["grup-b"] == 1274
    assert gaps["grup-c"] == 1274


def test_dnf_rows_get_no_time():
    rows = _row(1, "winner", "4:00:00") + _row("DNF", "quitter", ",,")
    res = _parse_results_table(_table(rows))
    gaps = {r["rider_slug"]: r["time_seconds"] for r in res}
    assert gaps["quitter"] is None


def test_looks_cancelled_flags_all_nr_placeholder_table():
    # Cancelled/unraced stages render the startlist under table.results markup
    # with every rider marked "NR" instead of a real finish position.
    rows = (
        _row("NR", "rider-a", "") + _row("NR", "rider-b", "") + _row("NR", "rider-d", "")
        + _row("NR", "rider-e", "") + _row("NR", "rider-f", "") + _row("DNF", "rider-c", "")
    )
    res = _parse_results_table(_table(rows))
    assert _looks_cancelled(res)


def test_looks_cancelled_false_for_real_results():
    rows = _row(1, "winner", "4:00:00") + _row(2, "second", "0:05")
    res = _parse_results_table(_table(rows))
    assert not _looks_cancelled(res)


# ── Live PCS markup (Oct 2026) ────────────────────────────────────────────────
# Rows trimmed from real pages: header th[data-code] drive the column mapping,
# body cells only carry presentational classes.

_LIVE_HEAD = (
    '<thead><tr><th class="w5" data-code="rnk">Rnk</th><th data-code="age">Age</th>'
    '<th data-code="specialty">Specialty</th><th data-code="bib">BIB</th>'
    '<th data-code="gc">GC</th><th data-code="gc_timelag">Timelag</th>'
    '<th data-code="h2h">H2H</th><th data-code="rider">Rider</th>'
    '<th class="w25 cu600" data-code="riderteam">Team</th>'
    '<th class="w5 mobSmall" data-code="uci_pnt">UCI</th>'
    '<th class="w5 mobSmall" data-code="pnt">PCS</th><th data-code="bonis"></th>'
    '<th class="w1 ar" data-code="timelag">Timelag</th>'
    '<th data-code="time_wonlost">Time won/lost</th></tr></thead>'
)


def _live_row(rnk, bib, slug, name, team_slug, team, uci, pcs, time):
    return (
        f'<tr data-filters="si,1253,climber,teamlevel1,WT"><td class="">{rnk}</td>'
        '<td class="">27</td><td class=""><div class="resSp bg purple1"></div>'
        '<span class="fs10 clr999">Climber</span></td>'
        f'<td class="">{bib}</td><td class="fs11 clr666"></td><td class="fs11 clr666"></td>'
        '<td class=""><input class="gotoH2H_v7" type="checkbox"/></td>'
        f'<td class="mb_w50"><span class="flag si"></span> <a href="rider/{slug}">{name}</a>'
        f'<div class="clear"></div><div class="showIfMobile fs12 clr999">{team}</div></td>'
        f'<td class="cu600"><a href="team/{team_slug}">{team}</a></td>'
        f'<td class="mobSmall">{uci}</td><td class="mobSmall">{pcs}</td>'
        f'<td class="ar cu600"></td><td class="ar">{time}</td>'
        '<td class="ar"><a class="TimeWonLostFromRider2" href="">..</a></td></tr>'
    )


def _live_table(rows):
    html = f'<table class="unit results results6">{_LIVE_HEAD}<tbody>{rows}</tbody></table>'
    return soup(html).find("table")


RONDE_2026_ROWS = (
    # Pogačar: 800 UCI points earned, 25-point sanction (littering)
    _live_row(1, 1, "tadej-pogacar", "POGAČAR Tadej", "uae-team-emirates-xrg-2026",
              "UAE Team Emirates - XRG", '800 <span class="red fs10">-25</span>', 275, "6:20:07")
    + _live_row(2, 11, "mathieu-van-der-poel", "VAN DER POEL Mathieu",
                "alpecin-premier-tech-2026", "Alpecin - Premier Tech", "640", "200", "0:34")
    + _live_row(3, 111, "remco-evenepoel", "EVENEPOEL Remco", "red-bull-bora-hansgrohe-2026",
                "Red Bull - BORA - hansgrohe", "520", "150", "1:11")
    + _live_row(4, 21, "wout-van-aert", "VAN AERT Wout", "team-visma-lease-a-bike-2026",
                "Team Visma | Lease a Bike", "440", "120", ",,")
    + _live_row("DNF", 14, "edward-planckaert", "PLANCKAERT Edward", "alpecin-premier-tech-2026",
                "Alpecin - Premier Tech", "", "", "")
)


def test_live_header_mapping_parses_all_fields():
    res = _parse_results_table(_live_table(RONDE_2026_ROWS))
    assert [r["rider_slug"] for r in res] == [
        "tadej-pogacar", "mathieu-van-der-poel", "remco-evenepoel", "wout-van-aert", "edward-planckaert"]
    pog = res[0]
    assert pog["rider_name"] == "POGAČAR Tadej"            # mobile team div not included
    assert (pog["position"], pog["status"], pog["bib"]) == (1, "finished", 1)
    assert pog["team_slug"] == "uae-team-emirates-xrg-2026"
    assert pog["team_name"] == "UAE Team Emirates - XRG"
    assert pog["points_pcs"] == 275
    vdp = res[1]
    assert (vdp["points_uci"], vdp["uci_penalty"], vdp["time_seconds"]) == (640, None, 34)
    assert res[2]["time_seconds"] == 71
    assert res[3]["time_seconds"] == 71        # ",," = previous rider's gap
    dnf = res[4]
    assert (dnf["position"], dnf["status"], dnf["time_seconds"]) == (None, "dnf", None)
    assert dnf["points_uci"] is None and dnf["uci_penalty"] is None


def test_live_penalty_is_split_not_concatenated():
    # Old parser stored 80025 here.
    pog = _parse_results_table(_live_table(RONDE_2026_ROWS))[0]
    assert pog["points_uci"] == 800
    assert pog["uci_penalty"] == 25


def test_live_penalty_variants():
    tdf_stage4 = _live_row(13, 78, "vlad-van-mechelen", "VAN MECHELEN Vlad", "bahrain-victorious-2026",
                           "Bahrain - Victorious", '15 <span class="red fs10">-25</span>', "4", ",,")
    only_penalty = _live_row(140, 9, "gianni-moscon", "MOSCON Gianni", "red-bull-bora-hansgrohe-2026",
                             "Red Bull - BORA - hansgrohe", ' <span class="red fs10">-25</span>', "", "2:10")
    two_penalties = _live_row(3, 17, "sepp-kuss", "KUSS Sepp", "team-visma-lease-a-bike-2026",
                              "Team Visma | Lease a Bike",
                              '110 <span class="red fs10">-25</span> <span class="red fs10">-50</span>',
                              "50", "0:31")
    res = _parse_results_table(_live_table(tdf_stage4 + only_penalty + two_penalties))
    got = {r["rider_slug"]: (r["points_uci"], r["uci_penalty"]) for r in res}
    assert got["vlad-van-mechelen"] == (15, 25)       # old parser: 1525
    assert got["gianni-moscon"] == (None, 25)         # old parser: 25 points
    assert got["sepp-kuss"] == (110, 75)


def test_live_mapping_follows_header_not_position():
    # Same cells, header order shuffled: mapping must come from th[data-code].
    html = (
        '<table class="results"><thead><tr><th data-code="rider">Rider</th>'
        '<th data-code="rnk">#</th><th data-code="pnt">PCS</th><th data-code="uci_pnt">UCI</th>'
        '<th data-code="timelag">Time</th><th data-code="riderteam">Team</th></tr></thead><tbody>'
        '<tr><td><a href="rider/a-rider">A Rider</a></td><td>1</td><td>50</td><td>60</td>'
        '<td>3:06:43</td><td><a href="team/lidl-trek-2026">Lidl - Trek</a></td></tr>'
        '<tr><td><a href="rider/b-rider">B Rider</a></td><td>2</td><td>30</td><td>40</td>'
        '<td>0:07</td><td><a href="team/movistar-team-2026">Movistar Team</a></td></tr>'
        '</tbody></table>'
    )
    res = _parse_results_table(soup(html).find("table"))
    assert [(r["position"], r["points_pcs"], r["points_uci"], r["time_seconds"], r["team_slug"])
            for r in res] == [(1, 50, 60, 0, "lidl-trek-2026"), (2, 30, 40, 7, "movistar-team-2026")]


def test_live_time_trial_gaps_ignore_hundredths_font():
    # Live ITT / prologue rows (TdF 2026 stage 16, Romandie 2026 prologue):
    # the hundredths sit in a font after the gap.  The old parser read only
    # the font (',36') and stored NULL for every finisher but the winner.
    rows = (
        _live_row(1, 1, "tadej-pogacar", "POGAČAR Tadej", "uae-team-emirates-xrg-2026",
                  "UAE Team Emirates - XRG", "120", "100", '32:19<font class="fs10">,33</font>')
        + _live_row(2, 21, "jonas-vingegaard", "VINGEGAARD Jonas", "team-visma-lease-a-bike-2026",
                    "Team Visma | Lease a Bike", "100", "80", '0:28<font class="fs10">,36</font>')
        + _live_row(3, 31, "joao-almeida", "ALMEIDA João", "uae-team-emirates-xrg-2026",
                    "UAE Team Emirates - XRG", "80", "60", ",,")
        + _live_row(4, 41, "remco-evenepoel", "EVENEPOEL Remco", "red-bull-bora-hansgrohe-2026",
                    "Red Bull - BORA - hansgrohe", "70", "50", '1:04<font class="fs10">,31</font>')
        + _live_row("DNF", 51, "a-quitter", "QUITTER A", "lidl-trek-2026", "Lidl - Trek", "", "", "")
    )
    res = _parse_results_table(_live_table(rows))
    # whole seconds as displayed; the ditto row repeats the previous gap
    assert [r["time_seconds"] for r in res] == [0, 28, 28, 64, None]


def test_legacy_time_cell_with_hundredths_font():
    html = ("<table class='results'><tr><td>1</td><td class='ridername'><a href='rider/a'>A</a></td>"
            "<td class='time ar'><font>12:01</font></td></tr>"
            "<tr><td>2</td><td class='ridername'><a href='rider/b'>B</a></td>"
            "<td class='time ar'>0:05<font class='fs10'>,92</font></td></tr></table>")
    res = _parse_results_table(soup(html).find("table"))
    assert [r["time_seconds"] for r in res] == [0, 5]


def test_header_text_fallback_without_data_codes():
    html = (
        '<table class="results"><thead><tr><th>Rnk</th><th>BIB</th><th>Rider</th><th>Team</th>'
        '<th>UCI</th><th>PCS</th><th>Time</th></tr></thead><tbody>'
        '<tr><td>1</td><td>51</td><td><a href="rider/paul-seixas">SEIXAS Paul</a></td>'
        '<td><a href="team/decathlon-cma-cgm-team-2026">Decathlon CMA CGM Team</a></td>'
        '<td>800</td><td>275</td><td>5:49:28</td></tr></tbody></table>'
    )
    (row,) = _parse_results_table(soup(html).find("table"))
    assert (row["rider_slug"], row["bib"], row["points_uci"], row["points_pcs"]) == (
        "paul-seixas", 51, 800, 275)


@pytest.mark.parametrize("html,expected", [
    ('<td>800</td>', (800, None)),
    ('<td>26.25</td>', (26.25, None)),          # TTT team share
    ('<td></td>', (None, None)),
    ('<td>5 <span class="red fs10">-25</span></td>', (5, 25)),
    ('<td>-25</td>', (None, 25)),
])
def test_parse_points(html, expected):
    assert _parse_points(soup(f"<table><tr>{html}</tr></table>").find("td")) == expected


def test_legacy_uci_cell_with_penalty_span_is_split():
    row = (
        '<tr><td>1</td><td class="ridername"><a href="rider/tadej-pogacar">POGAČAR Tadej</a></td>'
        '<td class="cu600"><a href="team/uae-team-emirates-xrg-2026">UAE</a></td>'
        '<td class="uci_pnt">800 <span class="red fs10">-25</span></td><td class="pnt">275</td>'
        '<td class="time ar"><font>6:20:07</font></td></tr>'
    )
    (r,) = _parse_results_table(_table(row))
    assert (r["points_uci"], r["uci_penalty"], r["team_slug"]) == (800, 25, "uae-team-emirates-xrg-2026")


# A final-stage page: the race overview URL renders it with the GC tab
# visible (resultCont without "hide"); the stage tab must still be chosen.
def _tabbed_page(stage_rows, gc_rows, visible="gc"):
    nav = (
        '<ul class="restabs"><li><a class="resultNav" data-navid="2070" href="x">'
        '<center>Stage</center></a></li><li><a class="resultNav" data-navid="2065" href="y">'
        '<center>GC</center></a></li></ul>'
    )
    stage_cls = "resultCont hide" if visible == "gc" else "resultCont"
    gc_cls = "resultCont" if visible == "gc" else "resultCont hide"
    return (
        f'<html><body>{nav}'
        f'<div class="{stage_cls}" data-navid="2070"><table class="results">{_LIVE_HEAD}'
        f'<tbody>{stage_rows}</tbody></table></div>'
        f'<div class="{gc_cls}" data-navid="2065"><table class="results">{_LIVE_HEAD}'
        f'<tbody>{gc_rows}</tbody></table></div></body></html>'
    )


STAGE_ROWS = (
    _live_row(1, 101, "mathieu-van-der-poel", "VAN DER POEL Mathieu", "alpecin-premier-tech-2026",
              "Alpecin - Premier Tech", "210", "100", "2:58:10")
    + _live_row(2, 105, "jasper-philipsen", "PHILIPSEN Jasper", "alpecin-premier-tech-2026",
                "Alpecin - Premier Tech", "150", "70", ",,")
)
GC_ROWS = _live_row(1, 1, "tadej-pogacar", "POGAČAR Tadej", "uae-team-emirates-xrg-2026",
                    "UAE Team Emirates - XRG", "1300", "1000", "79:50:01")


@pytest.mark.parametrize("visible", ["gc", "stage"])
def test_select_stage_result_picks_stage_tab(visible):
    s = soup(_tabbed_page(STAGE_ROWS, GC_ROWS, visible=visible))
    res = _parse_results_table(select_stage_result(s))
    assert [r["rider_slug"] for r in res] == ["mathieu-van-der-poel", "jasper-philipsen"]


def test_select_stage_result_one_day_page_without_tabs():
    page = (f'<div class="resultCont"><table class="results">{_LIVE_HEAD}<tbody>'
            f'{RONDE_2026_ROWS}</tbody></table></div>')
    res = _parse_results_table(select_stage_result(soup(page)))
    assert res[0]["rider_slug"] == "tadej-pogacar"


TTT_PAGE = (
    '<html><body><ul><li><a class="resultNav" data-navid="2070" href="x"><center>Stage</center></a></li>'
    '<li><a class="resultNav" data-navid="2065" href="y"><center>GC</center></a></li></ul>'
    '<div class="resultCont" data-navid="2070"><ul class="list ttt-results">'
    '<li class="fs10 clr999 uppercase hideIfMobile"><div class="w5">#</div><div class="w45">Team</div></li>'
    '<li><div class="w50 left mb_w100"><div class="w10 fs14">1</div><div class="w90 fs14 bold">'
    '<span class="flag nl"></span> <a href="team/team-visma-lease-a-bike-2026">Team Visma | Lease a Bike</a>'
    '</div></div><div class="w50 left mb_w100 timeSpeed"><div class="w25 fs14 bold time">21:47'
    '<font class="fs10">,87</font></div><div class="w25 fs14">0:00<font class="fs10">,00</font></div></div>'
    '<div class="w100"><table style="margin-left: 5%; "><tbody>'
    '<tr><td class="w80 mb_w70"><a href="rider/jonas-vingegaard">VINGEGAARD Jonas</a></td>'
    '<td class="w7 mb_w10">50</td><td class="w13 mb_w20">26.25</td></tr>'
    '<tr><td class="w80 mb_w70"><a href="rider/sepp-kuss">KUSS Sepp  <font class="blue">+1:57</font></a></td>'
    '<td class="w7 mb_w10">50</td><td class="w13 mb_w20">26.25</td></tr></tbody></table></div></li>'
    '<li><div class="w50 left mb_w100"><div class="w10 fs14">2</div><div class="w90 fs14 bold">'
    '<a href="team/netcompany-ineos-2026">Netcompany INEOS</a></div></div>'
    '<div class="w50 left mb_w100 timeSpeed"><div class="w25 fs14 bold time">21:55<font class="fs10">,19</font>'
    '</div></div><div class="w100"><table><tbody>'
    '<tr><td><a href="rider/filippo-ganna">GANNA Filippo</a></td><td>40</td><td>18.75</td></tr>'
    '</tbody></table></div></li></ul></div>'
    f'<div class="resultCont hide" data-navid="2065"><table class="results">{_LIVE_HEAD}<tbody>'
    f'{GC_ROWS}</tbody></table></div></body></html>'
)


def test_ttt_stage_tab_gives_individual_rows():
    target = select_stage_result(soup(TTT_PAGE))
    assert target.name == "ul"
    res = _parse_ttt_results(target)
    got = [(r["position"], r["rider_slug"], r["time_seconds"], r["points_uci"], r["points_pcs"], r["team_slug"])
           for r in res]
    assert got == [
        (1, "jonas-vingegaard", 0, 26.25, 50, "team-visma-lease-a-bike-2026"),
        (2, "filippo-ganna", 7, 18.75, 40, "netcompany-ineos-2026"),        # 21:55.19 - 21:47.87
        (3, "sepp-kuss", 117, 26.25, 50, "team-visma-lease-a-bike-2026"),   # team time + 1:57
    ]
    assert res[2]["rider_name"] == "KUSS Sepp"          # gap font not part of the name
    assert all(r["status"] == "finished" for r in res)


def test_fetch_stage_results_end_to_end_without_network(monkeypatch):
    pages = {
        "https://www.procyclingstats.com/race/tour-de-france/2026": _tabbed_page(STAGE_ROWS, GC_ROWS),
        "https://www.procyclingstats.com/race/tour-de-france/2026/stage-1": TTT_PAGE,
    }
    monkeypatch.setattr(results_mod, "fetch", lambda url, use_cache=True: pages.get(url))
    stage = fetch_stage_results("race/tour-de-france/2026")
    assert [r["rider_slug"] for r in stage] == ["mathieu-van-der-poel", "jasper-philipsen"]
    assert set(stage[0]) >= {"team_slug", "team_name", "uci_penalty", "points_uci", "bib"}
    ttt = fetch_stage_results("race/tour-de-france/2026/stage-1")
    assert len(ttt) == 3 and ttt[0]["points_uci"] == 26.25


def test_fetch_stage_results_falls_back_to_result_url(monkeypatch):
    pages = {
        "https://www.procyclingstats.com/race/gp-x/2026": "<html><body><p>overview</p></body></html>",
        "https://www.procyclingstats.com/race/gp-x/2026/result": (
            f'<div class="resultCont"><table class="results">{_LIVE_HEAD}<tbody>{RONDE_2026_ROWS}'
            '</tbody></table></div>'),
    }
    monkeypatch.setattr(results_mod, "fetch", lambda url, use_cache=True: pages.get(url))
    res = fetch_stage_results("race/gp-x/2026")
    assert len(res) == 5 and res[0]["uci_penalty"] == 25


def test_empty_table_without_outcome_is_not_refetched(monkeypatch):
    # PCS lists races it has no results for: empty table, blank "Won how".
    page = (
        f'<html><head><title>GP Emilia 2026</title></head><body><div class="resultCont">'
        f'<table class="results">{_LIVE_HEAD}<tbody></tbody></table></div>'
        '<div class="lineh16"><div class="bold mr5">Date</div><div>29 March 2026</div><br/>'
        '<div class="bold mr5">Won how: </div><div></div><br/></div></body></html>'
    )
    calls = []

    def fetch(url, use_cache=True):
        calls.append(url)
        return page
    monkeypatch.setattr(results_mod, "fetch", fetch)
    monkeypatch.setattr(results_mod, "_try_playwright", lambda url: pytest.fail("needless re-fetch"))
    assert fetch_stage_results("race/gp-emilia/2026") == []
    assert calls == ["https://www.procyclingstats.com/race/gp-emilia/2026"]


def test_empty_table_with_outcome_is_retried_with_browser(monkeypatch):
    skeleton = (
        f'<html><head><title>X</title></head><body><div class="resultCont"><table class="results">'
        f'{_LIVE_HEAD}<tbody></tbody></table></div><div class="lineh16"><div class="bold mr5">Won how: </div>'
        '<div>Sprint of a small group</div><br/></div></body></html>')
    rendered = soup(f'<div class="resultCont"><table class="results">{_LIVE_HEAD}<tbody>{RONDE_2026_ROWS}'
                    '</tbody></table></div>')
    monkeypatch.setattr(results_mod, "fetch", lambda url, use_cache=True: skeleton)
    monkeypatch.setattr(results_mod, "_try_playwright", lambda url: rendered)
    assert len(fetch_stage_results("race/x/2026/stage-1")) == 5


def test_empty_pcs_page_returns_nothing(monkeypatch):
    monkeypatch.setattr(results_mod, "fetch", lambda url, use_cache=True: "<html><head></head><body></body></html>")
    monkeypatch.setattr(results_mod, "_try_playwright", lambda url: pytest.fail("needless re-fetch"))
    assert fetch_stage_results("race/dauphine/2026/stage-2") == []


# ── annulled results (struck-through rank) ─────────────────────────────────

def test_struck_rank_is_an_annulled_result_not_a_finish():
    # San Juan 2023 stage 5: Miguel Ángel López's win was annulled; PCS keeps
    # him in the list with "<s> 1 </s>" and ranks Ganna 1st.  The old parser
    # stored two 1st places.  A struck mid-table rank (Gp de Wallonie 2023,
    # Bonnamour 6th) carries its gap into the next ",," row.  Every gap is
    # still printed from López's 4:07:10: Ganna's 4:07:40 makes them 30 s
    # too large (Higuita "0:38" is 8 s behind Ganna).
    rows = (
        _live_row('<s>&nbsp;1&nbsp;</s>', 215, "miguel-angel-lopez-moreno", "LÓPEZ Miguel Ángel",
                  "team-medellin-2023", "Team Medellín - EPM", "", "", "4:07:10")
        + _live_row(1, 62, "filippo-ganna", "GANNA Filippo", "ineos-grenadiers-2023",
                    "INEOS Grenadiers", '20 <span class="red fs10">-15</span>', "30", "4:07:40")
        + _live_row(2, 12, "sergio-higuita", "HIGUITA Sergio", "bora-hansgrohe-2023",
                    "BORA - hansgrohe", "15", "18", "0:38")
        + _live_row('<s>&nbsp;3&nbsp;</s>', 13, "franck-bonnamour", "BONNAMOUR Franck",
                    "ag2r-citroen-team-2023", "AG2R Citroën Team", "", "", "1:05")
        + _live_row(4, 61, "egan-bernal", "BERNAL Egan", "ineos-grenadiers-2023",
                    "INEOS Grenadiers", "10", "12", ",,")
    )
    res = {r["rider_slug"]: r for r in _parse_results_table(_live_table(rows))}
    lopez, bonnamour = res["miguel-angel-lopez-moreno"], res["franck-bonnamour"]
    assert (lopez["position"], lopez["status"], lopez["time_seconds"]) == (None, "dsq", None)
    assert (bonnamour["position"], bonnamour["status"], bonnamour["time_seconds"]) == (None, "dsq", None)
    assert lopez["bib"] == 215                   # bib still read (rider-merge evidence)
    assert (res["filippo-ganna"]["position"], res["filippo-ganna"]["time_seconds"]) == (1, 0)
    assert (res["sergio-higuita"]["position"], res["sergio-higuita"]["time_seconds"]) == (2, 8)
    assert (res["egan-bernal"]["position"], res["egan-bernal"]["time_seconds"]) == (4, 35)  # ",," after 1:05
    finished = [r["position"] for r in res.values() if r["status"] == "finished"]
    assert len(finished) == len(set(finished))


def test_empty_rider_placeholder_row_is_skipped():
    # San Juan 2023 stage 4 lists a nameless DNF row: <a href="rider/"></a>.
    # It was parsed as a rider with the slug 'rider'.
    rows = (_live_row(1, 1, "a-a", "A A", "t-2023", "T", "", "", "4:00:00")
            + _live_row("DNF", "", "", "", "", "", "", "", ""))
    got = [(r["rider_slug"], r["status"]) for r in _parse_results_table(_live_table(rows))]
    assert got == [("a-a", "finished")]
    legacy = _table(_row(1, "winner", "4:53:12") + _row("DNF", "", ""))
    assert [r["rider_slug"] for r in _parse_results_table(legacy)] == ["winner"]


def test_struck_winner_offset_same_time_or_gap():
    # Tour de Normandie Féminin 2023 stage 3: the annulled winner and the new
    # one share the time (",,"): no offset
    same = (_live_row('<s>&nbsp;1&nbsp;</s>', 1, "a-a", "A A", "t-2023", "T", "", "", "2:55:21")
            + _live_row(1, 2, "b-b", "B B", "t-2023", "T", "", "", "2:55:21")
            + _live_row(2, 3, "c-c", "C C", "t-2023", "T", "", "", ",,")
            + _live_row(3, 4, "d-d", "D D", "t-2023", "T", "", "", "0:12"))
    got = {r["rider_slug"]: r["time_seconds"] for r in _parse_results_table(_live_table(same))}
    assert got == {"a-a": None, "b-b": 0, "c-c": 0, "d-d": 12}
    # the new winner's cell printed as a gap to the annulled one
    gap = (_live_row('<s>&nbsp;1&nbsp;</s>', 1, "a-a", "A A", "t-2023", "T", "", "", "4:00:00")
           + _live_row(1, 2, "b-b", "B B", "t-2023", "T", "", "", "0:05")
           + _live_row(2, 3, "c-c", "C C", "t-2023", "T", "", "", "0:09"))
    got = {r["rider_slug"]: r["time_seconds"] for r in _parse_results_table(_live_table(gap))}
    assert got == {"a-a": None, "b-b": 0, "c-c": 4}
    # legacy markup, same rule
    legacy = _table(_row("<s>1</s>", "annulled", "4:53:12") + _row(1, "winner", "4:53:20")
                    + _row(2, "second", "0:20") + _row(3, "third", ",,"))
    got = {r["rider_slug"]: r["time_seconds"] for r in _parse_results_table(legacy)}
    assert got == {"annulled": None, "winner": 0, "second": 12, "third": 12}


def test_struck_rank_in_legacy_and_basic_tables():
    legacy = _table(_row(1, "winner", "4:53:12") + _row("<s>2</s>", "annulled", "0:12")
                    + _row(2, "second", ",,"))
    res = {r["rider_slug"]: r for r in _parse_results_table(legacy)}
    assert (res["annulled"]["position"], res["annulled"]["status"]) == (None, "dsq")
    assert (res["second"]["position"], res["second"]["time_seconds"]) == (2, 12)
    basic = soup('<table class="basic"><tr><td><s>1</s></td><td><a href="rider/a-b">A B</a></td></tr>'
                 '<tr><td>1</td><td><a href="rider/c-d">C D</a></td></tr></table>').find("table")
    got = [(r["rider_slug"], r["position"], r["status"]) for r in _parse_results_table(basic)]
    assert got == [("a-b", None, "dsq"), ("c-d", 1, "finished")]


# ── team time trial non-finishers ───────────────────────────────────────────

TTT_DNS_PAGE = TTT_PAGE.replace(
    '<tr><td><a href="rider/filippo-ganna">GANNA Filippo</a></td><td>40</td><td>18.75</td></tr>',
    '<tr><td><a href="rider/filippo-ganna">GANNA Filippo</a></td><td>40</td><td>18.75</td></tr>'
    '<tr><td><a href="rider/mauro-schmid"><font class="red">DNS</font> SCHMID Mauro</a></td>'
    '<td>0</td><td>0</td></tr>'
    '<tr><td><a href="rider/mark-padun"><font class="red">OTL</font> PADUN Mark &nbsp;'
    '<font class="blue">+8:53</font></a></td><td>0</td><td>0</td></tr>'
    '<tr><td><a href="rider/josh-tarling">TARLING Josh <font class="blue">+0:20</font></a></td>'
    '<td>40</td><td>18.75</td></tr>')


def test_ttt_status_marker_makes_a_non_finisher():
    # Paris-Nice 2023 stage 3 (Schmid DNS) and Okolo Slovenska 2024 stage 1
    # (Padun OTL +8:53): the old parser ranked both as finishers at the team
    # time (link.find('font') took the red marker as the gap font).
    res = _parse_ttt_results(select_stage_result(soup(TTT_DNS_PAGE)))
    by = {r["rider_slug"]: r for r in res}
    assert (by["mauro-schmid"]["position"], by["mauro-schmid"]["status"],
            by["mauro-schmid"]["time_seconds"]) == (None, "dns", None)
    assert (by["mark-padun"]["position"], by["mark-padun"]["status"],
            by["mark-padun"]["time_seconds"]) == (None, "otl", None)
    assert by["mauro-schmid"]["rider_name"] == "SCHMID Mauro"
    assert by["mark-padun"]["rider_name"] == "PADUN Mark"
    # finishers keep consecutive ranks; the blue gap is still read
    ranked = [(r["position"], r["rider_slug"], r["time_seconds"]) for r in res if r["status"] == "finished"]
    assert ranked == [(1, "jonas-vingegaard", 0), (2, "filippo-ganna", 7),
                      (3, "josh-tarling", 27), (4, "sepp-kuss", 117)]
    assert [r["rider_slug"] for r in res[-2:]] == ["mauro-schmid", "mark-padun"]
