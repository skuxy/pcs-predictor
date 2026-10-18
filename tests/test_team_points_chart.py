"""Tests for the team-UCI-points chart page (analysis/team_points_chart.py).

The Python tests check the document structure, the embedded JSON (parseable,
lossless, unable to break out of its <script> block) and the synthetic demo
payload.  The browser tests load rendered pages in headless Chromium and fail
on any console error; they are skipped when Playwright or the Chromium build
is not available (set PCS_CHROMIUM to a chrome binary to point them at one).
"""
import json
import math
import os
import re
from datetime import date

import pytest

from analysis.team_points_chart import PAGE_TITLE, _demo_payload, _json_for_script, render_html

DATA_RE = re.compile(r'<script type="application/json" id="tp-data">(.*?)</script>', re.S)


def _embedded(page: str):
    blocks = DATA_RE.findall(page)
    assert len(blocks) == 1
    return json.loads(blocks[0])


def _team(key="team-a-2026", name="Team A - Long Sponsor Name", short="Team A", rank=1,
          gains=(100.0, 0.0, 250.0), jumps=None):
    cum, run = [], 0.0
    for g in gains:
        run += g
        cum.append(run)
    return {
        "key": key, "name": name, "short": short, "class": "WT",
        "official_rank": rank, "official_total": run, "breakdown_total": run,
        "final_total": run, "final_rank": rank,
        "cumulative": cum, "gains": list(gains), "rank_by_week": [rank] * len(gains),
        "cumulative_best20": cum, "final_total_best20": run,
        "jumps": jumps or [], "top_riders": [{"rider": "Some Rider", "points": run}],
    }


def _payload(teams, weeks=("2026-03-02", "2026-03-09", "2026-03-16"), riders=None, **meta):
    m = {"title": "2026 UCI points", "season": 2026, "gender": "men", "generated_at": "2026-10-18 21:00",
         "data_through": "2026-03-20", "week_convention": "ISO weeks", "source": "test",
         "metric_default": "all_riders",
         "coverage": {"teams": len(teams), "riders": 3, "results_with_points": 9, "penalties": 1,
                      "reconciliation": {"max_abs_team_diff": 0, "teams_exact": len(teams), "note": "ok"},
                      "notes": ["note one"]}}
    m.update(meta)
    return {"meta": m, "weeks": list(weeks), "teams": teams, "riders": riders or [],
            "official": {"ranking": "rankings/me/ranking-2026-2028", "date": "2026-10-18",
                         "label": "Official UCI team ranking 2026 (PCS)"}}


def _rider(gains=(0.0, 800.0, 0.0), weeks=("2026-03-02", "2026-03-09", "2026-03-16")):
    cum, run = [], 0.0
    for g in gains:
        run += g
        cum.append(run)
    return {"key": "rider:tadej-pogacar", "kind": "rider", "name": "Tadej Pogačar", "short": "Pogačar",
            "team_slug": "uae-team-emirates-xrg-2026", "team_short": "UAE", "official_total": run,
            "final_total": run, "would_rank": 2, "cumulative": cum, "gains": list(gains),
            "rank_by_week": [2] * len(gains), "cumulative_best20": cum,
            "jumps": [{"week": weeks[1], "week_index": 1, "gain": 800.0, "share_of_season": 1.0,
                       "label": "Wins Milano-Sanremo",
                       "contributors": [{"rider": "Tadej Pogačar", "race": "Milano-Sanremo", "detail": "1st",
                                         "points": 800.0}]}],
            "top_riders": None}


# ---------------------------------------------------------------------------
# document + embedded JSON
# ---------------------------------------------------------------------------

