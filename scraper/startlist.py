"""Scrape start lists from race pages."""
import logging
import re

from scraper.utils import fetch, soup, pcs_url

log = logging.getLogger(__name__)

_TEAM_HREF = re.compile(r"^/?team/")
_RIDER_HREF = re.compile(r"^/?rider/")


def parse_startlist(html: str) -> list[dict]:
    """Parse a PCS start list page into [{slug, name, team, team_slug, bib}].

    Rider links are ``<a href="rider/SLUG">NAME</a>`` inside
    ``ul.startlist_v4``; each sits in its own ``<li>`` (with a
    ``span.bib`` once bibs are assigned) under the team's block, which
    carries the ``<a href="team/SLUG">`` link.  ``team`` / ``team_slug`` /
    ``bib`` are None when the page does not show them.
    """
    s = soup(html)
    riders = []
    seen = set()

    # Restrict to the actual startlist container — avoids sidebar rider links
    container = s.find("ul", class_="startlist_v4") or s
    for a in container.find_all("a", href=_RIDER_HREF):
        # 'rider/SLUG', or 'rider/SLUG/road' for riders with several disciplines
        slug = a["href"].strip("/").replace("rider/", "", 1).split("?")[0].split("/")[0]
        if not slug or slug in seen:
            continue
        seen.add(slug)

        name = a.get_text(separator=" ", strip=True)

        bib = None
        rider_li = a.find_parent("li")
        if rider_li is not None:
            bib_el = rider_li.find(class_="bib")
            if bib_el is not None:
                digits = re.sub(r"\D", "", bib_el.get_text())
                bib = int(digits) if digits else None

        # The team link belongs to the team block around the rider's <li>.
        team = team_slug = None
        for parent in a.parents:
            if parent is container.parent:
                break
            team_link = parent.find("a", href=_TEAM_HREF)
            if team_link is not None:
                team = team_link.get_text(strip=True) or None
                team_slug = team_link["href"].strip("/").replace("team/", "", 1) or None
                break

        riders.append({"slug": slug, "name": name, "team": team, "team_slug": team_slug, "bib": bib})
    return riders


def fetch_startlist(race_slug: str) -> list[dict]:
    """
    Return [{slug, name, team, team_slug, bib}] for all starters in a race.

    PCS startlist page: race/NAME/YEAR/startlist
    """
    url = pcs_url(f"{race_slug.rstrip('/')}/startlist")
    html = fetch(url, use_cache=False)   # always fresh — roster can change
    if not html:
        log.warning("could not fetch startlist for %s", race_slug)
        return []

    riders = parse_startlist(html)
    log.info("startlist for %s: %d riders", race_slug, len(riders))
    return riders
