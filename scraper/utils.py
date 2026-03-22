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
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.4.1 Safari/605.1.15"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Referer": "https://www.procyclingstats.com/",
    }
)

_last_request: float = 0.0


def _cache_path(url: str) -> pathlib.Path:
    key = hashlib.md5(url.encode()).hexdigest()
    return pathlib.Path(CACHE_DIR) / f"{key}.html"


def _is_cloudflare_challenge(html: str) -> bool:
    """Return True if the page is a Cloudflare JS challenge rather than real content."""
    return "Just a moment" in html and "cf-mitigated" in html.lower() or \
           "Checking if the site connection is secure" in html


def _fetch_with_playwright(url: str) -> Optional[str]:
    """Fetch a URL using a headless Chromium browser (bypasses Cloudflare challenges)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("playwright not installed — cannot bypass Cloudflare challenge")
        return None

    log.info("playwright fetch: %s", url)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                    "Version/17.4.1 Safari/605.1.15"
                ),
                locale="en-GB",
            )
            page = context.new_page()
            # Use "domcontentloaded" — PCS keeps background XHRs open so "networkidle"
            # times out. After DOM is ready, wait up to 8 s for CF challenge to auto-resolve.
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            try:
                page.wait_for_function(
                    "() => !document.title.includes('Just a moment')",
                    timeout=8_000,
                )
            except Exception:
                pass  # if it times out we still try to grab whatever content is there
            html = page.content()
            browser.close()
            if _is_cloudflare_challenge(html):
                log.warning("playwright: still on Cloudflare challenge page for %s", url)
                return None
            return html
    except Exception as exc:
        log.warning("playwright fetch failed for %s: %s", url, exc)
        return None


def fetch(url: str, use_cache: bool = True) -> Optional[str]:
    """Fetch a URL, respecting rate limits and using an HTML cache.

    Falls back to a headless Playwright browser if the server returns a
    Cloudflare challenge (403 / 'Just a moment...' page).
    """
    global _last_request

    cache_file = _cache_path(url)
    if use_cache and cache_file.exists():
        log.debug("cache hit: %s", url)
        return cache_file.read_text(encoding="utf-8")

    # Rate limit
    elapsed = time.time() - _last_request
    if elapsed < REQUEST_DELAY:
        time.sleep(REQUEST_DELAY - elapsed)

    html: Optional[str] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            _last_request = time.time()
            resp = SESSION.get(url, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 403 or _is_cloudflare_challenge(resp.text):
                log.info("Cloudflare challenge detected for %s — switching to playwright", url)
                html = _fetch_with_playwright(url)
                break
            resp.raise_for_status()
            html = resp.text
            break
        except requests.RequestException as exc:
            log.warning("attempt %d/%d failed for %s: %s", attempt, MAX_RETRIES, url, exc)
            if attempt < MAX_RETRIES:
                time.sleep(REQUEST_DELAY * attempt)

    if html:
        pathlib.Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)
        cache_file.write_text(html, encoding="utf-8")
        log.debug("fetched: %s", url)
        return html

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