class TestRenderHtml:
    def test_full_document(self):
        page = render_html(_demo_payload())
        assert page.lower().startswith("<!doctype html>")
        assert page.rstrip().endswith("</html>")
        assert f"<title>{PAGE_TITLE} 2026</title>" in page
        assert 'name="viewport"' in page and "viewport-fit=cover" in page
        for anchor in ('id="tp-ov"', 'id="tp-grid"', 'id="tp-table"', 'id="tp-pick"',
                       'data-metric-btn="best20"', 'data-scale-btn="own"'):
            assert anchor in page

    def test_title_is_two_to_four_words(self):
        page = render_html(_demo_payload())
        title = re.search(r"<title>(.*?)</title>", page).group(1)
        assert 2 <= len(title.split()) <= 4

    def test_theme_tokens_light_and_dark(self):
        page = render_html(_demo_payload())
        assert ':root:not([data-theme="light"])' in page
        assert ':root[data-theme="dark"]' in page
        assert "@media (prefers-color-scheme: dark)" in page
        assert re.search(r"body \{[^}]*background: var\(--plane\)", page)

    def test_no_external_requests(self):
        page = render_html(_demo_payload())
        assert not re.search(r'<script[^>]+src=', page)
        assert '<link rel="stylesheet"' not in page
        assert "http://" not in page.replace("http://www.w3.org/2000/svg", "")
        assert "https://" not in page

    def test_demo_json_round_trips(self):
        payload = _demo_payload()
        assert _embedded(render_html(payload)) == json.loads(json.dumps(payload))

    def test_fragment_mode(self):
        frag = render_html(_demo_payload(), standalone=False)
        assert "<html" not in frag and "<body" not in frag and "<!doctype" not in frag.lower()
        assert frag.lstrip().startswith("<title>")
        assert _embedded(frag)["meta"]["season"] == 2026

    def test_zero_jumps(self):
        payload = _payload([_team(jumps=[]), _team(key="b", short="B", name="B", rank=2, jumps=[])])
        page = render_html(payload)
        assert all(t["jumps"] == [] for t in _embedded(page)["teams"])

    def test_single_team(self):
        page = render_html(_payload([_team()]))
        data = _embedded(page)
        assert len(data["teams"]) == 1 and data["riders"] == []
        assert ">Teams</dt><dd>1<" in page

    def test_negative_gains_preserved(self):
        team = _team(gains=(-25.0, 100.0, -10.0))
        data = _embedded(render_html(_payload([team])))
        assert data["teams"][0]["gains"] == [-25.0, 100.0, -10.0]
        assert data["teams"][0]["cumulative"] == [-25.0, 75.0, 65.0]

    def test_script_breakout_in_labels(self):
        evil = "</script><script>window.__pwned=1</script><!-- & </SCRIPT >"
        team = _team(name=evil, short="X</script>", jumps=[{
            "week": "2026-03-16", "week_index": 2, "gain": 250.0, "share_of_season": 0.7, "label": evil,
            "contributors": [{"rider": evil, "race": "R", "detail": "1st", "points": 250.0}]}])
        page = render_html(_payload([team], title=evil, source=evil))
        # one closing tag for the JSON block and one for the code: nothing from the payload
        assert page.lower().count("</script") == 2
        block = DATA_RE.search(page).group(1)
        assert "<" not in block and ">" not in block and "&" not in block
        assert "\\u003c/script\\u003e\\u003cscript\\u003ewindow.__pwned=1" in block
        assert "</script><script>window.__pwned" not in page
        data = _embedded(page)
        assert data["teams"][0]["jumps"][0]["label"] == evil
        assert data["meta"]["title"] == evil
        # the static header text is HTML-escaped
        assert "&lt;/script&gt;&lt;script&gt;window.__pwned=1" in page

    def test_json_escaping_helper(self):
        text = _json_for_script({"a": "<b>&</b>", "line": "x y"})
        assert "<" not in text and ">" not in text and "&" not in text and " " not in text
        assert json.loads(text) == {"a": "<b>&</b>", "line": "x y"}

    def test_non_finite_numbers_become_null(self):
        team = _team()
        team["official_total"] = float("nan")
        team["cumulative"][1] = float("inf")
        data = _embedded(render_html(_payload([team])))
        assert data["teams"][0]["official_total"] is None
        assert data["teams"][0]["cumulative"][1] is None

    def test_empty_and_minimal_payloads(self):
        for payload in ({}, {"meta": {}}, _payload([], weeks=())):
            page = render_html(payload)
            assert page.lower().startswith("<!doctype html>")
            _embedded(page)

    def test_rejects_non_dict(self):
        with pytest.raises(TypeError):
            render_html([1, 2, 3])

    def test_rider_sentence(self):
        page = render_html(_payload([_team(), _team(key="b", short="B", name="B", rank=2)], riders=[_rider()]))
        # the basis of the rank is named (all-rider points by default)
        assert "Tadej Pogačar alone has 800 points. As a team he would rank 2nd on all-rider points." in page
        page = render_html(_payload([_team(), _team(key="b", short="B", name="B", rank=2)],
                                    riders=[dict(_rider(), would_rank_official=1)]))
        assert "would rank 2nd on all-rider points (1st against the official listing)." in page

    def test_best20_default_metric(self):
        page = render_html(_payload([_team()], metric_default="best20_riders"))
        assert 'data-metric-btn="best20" aria-pressed="true"' in page


# ---------------------------------------------------------------------------
# synthetic demo payload
# ---------------------------------------------------------------------------

