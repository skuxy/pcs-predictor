"""Scrape rider profiles from ProCyclingStats."""
import logging
import re

from scraper.utils import fetch, soup, pcs_url

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
    Verified PCS rider page structure:

    Name: <h1>Tadej Pogačar</h1>

    Info blocks — each lives in its own  div > ul.list > li:
      <li>
        <div class="bold mr5">Date of birth:</div>
        <div class="mr3">21st</div>
        <div class="mr3">September</div>
        <div class="mr3">1998</div>  ...
      </li>

    Weight/Height share one li:
      <div class="bold mr5">Weight:</div>
      <div class="mr3">66</div><div class="mr10">kg</div>
      <div class="bold mr5">Height:</div>
      <div class="mr3">1.76</div><div>m</div>

    Nationality:
      <div class="bold mr5">Nationality:</div>
      <div><span class="flag si"></span></div>
      <div><a href="nation/slovenia">Slovenia</a></div>

    Current team: found via <a href="team/..."> in the info section.

    Speciality bars — ul.pps.list > li each containing:
      div.xbar  (width class encodes score, e.g. "w93")
      div.xvalue  (numeric score)
      div.xtitle  (label: "Onedayraces", "GC", "TT", "Sprint", "Hills")
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

    # Parse all ul.list info blocks
    for li in s.select("ul.list li"):
        label_el = li.find("div", class_=re.compile(r"bold"))
        if not label_el:
            continue
        label = label_el.get_text(strip=True).rstrip(":").lower()
        siblings = label_el.find_next_siblings("div")
        values = [d.get_text(strip=True) for d in siblings]

        if "date of birth" in label or "born" in label:
            # values: ["21st", "September", "1998", "(", "27", ")"]
            data["dob"] = _parse_dob_parts(values)

        elif "nationality" in label:
            # Last div usually has the country link text
            for sib in reversed(siblings):
                txt = sib.get_text(strip=True)
                if txt:
                    data["nationality"] = txt
                    break

        elif "weight" in label:
            # "66" + "kg" — may also contain Height in the same li
            nums = [v for v in values if re.match(r"^\d", v)]
            if nums:
                data["weight_kg"] = _parse_float(nums[0])
            # Height follows: look for "m" unit
            height_idx = next((i for i, v in enumerate(values) if v == "m"), None)
            if height_idx and height_idx > 0:
                h = _parse_float(values[height_idx - 1])
                if h:
                    data["height_cm"] = round(h * 100, 1)

        elif "height" in label:
            nums = [v for v in values if re.match(r"^\d", v)]
            if nums:
                h = _parse_float(nums[0])
                if h and h < 3:   # stored as 1.76 m
                    data["height_cm"] = round(h * 100, 1)
                elif h:
                    data["height_cm"] = h

        elif "team" in label:
            a = li.find("a", href=re.compile(r"team/"))
            if a:
                data["team"] = a.get_text(strip=True)

    # Fallback team: first team/ link outside nav
    if not data["team"]:
        for a in s.select("a[href^='team/']"):
            href = a["href"]
            # skip nav links (they have year in them like team/uae-2026)
            if re.search(r"\d{4}$", href):
                data["team"] = a.get_text(strip=True)
                break

    # Speciality from the ul.pps.list bar chart
    data["speciality"] = _parse_speciality(s)

    return data


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
        "onedayraces": "classics",
        "gc": "gc",
        "tt": "tt",
        "sprint": "sprinter",
        "hills": "climber",
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
        if v == "(":
            break   # age in parentheses follows — stop here
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
