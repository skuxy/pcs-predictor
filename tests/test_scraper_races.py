"""Tests for race/stage parsing helpers."""
import pytest
from scraper.races import _parse_date_range, _parse_stage_date, _parse_float, _parse_int


class TestParseDateRange:
    def test_stage_race(self):
        start, end = _parse_date_range("16.01 - 21.01", 2024)
        assert start == "2024-01-16"
        assert end == "2024-01-21"

    def test_one_day_race(self):
        start, end = _parse_date_range("28.01", 2024)
        assert start == "2024-01-28"
        assert end == "2024-01-28"

    def test_december(self):
        start, end = _parse_date_range("07.12 - 15.12", 2023)
        assert start == "2023-12-07"
        assert end == "2023-12-15"


class TestParseStageDate:
    def test_standard(self):
        assert _parse_stage_date("16/01", 2024) == "2024-01-16"

    def test_single_digit_day(self):
        assert _parse_stage_date("03/07", 2024) == "2024-07-03"

    def test_none_year(self):
        result = _parse_stage_date("16/01", None)
        assert result is not None and result.endswith("-01-16")

    def test_invalid_returns_none(self):
        assert _parse_stage_date("not-a-date", 2024) is None


class TestParseNumeric:
    def test_float_with_km(self):
        assert _parse_float("144km") == 144.0

    def test_float_decimal(self):
        assert _parse_float("141.6") == 141.6

    def test_float_comma(self):
        assert _parse_float("141,6") == 141.6

    def test_int_with_spaces(self):
        assert _parse_int("3 200") == 3200

    def test_empty_returns_none(self):
        assert _parse_float("") is None
        assert _parse_int("") is None