class TestDemoPayload:
    @pytest.fixture(scope="class")
    def demo(self):
        return _demo_payload()

    def test_deterministic(self, demo):
        assert _demo_payload() == demo

    def test_shape(self, demo):
        assert set(demo) == {"meta", "weeks", "teams", "riders", "official"}
        assert len(demo["teams"]) == 30 and len(demo["weeks"]) == 40
        days = [date.fromisoformat(w) for w in demo["weeks"]]
        assert all(d.weekday() == 0 for d in days)
        assert all((b - a).days == 7 for a, b in zip(days, days[1:]))
        assert [t["official_rank"] for t in demo["teams"]] == list(range(1, 31))
        assert max(len(t["name"]) for t in demo["teams"]) >= 25
        assert {t["class"] for t in demo["teams"]} == {"WT", "PRT"}

    def test_series_consistent(self, demo):
        n = len(demo["weeks"])
        for t in demo["teams"]:
            for k in ("cumulative", "gains", "rank_by_week", "cumulative_best20"):
                assert len(t[k]) == n, (t["short"], k)
            run = 0.0
            for g, c in zip(t["gains"], t["cumulative"]):
                run += g
                assert math.isclose(run, c, abs_tol=0.05)
            assert math.isclose(t["final_total"], t["cumulative"][-1], abs_tol=0.01)

    def test_jumps(self, demo):
        weeks = demo["weeks"]
        assert any(not t["jumps"] for t in demo["teams"])  # a team with no standout week
        for t in demo["teams"]:
            for j in t["jumps"]:
                assert weeks[j["week_index"]] == j["week"]
                assert j["label"] and j["contributors"]
                assert math.isclose(j["gain"], t["gains"][j["week_index"]], abs_tol=0.01)

    def test_penalty_week(self, demo):
        assert any(min(t["gains"]) < 0 for t in demo["teams"])

    def test_pogacar_entry(self, demo):
        (r,) = demo["riders"]
        assert r["kind"] == "rider" and r["key"] == "rider:tadej-pogacar" and r["short"] == "Pogačar"
        finals = [t["final_total"] for t in demo["teams"]]
        assert r["would_rank"] == 1 + sum(v > r["final_total"] for v in finals)
        assert r["cumulative_best20"] == r["cumulative"] and r["top_riders"] is None
        assert any("Ronde" in j["label"] for j in r["jumps"])


# ---------------------------------------------------------------------------
# headless browser smoke tests
# ---------------------------------------------------------------------------

_CHROME = os.environ.get("PCS_CHROMIUM", "/opt/pw-browsers/chromium-1194/chrome-linux/chrome")


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    if not os.path.exists(_CHROME):
        pytest.skip(f"Chromium not found at {_CHROME} (set PCS_CHROMIUM)")
    with sync_api.sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=_CHROME, args=["--no-sandbox"])
        yield b
        b.close()


def _open(browser, tmp_path, payload, width=1280, scheme="light"):
    path = tmp_path / f"page_{width}_{scheme}.html"
    path.write_text(render_html(payload), encoding="utf-8")
    ctx = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=scheme)
    page = ctx.new_page()
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(path.as_uri())
    page.wait_for_selector("#tp-table tbody", state="attached")
    return ctx, page, errors


