"""Scrape rider profiles from ProCyclingStats."""
import datetime
import logging
import re

from scraper.utils import fetch, soup, pcs_url
from scraper.races import parse_label_pairs

log = logging.getLogger(__name__)


def fetch_rider(slug: str) -> dict | None:
    """Fetch and parse a rider profile. Returns None on failure."""
    url = pcs_url(f"rider/{slug}")
    html = fetch(url)
    if not html:
        return None
    s = soup(html)
    return _parse_rider(s, slug)


def _parse_rider(s, slug: str) -> dict:
    """
    Parse a PCS rider page.  Two markups are supported.

    Live markup (Oct 2026):
      Name:  <div class="page-title"><div class="title"><h1>Tadej Pogačar</h1></div>
             <div class="subtitle"><h2>UAE Team Emirates - XRG</h2></div></div>
             (the h2 is the rider's CURRENT team)
      Info:  one flat run of label/value divs, rows separated by <br>:
        <div class="bold mr5">Date of birth:</div><div class="mr4">21st</div>
        <div class="mr4">September 1998 (28)</div><br>
        <div class="bold mr5">Nationality:</div><div><span class="flag si"></span></div>
        <div><a href="nation/slovenia">Slovenia</a></div><br>
        <div class="bold mr5">Weight: </div><div>66</div><div>kg</div>
        <div class="bold mr5">Height: </div><div>1.76</div><div> m</div><br>
      Team history: ul.rdr-teams2 > li > div.season "2026" + div.name > a[href^="team/"]
                    (future contract seasons are listed too, e.g. "2027-2032").
      Rankings: ul.list.horizontal li > div > a "PCS Ranking" + div value.
      The site-wide nav menu also links every WorldTeam (team/xds-astana-team-2026 ...)
      — those links must never be read as the rider's team.

    Legacy markup: each label in its own ``ul.list > li`` with
    ``div.bold`` label + value divs (same label names), team under a "Team"
    label, ranking under a "... ranking" label.

    Speciality bars — ul.pps.list > li each containing:
      div.xbar  (width class encodes score, e.g. "w93")
      div.xvalue  (numeric score)
      div.xtitle  (label: "Oneday"/"Onedayraces", "GC", "TT", "Sprint",
                   "Climber", "Hills")
    """
    data: dict = {
        "pcs_slug": slug,
        "name": None,
        "nationality": None,
        "dob": None,
        "team": None,
        "pcs_rank": None,
        "speciality": None,
        "weight_kg": None,
        "height_cm": None,
    }

    h1 = s.find("h1")
    if h1:
        data["name"] = h1.get_text(strip=True)

    for label, els in parse_label_pairs(s).items():
        values = [e.get_text(" ", strip=True) for e in els]
        values = [v for v in values if v]

        if "date of birth" in label or label == "born":
            # values: ["21st", "September 1998 (28)"] or legacy
            # ["21st", "September", "1998", "(", "27", ")"]
            data["dob"] = _parse_dob_parts(" ".join(values).split())

        elif "nationality" in label:
            if values:
                data["nationality"] = values[-1]

        elif label.startswith("weight"):
            nums = [v for v in values if re.match(r"^\d", v)]
            if nums:
                data["weight_kg"] = _parse_float(nums[0])
            # Legacy single-li layout: "66", "kg", "Height:", "1.76", "m"
            height_idx = next((i for i, v in enumerate(values) if v == "m"), None)
            if height_idx and height_idx > 0 and data["height_cm"] is None:
                h = _parse_float(values[height_idx - 1])
                if h:
                    data["height_cm"] = round(h * 100, 1)

        elif label.startswith("height"):
            nums = [v for v in values if re.match(r"^\d", v)]
            if nums:
                h = _parse_float(nums[0])
                if h and h < 3:   # stored as 1.76 m
                    data["height_cm"] = round(h * 100, 1)
                elif h:
                    data["height_cm"] = h

        elif label in ("team", "current team"):
            for el in els:
                a = el if el.name == "a" else el.find("a", href=re.compile(r"^/?team/"))
                if a is not None:
                    data["team"] = a.get_text(strip=True)
                    break

        elif "ranking" in label and data["pcs_rank"] is None:
            data["pcs_rank"] = _rank_from_values(values)

    data["team"] = _current_team(s) or data["team"]

    if data["pcs_rank"] is None:
        data["pcs_rank"] = _pcs_rank_from_links(s)

    # Speciality from the ul.pps.list bar chart
    data["speciality"] = _parse_speciality(s)

    return data


