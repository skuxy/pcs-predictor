"""Scrape race schedules and stage lists from ProCyclingStats.

PCS URL styles
--------------
The DB keys races and stages by the *path* style
(``race/tour-de-france/2026``, ``race/tour-de-france/2026/stage-4``).  Since
mid-2026 PCS links use a *hyphen* style instead
(``race/tour-de-france-2026-stage-4``, ``race/il-lombardia-2026-result``).
Both URL styles are served; links scraped from pages are normalised back to
the path style (:func:`normalize_race_href`) so they keep matching existing
rows.

Stage lists
-----------
* Legacy markup: the race overview had a stages table
  (Date | Day | profile icon | Stage | KM).
* Live markup (Oct 2026): the race overview URL renders the *final stage*
  results page (GC tab visible).  Stage races are recognised by the stage
  ``<select>`` on that page (options ``race/<name>-<year>-stage-N`` /
  ``-prologue``); the stage list itself is read from
  ``race/<name>/<year>/gc/stages`` ("Stage winners and leaders":
  Date (MM/DD) | profile icon | Stage link | winner | leader).  That table has
  no distance; ``distance_km`` etc. come from each stage page
  (:func:`fetch_stage_meta`).

Race info block
---------------
Legacy: ``ul.list li > div.title + div.value``.  Live: a ``div.lineh16``
of ``div.bold`` labels ("Date", "Distance:", "Parcours type:", "Vertical
meters:", ...) each followed by value divs up to the next ``<br>``.
"""
import logging
import re
from datetime import date, datetime
from typing import Iterator

from scraper.utils import fetch, soup, pcs_url
from config import SCRAPE_YEARS, MEN_CIRCUITS, WOMEN_CIRCUIT, COBBLED_RACE_SLUGS, GRAVEL_RACE_SLUGS

log = logging.getLogger(__name__)

# Maps PCS profile-icon CSS classes to profile type strings.
_PROFILE_MAP = {
    "p1": "flat", "p2": "hilly", "p3": "hilly",
    "p4": "mountain", "p5": "mountain",
    "itt": "itt", "utt": "ttt", "ttt": "ttt",
}

_STAGE_SUFFIX_RE = re.compile(r"^(stage-\d+[a-z]?|prologue)$")


def _profile_from_classes(classes) -> str | None:
    """Exact CSS-class-token lookup (``icon profile p2 mg_rp4`` → hilly).

    Substring matching is wrong here: ``mg_rp4`` contains ``p4``.
    """
    for cls in classes or []:
        if cls in _PROFILE_MAP:
            return _PROFILE_MAP[cls]
    return None


def _profile_from_span(s) -> str | None:
    """Return profile type from the first profile icon inside ``s``.

    Prefers ``span.icon.profile`` (stage tables, race info block); unrelated
    icon spans (``iconv3 insta14`` social links) are ignored.
    """
    for span in s.select("span.profile"):
        prof = _profile_from_classes(span.get("class"))
        if prof:
            return prof
    for span in s.find_all("span", class_=re.compile(r"\bicon\b")):
        prof = _profile_from_classes(span.get("class"))
        if prof:
            return prof
    return None


def normalize_race_href(href: str, race_slug: str | None = None) -> str:
    """Convert a PCS race link to the path-style slug used in the DB.

    ``race/tour-de-france-2026-stage-1`` → ``race/tour-de-france/2026/stage-1``
    ``race/il-lombardia-2026-result``    → ``race/il-lombardia/2026/result``
    Path-style links are returned unchanged (minus slashes / query).  When the
    race slug is known its name is used as the prefix, so names that contain
    digits or year-like tokens are split correctly.
    """
    href = href.split("?")[0].split("#")[0].strip().strip("/")
    if race_slug:
        race_slug = race_slug.strip("/")
        m = re.match(r"^race/(.+)/(\d{4})$", race_slug)
        if m:
            prefix = f"race/{m.group(1)}-{m.group(2)}"
            if href == prefix:
                return race_slug
            if href.startswith(prefix + "-"):
                return f"{race_slug}/{href[len(prefix) + 1:]}"
    if re.match(r"^race/[^/]+/\d{4}(/|$)", href):
        return href
    m = re.match(r"^race/(.+?)-(\d{4})(?:-(.+))?$", href)
    if m:
        out = f"race/{m.group(1)}/{m.group(2)}"
        return f"{out}/{m.group(3)}" if m.group(3) else out
    return href