class TestInBrowser:
    def test_demo_renders_and_interacts(self, browser, tmp_path):
        ctx, page, errors = _open(browser, tmp_path, _demo_payload())
        try:
            assert page.locator("#tp-ov svg").count() == 1
            assert page.locator(".panel").count() == 31          # 30 teams + Pogačar
            assert page.locator("#tp-table tbody tr").count() == 31
            assert page.locator(".panel-rider .badge-rider").inner_text() == "Rider, not a team"
            rider_row = page.locator("#tp-table tbody tr.row-rider")
            # rows follow the official listing, so the rider is placed (and his ≈ rank
            # given) against the listing's totals; the title names both bases
            would = _demo_payload()["riders"][0]["would_rank_official"]
            assert rider_row.locator("td").first.inner_text() == f"≈{would}"
            assert "against the official listing" in rider_row.locator("td").first.get_attribute("title")
            assert page.locator("#tp-table tbody tr").nth(would - 1).get_attribute("class").startswith("row-rider")
            page.locator("#tp-ov svg").scroll_into_view_if_needed()
            box = page.locator("#tp-ov svg").bounding_box()
            page.mouse.move(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5)
            assert page.evaluate("!document.getElementById('tp-tip').hidden")
            assert "Week of" in page.locator("#tp-tip").inner_text()
            page.click("#tp-m-b20")
            page.click("#tp-s-own")
            page.select_option("#tp-pick", "xds-astana-team-2026")
            assert page.locator("#tp-chips .chip").count() == 4    # 3 teams + the rider chip
            page.focus("#tp-ov svg")
            for key in ("ArrowLeft", "ArrowDown", "Enter", "Escape"):
                page.keyboard.press(key)
            assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
            assert errors == []
        finally:
            ctx.close()

    def test_ux_review_fixes(self, browser, tmp_path):
        payload = _demo_payload()
        weeks = payload["weeks"]
        payload["meta"]["axis_start"] = weeks[3]
        payload["weeks_meta"] = [{"week": w, "top_race": f"Big Race {i}", "top_race_points": 1000 + i,
                                  "races": [{"race": f"Big Race {i}", "points": 1000 + i}]}
                                 for i, w in enumerate(weeks)]
        ctx, page, errors = _open(browser, tmp_path, payload, width=1440)
        try:
            # the season chart starts in the first screen (provenance moved below the table)
            assert page.evaluate("document.getElementById('tp-ov').getBoundingClientRect().top") < 600
            assert page.locator("#sources .prov").count() == 1
            # axis_start: lines are drawn from that week on
            d = page.locator("#tp-ov path.ln").first.get_attribute("d")
            assert d.count("L") + 1 == len(weeks) - 3
            # tooltip footer: the week's biggest race from weeks_meta, not from jump contributors
            box = page.locator("#tp-ov svg").bounding_box()
            page.mouse.move(box["x"] + box["width"] * 0.4, box["y"] + box["height"] * 0.5)
            assert "Biggest race of the week: Big Race" in page.locator("#tp-tip").inner_text()
            # a jump-list tooltip sits beside its panel, never over the panel's mini chart
            li = page.locator(".panel .jump-li").first
            li.scroll_into_view_if_needed()
            li.hover()
            tip = page.locator("#tp-tip").bounding_box()
            mini = page.locator(".panel svg.mini").first.bounding_box()
            overlap = not (tip["x"] >= mini["x"] + mini["width"] or tip["x"] + tip["width"] <= mini["x"]
                           or tip["y"] >= mini["y"] + mini["height"] or tip["y"] + tip["height"] <= mini["y"])
            assert not overlap
            # best-20 mode: the jump shows its best-20 gain with the all-rider gain beside it
            page.click("#tp-m-b20")
            li = page.locator(".panel .jump-li").first
            assert "(all riders +" in li.inner_text()
            # keyboard: after picking a line with ArrowDown, Tab reaches the first jump marker
            page.click("#tp-m-all")
            page.focus("#tp-ov svg")
            page.keyboard.press("ArrowDown")
            page.keyboard.press("Tab")
            assert (page.evaluate("document.activeElement.getAttribute('data-fid') || ''")).startswith("jm:")
            page.keyboard.press("Tab")
            assert page.evaluate("document.activeElement.tagName") != "BODY"
            assert errors == []
        finally:
            ctx.close()

    def test_phone_table_fits(self, browser, tmp_path):
        ctx, page, errors = _open(browser, tmp_path, _demo_payload(), width=390)
        try:
            wrap = page.evaluate("(() => { const w = document.querySelector('.table-wrap');"
                                 " return [w.clientWidth, w.scrollWidth]; })()")
            assert wrap[1] <= wrap[0] + 1          # the metric columns fit without sideways scrolling
            assert page.locator("th.col-jump").is_hidden() and page.locator("th.col-cls").is_hidden()
            assert errors == []
        finally:
            ctx.close()

    def _phone(self, browser, tmp_path, width=390, height=844):
        path = tmp_path / f"phone_{width}x{height}.html"
        path.write_text(render_html(_demo_payload()), encoding="utf-8")
        ctx = browser.new_context(viewport={"width": width, "height": height}, has_touch=True, is_mobile=True)
        page = ctx.new_page()
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(path.as_uri())
        page.wait_for_selector("#tp-table tbody", state="attached")
        return ctx, page, errors

    def test_phone_season_chart_in_first_screen(self, browser, tmp_path):
        ctx, page, errors = self._phone(browser, tmp_path)
        try:
            top = page.evaluate("document.querySelector('#tp-ov svg').getBoundingClientRect().top")
            assert top < 844 - 120               # at least the top of the chart without scrolling
            # the shortened masthead: minor facts and the long dek are hidden, the
            # rider sentence keeps its own numbers but drops the team comparison
            assert page.locator(".facts .fact-minor").first.is_hidden()
            assert page.locator(".dek-full").is_hidden() and page.locator(".dek-phone").is_visible()
            assert page.locator("#tp-dek-rider .dek-more").is_hidden()
            assert "alone has" in page.locator("#tp-dek-rider").inner_text()
            assert errors == []
        finally:
            ctx.close()

    @pytest.mark.parametrize("panel,item,frac", [(0, 0, 0.55), (0, 0, 0.8), (0, 0, 0.95), (6, 0, 0.8),
                                                 (1, -1, 0.95), (1, -1, 0.5)])
    def test_phone_jump_tap_never_covers_mini_chart_or_item(self, browser, tmp_path, panel, item, frac):
        """A jump-list tap low on a phone screen used to clamp the tooltip up over
        the panel's mini chart (and its cursor) and over the tapped item."""
        ctx, page, errors = self._phone(browser, tmp_path)
        try:
            pan = page.locator(".panel").nth(panel)
            li = pan.locator(".jump-li").nth(item if item >= 0 else pan.locator(".jump-li").count() + item)
            y = li.evaluate("el => el.getBoundingClientRect().top + scrollY")
            page.evaluate("(y) => window.scrollTo(0, y)", y - 844 * frac)
            box = li.bounding_box()
            page.touchscreen.tap(box["x"] + 40, box["y"] + 6)
            page.wait_for_timeout(50)
            assert page.evaluate("!document.getElementById('tp-tip').hidden")
            tip = page.locator("#tp-tip").bounding_box()
            mini = pan.locator("svg.mini").bounding_box()
            itm = li.bounding_box()

            def overlaps(a, b):
                return not (a["x"] >= b["x"] + b["width"] or a["x"] + a["width"] <= b["x"]
                            or a["y"] >= b["y"] + b["height"] or a["y"] + a["height"] <= b["y"])
            assert not overlaps(tip, mini)
            assert not overlaps(tip, itm)
            assert tip["y"] >= 0 and tip["y"] + tip["height"] <= 844     # fully on screen
            # the mini chart shows the jump week's cursor
            assert pan.locator("svg.mini .crosshair").count() == 1
            assert errors == []
        finally:
            ctx.close()

    def test_phone_width_dark_no_overflow(self, browser, tmp_path):
        ctx, page, errors = _open(browser, tmp_path, _demo_payload(), width=390, scheme="dark")
        try:
            assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
            bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
            assert bg not in ("rgba(0, 0, 0, 0)", "transparent")
            assert errors == []
        finally:
            ctx.close()

    @pytest.mark.parametrize("name", ["single_team", "zero_jumps", "negative", "no_weeks", "empty", "breakout"])
    def test_edge_payloads_have_no_console_errors(self, browser, tmp_path, name):
        evil = "</script><script>window.__pwned=1</script><!--"
        payloads = {
            "single_team": _payload([_team()]),
            "zero_jumps": _payload([_team(), _team(key="b", short="B", name="B", rank=2)], riders=[_rider()]),
            "negative": _payload([_team(gains=(-25.0, 100.0, -10.0), jumps=[{
                "week": "2026-03-09", "week_index": 1, "gain": 100.0, "share_of_season": 1.5,
                "label": "Win", "contributors": []}])], riders=[_rider(gains=(-25.0, 0.0, 0.0))]),
            "no_weeks": _payload([dict(_team(), cumulative=[], gains=[], rank_by_week=[], cumulative_best20=[])],
                                 weeks=()),
            "empty": {},
            "breakout": _payload([_team(short=evil, jumps=[{
                "week": "2026-03-16", "week_index": 2, "gain": 250.0, "share_of_season": 0.7, "label": evil,
                "contributors": [{"rider": evil, "race": evil, "detail": evil, "points": 250.0}]}])], title=evil),
        }
        ctx, page, errors = _open(browser, tmp_path, payloads[name])
        try:
            assert errors == []
            assert page.evaluate("window.__pwned === undefined")
            if name == "breakout":
                assert evil in page.locator("#tp-table").inner_text()
            if name in ("single_team", "negative", "breakout"):
                page.locator("#tp-ov svg").scroll_into_view_if_needed()
                box = page.locator("#tp-ov svg").bounding_box()
                page.mouse.move(box["x"] + box["width"] * 0.4, box["y"] + box["height"] * 0.5)
                assert page.evaluate("!document.getElementById('tp-tip').hidden")
                assert errors == []
            if name == "negative":
                assert page.locator("svg.mini .bar-neg").count() >= 1
        finally:
            ctx.close()
