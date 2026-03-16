"""Scrape start lists from race pages."""
import logging
import re

from scraper.utils import fetch, soup, pcs_url

log = logging.getLogger(__name__)


def fetch_startlist(race_slug: str) -> list[dict]:
    """
    Return [{slug, name, team, bib}] for all starters in a race.

    PCS startlist page: race/NAME/YEAR/startlist
    Rider links appear as <a href="rider/SLUG">NAME</a>.
    """
    url = pcs_url(f"{race_slug.rstrip('/')}/startlist")
    html = fetch(url, use_cache=False)   # always fresh — roster can change
    if not html:
        log.warning("could not fetch startlist for %s", race_slug)
        return []

    s = soup(html)
    riders = []
    seen = set()

    # Restrict to the actual startlist container — avoids sidebar rider links
    container = s.find("ul", class_="startlist_v4") or s
    for a in container.find_all("a", href=re.compile(r"^rider/")):
        slug = a["href"].strip("/").replace("rider/", "")
        if slug in seen:
            continue
        seen.add(slug)

        name = a.get_text(separator=" ", strip=True)
        # Team name is often in a sibling or parent element
        team_el = a.find_parent("li")
        team = None
        if team_el:
            team_link = team_el.find("a", href=re.compile(r"^team/"))
            if team_link:
                team = team_link.get_text(strip=True)

        riders.append({"slug": slug, "name": name, "team": team, "bib": None})

    log.info("startlist for %s: %d riders", race_slug, len(riders))
    return riders
