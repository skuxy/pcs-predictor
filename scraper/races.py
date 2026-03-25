"""Scrape race schedules and stage lists from ProCyclingStats."""
import logging
import re
from typing import Iterator

from scraper.utils import fetch, soup, pcs_url
from config import SCRAPE_YEARS, MEN_CIRCUITS, COBBLED_RACE_SLUGS, GRAVEL_RACE_SLUGS

log = logging.getLogger(__name__)


def iter_races(
    years: list[int] = SCRAPE_YEARS,
    circuits: dict[str, list[str]] | None = None,
) -> Iterator[dict]:
    """Yield race metadata dicts for all configured years and circuits.

    ``circuits`` maps PCS circuit number → list of allowed race class codes.
    Defaults to ``MEN_CIRCUITS`` from config.
    """
    if circuits is None:
        circuits = MEN_CIRCUITS
    seen_slugs: set[str] = set()
    for year in years:
        for circuit, race_classes in circuits.items():
            url = pcs_url(f"races.php?year={year}&circuit={circuit}&class=")
            html = fetch(url)
            if not html:
                continue
            for race in _parse_race_list(html, year, race_classes):
                if race["pcs_slug"] not in seen_slugs:
                    seen_slugs.add(race["pcs_slug"])
                    yield race


def _parse_race_list(html: str, year: int, race_classes: list[str]) -> Iterator[dict]:
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
            "gender": _detect_gender(link.get_text(strip=True), race_class),
        }


_WOMEN_NAME_RE = re.compile(
    r"\b(women|ladies|femina|femenina|féminin|vrouwen|dames|donne|femmes|mujeres)\b|\bWE\b",
    re.IGNORECASE,
)

def _detect_gender(name: str, race_class: str) -> str:
    """Return 'women' if the race name or class signals a women's event."""
    if "WWT" in race_class:
        return "women"
    if _WOMEN_NAME_RE.search(name):
        return "women"
    return "men"


def _surface_for_race(race_slug: str) -> str:
    """Return 'cobbled', 'gravel', or 'road' based on the race slug."""
    for pattern in COBBLED_RACE_SLUGS:
        if pattern in race_slug:
            return "cobbled"
    for pattern in GRAVEL_RACE_SLUGS:
        if pattern in race_slug:
            return "gravel"
    return "road"


def fetch_result_meta(stage_slug: str) -> dict:
    """
    Visit {stage_slug}/result and return a dict with:
      - profile_type: mapped from p1-p5 span class (flat/hilly/mountain), or None
      - gradient_final_km: float from "gradient final km" info field, or None
      - profile_score: int from "profilescore" info field, or None

    Works for both one-day races and multi-stage race stage pages.
    Returns an empty dict on HTTP failure.
    """
    try:
        result_html = fetch(pcs_url(f"{stage_slug}/result"))
        if not result_html:
            return {}
        s = soup(result_html)
        info = _parse_infolist(s)

        # gradient final km
        grad_raw = info.get("gradient final km", "")
        gradient_final_km = None
        if grad_raw:
            try:
                gradient_final_km = float(re.sub(r"[^\d.]", "", grad_raw.replace(",", ".")))
            except (ValueError, TypeError):
                pass

        # profile score
        score_raw = info.get("profilescore", "")
        profile_score = None
        if score_raw:
            try:
                profile_score = int(re.sub(r"[^\d]", "", score_raw))
            except (ValueError, TypeError):
                pass

        # profile type from p-icon span
        profile_type = None
        mapping = {"p1": "flat", "p2": "hilly", "p3": "hilly",
                   "p4": "mountain", "p5": "mountain"}
        span = s.find("span", class_=re.compile(r"profile|icon"))
        if span:
            cls = " ".join(span.get("class", []))
            for key, val in mapping.items():
                if key in cls:
                    profile_type = val
                    break

        return {
            "gradient_final_km": gradient_final_km,
            "profile_score":     profile_score,
            "profile_type":      profile_type,
        }
    except Exception:
        return {}