def is_missing_page(html: str | None) -> bool:
    """True for PCS's answer to an unknown slug: the browser renders an empty
    document (``<html><head></head><body></body></html>``, no <title>).

    ``None`` (the fetch failed) is NOT a missing page: a network error says
    nothing about the slug, and treating it as one sent callers looking for a
    renamed race (see ``find_race_alias``) on every transient failure.
    """
    if html is None:
        return False
    if not html.strip():
        return True
    return len(html) < 2000 and re.search(r"<body[^>]*>\s*</body>", html, re.I) is not None


def find_race_alias(race_slug: str, start_date: str | None, end_date: str | None,
                    race_class: str | None, name: str | None = None) -> str | None:
    """Find the current PCS slug of a race whose stored slug went dead.

    PCS renames races (2026: ``race/dauphine`` → ``race/tour-auvergne-rhone-alpes``,
    ``race/international-tour-of-hellas`` → ``race/tour-of-hellas``); the old
    URLs then return an empty page.  The season calendars (``races.php``, one
    page per circuit, cached) are searched for a race of the same class with
    the same start and end date; with several candidates the one with the same
    name wins, otherwise nothing is returned.  When the stored slug itself is
    still listed in a calendar the race was not renamed (its page is merely
    unavailable) and nothing is returned either — a same-class race on the
    same dates (GP Lillers / Drôme Classic, both 1.1 on 2026-03-01) is not an
    alias.
    """
    year = _year_from_slug(race_slug)
    if not (year and start_date and end_date and race_class):
        return None
    circuits = list(dict.fromkeys([*MEN_CIRCUITS, WOMEN_CIRCUIT]))
    candidates: dict[str, str] = {}
    for circuit in circuits:
        html = fetch(pcs_url(f"races.php?year={year}&circuit={circuit}&class="))
        if not html:
            continue
        for race in _parse_race_list(html, year, [race_class]):
            if race["pcs_slug"] == race_slug:
                log.info("race %s is still in the %d calendar — not renamed", race_slug, year)
                return None
            if race["class"] != race_class:
                continue
            if race["start_date"] == start_date and race["end_date"] == end_date:
                candidates[race["pcs_slug"]] = race["name"]
    if len(candidates) == 1:
        return next(iter(candidates))
    same_name = [slug for slug, n in candidates.items() if name and n == name]
    return same_name[0] if len(same_name) == 1 else None


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
      2: race name + flag  <a href="race/NAME/YEAR/gc|result"> (or the
         hyphen style race/NAME-YEAR-gc)
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
        href = normalize_race_href(link.get("href", ""))
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
            "gender": _detect_gender(link.get_text(strip=True), race_class, slug),
        }


_WOMEN_NAME_RE = re.compile(
    r"\b(women|women's|ladies|femina|feminas|femenina|femenino|féminin|féminine|feminine|fèmines|femines"
    r"|vrouwen|dames|donne|femmes|mujeres)\b|\bWE\b",
    re.IGNORECASE,
)
# PCS race slugs of women's editions: 'clasica-de-almeria-we', 'strade-bianche-donne',
# 'vuelta-a-burgos-feminas', 'giro-dell-emilia-internazionale-donne-elite'
_WOMEN_SLUG_RE = re.compile(r"(^|-)(we|women|donne|feminas|femenina|femmes|feminin|feminine|ladies)(-|$)")


def gender_from_category(category: str | None) -> str | None:
    """'women' / 'men' from a PCS "Race category" ('WE' Women Elite, 'ME' Men
    Elite, 'MU' Men U23, 'WJ' ...), None when unknown."""
    cat = (category or "").strip().upper()
    if cat.startswith("W"):
        return "women"
    if cat.startswith("M"):
        return "men"
    return None


