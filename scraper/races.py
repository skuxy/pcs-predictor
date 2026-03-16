"""Scrape race schedules and stage lists from ProCyclingStats."""
import logging
import re
from typing import Iterator

from scraper.utils import fetch, soup, pcs_url
from config import SCRAPE_YEARS, RACE_CLASSES, WOMEN_RACE_CLASSES, WOMEN_CIRCUIT, \
    COBBLED_RACE_SLUGS, GRAVEL_RACE_SLUGS

log = logging.getLogger(__name__)


def iter_races(
    years: list[int] = SCRAPE_YEARS,
    race_classes: list[str] | None = None,
    circuit: str = "1",
) -> Iterator[dict]:
    """Yield race metadata dicts for all configured years and classes."""
    if race_classes is None:
        race_classes = RACE_CLASSES
    for year in years:
        url = pcs_url(f"races.php?year={year}&circuit={circuit}&class=")
        html = fetch(url)
        if not html:
            continue
        yield from _parse_race_list(html, year, race_classes)


def _parse_race_list(html: str, year: int, race_classes: list[str] = RACE_CLASSES) -> Iterator[dict]:
    """
    Calendar table columns (verified against live PCS HTML):
      0: date range  e.g. "16.01 - 21.01" or "28.01"
      1: date (hidden on mobile)
      2: race name + flag  <a href="race/NAME/YEAR/gc|result">
      3: winner
      4: class  e.g. "2.UWT"
    """
    s = soup(html)
    table = s.select_one("table.basic")
    if not table:
        log.warning("no race table found on calendar page")
        return

    for row in table.select("tbody tr, tr:not(:first-child)"):
        cells = row.find_all("td")
        if len(cells) < 5:
            continue

        link = cells[2].find("a")
        if not link:
            continue

        race_class = cells[4].get_text(strip=True)
        if not any(rc in race_class for rc in race_classes):
            continue

        # href: "race/tour-down-under/2024/gc" → strip trailing /gc or /result
        href = link.get("href", "").strip("/")
        slug = re.sub(r"/(gc|result|prologue)$", "", href)

        date_raw = cells[0].get_text(strip=True)   # "16.01 - 21.01" or "28.01"
        start_date, end_date = _parse_date_range(date_raw, year)
        is_stage = "-" in date_raw

        yield {
            "pcs_slug": slug,
            "name": link.get_text(strip=True),
            "year": year,
            "start_date": start_date,
            "end_date": end_date,
            "class": race_class,
            "country": _flag_country(cells[2]),
            "is_stage_race": 1 if is_stage else 0,
            "gender": "women" if "WWT" in race_class else "men",
        }


def _surface_for_race(race_slug: str) -> str:
    """Return 'cobbled', 'gravel', or 'road' based on the race slug."""
    for pattern in COBBLED_RACE_SLUGS:
        if pattern in race_slug:
            return "cobbled"
    for pattern in GRAVEL_RACE_SLUGS:
        if pattern in race_slug:
            return "gravel"
    return "road"


def fetch_stage_elevation(stage_slug: str) -> int | None:
    """
    Fetch a stage's detail page and extract elevation gain (vertical meters).
    Returns None if not found or page unavailable.
    """
    url = pcs_url(stage_slug)
    html = fetch(url)
    if not html:
        return None
    info = _parse_infolist(soup(html))
    return _parse_int(info.get("vertical meters", "") or info.get("altitude difference", ""))


def fetch_race_stages(race_slug: str) -> list[dict]:
    """
    Return a list of stage dicts from the race overview page.

    The race overview (no suffix) contains a stages table with header:
      Date | Day | (profile icon) | Stage link | KM
    """
    url = pcs_url(race_slug)
    html = fetch(url)
    if not html:
        return []

    s = soup(html)
    surface = _surface_for_race(race_slug)
    # The stages table has header containing "Date" and "KM"
    stage_table = _find_stages_table(s)
    if stage_table:
        year = _year_from_slug(race_slug)
        return _parse_stages_table(stage_table, race_slug, year, surface=surface)

    # One-day race — use the /result page info
    return _single_day_stage(race_slug, s)


def _find_stages_table(s):
    """Find the table whose header contains 'Date' and 'KM' (stage list)."""
    for table in s.find_all("table"):
        header_text = table.find("tr").get_text() if table.find("tr") else ""
        if "Date" in header_text and "KM" in header_text:
            return table
    return None


