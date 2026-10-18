"""Scrape the official UCI team ranking and the dated rider results behind it.

Pages used (all fetched through ``scraper.utils.fetch``; markup verified on the
live site in October 2026):

* ``rankings/me/ranking-<cycle>`` — official UCI team ranking for the licence
  cycle (``ranking-2026-2028``).  ``table.basic`` with header
  ``# | Prev. | Diff. | Team | Class | Points``; the team cell links
  ``team/<slug-with-year>``; the ranking date is the selected option of
  ``select[name=date]`` (the page only ever offers the current date).
* ``team/<slug>/results/ranking-<cycle>`` — the team's breakdown.  Two
  ``table.basic``: "Points per year" (``Season | Points``) and "Points per rider
  per year" (``Season | Rider | Nth best rider for team | Points counted |
  Not counted | Sanction points``), each closed by a ``tr.sum`` row.  The page
  says "Points of the best 20 riders per season on the team are counted" and
  "The sanctions are already subtracted from the points total", i.e.
  ``counted + not_counted = points earned - sanction points``.  Numbers may be
  fractional (``9331.75`` — team time-trial shares); ``-`` means none.
* ``rider/<slug>/results`` — every result of the rider's career, newest first,
  100 rows per page.  ``table.basic`` header ``# | Date | Result | Race | Class |
  KMs | PCS points | UCI points | Vert. mtr``; the race cell reads
  ``Tour de France | Stage 6`` / ``... | General classification`` /
  ``Milano-Sanremo`` and links ``race/tour-de-france-2026-stage-6``.  The
  pretty URL ignores filter query strings; the season-filtered list is the
  form endpoint ``rider.php?id=<numeric id>&p=results&xseason=<season>&offset=N``
  (the numeric id is the form's hidden ``input[name=id]``).
* ``rider.php?id=<id>&p=results&s=uci-world-ranking&date=<date>`` — every
  UCI point item of the 52 weeks up to ``date``, dated: results, the GC
  leader-jersey points after each stage (which rider/<slug>/results omits),
  previous-October items UCI counts in the season, and dated sanctions.  Its
  total equals the team breakdown's counted + not counted (see
  ``parse_rider_uci_ranking`` / ``merge_season_rows``).
* ``rider/<slug>`` — the profile.  ``table.rdrResults`` lists the current
  season grouped by race (``tr.main`` race header, ``tr.stage`` stage and
  classification rows) and is the only place that shows the UCI points the GC
  leader earns after each stage (``<span class="gc_pnt">+25</span>`` in the UCI
  cell).  On the race's final day the same span repeats the final GC points
  (``+1300`` next to the GC row's ``1300``) and is not extra.  Sanctions show as
  ``800 <font class="red fs10">-25</font>``.  ``ul.rdr-teams2`` holds the team
  history (``li.main`` > ``div.season`` + ``div.name`` > ``a[href^=team/]``).
* ``rider/<slug>/info/fines-penalties`` — ``Date | Race | Penalty points |
  Fine | Other penalties | Article``.
* ``team/<slug>/results/latest-uci-points`` — the team's UCI points per day for
  the last ~30 days (``Date | UCI Points | (Indirect)``).
"""
from __future__ import annotations

import logging
import re
from datetime import date as _date
from typing import Optional

from scraper.utils import fetch, soup, pcs_url

log = logging.getLogger(__name__)

RESULTS_PAGE_SIZE = 100
MAX_RESULT_PAGES = 10

_STATUS_WORDS = {"dnf", "dns", "otl", "dsq", "df", "nr", "nq", "dnq", "hd"}

