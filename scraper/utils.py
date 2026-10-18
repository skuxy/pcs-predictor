import os
import time
import atexit
import hashlib
import pathlib
import logging
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
    Cloudflare challenge (403 / 'Just a moment...' page).
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