def _detect_gender(name: str, race_class: str, slug: str | None = None) -> str:
    """Return 'women' if the race name, slug or class signals a women's event.

    Only a first guess from the calendar row: the men's calendar circuits
    (16 above all) list women's races of the same classes (2.1, 1.Pro ...),
    some with no marker in the name at all (Tour El Salvador, GP Oetingen,
    GP Lucien Van Impe).  Every result page states the real one ("Race
    category: WE"), and the scrape paths correct the stored gender from it
    (main._sync_race_gender).
    """
    if "WWT" in race_class:
        return "women"
    if _WOMEN_NAME_RE.search(name):
        return "women"
    name_part = (slug or "").strip("/").split("/")
    name_part = name_part[1] if len(name_part) > 1 and name_part[0] == "race" else name_part[0]
    if slug and _WOMEN_SLUG_RE.search(name_part):
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


# ── stage / race info block ──────────────────────────────────────────────────

def stage_meta_from_soup(s) -> dict:
    """Parse the race info block of a stage / one-day result page.

    Returns date (ISO), distance_km, elevation_m, profile_type,
    gradient_final_km, profile_score, departure, arrival, race_class,
    points_scale (e.g. 'GT.A.Stage', '1.WT.A') and race_category ('WE',
    'ME', ...); missing fields are None.
    profile_type comes from the parcours icon (p1 flat, p2/p3 hilly, p4/p5
    mountain) unless "Won how" / the points scale ('GT.A.TTT', '2.WT.ITT')
    says (team) time trial.
    """
    info = _parse_infolist(s)
    pairs = parse_label_pairs(s)

    points_scale = info.get("points scales") or info.get("points scale")
    won_how = info.get("won how", "").lower()
    scale = (points_scale or "").upper()
    profile_type = None
    if "team time trial" in won_how or scale.endswith("TTT"):
        profile_type = "ttt"
    elif "time trial" in won_how or scale.endswith("ITT"):
        profile_type = "itt"
    if profile_type is None:
        for el in pairs.get("parcours type", []):
            span = el if el.name == "span" else el.find("span", class_="profile")
            if span is not None:
                profile_type = _profile_from_classes(span.get("class"))
                if profile_type:
                    break

    return {
        "date": _normalise_date(info.get("date") or info.get("startdate")),
        "distance_km": _parse_float(info.get("distance", "") or info.get("total distance", "")),
        "elevation_m": _parse_int(info.get("vertical meters", "") or info.get("altitude difference", "")),
        "profile_type": profile_type,
        "gradient_final_km": _parse_float(info.get("gradient final km", "")),
        # "316 (final: 139 )" — the first number is the race profile score
        "profile_score": _parse_int((info.get("profilescore", "") or info.get("profile score", "")).split("(")[0]),
        "departure": info.get("departure") or info.get("start"),
        "arrival": info.get("arrival") or info.get("finish"),
        "race_class": info.get("classification"),
        "points_scale": points_scale,
        # 'WE' (Women Elite), 'ME' (Men Elite), ...: the race's real gender,
        # which the calendar row does not always show (gender_from_category)
        "race_category": info.get("race category"),
    }


def fetch_stage_meta(stage_slug: str) -> dict:
    """Fetch (normally a cache hit) a stage / one-day page and return
    :func:`stage_meta_from_soup` for it; {} if the page is unavailable."""
    html = fetch(pcs_url(stage_slug))
    if not html:
        return {}
    try:
        return stage_meta_from_soup(soup(html))
    except Exception as exc:  # defensive: never break a scrape over metadata
        log.warning("could not parse stage meta for %s: %s", stage_slug, exc)
        return {}


def fetch_result_meta(stage_slug: str) -> dict:
    """
    Return a dict with:
      - profile_type: mapped from the p1-p5 parcours icon (flat/hilly/mountain),
        or 'itt' when "Won how" mentions a time trial; None if unknown
      - gradient_final_km: float from "gradient final km" info field, or None
      - profile_score: int from "profile score" info field, or None

    Reads the stage / race page itself first (live markup carries the info
    block there and it is usually cached by the results scrape), then the
    legacy ``{stage_slug}/result`` page.  Returns an empty dict on HTTP failure.
    """
    out: dict = {}
    for url in (pcs_url(stage_slug), pcs_url(f"{stage_slug}/result")):
        try:
            html = fetch(url)
            if not html:
                continue
            meta = stage_meta_from_soup(soup(html))
        except Exception:
            continue
        for key in ("gradient_final_km", "profile_score", "profile_type"):
            if out.get(key) is None:
                out[key] = meta.get(key)
        if out.get("gradient_final_km") is not None or out.get("profile_score") is not None:
            break
    return out


