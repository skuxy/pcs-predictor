"""Scrape race results from ProCyclingStats.

Two generations of PCS result markup are supported:

* **Live markup (since ~Aug 2026).**  A stage / one-day page holds one
  ``div.resultCont`` per classification tab (stage, GC, points, KOM, youth,
  teams, "today" variants ...), each wrapping a ``table.results``.  Only the
  default tab is visible; the others carry ``class="resultCont hide"``.  The
  tab labels live in ``a.resultNav[data-navid]`` links whose ``data-navid``
  matches the container's ``data-navid``.  Table headers carry stable
  ``th[data-code]`` attributes (``rnk``, ``bib``, ``rider``, ``riderteam``,
  ``uci_pnt``, ``pnt``, ``timelag`` ...); body cells only have presentational
  classes, so columns are mapped by header index.  Team time trials render
  the stage tab as ``ul.list.ttt-results`` instead of a table.
* **Legacy markup.**  Rows with ``td.ridername`` / ``td.uci_pnt`` /
  ``td.time > font`` and no header codes.  Kept as a fallback (the HTML cache
  and the regression tests still contain it).

UCI sanctions are rendered inside the UCI cell as red spans:
``800 <span class="red fs10">-25</span>`` means 800 points earned and a
25-point sanction.  ``points_uci`` is the earned (placing) value only; the
sanction goes to ``uci_penalty`` (positive, summed when several spans).
"""
import logging
import re

from scraper.races import _parse_infolist
from scraper.utils import fetch, soup, pcs_url, parse_time_gap, fetch_with_playwright, cache_path

log = logging.getLogger(__name__)

# th[data-code] → field name.  Codes not listed here (age, specialty, gc,
# gc_timelag, h2h, bonis, time_wonlost, prev_rnk, delta_rnk, pnt_won, ...) are
# ignored.
_HEADER_CODES = {
    "rnk": "rank",
    "bib": "bib",
    "rider": "rider",
    "riderteam": "team",
    "team": "team",
    "uci_pnt": "uci",
    "pnt": "pcs",
    "timelag": "time",
    "time": "time",
}

# Header text fallback when a th has no data-code.
_HEADER_TEXT = {
    "rnk": "rank", "#": "rank", "pos": "rank",
    "bib": "bib",
    "rider": "rider",
    "team": "team",
    "uci": "uci",
    "pcs": "pcs", "pnt": "pcs",
    "time": "time", "timelag": "time",
}

# Labels of the result-tab link that holds the stage / race finish order.
_STAGE_TAB_LABELS = {"stage", "prologue", "result", "results"}


def _try_playwright(url: str):
    """Fetch url via Playwright, overwriting the stale cache entry. Returns soup or None."""
    pw_html = fetch_with_playwright(url)
    if not pw_html:
        return None
    cache_path(url).write_text(pw_html, encoding="utf-8")
    return soup(pw_html)


def _is_missing_page(html: str) -> bool:
    """PCS answers an unknown slug with an empty document
    (``<html><head></head><body></body></html>``)."""
    return not html.strip() or (
        len(html) < 2000 and re.search(r"<body[^>]*>\s*</body>", html, re.I) is not None)


def _page_claims_results(s) -> bool:
    """True if the race info block reports an outcome ("Won how", winner's
    average speed) — i.e. an empty results table there is a rendering gap,
    not a race PCS has no results for."""
    info = _parse_infolist(s)
    return bool(info.get("won how") or info.get("avg. speed winner"))


def _is_skeleton(table) -> bool:
    """True for an empty (header-only / JS skeleton) results table."""
    return table is not None and table.name == "table" and len(table.select("tr")) <= 1