def _parse_stages_table(table, race_slug: str, year: int, surface: str = "road") -> list[dict]:
    """
    Stage table row structure (verified):
      td[0]: date  "16/01"
      td[1]: day name  "Tuesday"
      td[2]: profile icon span
      td[3]: <a href="race/.../stage-N">Stage N | Town - Town</a>
      td[4]: distance km
    """
    stages = []
    for i, row in enumerate(table.find_all("tr")[1:], start=1):
        cells = row.find_all("td")
        if len(cells) < 4:
            continue

        link = cells[3].find("a") if len(cells) > 3 else None
        if not link:
            continue

        href = link.get("href", "").strip("/")
        profile_type = _profile_from_icon(cells[2])

        # date: "16/01" → year inferred from race slug
        date_str = _parse_stage_date(cells[0].get_text(strip=True), year)

        # "Stage 1 | Tanunda - Tanunda" → split on |
        text = link.get_text(strip=True)
        parts = text.split("|", 1)
        departure, arrival = None, None
        if len(parts) == 2:
            towns = parts[1].strip().split(" - ", 1)
            if len(towns) == 2:
                departure, arrival = towns[0].strip(), towns[1].strip()

        stages.append({
            "pcs_slug": href,
            "stage_num": i,
            "date": date_str,
            "distance_km": _parse_float(cells[4].get_text(strip=True)) if len(cells) > 4 else None,
            "elevation_m": None,          # not on this page; backfilled via scrape-elevation
            "profile_type": profile_type,
            "surface": surface,
            "departure": departure,
            "arrival": arrival,
            "gpx_path": None,
        })
    return stages


def _single_day_stage(race_slug: str, s) -> list[dict]:
    """Build a single pseudo-stage entry for a one-day race."""
    info = _parse_infolist(s)
    return [{
        "pcs_slug": race_slug,
        "stage_num": None,
        "date": info.get("date"),
        "distance_km": _parse_float(info.get("distance", "")),
        "elevation_m": _parse_int(info.get("vertical meters", "")),
        "profile_type": info.get("parcours type", "").lower() or None,
        "surface": _surface_for_race(race_slug),
        "departure": info.get("departure") or info.get("start"),
        "arrival": info.get("arrival") or info.get("finish"),
        "gpx_path": None,
    }]


def _parse_infolist(s) -> dict:
    """Parse a ul.list info section into a flat dict (label → value)."""
    info = {}
    for li in s.select("ul.list li"):
        label_el = li.find("div", class_="bold")
        if not label_el:
            continue
        label = label_el.get_text(strip=True).rstrip(":").lower()
        # Value = all sibling divs after the label
        siblings = label_el.find_next_siblings("div")
        value = " ".join(d.get_text(strip=True) for d in siblings).strip()
        info[label] = value
    return info


# ── helpers ──────────────────────────────────────────────────────────────────

def _year_from_slug(slug: str) -> int | None:
    m = re.search(r"/(\d{4})", slug)
    return int(m.group(1)) if m else None


def _parse_date_range(raw: str, year: int):
    """'16.01 - 21.01' → ('2024-01-16', '2024-01-21')"""
    parts = [p.strip() for p in raw.split("-")]
    def fmt(p):
        try:
            day, month = p.split(".")
            return f"{year}-{int(month):02d}-{int(day):02d}"
        except Exception:
            return None
    if len(parts) == 2:
        return fmt(parts[0]), fmt(parts[1])
    return fmt(parts[0]), fmt(parts[0])


def _parse_stage_date(raw: str, year: int | None) -> str | None:
    """'16/01' → '2024-01-16'"""
    try:
        day, month = raw.split("/")
        y = year or "????"
        return f"{y}-{int(month):02d}-{int(day):02d}"
    except Exception:
        return None


def _profile_from_icon(cell) -> str | None:
    """Read the profile type from the icon span class in the stages table."""
    span = cell.find("span", class_=re.compile(r"profile|icon"))
    if not span:
        return None
    cls = " ".join(span.get("class", []))
    mapping = {"p1": "flat", "p2": "hilly", "p3": "hilly",
               "p4": "mountain", "p5": "mountain", "itt": "itt", "utt": "utt"}
    for key, val in mapping.items():
        if key in cls:
            return val
    return None


def _flag_country(cell) -> str | None:
    span = cell.find("span", class_=re.compile(r"^flag "))
    if not span:
        return None
    classes = span.get("class", [])
    for cls in classes:
        if cls != "flag":
            return cls.upper()
    return None


def _parse_float(s: str) -> float | None:
    try:
        # Replace comma decimal separator before stripping non-numeric chars
        return float(re.sub(r"[^\d.]", "", s.replace(",", ".")))
    except Exception:
        return None


def _parse_int(s: str) -> int | None:
    try:
        return int(re.sub(r"[^\d]", "", s))
    except Exception:
        return None