def fetch_stage_elevation(stage_slug: str) -> int | None:
    """
    Fetch a stage's detail page and extract elevation gain (vertical meters).
    Legacy stage pages have a div.title/div.value pair; live pages carry it in
    the race info block.  For one-day races fall back to /result.
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


# ── stage lists ──────────────────────────────────────────────────────────────

def fetch_race_stages(race_slug: str) -> list[dict]:
    """
    Return a list of stage dicts for a race (one pseudo-stage for a one-day
    race).

    1. Legacy overview with a stages table (Date | Day | icon | Stage | KM).
    2. Live overview: if its stage ``<select>`` lists stages, read
       ``{race_slug}/gc/stages``; if that page fails, fall back to the select
       options (no dates).
    3. Otherwise a one-day race (:func:`_single_day_stage`).
    """
    url = pcs_url(race_slug)
    html = fetch(url)
    if not html:
        return []
    if is_missing_page(html):
        log.warning("PCS returned an empty page for %s (unknown or renamed race slug)", race_slug)
        return []

    s = soup(html)
    surface = _surface_for_race(race_slug)
    year = _year_from_slug(race_slug)
    # The stages table has header containing "Date" and "KM"
    stage_table = _find_stages_table(s)
    if stage_table:
        return _parse_stages_table(stage_table, race_slug, year, surface=surface)

    options = _stage_options(s, race_slug)
    if options:
        stages = _fetch_stage_list_page(race_slug, year, surface)
        if stages:
            return stages
        log.warning("stage list page unavailable for %s — using the stage <select> (no dates)", race_slug)
        return _stages_from_options(options, surface)

    # One-day race — use the /result page info
    return _single_day_stage(race_slug, s, surface=surface)


def _stage_options(s, race_slug: str) -> list[tuple[str, str]]:
    """(path-style stage slug, label) for every stage option in the page's
    ``<select>`` elements that belongs to ``race_slug``, in page order."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    prefix = race_slug.strip("/") + "/"
    for opt in s.select("select option"):
        value = opt.get("value") or ""
        if not value.startswith("race/"):
            continue
        slug = normalize_race_href(value, race_slug)
        if not slug.startswith(prefix) or not _STAGE_SUFFIX_RE.match(slug[len(prefix):]):
            continue
        if slug not in seen:
            seen.add(slug)
            out.append((slug, opt.get_text(" ", strip=True)))
    return out


def _fetch_stage_list_page(race_slug: str, year: int | None, surface: str) -> list[dict]:
    html = fetch(pcs_url(f"{race_slug}/gc/stages"))
    if not html:
        return []
    table = _find_stage_list_table(soup(html))
    if table is None:
        return []
    return _parse_stage_list_table(table, race_slug, year, surface=surface)


def _find_stage_list_table(s):
    """The "Stage winners and leaders" table: header has Date and Stage."""
    for table in s.find_all("table"):
        first = table.find("tr")
        headers = [th.get_text(strip=True).lower() for th in first.find_all("th")] if first else []
        if "date" in headers and "stage" in headers:
            return table
    return None


def _parse_stage_list_table(table, race_slug: str, year: int | None, surface: str = "road") -> list[dict]:
    """
    Live ``{race}/gc/stages`` table (Oct 2026):
      th: Date | (icon) | Stage | Stage winner | Leader after stage
      td[0]: "07/04"  — MONTH/DAY (legacy overview tables used DAY/MONTH;
             the order is resolved per table, see _resolve_stage_dates)
      td[1]: <span class="icon profile p3 mg_rp4">
      td[2]: <a href="race/tour-de-france-2026-stage-1">Stage 1 (TTT) | Barcelona - Barcelona</a>
    A "KM" column is used when present.  stage_num is the 1-based ordinal
    position in the list (prologue = 1, stage 3a/3b consecutive).
    """
    header = [th.get_text(strip=True).lower() for th in table.find("tr").find_all("th")]
    km_idx = header.index("km") if "km" in header else None
    prefix = race_slug.strip("/") + "/"

    rows = []
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        link = None
        for cell in cells:
            a = cell.find("a", href=re.compile(r"^/?race/"))
            if a is not None:
                link = a
                break
        if link is None:
            continue
        slug = normalize_race_href(link.get("href", ""), race_slug)
        if not slug.startswith(prefix) or not _STAGE_SUFFIX_RE.match(slug[len(prefix):]):
            continue
        date_raw = next((c.get_text(strip=True) for c in cells
                         if re.match(r"^\d{1,2}[/.]\d{1,2}$", c.get_text(strip=True))), None)
        icon_prof = _profile_from_span(row)
        km = None
        if km_idx is not None and km_idx < len(cells):
            km = _parse_float(cells[km_idx].get_text(strip=True))
        rows.append((slug, link.get_text(" ", strip=True), date_raw, icon_prof, km))

    dates = _resolve_stage_dates([r[2] for r in rows], year)
    stages = []
    for i, ((slug, text, _, icon_prof, km), iso) in enumerate(zip(rows, dates), start=1):
        stages.append(_stage_dict(slug, i, iso, text, icon_prof, km, surface))
    return stages


