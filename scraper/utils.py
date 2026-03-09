import time
import hashlib
import pathlib
import logging
from typing import Optional

import requests
from bs4 import BeautifulSoup

from config import PCS_BASE, REQUEST_DELAY, REQUEST_TIMEOUT, MAX_RETRIES, CACHE_DIR

log = logging.getLogger(__name__)

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
)

_last_request: float = 0.0


def _cache_path(url: str) -> pathlib.Path:
    key = hashlib.md5(url.encode()).hexdigest()
    return pathlib.Path(CACHE_DIR) / f"{key}.html"


def fetch(url: str, use_cache: bool = True) -> Optional[str]:
    """Fetch a URL, respecting rate limits and using an HTML cache."""
    global _last_request

    cache_file = _cache_path(url)
    if use_cache and cache_file.exists():
        log.debug("cache hit: %s", url)
        return cache_file.read_text(encoding="utf-8")

    # Rate limit
    elapsed = time.time() - _last_request
    if elapsed < REQUEST_DELAY:
        time.sleep(REQUEST_DELAY - elapsed)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            _last_request = time.time()
            resp = SESSION.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            html = resp.text
            pathlib.Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)
            cache_file.write_text(html, encoding="utf-8")
            log.debug("fetched: %s", url)
            return html
        except requests.RequestException as exc:
            log.warning("attempt %d/%d failed for %s: %s", attempt, MAX_RETRIES, url, exc)
            if attempt < MAX_RETRIES:
                time.sleep(REQUEST_DELAY * attempt)

    log.error("giving up on %s", url)
    return None


def soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def pcs_url(path: str) -> str:
    return f"{PCS_BASE}/{path.lstrip('/')}"


def parse_time_gap(raw: str) -> Optional[int]:
    """Convert a PCS time gap string like '0:12' or '1:23:45' to seconds.

    Returns 0 for the leader, None if unparseable (DNF, etc.).
    """
    raw = raw.strip().lstrip("+").strip()
    if not raw or raw in {",,", "-", ""}:
        return None
    parts = raw.split(":")
    try:
        parts = [int(p) for p in parts]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        return int(parts[0])
    except ValueError:
        return None
