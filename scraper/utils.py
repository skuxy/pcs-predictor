import os
import time
import atexit
import hashlib
import pathlib
import logging
import datetime
from contextlib import contextmanager
from typing import Optional

import requests
from bs4 import BeautifulSoup

from config import PCS_BASE, REQUEST_DELAY, REQUEST_TIMEOUT, MAX_RETRIES, CACHE_DIR

log = logging.getLogger(__name__)

LIVE_CACHE_TTL = 6 * 3600


def _is_live_url(url: str) -> bool:
    return "/startlist" in url

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
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "Referer": "https://www.procyclingstats.com/",
    }
)

_last_request: float = 0.0


def cache_path(url: str) -> pathlib.Path:
    key = hashlib.md5(url.encode()).hexdigest()
    return pathlib.Path(CACHE_DIR) / f"{key}.html"


# Keep underscore alias for any external callers during transition.
_cache_path = cache_path


# Set by pages_newer_than(): a cached page written on or before this ISO date
# is fetched again.  A page this process fetched less than LIVE_CACHE_TTL ago
# counts as fresh (a second read of the same page, for another fact, uses the
# copy just fetched; a UI server that stays up for days asks again after six
# hours); a fetch that failed is tried again at the next read.
_stale_through: Optional[str] = None
_fetched_at: dict[str, float] = {}


@contextmanager
def pages_newer_than(date: Optional[str]):
    """Inside the block, a cached page written on or before ``date`` (ISO) is
    fetched again and its cache entry replaced.

    Cached pages never expire, so a stage page stored before the stage was
    ridden would be served for ever: it shows the start list where the result
    will be (which parses as a cancelled stage) and a provisional route.  Wrap
    the fetch of a stage's result or route in ``pages_newer_than(stage date)``:
    only a copy fetched after that day is trusted.  A stage still to come is
    therefore fetched afresh by every run (once per run, however often the
    page is read; a process that stays up asks again after LIVE_CACHE_TTL).
    If the fetch fails the old copy is not served: the caller gets None, as
    for any page that cannot be fetched.  ``None`` as the date leaves the
    cache as it is.
    """
    global _stale_through
    previous, _stale_through = _stale_through, date
    try:
        yield
    finally:
        _stale_through = previous


def cached_on(url: str) -> Optional[str]:
    """ISO date on which the cached copy of ``url`` was written (None if not cached)."""
    cache_file = cache_path(url)
    if not cache_file.exists():
        return None
    return datetime.date.fromtimestamp(cache_file.stat().st_mtime).isoformat()


def _is_cloudflare_challenge(html: str) -> bool:
    """Return True if the page is a Cloudflare JS challenge rather than real content."""
    return (
        "<title>Just a moment" in html
        or "Checking if the site connection is secure" in html
        or ("Just a moment" in html and "challenge-platform" in html)
    )


# Once plain requests get a Cloudflare challenge, every later request in this
# process goes straight to the browser instead of paying for a doomed request.
_browser_only = False
# Lazily started (playwright, browser, page) shared by every browser fetch —
# launching Chromium per URL costs seconds, a page navigation costs ~1 s.
_browser = None


def _find_chromium_executable() -> Optional[str]:
    """Locate a Chromium binary when Playwright's own bundled build is missing.

    Pre-provisioned environments (CI images, cloud sandboxes) often ship a
    Chromium for a different Playwright release; any recent build passes the
    Cloudflare check. ``PCS_CHROMIUM_EXECUTABLE`` overrides the search.
    """
    override = os.environ.get("PCS_CHROMIUM_EXECUTABLE")
    if override:
        return override
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if not root:
        return None
    for pattern in ("chromium-*/chrome-linux*/chrome", "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium"):
        hits = sorted(pathlib.Path(root).glob(pattern), reverse=True)
        if hits:
            return str(hits[0])
    return None


