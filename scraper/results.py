"""Scrape race results from ProCyclingStats."""
import logging
import re

from scraper.utils import fetch, soup, pcs_url, parse_time_gap

log = logging.getLogger(__name__)


def fetch_stage_results(stage_slug: str) -> list[dict]:
    """
    Return a list of result dicts for a stage or one-day race.

    PCS stage pages have multiple table.results elements (stage result,
    GC, sprint points, KOM, etc.).  We want the first one — the stage
    finish order.

    Verified column layout (table.results rows):
      td[0]:           position (or DNF/DNS/OTL)
      td.bibs:         bib number
      td.h2h:          head-to-head checkbox (skip)
      td.specialty:    rider speciality tag
      td.age:          age
      td.ridername:    <a href="rider/SLUG"> name </a>
      td.cu600:        team name
      td.uci_pnt:      UCI points
      td.pnt:          PCS points
      td.ar.cu600:     time bonus (seconds, blue font)  [optional]
      td.time.ar:      time gap — <font> contains ",," (leader) or "H:MM:SS"
    """
    url = pcs_url(stage_slug)
    html = fetch(url)
    if not html:
        return []

    s = soup(html)
    # First table.results is the stage finish table
    table = s.select_one("table.results")
    if table and len(table.select("tr")) <= 1:
        table = None  # empty JS skeleton — ignore
    if not table:
        # One-day races have results at /result suffix (JS-rendered)
        alt_url = pcs_url(stage_slug.rstrip("/") + "/result")
        alt_html = fetch(alt_url)
        if alt_html:
            alt_s = soup(alt_html)
            alt_table = alt_s.select_one("table.results")
            if alt_table and len(alt_table.select("tr")) > 1:
                table = alt_table
    if not table:
        # Fallback: main page may have a table.basic with top-10
        table = s.select_one("table.basic")
        if table and len(table.select("tr")) <= 1:
            table = None
    if not table:
        log.warning("no results table on %s", stage_slug)
        return []

    return _parse_results_table(table)


def _parse_results_table(table) -> list[dict]:
    # Detect table.basic layout (no class-annotated cells — simpler structure)
    is_basic = "basic" in (table.get("class") or [])

    results = []
    for row in table.select("tr"):
        cells = row.find_all("td")
        if not cells:
            continue

        if is_basic:
            # table.basic: td[0]=pos, td[1]=rider (a href), td[2]=team, td[3]=time
            if len(cells) < 2:
                continue
            rider_link = cells[1].find("a", href=re.compile(r"rider/")) if len(cells) > 1 else None
            if not rider_link:
                continue
            rider_slug = rider_link["href"].strip("/").replace("rider/", "")
            rider_name = rider_link.get_text(separator=" ", strip=True)
            pos_raw = cells[0].get_text(strip=True)
            position, status = _parse_position(pos_raw)
            time_seconds = 0 if position == 1 else None
            results.append({
                "rider_slug": rider_slug,
                "rider_name": rider_name,
                "position": position,
                "status": status,
                "time_seconds": time_seconds,
                "points_pcs": None,
                "points_uci": None,
                "bib": None,
            })
            continue

        # Rider link
        rider_cell = row.select_one("td.ridername")
        if not rider_cell:
            continue
        rider_link = rider_cell.find("a", href=re.compile(r"rider/"))
        if not rider_link:
            continue

        rider_slug = rider_link["href"].strip("/").replace("rider/", "")
        # Name stored as "LASTNAME Firstname" on PCS — normalise later
        rider_name = rider_link.get_text(separator=" ", strip=True)

        # Position / status
        pos_raw = cells[0].get_text(strip=True)
        position, status = _parse_position(pos_raw)

        # Time gap — inside td.time > font
        # Position 1 = leader, always 0 gap (their cell has absolute time)
        time_cell = row.select_one("td.time")
        time_seconds = None
        if position == 1:
            time_seconds = 0
        elif time_cell:
            font = time_cell.find("font")
            raw_time = font.get_text(strip=True) if font else ""
            if raw_time == ",," or raw_time == "":
                time_seconds = 0   # same time as leader
            else:
                time_seconds = parse_time_gap(raw_time)

        # Points
        uci_pnt = _cell_int(row.select_one("td.uci_pnt"))
        pcs_pnt = _cell_int(row.select_one("td.pnt"))
        bib = _cell_int(row.select_one("td.bibs"))

        results.append({
            "rider_slug": rider_slug,
            "rider_name": rider_name,
            "position": position,
            "status": status,
            "time_seconds": time_seconds,
            "points_pcs": pcs_pnt,
            "points_uci": uci_pnt,
            "bib": bib,
        })

    return results


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


def _cell_int(cell) -> int | None:
    if not cell:
        return None
    try:
        return int(re.sub(r"[^\d]", "", cell.get_text(strip=True)))
    except Exception:
        return None