def fetch_stage_elevation(stage_slug: str) -> int | None:
    """
    Fetch a stage's detail page and extract elevation gain (vertical meters).
    For multi-stage races the stage page has a div.title/div.value pair.
    For one-day races the overview page lacks elevation; fall back to /result.
    Returns None if not found or page unavailable.
    """
    def _elev_from_soup(s) -> int | None:
        for div in s.find_all("div", class_="title"):
            if "vertical" in div.get_text(strip=True).lower():
                val = div.find_next_sibling("div", class_="value")
                if val:
                    return _parse_int(val.get_text(strip=True))
        info = _parse_infolist(s)
        return _parse_int(info.get("vertical meters", "") or info.get("altitude difference", ""))

    html = fetch(pcs_url(stage_slug))
    if html:
        elev = _elev_from_soup(soup(html))
        if elev:
            return elev

    # One-day races: overview page has no elevation — try /result page
    if not any(part in stage_slug for part in ["/stage-", "/prologue", "/itt"]):
        result_html = fetch(pcs_url(f"{stage_slug}/result"))
        if result_html:
            return _elev_from_soup(soup(result_html))

    return None


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

    # Overview page uses "Startdate:"/"Total distance:"; result page uses "Date:"/"Distance:".
    # Always fetch the /result page for gradient/profile_score/profile_type,
    # and use it as date fallback if overview didn't yield a usable date.
    result_html = fetch(pcs_url(f"{race_slug}/result"))
    result_info = {}
    result_profile_type = None
    gradient_final_km = None
    profile_score = None

    if result_html:
        result_s = soup(result_html)
        result_info = _parse_infolist(result_s)

        # gradient final km
        grad_raw = result_info.get("gradient final km", "")
        if grad_raw:
            try:
                gradient_final_km = float(re.sub(r"[^\d.]", "", grad_raw.replace(",", ".")))
            except (ValueError, TypeError):
                pass

        # profile score
        score_raw = result_info.get("profilescore", "")
        if score_raw:
            try:
                profile_score = int(re.sub(r"[^\d]", "", score_raw))
            except (ValueError, TypeError):
                pass

        # profile type from p-icon span on result page
        mapping = {"p1": "flat", "p2": "hilly", "p3": "hilly",
                   "p4": "mountain", "p5": "mountain"}
        span = result_s.find("span", class_=re.compile(r"profile|icon"))
        if span:
            cls = " ".join(span.get("class", []))
            for key, val in mapping.items():
                if key in cls:
                    result_profile_type = val
                    break

        # Use result_info as date fallback if overview didn't give a date
        if not info.get("date") and not info.get("startdate"):
            info = result_info

    raw_date = info.get("date") or info.get("startdate")
    iso_date = _normalise_date(raw_date)

    # profile_type: prefer overview value, fall back to result page icon
    overview_profile = info.get("parcours type", "").lower() or None
    profile_type = overview_profile or result_profile_type

    return [{
        "pcs_slug": race_slug,
        "stage_num": None,
        "date": iso_date,
        "distance_km": _parse_float(
            info.get("distance", "") or info.get("total distance", "")
        ),
        "elevation_m": _parse_int(info.get("vertical meters", "")),
        "profile_type": profile_type,
        "surface": _surface_for_race(race_slug),
        "departure": info.get("departure") or info.get("start"),
        "arrival": info.get("arrival") or info.get("finish"),
        "gpx_path": None,
        "gradient_final_km": gradient_final_km,
        "profile_score":     profile_score,
    }]


def _parse_infolist(s) -> dict:
    """Parse a ul.list info section into a flat dict (label → value).

    PCS uses div.title/div.value pairs inside ul.list (both race overview and
    result pages).  Older code looked for div.bold which is only on rider pages.
    """
    info = {}
    for li in s.select("ul.list li"):
        label_el = li.find("div", class_="title")
        if not label_el:
            continue
        label = label_el.get_text(strip=True).rstrip(":").lower()
        val_el = label_el.find_next_sibling("div", class_="value")
        value = val_el.get_text(strip=True) if val_el else ""
        if label and value:
            info[label] = value
    return info


def _normalise_date(raw: str | None) -> str | None:
    """Convert various PCS date formats to ISO YYYY-MM-DD."""
    if not raw:
        return None
    # Already ISO: "2026-02-28"
    if re.match(r"\d{4}-\d{2}-\d{2}", raw):
        return raw[:10]
    # "28 February 2026" / "1 March 2026"
    try:
        from datetime import datetime
        return datetime.strptime(raw.strip(), "%d %B %Y").strftime("%Y-%m-%d")
    except ValueError:
        pass
    return None


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