def fetch_stage_results(stage_slug: str) -> list[dict]:
    """
    Return a list of result dicts for a stage or one-day race.

    The stage-finish tab is chosen with :func:`select_stage_result` (never the
    GC tab, even on a final-stage page whose GC tab is the visible one).  If
    the page has no usable result, one-day races are retried at
    ``<slug>/result``; a ``table.basic`` top-N is the last resort.

    Each dict has: rider_slug, rider_name, position, status, time_seconds,
    points_pcs, points_uci (placing points only), uci_penalty (positive
    points deducted, or None), bib, team_slug (no ``team/`` prefix, with the
    year suffix, e.g. ``bahrain-victorious-2026``), team_name.
    """
    url = pcs_url(stage_slug)
    html = fetch(url)
    if not html:
        return []
    if _is_missing_page(html):
        log.warning("PCS returned an empty page for %s (unknown or renamed slug)", stage_slug)
        return []

    s = soup(html)
    target = select_stage_result(s)

    # Empty JS skeleton in cache — try Playwright to get JS-rendered data.
    # Skipped when the page itself says the race has no outcome (no "Won how",
    # no winner's speed): PCS simply has no results, a re-fetch can't help.
    if _is_skeleton(target) and not _page_claims_results(s):
        log.warning("no results on PCS for %s (empty table, no race outcome on the page)", stage_slug)
        return []
    if _is_skeleton(target):
        log.info("empty JS skeleton on %s — retrying with Playwright", stage_slug)
        pw_s = _try_playwright(url)
        if pw_s:
            s = pw_s
            target = select_stage_result(pw_s)
            if _is_skeleton(target):
                target = None
        else:
            target = None

    if target is None and not re.search(r"/(stage-\w+|prologue)$", stage_slug.rstrip("/")):
        # One-day races have results at /result suffix
        alt_url = pcs_url(stage_slug.rstrip("/") + "/result")
        alt_html = fetch(alt_url)
        if alt_html:
            alt_s = soup(alt_html)
            alt = select_stage_result(alt_s)
            if _is_skeleton(alt) and not _page_claims_results(alt_s):
                alt = None
            elif _is_skeleton(alt):
                log.info("empty JS skeleton on %s/result — retrying with Playwright", stage_slug)
                pw_s = _try_playwright(alt_url)
                alt = select_stage_result(pw_s) if pw_s else None
                if _is_skeleton(alt):
                    alt = None
            if alt is not None:
                target = alt

    if target is None:
        # Fallback: main page may have a table.basic with top-10
        basic = s.select_one("table.basic")
        if basic is not None and len(basic.select("tr")) > 1 and basic.find("a", href=re.compile(r"rider/")):
            target = basic
    if target is None:
        log.warning("no results table on %s", stage_slug)
        return []

    if target.name == "ul":
        results = _parse_ttt_results(target)
    else:
        results = _parse_results_table(target)
    if _looks_cancelled(results):
        log.warning(
            "stage %s has no real finishing positions (all 'NR' placeholders) — "
            "likely a cancelled/unraced stage, skipping", stage_slug,
        )
        return []
    return results


def select_stage_result(s):
    """Return the element holding the stage / race finish order, or None.

    Live markup: the ``div.resultCont`` whose ``data-navid`` belongs to the
    ``a.resultNav`` labelled "Stage" (or "Prologue"/"Result"); without a tab
    bar (one-day races) the first visible ``div.resultCont``.  Inside it,
    ``ul.ttt-results`` (team time trial) wins over ``table.results``.

    Legacy markup (no ``resultCont``): the first ``table.results``.
    """
    containers = s.select("div.resultCont")
    if containers:
        cont = None
        for a in s.select("a.resultNav[data-navid]"):
            if a.get_text(" ", strip=True).lower() in _STAGE_TAB_LABELS:
                navid = a.get("data-navid")
                cont = next((c for c in containers if c.get("data-navid") == navid), None)
                if cont is not None:
                    break
        if cont is None:
            cont = next((c for c in containers if "hide" not in (c.get("class") or [])), None)
        if cont is not None:
            ttt = cont.select_one("ul.ttt-results")
            if ttt is not None:
                return ttt
            table = cont.select_one("table.results")
            if table is not None:
                return table
    return s.select_one("table.results")


