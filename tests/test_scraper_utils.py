"""Tests for scraper utility functions."""
import pytest
from scraper.utils import parse_time_gap


class TestParseTimeGap:
    def test_leader_double_comma(self):
        assert parse_time_gap(",,") is None  # handled upstream as 0

    def test_zero_gap(self):
        assert parse_time_gap("0:00") == 0

    def test_seconds_only(self):
        assert parse_time_gap("0:12") == 12

    def test_minutes_seconds(self):
        assert parse_time_gap("1:23") == 83

    def test_hours_minutes_seconds(self):
        assert parse_time_gap("1:23:45") == 5025

    def test_plus_prefix(self):
        assert parse_time_gap("+0:45") == 45

    def test_whitespace(self):
        assert parse_time_gap("  0:30  ") == 30

    def test_dnf_returns_none(self):
        assert parse_time_gap("DNF") is None

    def test_empty_returns_none(self):
        assert parse_time_gap("") is None

    def test_dash_returns_none(self):
        assert parse_time_gap("-") is None


class TestStaleCache:
    """A cached page never expires — except inside pages_newer_than(day), where
    a copy written on or before that day is fetched again."""

    URL = "https://www.procyclingstats.com/race/some-race/2027/stage-3"

    @pytest.fixture
    def cache(self, tmp_path, monkeypatch):
        import os, time
        from scraper import utils

        monkeypatch.setattr(utils, "CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(utils, "REQUEST_DELAY", 0)
        monkeypatch.setattr(utils, "_browser_only", False)
        monkeypatch.setattr(utils, "_fetched_at", {})
        calls = []

        class Resp:
            status_code = 200
            text = "<html><body>result page</body></html>"
            def raise_for_status(self):
                pass

        monkeypatch.setattr(utils.SESSION, "get", lambda url, timeout=None: calls.append(url) or Resp())

        def put(html, day):
            f = utils.cache_path(self.URL)
            f.write_text(html, encoding="utf-8")
            ts = time.mktime(time.strptime(day + " 12:00", "%Y-%m-%d %H:%M"))
            os.utime(f, (ts, ts))
        return utils, calls, put

    def test_cache_hit_without_the_block(self, cache):
        utils, calls, put = cache
        put("<html>start list</html>", "2027-03-01")
        assert utils.fetch(self.URL) == "<html>start list</html>"
        assert calls == [] and utils.cached_on(self.URL) == "2027-03-01"

    def test_copy_from_before_the_stage_is_fetched_again(self, cache):
        utils, calls, put = cache
        put("<html>start list</html>", "2027-03-01")           # cached during the calendar scrape
        with utils.pages_newer_than("2027-03-10"):             # the stage was on 10 March
            assert "result page" in utils.fetch(self.URL)
        assert calls == [self.URL]
        assert "result page" in utils.cache_path(self.URL).read_text()   # the cache entry is replaced
        assert utils._stale_through is None

    def test_a_stale_page_is_fetched_once_per_run(self, cache):
        utils, calls, put = cache
        put("<html>start list</html>", "2027-03-01")
        with utils.pages_newer_than("2027-12-31"):             # a stage still to come: every copy predates it
            utils.fetch(self.URL)                              # the result ...
            assert "result page" in utils.fetch(self.URL)      # ... and the route, from the same page
        assert calls == [self.URL]

    def test_a_long_running_process_asks_again_after_six_hours(self, cache):
        """The UI server predicts the same race on Monday and on Thursday: the
        route PCS published in between must not be hidden by Monday's fetch."""
        utils, calls, put = cache
        put("<html>start list</html>", "2027-03-01")
        with utils.pages_newer_than("2027-12-31"):
            utils.fetch(self.URL)
            utils._fetched_at[self.URL] -= utils.LIVE_CACHE_TTL + 60      # … six hours later
            utils.fetch(self.URL)
            utils.fetch(self.URL)
        assert calls == [self.URL, self.URL]

    def test_a_failed_refetch_is_tried_again_and_the_old_copy_is_not_served(self, cache, monkeypatch):
        import requests
        utils, calls, put = cache
        put("<html>start list</html>", "2027-03-01")

        class Down:
            status_code = 500
            text = "Internal Server Error"
            def raise_for_status(self):
                raise requests.HTTPError("500")

        monkeypatch.setattr(utils.SESSION, "get", lambda url, timeout=None: calls.append(url) or Down())
        monkeypatch.setattr(utils, "MAX_RETRIES", 1)
        with utils.pages_newer_than("2027-03-10"):
            assert utils.fetch(self.URL) is None
            assert utils.fetch(self.URL) is None               # asked again, not answered from the old copy
        assert calls == [self.URL, self.URL]
        assert utils.fetch(self.URL) == "<html>start list</html>"   # outside the block the cache is as before

    def test_copy_from_the_stage_day_itself_is_not_trusted(self, cache):
        utils, calls, put = cache
        put("<html>start list</html>", "2027-03-10")
        with utils.pages_newer_than("2027-03-10"):
            utils.fetch(self.URL)
        assert calls == [self.URL]

    def test_copy_from_after_the_stage_is_used(self, cache):
        utils, calls, put = cache
        put("<html>the result</html>", "2027-03-11")
        with utils.pages_newer_than("2027-03-10"):
            assert utils.fetch(self.URL) == "<html>the result</html>"
        with utils.pages_newer_than(None):
            assert utils.fetch(self.URL) == "<html>the result</html>"
        assert calls == []

    def test_blocks_nest_and_restore(self, cache):
        utils, _, _ = cache
        with utils.pages_newer_than("2027-03-10"):
            with utils.pages_newer_than(None):
                assert utils._stale_through is None
            assert utils._stale_through == "2027-03-10"
        assert utils._stale_through is None

    def test_not_cached(self, cache):
        utils, calls, _ = cache
        assert utils.cached_on(self.URL) is None
        assert "result page" in utils.fetch(self.URL) and calls == [self.URL]
