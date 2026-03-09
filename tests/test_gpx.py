"""Tests for GPX parsing and climb detection."""
import math
import pytest
from scraper.gpx import (
    _haversine,
    _elevation_gain,
    _elevation_loss,
    _moving_average,
    detect_climbs,
)


class TestHaversine:
    def test_same_point_is_zero(self):
        assert _haversine((48.0, 2.0, 0), (48.0, 2.0, 0)) == pytest.approx(0.0)

    def test_known_distance(self):
        # Paris to London is ~340 km
        paris  = (48.8566, 2.3522, 0)
        london = (51.5074, -0.1278, 0)
        dist = _haversine(paris, london)
        assert 330 < dist < 350

    def test_symmetric(self):
        a = (46.0, 6.0, 0)
        b = (47.0, 7.0, 0)
        assert _haversine(a, b) == pytest.approx(_haversine(b, a))


class TestElevation:
    def test_gain_flat(self):
        points = [(0, 0, 100), (0, 1, 100), (0, 2, 100)]
        assert _elevation_gain(points) == 0.0

    def test_gain_uphill(self):
        points = [(0, 0, 0), (0, 1, 50), (0, 2, 150)]
        assert _elevation_gain(points) == pytest.approx(150.0)

    def test_loss_downhill(self):
        points = [(0, 0, 200), (0, 1, 100), (0, 2, 50)]
        assert _elevation_loss(points) == pytest.approx(150.0)

    def test_gain_ignores_descents(self):
        points = [(0, 0, 0), (0, 1, 100), (0, 2, 50), (0, 3, 200)]
        assert _elevation_gain(points) == pytest.approx(250.0)  # 100 + 150


class TestMovingAverage:
    def test_constant_series(self):
        result = _moving_average([5.0] * 10, window=3)
        assert all(v == pytest.approx(5.0) for v in result)

    def test_length_preserved(self):
        values = list(range(20))
        result = _moving_average(values, window=5)
        assert len(result) == len(values)

    def test_smoothing(self):
        # A spike should be smoothed out
        values = [0.0] * 5 + [100.0] + [0.0] * 5
        result = _moving_average(values, window=5)
        assert result[5] < 100.0  # spike is reduced


class TestDetectClimbs:
    def _flat_points(self, n=50, ele=100.0):
        return [(45.0 + i * 0.001, 6.0, ele) for i in range(n)]

    def _climb_points(self, length=50, start_ele=500.0, gain=500.0):
        """Simulate a steady climb."""
        return [
            (45.0 + i * 0.001, 6.0, start_ele + gain * i / length)
            for i in range(length)
        ]

    def test_flat_no_climbs(self):
        points = self._flat_points()
        climbs = detect_climbs(points)
        assert climbs == []

    def test_climb_detected(self):
        # Continuous profile: 20 flat points then 60 points climbing 600m
        # (must be continuous — no elevation jump at the boundary)
        flat   = [(45.0 + i * 0.001, 6.0, 100.0) for i in range(20)]
        climb  = [(45.0 + (20 + i) * 0.001, 6.0, 100.0 + 600.0 * i / 60)
                  for i in range(60)]
        points = flat + climb
        climbs = detect_climbs(points)
        assert len(climbs) >= 1

    def test_climb_has_required_fields(self):
        points = self._flat_points(20) + self._climb_points(60, gain=600)
        climbs = detect_climbs(points)
        if climbs:
            c = climbs[0]
            assert "start_km" in c
            assert "length_km" in c
            assert "elevation_gain_m" in c
            assert "avg_gradient_pct" in c

    def test_small_bump_ignored(self):
        # Tiny 30m gain continuous from flat — below min_gain_m threshold
        flat = [(45.0 + i * 0.001, 6.0, 100.0) for i in range(20)]
        bump = [(45.0 + (20 + i) * 0.001, 6.0, 100.0 + 30.0 * i / 10) for i in range(10)]
        climbs = detect_climbs(flat + bump, min_gain_m=80)
        assert climbs == []