def _looks_cancelled(results: list[dict]) -> bool:
    """True if a 'results' table is actually a startlist rendered under the
    same markup — PCS does this for cancelled/neutralised stages, showing every
    non-DNF rider with an 'NR' placeholder (in team-roster order) instead of a
    real finish position."""
    if not results:
        return False
    ranked = [r for r in results if r["status"] == "finished"]
    if ranked:
        return False
    nr_count = sum(1 for r in results if r["status"] == "unknown")
    return nr_count / len(results) > 0.8


def _header_map(table) -> dict[str, int]:
    """Map field name → column index from the table header (th[data-code],
    falling back to the header text).  Empty when the table has no header."""
    head_row = table.select_one("thead tr")
    if head_row is None:
        first = table.find("tr")
        if first is None or not first.find("th"):
            return {}
        head_row = first
    mapping: dict[str, int] = {}
    for idx, th in enumerate(head_row.find_all("th")):
        code = (th.get("data-code") or "").strip().lower()
        field = _HEADER_CODES.get(code)
        if field is None and not code:
            field = _HEADER_TEXT.get(th.get_text(strip=True).lower())
        if field and field not in mapping:
            mapping[field] = idx
    return mapping


def _parse_results_table(table) -> list[dict]:
    """Parse a results table (live header-mapped, legacy, or table.basic)."""
    # Detect table.basic layout (no class-annotated cells — simpler structure)
    if "basic" in (table.get("class") or []):
        return _parse_basic_table(table)
    columns = _header_map(table)
    if "rider" in columns:
        return _parse_mapped_table(table, columns)
    return _parse_legacy_table(table)


def _blank_row(rider_slug: str, rider_name: str, position, status) -> dict:
    return {
        "rider_slug": rider_slug,
        "rider_name": rider_name,
        "position": position,
        "status": status,
        "time_seconds": None,
        "points_pcs": None,
        "points_uci": None,
        "uci_penalty": None,
        "bib": None,
        "team_slug": None,
        "team_name": None,
    }


def _parse_mapped_table(table, columns: dict[str, int]) -> list[dict]:
    """Live markup: cells are located through the header index map.

    Verified row layout (Oct 2026, header data-codes):
      rnk | age | specialty | bib | gc | gc_timelag | h2h | rider | riderteam |
      uci_pnt | pnt | bonis | timelag | time_wonlost
    The rider cell also carries a mobile-only team name in
    div.showIfMobile — the name is taken from the rider link only.
    The timelag cell holds the winner's absolute time, then gaps or ",,".
    Time-trial and prologue pages add the hundredths in a font
    (``0:28<font class="fs10">,36</font>``); they are dropped (see
    :func:`_gap_text`), so ``time_seconds`` is whole seconds as on road stages.
    """
    results = []
    # PCS renders ",," (ditto) when a rider's gap equals the previous row's —
    # the previous rider's gap, NOT the leader's. Carry it forward.
    last_gap = 0
    body_rows = table.select("tbody tr") or table.select("tr")
    for row in body_rows:
        cells = row.find_all("td", recursive=False)
        if not cells:
            continue

        def cell(field):
            idx = columns.get(field)
            if idx is None or idx >= len(cells):
                return None
            return cells[idx]

        rider_cell = cell("rider")
        rider_link = rider_cell.find("a", href=re.compile(r"^/?rider/")) if rider_cell else None
        if rider_link is None:  # misaligned row — search the whole row
            rider_link = row.find("a", href=re.compile(r"^/?rider/"))
        if rider_link is None:
            continue

        rank_cell = cell("rank") or cells[0]
        position, status = _parse_position(rank_cell.get_text(strip=True))
        rec = _blank_row(_rider_slug(rider_link), rider_link.get_text(separator=" ", strip=True),
                         position, status)

        team_cell = cell("team")
        team_link = team_cell.find("a", href=re.compile(r"^/?team/")) if team_cell else None
        if team_link is None:
            team_link = row.find("a", href=re.compile(r"^/?team/"))
        if team_link is not None:
            rec["team_slug"], rec["team_name"] = _team_from_link(team_link)

        rec["points_uci"], rec["uci_penalty"] = _parse_points(cell("uci"))
        rec["points_pcs"], _ = _parse_points(cell("pcs"))
        rec["bib"] = _cell_int(cell("bib"))

        time_cell = cell("time")
        if position == 1:
            rec["time_seconds"] = 0
            last_gap = 0
        elif position is not None and time_cell is not None:
            raw_time = _gap_text(time_cell)
            if raw_time in (",,", ""):
                rec["time_seconds"] = last_gap
            else:
                rec["time_seconds"] = parse_time_gap(raw_time)
                if rec["time_seconds"] is not None:
                    last_gap = rec["time_seconds"]
        results.append(rec)
    return results


