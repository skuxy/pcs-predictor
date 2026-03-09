"""Tests for rider profile parsing."""
import pytest
from scraper.riders import _parse_dob_parts, _parse_speciality, _parse_float
from bs4 import BeautifulSoup


class TestParseDobParts:
    def test_standard(self):
        assert _parse_dob_parts(["21st", "September", "1998", "(", "27", ")"]) == "1998-09-21"

    def test_no_parentheses(self):
        assert _parse_dob_parts(["3rd", "January", "2000"]) == "2000-01-03"

    def test_single_digit_day(self):
        assert _parse_dob_parts(["1st", "May", "1985", "(", "40", ")"]) == "1985-05-01"

    def test_age_not_mistaken_for_day(self):
        # Age "27" should not overwrite day "21"
        result = _parse_dob_parts(["21st", "September", "1998", "(", "27", ")"])
        assert result.endswith("-21")

    def test_missing_data_returns_none(self):
        assert _parse_dob_parts(["September", "1998"]) is None

    def test_empty_returns_none(self):
        assert _parse_dob_parts([]) is None


class TestParseSpeciality:
    def _make_soup(self, items: list[tuple[str, int]]) -> BeautifulSoup:
        """Build a minimal PCS-style ul.pps.list from (label, score) pairs."""
        lis = ""
        for label, score in items:
            lis += f"""
            <li>
              <div class="xbar"><div class="valuebar"><div class="w{score} bg left"></div></div></div>
              <div class="xvalue ac">{score * 100}</div>
              <div class="xtitle"><a href="#">{label}</a></div>
            </li>"""
        html = f'<ul class="pps list">{lis}</ul>'
        return BeautifulSoup(html, "lxml")

    def test_climber_wins(self):
        s = self._make_soup([("GC", 85), ("Sprint", 20), ("TT", 40)])
        assert _parse_speciality(s) == "gc"

    def test_sprinter_wins(self):
        s = self._make_soup([("Sprint", 92), ("GC", 30), ("Hills", 25)])
        assert _parse_speciality(s) == "sprinter"

    def test_classics_wins(self):
        s = self._make_soup([("Onedayraces", 90), ("GC", 60)])
        assert _parse_speciality(s) == "classics"

    def test_tt_wins(self):
        s = self._make_soup([("TT", 88), ("GC", 70)])
        assert _parse_speciality(s) == "tt"

    def test_empty_returns_none(self):
        s = BeautifulSoup("<div></div>", "lxml")
        assert _parse_speciality(s) is None


class TestParseFloat:
    def test_integer_string(self):
        assert _parse_float("66") == 66.0

    def test_decimal_string(self):
        assert _parse_float("1.76") == 1.76

    def test_with_unit(self):
        assert _parse_float("66 kg") == 66.0

    def test_empty_returns_none(self):
        assert _parse_float("") is None

    def test_non_numeric_returns_none(self):
        assert _parse_float("abc") is None