def _stages_from_options(options: list[tuple[str, str]], surface: str) -> list[dict]:
    return [_stage_dict(slug, i, None, text, None, None, surface)
            for i, (slug, text) in enumerate(options, start=1)]


def _stage_dict(slug, stage_num, iso_date, text, icon_prof, km, surface) -> dict:
    """Build a stage dict from a stage label like
    'Stage 10 (ITT) | Town - Town' / 'Prologue | Town - Town'."""
    profile_type = icon_prof
    # PCS puts (ITT)/(TTT) in the stage name but often a generic icon.
    if "(ITT)" in text or text.lower().startswith("prologue"):
        profile_type = "itt"
    elif "(TTT)" in text:
        profile_type = "ttt"
    departure, arrival = _towns(text)
    return {
        "pcs_slug": slug,
        "stage_num": stage_num,
        "date": iso_date,
        "distance_km": km,
        "elevation_m": None,          # backfilled from the stage page / scrape-elevation
        "profile_type": profile_type,
        "surface": surface,
        "departure": departure,
        "arrival": arrival,
        "gpx_path": None,
    }


def _towns(text: str) -> tuple[str | None, str | None]:
    parts = text.split("|", 1)
    if len(parts) == 2:
        towns = parts[1].strip().split(" - ", 1)
        if len(towns) == 2:
            return towns[0].strip(), towns[1].strip()
    return None, None


def _resolve_stage_dates(raws: list[str | None], year: int | None) -> list[str | None]:
    """Turn 'NN/NN' strings into ISO dates, deciding DAY/MONTH vs MONTH/DAY
    for the whole table: a value > 12 settles it; otherwise the reading whose
    dates are non-decreasing and span the fewest days wins (ties → MONTH/DAY,
    the live PCS format)."""
    pairs = []
    for raw in raws:
        m = re.match(r"^\s*(\d{1,2})[/.](\d{1,2})\s*$", raw or "")
        pairs.append((int(m.group(1)), int(m.group(2))) if m else None)
    valid = [p for p in pairs if p]
    if year is None or not valid:
        return [None for _ in pairs]

    def as_date(p, order):
        a, b = p
        month, day = (a, b) if order == "md" else (b, a)
        try:
            return date(year, month, day)
        except ValueError:
            return None

    def cost(order):
        ds = [as_date(p, order) for p in valid]
        if any(d is None for d in ds):
            return float("inf")
        backwards = sum(1 for x, y in zip(ds, ds[1:]) if y < x)
        return backwards * 1000 + (max(ds) - min(ds)).days

    if any(a > 12 for a, _ in valid):
        order = "dm"
    elif any(b > 12 for _, b in valid):
        order = "md"
    else:
        order = min(("md", "dm"), key=cost)
    out = []
    for p in pairs:
        d = as_date(p, order) if p else None
        out.append(d.isoformat() if d else None)
    return out


def _find_stages_table(s):
    """Find the table whose header contains 'Date' and 'KM' (stage list)."""
    for table in s.find_all("table"):
        header_text = table.find("tr").get_text() if table.find("tr") else ""
        if "Date" in header_text and "KM" in header_text:
            return table
    return None