_HUNDREDTHS = re.compile(r"^[,.]\d+$")


def _gap_text(time_cell) -> str:
    """Displayed time gap of a results time cell, whole seconds only.

    * Live road stage: the gap or ditto mark is the cell's own text
      (``<td class="ar">0:02</td>``, ``<td class="ar">,,</td>``).
    * Live time trial / prologue: the hundredths follow in a font,
      ``<td class="ar">0:28<font class="fs10">,36</font></td>`` → ``'0:28'``
      (the pre-Oct-2026 parser read only the font, ``',36'``, and stored NULL
      gaps for every ITT finisher but the winner).  The truncated value is
      what GC timing uses (hundredths are dropped) and what a following ditto
      ``,,`` row repeats.
    * Legacy: the gap sits inside the font (``<font>2:30</font>``,
      ``<font>,,</font>``) and is returned as is.
    """
    for font in time_cell.find_all("font"):
        text = font.get_text(strip=True)
        if text and not _HUNDREDTHS.match(text):
            return text
    return "".join(str(t) for t in time_cell.find_all(string=True)
                   if not _inside_tag(t, time_cell, "font")).strip()


def _inside_tag(node, cell, name: str) -> bool:
    """True if ``node`` sits inside a ``name`` element below ``cell``."""
    p = node.parent
    while p is not None and p is not cell:
        if p.name == name:
            return True
        p = p.parent
    return False


def _parse_legacy_table(table) -> list[dict]:
    """
    Legacy PCS layout (before Aug 2026), no header codes:
      td[0]:           position (or DNF/DNS/OTL)
      td.bibs:         bib number
      td.ridername:    <a href="rider/SLUG"> name </a>
      td.cu600:        team name (optionally <a href="team/SLUG">)
      td.uci_pnt:      UCI points
      td.pnt:          PCS points
      td.time.ar:      time gap — <font> contains ",," (ditto) or "H:MM:SS"
    """
    results = []
    last_gap = 0
    for row in table.select("tr"):
        cells = row.find_all("td")
        if not cells:
            continue

        rider_cell = row.select_one("td.ridername")
        if not rider_cell:
            continue
        rider_link = rider_cell.find("a", href=re.compile(r"rider/"))
        if not rider_link:
            continue

        # Name stored as "LASTNAME Firstname" on PCS — normalise later
        position, status = _parse_position(cells[0].get_text(strip=True))
        rec = _blank_row(_rider_slug(rider_link), rider_link.get_text(separator=" ", strip=True),
                         position, status)

        # Time gap — inside td.time > font
        # Position 1 = leader, always 0 gap (their cell has absolute time)
        time_cell = row.select_one("td.time")
        if position == 1:
            rec["time_seconds"] = 0
            last_gap = 0
        elif position is not None and time_cell:
            raw_time = _gap_text(time_cell) if time_cell.find("font") else ""
            if raw_time == ",," or raw_time == "":
                rec["time_seconds"] = last_gap
            else:
                rec["time_seconds"] = parse_time_gap(raw_time)
                if rec["time_seconds"] is not None:
                    last_gap = rec["time_seconds"]

        rec["points_uci"], rec["uci_penalty"] = _parse_points(row.select_one("td.uci_pnt"))
        rec["points_pcs"], _ = _parse_points(row.select_one("td.pnt"))
        rec["bib"] = _cell_int(row.select_one("td.bibs"))
        team_link = row.find("a", href=re.compile(r"^/?team/"))
        if team_link is not None:
            rec["team_slug"], rec["team_name"] = _team_from_link(team_link)
        else:
            team_cell = row.select_one("td.cu600")
            if team_cell is not None and team_cell.get_text(strip=True):
                rec["team_name"] = team_cell.get_text(strip=True)
        results.append(rec)
    return results