def _launch_chromium(pw):
    kwargs = {"headless": True}
    # Chromium ignores HTTPS_PROXY; pass it explicitly so egress-proxied hosts work.
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        kwargs["proxy"] = {"server": proxy}
    try:
        return pw.chromium.launch(**kwargs)
    except Exception as exc:
        exe = _find_chromium_executable()
        if not exe:
            raise
        log.info("bundled chromium unavailable (%s) — using %s", str(exc).splitlines()[0], exe)
        return pw.chromium.launch(executable_path=exe, **kwargs)


def _browser_page():
    global _browser
    if _browser is None:
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        try:
            browser = _launch_chromium(pw)
        except Exception:
            pw.stop()
            raise
        context = browser.new_context(user_agent=SESSION.headers["User-Agent"], locale="en-GB")
        _browser = (pw, browser, context.new_page())
        atexit.register(close_browser)
    return _browser[2]


def close_browser() -> None:
    """Shut down the shared browser (safe to call repeatedly)."""
    global _browser
    if _browser is None:
        return
    pw, browser, _ = _browser
    _browser = None
    try:
        browser.close()
    finally:
        pw.stop()


def fetch_with_playwright(url: str) -> Optional[str]:
    """Fetch a URL using a headless Chromium browser (bypasses Cloudflare challenges).

    Reuses one browser page for the whole process; a crashed browser is
    restarted once before giving up on the URL.
    """
    try:
        import playwright  # noqa: F401
    except ImportError:
        log.warning("playwright not installed — cannot bypass Cloudflare challenge")
        return None

    log.info("playwright fetch: %s", url)
    for attempt in (1, 2):
        try:
            page = _browser_page()
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
            # For result pages: wait up to 5 s for the results table to have data rows
            try:
                page.wait_for_function(
                    "() => { var t = document.querySelector('table.results, table.basic'); "
                    "return t && t.querySelectorAll('tr').length > 1; }",
                    timeout=5_000,
                )
            except Exception:
                pass
            html = page.content()
            if _is_cloudflare_challenge(html):
                log.warning("playwright: still on Cloudflare challenge page for %s", url)
                return None
            return html
        except Exception as exc:
            log.warning("playwright fetch failed for %s (attempt %d): %s", url, attempt, exc)
            close_browser()
    return None


# Keep underscore alias for any external callers during transition.
_fetch_with_playwright = fetch_with_playwright


def fetch(url: str, use_cache: bool = True) -> Optional[str]:
    """Fetch a URL, respecting rate limits and using an HTML cache.

    Falls back to a headless Playwright browser if the server returns a
    Cloudflare challenge (403 / 'Just a moment...' page).  Cached pages never
    expire, except start lists (6 hours) and, inside ``pages_newer_than``,
    copies that predate the given day.
    """
    global _last_request, _browser_only

    cache_file = cache_path(url)
    if use_cache and cache_file.exists():
        if _is_live_url(url):
            age = time.time() - cache_file.stat().st_mtime
            if age <= LIVE_CACHE_TTL:
                log.debug("cache hit: %s", url)
                return cache_file.read_text(encoding="utf-8")
            cache_file.unlink()
            log.debug("cache expired (startlist): %s", url)
        elif (_stale_through and (cached_on(url) or "") <= _stale_through
              and time.time() - _fetched_at.get(url, 0.0) > LIVE_CACHE_TTL):
            log.info("cached copy of %s is from %s, not after %s — fetching it again",
                     url, cached_on(url), _stale_through)
        else:
            log.debug("cache hit: %s", url)
            return cache_file.read_text(encoding="utf-8")

    # Rate limit
    elapsed = time.time() - _last_request
    if elapsed < REQUEST_DELAY:
        time.sleep(REQUEST_DELAY - elapsed)

    html: Optional[str] = None
    if _browser_only:
        _last_request = time.time()
        html = fetch_with_playwright(url)
    else:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                _last_request = time.time()
                resp = SESSION.get(url, timeout=REQUEST_TIMEOUT)
                if resp.status_code == 403 or _is_cloudflare_challenge(resp.text):
                    log.info("Cloudflare challenge detected for %s — switching to playwright", url)
                    _browser_only = True
                    html = fetch_with_playwright(url)
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
        _fetched_at[url] = time.time()
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
