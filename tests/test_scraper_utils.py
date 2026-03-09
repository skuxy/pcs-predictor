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