def _current_team(s, season: int | None = None) -> str | None:
    """The rider's current team: the page subtitle (div.page-title h2), else
    the latest ``ul.rdr-teams2`` entry whose season is not in the future."""
    sub = s.select_one("div.page-title div.subtitle h2") or s.select_one("div.page-title h2")
    if sub is not None and sub.get_text(strip=True):
        return sub.get_text(strip=True)
    season = season or datetime.date.today().year
    best = None
    for li in s.select("ul.rdr-teams2 > li"):
        season_el = li.select_one("div.season")
        a = li.find("a", href=re.compile(r"^/?team/"))
        if season_el is None or a is None:
            continue
        years = [int(y) for y in re.findall(r"\d{4}", season_el.get_text())]
        if not years or min(years) > season:
            continue
        start = min(years)
        if best is None or start > best[0]:
            best = (start, a.get_text(strip=True))
    return best[1] if best else None


def _rank_from_values(values: list[str]) -> int | None:
    for v in values:
        clean = re.sub(r"[^\d]", "", v)
        if clean:
            try:
                rank = int(clean)
            except ValueError:
                continue
            if 0 < rank < 10000:
                return rank
    return None


def _pcs_rank_from_links(s) -> int | None:
    """Live markup: <li><div><a href="rankings/me/individual">PCS Ranking</a></div>
    <div class="bg grey2 bold ...">1</div></li>."""
    for a in s.find_all("a", href=re.compile(r"rankings/")):
        if a.get_text(strip=True).lower() != "pcs ranking":
            continue
        li = a.find_parent("li")
        if li is None:
            continue
        holder = a.find_parent("div")
        values = [d.get_text(strip=True) for d in li.find_all("div") if d is not holder]
        rank = _rank_from_values(values)
        if rank:
            return rank
    return None


def _parse_speciality(s) -> str | None:
    """
    ul.pps.list contains one li per speciality category.
    Each li has:
      div.xbar > div.valuebar > div.wNN  (NN = score out of 100)
      div.xvalue  (raw numeric score)
      div.xtitle  (label: Onedayraces / GC / TT / Sprint / Hills)

    We take the category with the highest numeric score.
    """
    best_label = None
    best_score = -1

    for li in s.select("ul.pps li"):
        score_el = li.select_one("div.xvalue")
        title_el = li.select_one("div.xtitle")
        if not score_el or not title_el:
            continue
        try:
            score = int(re.sub(r"[^\d]", "", score_el.get_text()))
        except Exception:
            continue
        if score > best_score:
            best_score = score
            best_label = title_el.get_text(strip=True).lower()

    if not best_label:
        return None

    mapping = {
        "oneday": "classics",       # "Onedayraces" (legacy) / "Oneday" (live)
        "gc": "gc",
        "tt": "tt",
        "sprint": "sprinter",
        "hills": "climber",
        "climber": "climber",
    }
    for key, val in mapping.items():
        if key in best_label:
            return val
    return best_label


def _parse_dob_parts(values: list[str]) -> str | None:
    """['21st', 'September', '1998', '(', '27', ')'] → '1998-09-21'

    Stop parsing at '(' to avoid treating the age as the day.
    """
    months = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
    }
    day = month = year = None
    for v in values:
        if v.startswith("("):
            break   # age in parentheses follows — stop here ("(" or "(28)")
        if re.match(r"^\d{4}$", v):
            year = int(v)
        elif v.lower() in months:
            month = months[v.lower()]
        elif re.match(r"^\d{1,2}[a-z]*$", v):
            try:
                day = int(re.sub(r"[^\d]", "", v))
            except Exception:
                pass
    if day and month and year:
        return f"{year}-{month:02d}-{day:02d}"
    return None


def _parse_float(s: str) -> float | None:
    try:
        return float(re.sub(r"[^\d.]", "", str(s)))
    except Exception:
        return None