def _parse_basic_table(table) -> list[dict]:
    """table.basic: td[0]=pos, td[1]=rider (a href), td[2]=team, td[3]=time."""
    results = []
    for row in table.select("tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        rider_link = cells[1].find("a", href=re.compile(r"rider/"))
        if not rider_link:
            continue
        position, status = _parse_position(cells[0].get_text(strip=True))
        rec = _blank_row(_rider_slug(rider_link), rider_link.get_text(separator=" ", strip=True),
                         position, status)
        rec["time_seconds"] = 0 if position == 1 else None
        team_link = row.find("a", href=re.compile(r"^/?team/"))
        if team_link is not None:
            rec["team_slug"], rec["team_name"] = _team_from_link(team_link)
        results.append(rec)
    return results


def _parse_ttt_results(ul) -> list[dict]:
    """Team time trial stage tab (live markup), one ``li`` per team:

      <li>
        <div class="w50 ..."><div class="w10 fs14">1</div>
          <div class="w90 fs14 bold"><a href="team/team-visma-lease-a-bike-2026">...</a></div></div>
        <div class="w50 ... timeSpeed"><div class="w25 fs14 bold time">21:47<font>,87</font></div>...</div>
        <div class="w100"><table><tbody>
          <tr><td><a href="rider/jonas-vingegaard">VINGEGAARD Jonas</a></td><td>50</td><td>26.25</td></tr>
          <tr><td><a href="rider/sepp-kuss">KUSS Sepp <font class="blue">+1:57</font></a></td>...</tr>
        </tbody></table></div>
      </li>

    Columns of the rider table: rider (+ own gap to the team time in
    font.blue), PCS points, UCI points (the team's share, often fractional).

    Output is one row per rider, ranked individually by team time + own gap
    (ties keep team order), so the stage keeps per-rider positions and
    ``time_seconds`` like any other stage.  A team without a parsable time
    sorts after all timed teams.
    """
    entries = []
    for team_rank, li in enumerate(ul.find_all("li", recursive=False)):
        team_link = li.find("a", href=re.compile(r"^/?team/"))
        rider_rows = li.select("table tr")
        if team_link is None or not rider_rows:
            continue
        team_slug, team_name = _team_from_link(team_link)
        time_el = li.select_one("div.time")
        team_time = _ttt_time_seconds(time_el.get_text("", strip=True)) if time_el else None
        for order, tr in enumerate(rider_rows):
            tds = tr.find_all("td")
            link = tr.find("a", href=re.compile(r"^/?rider/"))
            if link is None:
                continue
            gap_el = link.find("font")
            gap = parse_time_gap(gap_el.get_text(strip=True)) if gap_el else 0
            name_parts = [t.strip() for t in link.find_all(string=True, recursive=False) if t.strip()]
            rec = _blank_row(_rider_slug(link), " ".join(name_parts) or link.get_text(" ", strip=True),
                             None, "finished")
            rec["team_slug"], rec["team_name"] = team_slug, team_name
            if len(tds) > 1:
                rec["points_pcs"], _ = _parse_points(tds[1])
            if len(tds) > 2:
                rec["points_uci"], rec["uci_penalty"] = _parse_points(tds[2])
            abs_time = None if team_time is None else team_time + (gap or 0)
            entries.append((abs_time is None, abs_time or 0, team_rank, order, rec))

    entries.sort(key=lambda e: e[:4])
    timed = [e[1] for e in entries if not e[0]]
    best = min(timed) if timed else None
    results = []
    for pos, (untimed, abs_time, _, _, rec) in enumerate(entries, start=1):
        rec["position"] = pos
        rec["time_seconds"] = None if untimed or best is None else int(round(abs_time - best))
        results.append(rec)
    return results


def _ttt_time_seconds(raw: str) -> float | None:
    """'21:47,87' → 1307.87 (PCS shows hundredths after a comma)."""
    raw = raw.strip()
    m = re.match(r"^([\d:]+)(?:[,.](\d+))?$", raw)
    if not m:
        return None
    whole = parse_time_gap(m.group(1))
    if whole is None:
        return None
    frac = float("0." + m.group(2)) if m.group(2) else 0.0
    return whole + frac


def _rider_slug(link) -> str:
    return link["href"].strip("/").split("rider/", 1)[-1].split("?")[0].strip("/")


def _team_from_link(link) -> tuple[str | None, str | None]:
    slug = link.get("href", "").strip("/").split("team/", 1)[-1].split("?")[0].strip("/")
    name = link.get_text(" ", strip=True)
    return (slug or None), (name or None)


def _parse_position(raw: str):
    raw = raw.strip()
    status_map = {
        "DNF": "dnf", "DNS": "dns", "OTL": "otl",
        "DSQ": "dsq", "DF": "dnf", "OTB": "otl",
    }
    upper = raw.upper()
    if upper in status_map:
        return None, status_map[upper]
    try:
        return int(re.sub(r"[^\d]", "", raw)), "finished"
    except Exception:
        return None, "unknown"


def _number(text: str):
    """'800' → 800, '26.25' → 26.25, '1 300' → 1300, '' → None."""
    text = text.replace("\xa0", " ").strip()
    m = re.search(r"\d[\d ]*(?:[.,]\d+)?", text)
    if not m:
        return None
    raw = m.group(0).replace(" ", "")
    if "," in raw:
        # "26,25" is a decimal comma; PCS does not use thousands commas here
        raw = raw.replace(",", ".")
    try:
        val = float(raw)
    except ValueError:
        return None
    return int(val) if val.is_integer() else val


def _parse_points(cell) -> tuple:
    """Split a points cell into (earned points, sanction).

    Live markup puts UCI sanctions in red spans after the earned value:
      ``800 <span class="red fs10">-25</span>``            → (800, 25)
      ``<span class="red fs10">-25</span>``                → (None, 25)
      ``110 <span class="red">-25</span> <span class="red">-50</span>`` → (110, 75)
      ``26.25`` (TTT share)                                 → (26.25, None)
    Digits of the two parts are never concatenated (the pre-2026-10 parser
    turned '800 -25' into 80025).
    """
    if cell is None:
        return None, None
    penalty = 0
    found_penalty = False
    earned_parts = []
    for node in cell.descendants:
        if isinstance(node, str):
            if not _inside_red(node, cell):
                earned_parts.append(str(node))
        elif "red" in (node.get("class") or []) and not _inside_red(node, cell):
            val = _number(node.get_text(strip=True))
            if val is not None:
                penalty += val
                found_penalty = True
    earned = _number(" ".join(earned_parts))
    if earned is None:
        # Some legacy cells put a negative value as plain text: "-25"
        txt = " ".join(earned_parts).strip()
        if txt.startswith("-") and _number(txt) is not None:
            penalty += _number(txt)
            found_penalty = True
    elif " ".join(earned_parts).strip().startswith("-"):
        penalty += earned
        found_penalty = True
        earned = None
    return earned, (penalty if found_penalty else None)


def _inside_red(node, cell) -> bool:
    """True if node sits inside a red (sanction) element below cell."""
    p = node.parent
    while p is not None and p is not cell:
        if "red" in (p.get("class") or []):
            return True
        p = p.parent
    return False


def _cell_int(cell) -> int | None:
    if not cell:
        return None
    try:
        return int(re.sub(r"[^\d]", "", cell.get_text(strip=True)))
    except Exception:
        return None