def _parse_stages_table(table, race_slug: str, year: int, surface: str = "road") -> list[dict]:
    """
    Legacy overview stage table row structure:
      td[0]: date  "16/01"  (DAY/MONTH)
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

        link = cells[3].find("a")
        if not link:
            continue

        href = normalize_race_href(link.get("href", ""), race_slug)
        profile_type = _profile_from_icon(cells[2])

        # date: "16/01" → year inferred from race slug
        date_str = _parse_stage_date(cells[0].get_text(strip=True), year)

        # "Stage 1 | Tanunda - Tanunda" or "Stage 10 (ITT) | Town - Town"
        text = link.get_text(strip=True)

        # Override profile_type for time trials — PCS puts (ITT)/(TTT) in the
        # stage name but uses the generic p1 icon, so the icon-based detection
        # always produces "flat" for TT stages.
        if "(ITT)" in text:
            profile_type = "itt"
        elif "(TTT)" in text:
            profile_type = "ttt"

        departure, arrival = _towns(text)

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


def _single_day_stage(race_slug: str, s, surface: str | None = None) -> list[dict]:
    """Build a single pseudo-stage entry for a one-day race.

    Live markup: the race URL renders the result page itself, info block
    included, so no extra request is needed.  Legacy overview pages lacked
    the date/gradient/profile score — then ``{race_slug}/result`` fills the
    gaps.
    """
    if surface is None:
        surface = _surface_for_race(race_slug)
    meta = stage_meta_from_soup(s)

    if not meta.get("date") or (meta.get("gradient_final_km") is None and meta.get("profile_score") is None):
        result_html = fetch(pcs_url(f"{race_slug}/result"))
        if result_html:
            result_meta = stage_meta_from_soup(soup(result_html))
            for key, val in result_meta.items():
                if meta.get(key) is None:
                    meta[key] = val

    return [{
        "pcs_slug": race_slug,
        "stage_num": None,
        "date": meta.get("date"),
        "distance_km": meta.get("distance_km"),
        "elevation_m": meta.get("elevation_m"),
        "profile_type": meta.get("profile_type"),
        "surface": surface,
        "departure": meta.get("departure"),
        "arrival": meta.get("arrival"),
        "gpx_path": None,
        "gradient_final_km": meta.get("gradient_final_km"),
        "profile_score":     meta.get("profile_score"),
    }]


def parse_label_pairs(s) -> dict[str, list]:
    """``div.bold`` label → list of the value elements that follow it.

    Live PCS info blocks (race and rider pages) are flat runs of
    ``<div class="bold mr5">Label:</div><div>value</div>...<br>``; a label's
    values are its element siblings up to the next ``<br>`` or the next bold
    label.  Labels are lower-cased without the trailing colon; the first
    occurrence of a label wins.
    """
    pairs: dict[str, list] = {}
    for label_el in s.find_all("div", class_="bold"):
        text = label_el.get_text(" ", strip=True)
        if not text or len(text) > 40:
            continue
        label = text.rstrip(":").strip().lower()
        values = []
        for sib in label_el.next_siblings:
            name = getattr(sib, "name", None)
            if name is None:
                continue  # text node
            if name == "br" or (name == "div" and "bold" in (sib.get("class") or [])):
                break
            values.append(sib)
        if values and label not in pairs:
            pairs[label] = values
    return pairs


def _parse_infolist(s) -> dict:
    """Parse the race info section into a flat dict (label → value text).

    Legacy: ``ul.list li`` with ``div.title`` / ``div.value`` pairs.  Live:
    ``div.bold`` label runs (see :func:`parse_label_pairs`); multi-part values
    are joined with spaces ("181.9 km").  Legacy keys win on conflict.
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
    for label, els in parse_label_pairs(s).items():
        value = " ".join(t for t in (e.get_text(" ", strip=True) for e in els) if t)
        if label and value and label not in info:
            info[label] = value
    if "profile score" in info and "profilescore" not in info:
        info["profilescore"] = info["profile score"]
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
    """'16/01' → '2024-01-16' (legacy DAY/MONTH overview format)"""
    try:
        day, month = raw.split("/")
        y = year or "????"
        return f"{y}-{int(month):02d}-{int(day):02d}"
    except Exception:
        return None


def _profile_from_icon(cell) -> str | None:
    """Read the profile type from the icon span class in the stages table."""
    return _profile_from_span(cell)


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