_KIND_BY_LABEL = [
    (re.compile(r"^(stage\b|prologue\b)", re.I), "stage"),
    (re.compile(r"^general classification", re.I), "gc"),
    (re.compile(r"^points classification", re.I), "points"),
    (re.compile(r"^(mountains?|kom) classification", re.I), "kom"),
    (re.compile(r"^youth classification", re.I), "youth"),
]


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def licence_cycle(season: int) -> str:
    """UCI licence cycle containing ``season``: 2026 -> '2026-2028', 2025 -> '2023-2025'."""
    start = 2023 + ((season - 2023) // 3) * 3
    return f"{start}-{start + 2}"


def team_ranking_path(season: int) -> str:
    return f"rankings/me/ranking-{licence_cycle(season)}"


def breakdown_totals_ranking(season: int) -> str:
    """``uci_team_totals.ranking`` key for the team breakdown pages' own season totals."""
    return f"team-breakdown/ranking-{licence_cycle(season)}"


def parse_number(text: Optional[str]) -> Optional[float]:
    """'9331.75' -> 9331.75, '30607' -> 30607.0, '-' / '' -> None."""
    if text is None:
        return None
    t = text.strip().replace("\xa0", " ")
    m = re.search(r"-?\d+(?:\.\d+)?", t.replace(",", ""))
    if not m or t in {"-", ""}:
        return None
    return float(m.group(0))


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _header_map(table) -> dict[str, int]:
    """Map normalised header text -> column index for a ``table.basic``."""
    head = table.select_one("thead tr") or table.select_one("tr")
    if head is None:
        return {}
    return {_norm(th.get_text(" ")): i for i, th in enumerate(head.find_all(["th", "td"]))}


def _body_rows(table):
    """Data rows of a ``table.basic`` (no header, no ``tr.sum`` total row)."""
    body = table.select_one("tbody") or table
    return [tr for tr in body.find_all("tr", recursive=False)
            if "sum" not in (tr.get("class") or []) and tr.find("td")]


def _cell(cells, hmap: dict[str, int], *names: str):
    for n in names:
        i = hmap.get(n)
        if i is not None and i < len(cells):
            return cells[i]
    return None


def _strip_prefix(href: str, prefix: str) -> str:
    href = (href or "").split("?")[0].strip("/")
    return href[len(prefix):] if href.startswith(prefix) else href


def display_name(raw: str) -> str:
    """'POGAČAR Tadej' -> 'Tadej Pogačar' (PCS lists surnames in capitals).

    Approximate (particles come out capitalised: 'Mathieu Van Der Poel'); the
    rider page's <h1> is authoritative and replaces this once fetched.
    """
    tokens = (raw or "").split()

    def is_surname(tok: str) -> bool:
        letters = [c for c in tok if c.isalpha()]
        return bool(letters) and all(not c.islower() or c == "ß" for c in letters)

    surname = [t for t in tokens if is_surname(t)]
    given = [t for t in tokens if not is_surname(t)]
    if not surname or not given:
        return " ".join(tokens)

    def cap(tok: str) -> str:
        return "-".join(p[:1].upper() + p[1:].lower() for p in tok.split("-"))

    return " ".join(given + [cap(t) for t in surname])


def race_slug_from_href(href: str, year: Optional[int] = None) -> Optional[str]:
    """PCS race href -> 'race/<slug>/<year>' (the ``races.pcs_slug`` format).

    Handles the current hyphenated hrefs ('race/tour-de-france-2026-stage-6',
    'race/milano-sanremo-2026-result', 'race/tour-de-france-2026-gc') and the
    older path form ('race/tour-de-france/2026/stage-6').  ``year`` (the result
    date's year) disambiguates slugs that contain a year-like number.
    """
    path = (href or "").split("?")[0].strip("/")
    if not path.startswith("race/"):
        return None
    rest = path[len("race/"):]
    m = re.match(r"^([^/]+)/((?:19|20)\d{2})(?:/.*)?$", rest)
    if m:
        return f"race/{m.group(1)}/{m.group(2)}"
    if year is not None:
        m = re.match(rf"^(.+?)-{year}(?:-.*)?$", rest)
        if m:
            return f"race/{m.group(1)}/{year}"
    m = re.match(r"^(.+?)-((?:19|20)\d{2})(?:-.*)?$", rest)
    if m:
        return f"race/{m.group(1)}/{m.group(2)}"
    return None


def result_kind(label: str) -> str:
    """Race-cell suffix -> result kind ('' -> 'oneday', 'Stage 6' -> 'stage', ...)."""
    label = (label or "").strip()
    if not label:
        return "oneday"
    for rx, kind in _KIND_BY_LABEL:
        if rx.search(label):
            return kind
    return "other"


def parse_position(text: str) -> tuple[Optional[int], str]:
    """'1' -> (1, 'finished'); 'DNF' -> (None, 'dnf'); '' -> (None, 'unknown')."""
    t = (text or "").strip()
    if re.fullmatch(r"\d+", t):
        return int(t), "finished"
    low = t.lower()
    if low in _STATUS_WORDS:
        return None, low
    return None, low or "unknown"


def split_points_cell(cell) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Split a UCI-points cell into (points earned, sanction, GC-leader bonus).

    '180'                                   -> (180, None, None)
    '800 <font class="red fs10">-25</font>' -> (800, 25, None)   (old parser read 80025)
    '180 <span class="gc_pnt">+20</span>'   -> (180, None, 20)
    ' <span class="gc_pnt">+25</span>'      -> (None, None, 25)
    '-'                                     -> (None, None, None)
    """
    if cell is None:
        return None, None, None
    main_parts, penalty, bonus = [], None, None
    for s in cell.find_all(string=True):
        classes = set()
        for p in s.parents:
            if p is cell:
                break
            classes.update(p.get("class") or [])
        txt = str(s).strip()
        if not txt:
            continue
        if "gc_pnt" in classes:
            v = parse_number(txt.lstrip("+"))
            bonus = (bonus or 0) + v if v is not None else bonus
        elif "red" in classes:
            v = parse_number(txt)
            penalty = (penalty or 0) + abs(v) if v is not None else penalty
        else:
            main_parts.append(txt)
    return parse_number(" ".join(main_parts)), penalty, bonus


# ---------------------------------------------------------------------------
# official ranking listing
# ---------------------------------------------------------------------------

def parse_team_ranking_listing(html: str) -> list[dict]:
    """Parse rankings/me/ranking-<cycle> into team rows.

    Each row: rank, team_slug ('uae-team-emirates-xrg-2026'), team_name,
    team_class ('WT'/'PRT'), points, ranking_date (ISO, the selected
    ``select[name=date]`` option).
    """
    s = soup(html)
    ranking_date = None
    sel = s.select_one("select[name=date]")
    if sel is not None:
        opt = sel.select_one("option[selected]") or sel.select_one("option")
        if opt is not None:
            ranking_date = (opt.get("value") or opt.get_text()).strip() or None
    rows: list[dict] = []
    for table in s.select("table.basic"):
        hmap = _header_map(table)
        if "team" not in hmap or "points" not in hmap:
            continue
        for tr in _body_rows(table):
            cells = tr.find_all("td")
            a = tr.select_one('a[href^="team/"]')
            if a is None:
                continue
            rank_cell = _cell(cells, hmap, "#")
            rank = parse_number(rank_cell.get_text()) if rank_cell is not None else None
            cls = _cell(cells, hmap, "class")
            pts = _cell(cells, hmap, "points")
            rows.append({
                "rank": int(rank) if rank is not None else None,
                "team_slug": _strip_prefix(a["href"], "team/"),
                "team_name": a.get_text(" ", strip=True),
                "team_class": cls.get_text(strip=True) if cls is not None else None,
                "points": parse_number(pts.get_text()) if pts is not None else None,
                "ranking_date": ranking_date,
            })
        break
    return rows


def fetch_team_ranking_listing(season: int, use_cache: bool = True) -> tuple[str, list[dict]]:
    path = team_ranking_path(season)
    html = fetch(pcs_url(path), use_cache=use_cache)
    if not html:
        return path, []
    return path, parse_team_ranking_listing(html)


# ---------------------------------------------------------------------------
# team breakdown
# ---------------------------------------------------------------------------

def parse_team_breakdown_meta(html: str) -> dict:
    """Team name/class from the <h1> plus the 'Points per year' totals.

    Returns {team_name, team_class, season_totals: {season: points},
             sanction_total} (sanction_total from the rider table's sum row).
    """
    s = soup(html)
    meta: dict = {"team_name": None, "team_class": None, "season_totals": {}, "sanction_total": None}
    h1 = s.select_one("h1")
    if h1 is not None:
        span = h1.find("span")
        if span is not None:
            m = re.search(r"\(([^)]+)\)", span.get_text())
            meta["team_class"] = m.group(1).strip() if m else None
            span.extract()
        meta["team_name"] = h1.get_text(" ", strip=True) or None
    for table in s.select("table.basic"):
        hmap = _header_map(table)
        if set(hmap) == {"season", "points"}:
            for tr in _body_rows(table):
                cells = tr.find_all("td")
                season = parse_number(_cell(cells, hmap, "season").get_text())
                if season is not None:
                    meta["season_totals"][int(season)] = parse_number(
                        _cell(cells, hmap, "points").get_text())
        elif "sanction points" in hmap:
            total = table.select_one("tr.sum")
            if total is not None:
                cells = total.find_all("td")
                c = _cell(cells, hmap, "sanction points")
                meta["sanction_total"] = parse_number(c.get_text()) if c is not None else None
    return meta


def parse_team_breakdown(html: str, season: Optional[int] = None) -> list[dict]:
    """Parse team/<slug>/results/ranking-<cycle> rider rows.

    Each row: season, rider_slug, rider_name ('Tadej Pogačar'), rider_name_raw
    ('POGAČAR Tadej'), nth_best, points_counted, points_not_counted,
    sanction_points ('-' -> None), team_name, team_class.  ``season`` filters.
    """
    meta = parse_team_breakdown_meta(html)
    s = soup(html)
    rows: list[dict] = []
    for table in s.select("table.basic"):
        hmap = _header_map(table)
        if "rider" not in hmap or "points counted" not in hmap:
            continue
        for tr in _body_rows(table):
            cells = tr.find_all("td")
            a = tr.select_one('a[href^="rider/"]')
            if a is None:
                continue
            yr = parse_number(_cell(cells, hmap, "season").get_text())
            if yr is None or (season is not None and int(yr) != season):
                continue
            nth = _cell(cells, hmap, "nth best rider for team")
            nth_v = parse_number(nth.get_text()) if nth is not None else None
            raw = a.get_text(" ", strip=True)
            rows.append({
                "season": int(yr),
                "rider_slug": _strip_prefix(a["href"], "rider/"),
                "rider_name": display_name(raw),
                "rider_name_raw": raw,
                "nth_best": int(nth_v) if nth_v is not None else None,
                "points_counted": parse_number(_cell(cells, hmap, "points counted").get_text()),
                "points_not_counted": parse_number(_cell(cells, hmap, "not counted").get_text()),
                "sanction_points": parse_number(_cell(cells, hmap, "sanction points").get_text()),
                "team_name": meta["team_name"],
                "team_class": meta["team_class"],
            })
    return rows


def team_breakdown_path(team_slug: str, season: int) -> str:
    return f"team/{team_slug}/results/ranking-{licence_cycle(season)}"


def fetch_team_breakdown(team_slug: str, season: int, use_cache: bool = True) -> Optional[tuple[dict, list[dict]]]:
    html = fetch(pcs_url(team_breakdown_path(team_slug, season)), use_cache=use_cache)
    if not html:
        return None
    return parse_team_breakdown_meta(html), parse_team_breakdown(html, season)


# ---------------------------------------------------------------------------
# team latest UCI points (last ~30 days, used for reconciliation only)
# ---------------------------------------------------------------------------

def parse_team_recent_points(html: str) -> list[dict]:
    """team/<slug>/results/latest-uci-points -> [{date, points, indirect_points}]."""
    s = soup(html)
    out: list[dict] = []
    for table in s.select("table.basic"):
        hmap = _header_map(table)
        if "date" not in hmap or "uci points" not in hmap:
            continue
        for tr in _body_rows(table):
            cells = tr.find_all("td")
            d = _cell(cells, hmap, "date").get_text(strip=True)
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
                continue
            ind = _cell(cells, hmap, "(indirect)")
            out.append({
                "date": d,
                "points": parse_number(_cell(cells, hmap, "uci points").get_text()) or 0.0,
                "indirect_points": (parse_number(ind.get_text()) if ind is not None else None) or 0.0,
            })
        break
    return out


def fetch_team_recent_points(team_slug: str, use_cache: bool = True) -> Optional[list[dict]]:
    html = fetch(pcs_url(f"team/{team_slug}/results/latest-uci-points"), use_cache=use_cache)
    return parse_team_recent_points(html) if html else None


# ---------------------------------------------------------------------------
# rider season results
# ---------------------------------------------------------------------------

def earliest_season_start(season: int) -> str:
    """First day a ``season`` UCI point can be dated: 1 October of ``season - 1``.

    The UCI season begins the day after the previous season's last WorldTour
    race (Tour of Guangxi, ~20 October), so October-December results of the
    previous calendar year can count for ``season`` (Israeli national ITT
    championship 2025-10-25: 90 points for NSN in 2026).  Rows from this date
    on are kept by the parsers; ``merge_season_rows`` / ``store_rider`` cut
    them at the calibrated season start.
    """
    return f"{season - 1}-10-01"


def parse_rider_results(html: str, season: int) -> dict:
    """Parse one rider/<slug>/results (or rider.php?...&p=results) page.

    Returns {rider_name, rider_id, rows, n_rows (data rows on the page, any
    season), has_older (a row older than :func:`earliest_season_start` is
    present, i.e. the page reaches back past the whole season)}.  ``rows``
    holds ``season`` rows plus the previous year's rows from 1 October on —
    the UCI season starts in late October of the previous year, and dropping
    every pre-January row lost results the world-ranking page does not list
    (2025-10-25 Israeli ITT championship: Raisberg 50, Kogut 30, Einhorn 10).
    Row keys follow the ``rider_uci_results`` contract.
    """
    s = soup(html)
    h1 = s.select_one("h1")
    rider_name = h1.get_text(" ", strip=True) if h1 is not None else None
    rid = s.select_one('form[action="rider.php"] input[name="id"]') or s.select_one('input[name="id"]')
    rider_id = (rid.get("value") or None) if rid is not None else None
    out = {"rider_name": rider_name, "rider_id": rider_id, "rows": [], "n_rows": 0, "has_older": False}
    for table in s.select("table.basic"):
        hmap = _header_map(table)
        if "race" not in hmap or "uci points" not in hmap or "date" not in hmap:
            continue
        for tr in _body_rows(table):
            cells = tr.find_all("td")
            d = _cell(cells, hmap, "date").get_text(strip=True)
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
                continue
            out["n_rows"] += 1
            year = int(d[:4])
            if d < earliest_season_start(season):
                out["has_older"] = True
                continue
            if year > season:
                continue
            race_cell = _cell(cells, hmap, "race")
            a = race_cell.select_one('a[href^="race/"]') if race_cell is not None else None
            text = (a or race_cell).get_text(" ", strip=True) if race_cell is not None else ""
            name, _, label = text.partition(" | ")
            pos, status = parse_position(_cell(cells, hmap, "result").get_text(" ", strip=True))
            uci, _penalty, _bonus = split_points_cell(_cell(cells, hmap, "uci points"))
            pcs, _, _ = split_points_cell(_cell(cells, hmap, "pcs points"))
            cls = _cell(cells, hmap, "class")
            href = (a.get("href") if a is not None else "") or ""
            out["rows"].append({
                "rider_name": rider_name,
                "date": d,
                "race_name": name.strip(),
                "race_slug": race_slug_from_href(href, year),
                "result_label": label.strip(),
                "result_kind": result_kind(label),
                "race_class": cls.get_text(strip=True) if cls is not None else None,
                "position": pos,
                "status": status,
                "points_uci": uci,
                "points_pcs": pcs,
                "result_url": href.strip("/") or f"{name.strip()}|{d}",
            })
        break
    return out


def rider_results_form_path(rider_id: str, season: int, offset: int) -> str:
    return (f"rider.php?id={rider_id}&sportid=1&p=results&s=&xseason={season}"
            f"&offset={offset}&filter=Filter")


def fetch_rider_season_results(rider_slug: str, season: int, use_cache: bool = True) -> Optional[dict]:
    """All ``season`` rows of a rider: {rider_name, rider_id, rows, pages, complete}.

    One fetch for almost every rider: rider/<slug>/results lists the newest 100
    results, so when it already reaches back past the season (1 October of the
    previous year) it holds the whole season.  Otherwise the season-filtered
    form endpoint is paginated by ``offset`` until a page comes back with
    fewer than 100 rows, and the previous year's first filtered page supplies
    its October-December rows.  ``complete`` is False when a later page
    failed (the rows are then partial and must not replace stored ones).
    Returns None when the first page cannot be fetched.
    """
    html = fetch(pcs_url(f"rider/{rider_slug}/results"), use_cache=use_cache)
    if not html:
        return None
    first = parse_rider_results(html, season)
    result = {"rider_name": first["rider_name"], "rider_id": first["rider_id"],
              "rows": first["rows"], "pages": 1, "complete": True}
    if first["has_older"] or first["n_rows"] < RESULTS_PAGE_SIZE:
        return result
    if not first["rider_id"]:
        log.warning("%s: results page full but no rider id — season may be truncated", rider_slug)
        result["complete"] = False
        return result
    rows, seen = [], set()

    def add(parsed_rows):
        for r in parsed_rows:
            key = (r["date"], r["result_url"], r["result_label"])
            if key not in seen:
                seen.add(key)
                rows.append(r)

    add(first["rows"])
    for page in range(MAX_RESULT_PAGES):
        html = fetch(pcs_url(rider_results_form_path(first["rider_id"], season,
                                                     page * RESULTS_PAGE_SIZE)),
                     use_cache=use_cache)
        result["pages"] += 1
        if not html:
            log.warning("%s: season page offset=%d failed — rows are partial",
                        rider_slug, page * RESULTS_PAGE_SIZE)
            return {**result, "rows": rows, "complete": False}
        parsed = parse_rider_results(html, season)
        add(parsed["rows"])
        if parsed["n_rows"] < RESULTS_PAGE_SIZE or not parsed["rows"]:
            break
    # the season filter is the calendar year: the previous October-December
    # rows (UCI season ``season``) are on the previous year's newest page
    html = fetch(pcs_url(rider_results_form_path(first["rider_id"], season - 1, 0)),
                 use_cache=use_cache)
    result["pages"] += 1
    if not html:
        log.warning("%s: previous-season page failed — late-%d rows may be missing",
                    rider_slug, season - 1)
        return {**result, "rows": rows, "complete": False}
    add(parse_rider_results(html, season)["rows"])
    result["rows"] = sorted(rows, key=lambda r: r["date"], reverse=True)
    return result


# ---------------------------------------------------------------------------
# rider UCI world-ranking breakdown: every dated UCI point item
# ---------------------------------------------------------------------------

def rider_uci_ranking_path(rider_id: str, ranking_date: str) -> str:
    return f"rider.php?id={rider_id}&p=results&s=uci-world-ranking&date={ranking_date}"


def parse_rider_uci_ranking(html: str) -> dict:
    """Parse rider.php?id=<id>&p=results&s=uci-world-ranking&date=<date>.

    The page breaks the rider's rolling 52-week UCI World Ranking total (as of
    ``date``) into every point item.  Two ``table.basic``:

    * per race — ``Date start | Race | Class | Points``; the race cell links the
      race (``race/vuelta-a-espana-2026-gc``, ``race/nc-mexico-2025-result``);
    * per result — ``Date | Race | Result | Points``; race cell
      ``La Vuelta Ciclista a España | Stage 6`` (its link is a bare ``race/``).
      GC leader-jersey points appear here as ``... | General classification``
      rows dated on each stage day (``20`` points, result ``1``), the final GC
      as the same label on the final day, and sanctions as a dated
      ``Sanctions`` row with a negative value (``-25``).

    Both end with a ``tr.sum`` row: the net total, which equals the team
    breakdown's ``counted + not counted`` for the season.

    Returns {rider_name, rider_id, date, total, items: [{date, text,
    race_name, label, position, status, points}], sanctions: [{date, points}]
    (points positive), races: [{date_start, race_name, race_url, race_class,
    points}]}.
    """
    s = soup(html)
    h1 = s.select_one("h1")
    form = s.select_one('form[action="rider.php"]')
    out: dict = {
        "rider_name": h1.get_text(" ", strip=True) if h1 is not None else None,
        "rider_id": None, "date": None, "total": None,
        "items": [], "sanctions": [], "races": [],
    }
    if form is not None:
        rid = form.select_one('input[name="id"]')
        dt = form.select_one('input[name="date"]')
        out["rider_id"] = rid.get("value") if rid is not None else None
        out["date"] = (dt.get("value") or None) if dt is not None else None
    for table in s.select("table.basic"):
        hmap = _header_map(table)
        total = table.select_one("tr.sum")
        if total is not None and out["total"] is None:
            c = _cell(total.find_all("td"), hmap, "points")
            out["total"] = parse_number(c.get_text()) if c is not None else None
        if "date start" in hmap and "class" in hmap:
            for tr in _body_rows(table):
                cells = tr.find_all("td")
                race = _cell(cells, hmap, "race")
                name = race.get_text(" ", strip=True) if race is not None else ""
                if name.lower() == "sanctions":
                    continue
                a = race.select_one('a[href^="race/"]') if race is not None else None
                href = a.get("href", "").strip("/") if a is not None else ""
                out["races"].append({
                    "date_start": _cell(cells, hmap, "date start").get_text(strip=True) or None,
                    "race_name": name,
                    "race_url": href if href != "race" else None,
                    "race_class": _cell(cells, hmap, "class").get_text(strip=True) or None,
                    "points": parse_number(_cell(cells, hmap, "points").get_text()),
                })
        elif "date" in hmap and "result" in hmap and "points" in hmap:
            for tr in _body_rows(table):
                cells = tr.find_all("td")
                d = _cell(cells, hmap, "date").get_text(strip=True)
                text = _cell(cells, hmap, "race").get_text(" ", strip=True)
                pts = parse_number(_cell(cells, hmap, "points").get_text())
                if text.lower() == "sanctions":
                    if pts:
                        out["sanctions"].append({"date": d or None, "points": abs(pts)})
                    continue
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
                    continue
                name, _, label = text.partition(" | ")
                pos, status = parse_position(_cell(cells, hmap, "result").get_text(" ", strip=True))
                out["items"].append({"date": d, "text": text, "race_name": name.strip(),
                                     "label": label.strip(), "position": pos, "status": status,
                                     "points": pts or 0.0})
    return out


def fetch_rider_uci_ranking(rider_id: str, ranking_date: str, use_cache: bool = True) -> Optional[dict]:
    html = fetch(pcs_url(rider_uci_ranking_path(rider_id, ranking_date)), use_cache=use_cache)
    return parse_rider_uci_ranking(html) if html else None


def _key_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip().lower()


def calibrate_season_start(samples: list[tuple[float, dict]], season: int,
                           tolerance: float = 0.5) -> tuple[str, dict]:
    """Find where the UCI season begins from (official total, ranking page) pairs.

    UCI's season ``season`` starts the day after the previous season's last
    WorldTour race (Tour of Guangxi, ~20 October of ``season - 1``): Mexico's
    national championships of 2025-10-23/25 count for 2026, the 2025-10-19 Japan
    Cup does not.  A ranking page fetched on ``date`` reaches back 52 weeks, so
    it holds a few items from before that boundary.  Every distinct pre-January
    item date is tried as the cutoff; the one under which the most riders'
    ranking items (minus sanctions) equal their breakdown total wins (ties: the
    earliest).  Returns (season_start, {cutoff: riders matched}) where
    season_start is the day after the last date the winning cutoff excludes.
    """
    from datetime import date as _d, timedelta
    jan1 = f"{season}-01-01"
    dates = sorted({i["date"] for _, wr in samples for i in wr["items"] if i["date"] < jan1}
                   | {s["date"] for _, wr in samples for s in wr["sanctions"]
                      if s["date"] and s["date"] < jan1})
    candidates = dates + [jan1]

    def matched(cut: str) -> int:
        n = 0
        for official, wr in samples:
            tot = sum(i["points"] for i in wr["items"] if i["date"] >= cut) - \
                sum(s["points"] for s in wr["sanctions"] if (s["date"] or jan1) >= cut)
            n += abs(tot - official) <= tolerance
        return n

    scores = {c: matched(c) for c in candidates}
    best = max(scores.values())
    cut = min(c for c in candidates if scores[c] == best)
    earlier = [c for c in candidates if c < cut]
    if earlier:
        start = (_d.fromisoformat(earlier[-1]) + timedelta(days=1)).isoformat()
    else:
        start = cut
    return start, scores


def merge_season_rows(results_rows: list[dict], ranking: dict, season: int,
                      rider_name: Optional[str] = None,
                      season_start: Optional[str] = None,
                      race_end_dates: Optional[dict] = None) -> tuple[list[dict], dict]:
    """Combine rider/<slug>/results rows with the world-ranking point items.

    The results page is the row list (every race day, with class, link, kind,
    position — including the many results worth 0 points); the ranking page is
    the authority on points.  Each ranking item is matched to the results row
    with the same date and ``Race | Label`` text (falling back to same date,
    same label, same points); a matched row takes the item's points.  Ranking
    items with no results row become extra rows:

    * ``... | General classification`` on a day that is not the race's final
      GC -> GC leader-jersey points: ``result_kind='other'``,
      ``status='leader'``, ``result_label='Leader jersey after Stage 6'``,
      ``result_url`` = that day's stage link.  The final GC is recognised by
      a GC row on the results page that day, or — when the results page is
      older than the ranking page (``--refresh`` on a later day re-fetches
      only the ranking pages) — by the race's last day in ``race_end_dates``
      (``{'race/tour-of-guangxi/2026': '2026-10-19'}``, the races table);
      without either, a stale results page would turn a final GC into a
      "leader jersey" row;
    * anything else (e.g. a national championship held in late October of the
      previous year, which UCI counts in this season) -> an ordinary row built
      from the item and the per-race table (class, link).

    Items dated before ``season_start`` (default 1 January) belong to the
    previous UCI season and are skipped (``diag['outside_season']``).

    Returns (rows, diagnostics) where diagnostics holds ``points_changed``
    (matched rows whose results-page points differed), ``unlisted`` (results
    rows with points the ranking does not count; their points are kept),
    ``extra`` (items added), ``outside_season`` and ``ranking_total``.
    """
    season_start = season_start or f"{season}-01-01"
    rows = [dict(r) for r in results_rows if r["date"] >= season_start]
    results_rows = rows[:]
    diag = {"points_changed": [], "unlisted": [], "extra": [], "outside_season": [],
            "ranking_total": ranking.get("total")}
    by_text: dict = {}
    for i, r in enumerate(rows):
        text = r["race_name"] + (f" | {r['result_label']}" if r.get("result_label") else "")
        by_text.setdefault((r["date"], _key_text(text)), []).append(i)
    used: set[int] = set()
    gc_final = {(r["race_name"], r["date"]) for r in rows if r["result_kind"] == "gc"}

    def find(item) -> Optional[int]:
        for i in by_text.get((item["date"], _key_text(item["text"])), []):
            if i not in used:
                return i
        for i, r in enumerate(rows):  # race named differently on the two pages
            if i in used or r["date"] != item["date"]:
                continue
            if _key_text(r.get("result_label")) == _key_text(item["label"]) and \
               abs((r["points_uci"] or 0) - item["points"]) < 0.01:
                return i
        return None

    races = ranking.get("races") or []

    def race_meta(name: str, day: str) -> dict:
        cands = [r for r in races if _key_text(r["race_name"]) == _key_text(name)]
        cands = [r for r in cands if (r["date_start"] or "") <= day] or cands
        return max(cands, key=lambda r: r["date_start"] or "") if cands else {}

    def is_final_gc(name: str, day: str, meta: dict, same_day: list) -> bool:
        if (name, day) in gc_final:
            return True
        slug = race_slug_from_href(meta.get("race_url") or "", int(day[:4])) if meta.get("race_url") else None
        slugs = {slug} | {r["race_slug"] for r in same_day}
        ends = [(race_end_dates or {}).get(x) for x in slugs if x]
        return any(e is not None and day >= e for e in ends)

    for item in ranking.get("items", []):
        if item["date"] < season_start:
            diag["outside_season"].append({k: item[k] for k in ("date", "text", "points")})
            continue
        i = find(item)
        if i is not None:
            used.add(i)
            old = rows[i]["points_uci"] or 0.0
            if abs(old - item["points"]) > 0.005:
                diag["points_changed"].append({**{k: rows[i][k] for k in ("date", "race_name", "result_label")},
                                               "results_page": old, "ranking": item["points"]})
            rows[i]["points_uci"] = item["points"]
            continue
        kind = result_kind(item["label"])
        name = item["race_name"]
        meta = race_meta(name, item["date"])
        same_day = [r for r in rows if r["date"] == item["date"] and r["result_kind"] == "stage"
                    and (r["race_name"] == name or _key_text(r["race_name"]) == _key_text(name))]
        if not same_day:
            same_day = [r for r in rows if r["date"] == item["date"] and r["result_kind"] == "stage"]
        if kind == "gc" and not is_final_gc(name, item["date"], meta, same_day):
            stage = same_day[0] if len(same_day) >= 1 else None
            href = stage["result_url"] if stage else (meta.get("race_url") or f"{name}|gc")
            row = {
                "rider_name": rider_name, "date": item["date"], "race_name": name,
                "race_slug": stage["race_slug"] if stage else race_slug_from_href(href, int(item["date"][:4])),
                "result_label": f"Leader jersey after {stage['result_label']}" if stage
                                else "Leader jersey",
                "result_kind": "other",
                "race_class": stage["race_class"] if stage else meta.get("race_class"),
                "position": 1, "status": "leader", "points_uci": item["points"], "points_pcs": None,
                "result_url": href,
            }
        else:
            href = meta.get("race_url") or f"{name}|{item['date']}"
            row = {
                "rider_name": rider_name, "date": item["date"], "race_name": name,
                "race_slug": race_slug_from_href(href, int(item["date"][:4])),
                "result_label": item["label"], "result_kind": kind,
                "race_class": meta.get("race_class"), "position": item["position"],
                "status": item["status"], "points_uci": item["points"], "points_pcs": None,
                "result_url": href,
            }
        # keep the unique key (date, url, label) unique
        while any(r["date"] == row["date"] and r["result_url"] == row["result_url"]
                  and r["result_label"] == row["result_label"] for r in rows):
            row["result_label"] += " (2)"
        rows.append(row)
        diag["extra"].append({k: row[k] for k in ("date", "race_name", "result_label", "points_uci")})
    for i, r in enumerate(rows[:len(results_rows)]):
        if i not in used and (r["points_uci"] or 0) > 0:
            diag["unlisted"].append({k: r[k] for k in ("date", "race_name", "result_label", "points_uci")})
    return rows, diag


# ---------------------------------------------------------------------------
# rider profile: GC leader-jersey points, sanctions, team history
# ---------------------------------------------------------------------------

def _ddmm_to_iso(text: str, season: int) -> Optional[str]:
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})", (text or "").strip())
    if not m:
        return None
    try:
        return _date(season, int(m.group(2)), int(m.group(1))).isoformat()
    except ValueError:
        return None


def parse_rider_profile_season(html: str, season: int) -> dict:
    """Season data from the rider profile's ``table.rdrResults``.

    Returns {rider_name, season_tab (data-id of the selected season tab),
    leader_rows (GC leader-jersey points, rider_uci_results rows with
    result_kind 'other', status 'leader'), sanctions ([{date, race_name,
    penalty_points}] from red '-N' cells), team_history}.

    ``table.rdrResults`` markup::

        <tr class="main"><td>04.07 › 26.07</td>...<a href="race/tour-de-france-2026-stage-1">
            <span>Tour de France</span> (2.UWT)</a>...</tr>
        <tr class="stage"><td>09.07</td><td>1</td><td>1</td><td></td>
            <td><a href="race/tour-de-france-2026-stage-6"><span class="imob">6</span>
                <span class="idesk">Stage 6</span> - Pau › Gavarnie-Gèdre</a></td>
            ...<td>210 <span class="gc_pnt">+25</span></td><td></td></tr>

    The final day's ``gc_pnt`` (the race header's end date) repeats the GC
    award and is skipped.  Only the season shown by default (the current one)
    is in the page; for another ``season`` leader_rows is empty.
    """
    s = soup(html)
    h1 = s.select_one("h1")
    out: dict = {"rider_name": h1.get_text(" ", strip=True) if h1 is not None else None,
                 "season_tab": None, "leader_rows": [], "sanctions": [],
                 "team_history": parse_team_history(s)}
    cur = s.select_one("ul.rdrSeasonNav2 li.cur a") or s.select_one("ul.rdrSeasonNav2 li.tab.cur a")
    if cur is not None:
        out["season_tab"] = int(cur.get("data-id")) if (cur.get("data-id") or "").isdigit() else None
    if out["season_tab"] is not None and out["season_tab"] != season:
        log.warning("profile shows season %s, not %s — no leader points parsed",
                    out["season_tab"], season)
        return out
    table = s.select_one("table.rdrResults")
    if table is None:
        return out
    race_name = race_class = race_end = None
    for tr in table.find_all("tr"):
        classes = tr.get("class") or []
        tds = tr.find_all("td")
        if not tds:
            continue
        a = tr.select_one('a[href^="race/"]')
        if "main" in classes:
            race_name = race_class = race_end = None
            if a is not None:
                span = a.find("span")
                race_name = (span or a).get_text(" ", strip=True)
                m = re.search(r"\(([^)]+)\)\s*$", a.get_text(" ", strip=True))
                race_class = m.group(1) if m else None
            dates = re.findall(r"\d{1,2}\.\d{1,2}", tds[0].get_text())
            race_end = _ddmm_to_iso(dates[-1], season) if dates else None
            day = _ddmm_to_iso(dates[0], season) if len(dates) == 1 else None
        elif "stage" in classes:
            day = _ddmm_to_iso(tds[0].get_text(strip=True), season) or race_end
        else:
            continue
        for td in tds:
            pts, penalty, bonus = split_points_cell(td) if td.select(".gc_pnt, .red") else (None, None, None)
            if penalty:
                out["sanctions"].append({"date": day, "race_name": race_name,
                                         "penalty_points": penalty})
            if bonus and "stage" in classes and day and day != race_end and a is not None:
                idesk = a.select_one("span.idesk")
                label = (idesk or a).get_text(" ", strip=True)
                href = a.get("href", "").strip("/")
                out["leader_rows"].append({
                    "rider_name": out["rider_name"],
                    "date": day,
                    "race_name": race_name,
                    "race_slug": race_slug_from_href(href, season),
                    "result_label": f"Leader jersey after {label}",
                    "result_kind": "other",
                    "race_class": race_class,
                    "position": 1,
                    "status": "leader",
                    "points_uci": bonus,
                    "points_pcs": None,
                    "result_url": href,
                })
    return out


def parse_team_history(s) -> list[dict]:
    """Team history from ``ul.rdr-teams2``: [{season, team_slug, team_name,
    team_class, note}] — ``note`` is the ``span.fs10`` text PCS prints next to
    a mid-season move (e.g. a start or end date), '' otherwise."""
    out: list[dict] = []
    for li in s.select("ul.rdr-teams2 li.main"):
        season_div = li.select_one("div.season")
        a = li.select_one('a[href^="team/"]')
        if season_div is None or a is None:
            continue
        yr = season_div.get_text(strip=True)
        if not yr.isdigit():
            continue
        name_div = li.select_one("div.name") or li
        note_span = name_div.select_one("span.fs10")
        note = note_span.get_text(" ", strip=True) if note_span is not None else ""
        m = re.search(r"\(([A-Z]+)\)", name_div.get_text(" ", strip=True))
        out.append({"season": int(yr), "team_slug": _strip_prefix(a["href"], "team/"),
                    "team_name": a.get_text(" ", strip=True),
                    "team_class": m.group(1) if m else None, "note": note})
    return out


def fetch_rider_profile_season(rider_slug: str, season: int, use_cache: bool = True) -> Optional[dict]:
    html = fetch(pcs_url(f"rider/{rider_slug}"), use_cache=use_cache)
    return parse_rider_profile_season(html, season) if html else None


# ---------------------------------------------------------------------------
# fines & penalties
# ---------------------------------------------------------------------------

def parse_fines_penalties(html: str, season: int) -> list[dict]:
    """rider/<slug>/info/fines-penalties -> season rows with penalty points > 0.

    Rows from 1 October of the previous year on are kept (the UCI season
    starts in late October); the caller cuts them at the season start.

    Each row: date, race_name ('Ronde van Vlaanderen ME', 'Tour de France |
    Stage 4'), race_url, penalty_points (positive), fine, article.
    """
    s = soup(html)
    out: list[dict] = []
    for table in s.select("table.basic"):
        hmap = _header_map(table)
        if "penalty points" not in hmap:
            continue
        for tr in _body_rows(table):
            cells = tr.find_all("td")
            d = _cell(cells, hmap, "date").get_text(strip=True)
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) or \
               not earliest_season_start(season) <= d < f"{season + 1}-01-01":
                continue
            pts = parse_number(_cell(cells, hmap, "penalty points").get_text())
            if not pts:
                continue
            race = _cell(cells, hmap, "race")
            a = race.select_one('a[href^="race/"]') if race is not None else None
            fine = _cell(cells, hmap, "fine")
            art = _cell(cells, hmap, "article")
            out.append({
                "date": d,
                "race_name": race.get_text(" ", strip=True) if race is not None else None,
                "race_url": a.get("href", "").strip("/") if a is not None else None,
                "penalty_points": abs(pts),
                "fine": fine.get_text(" ", strip=True) if fine is not None else None,
                "article": art.get_text(" ", strip=True) if art is not None else None,
            })
    return out


def fetch_rider_penalties(rider_slug: str, season: int, use_cache: bool = True) -> Optional[list[dict]]:
    html = fetch(pcs_url(f"rider/{rider_slug}/info/fines-penalties"), use_cache=use_cache)
    return parse_fines_penalties(html, season) if html else None
