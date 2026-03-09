"""Parse GPX files and extract cycling-relevant features."""
import logging
import math
from pathlib import Path

import gpxpy
import gpxpy.gpx

log = logging.getLogger(__name__)


def parse_gpx(path: str | Path) -> dict:
    """Parse a GPX file and return a summary dict with climb features."""
    path = Path(path)
    with path.open() as f:
        gpx = gpxpy.parse(f)

    points = []
    for track in gpx.tracks:
        for segment in track.segments:
            for pt in segment.points:
                points.append((pt.latitude, pt.longitude, pt.elevation or 0.0))

    if not points:
        log.warning("no track points in %s", path)
        return {}

    return {
        "total_distance_km": _total_distance(points),
        "total_elevation_gain_m": _elevation_gain(points),
        "total_elevation_loss_m": _elevation_loss(points),
        "max_elevation_m": max(p[2] for p in points),
        "min_elevation_m": min(p[2] for p in points),
        "climbs": detect_climbs(points),
    }


def detect_climbs(
    points: list[tuple],
    min_length_km: float = 2.0,
    min_gain_m: float = 80.0,
    smoothing_window: int = 10,
) -> list[dict]:
    """Detect significant climbs in a point sequence.

    Returns a list of climb dicts sorted by start_km.
    """
    if len(points) < smoothing_window * 2:
        return []

    # Build cumulative distance and smooth elevation
    cum_dist = [0.0]
    for i in range(1, len(points)):
        cum_dist.append(cum_dist[-1] + _haversine(points[i - 1], points[i]))

    elevations = [p[2] for p in points]
    smoothed = _moving_average(elevations, smoothing_window)

    climbs = []
    in_climb = False
    climb_start_idx = 0

    def _check_and_add(start: int, end: int) -> None:
        length_km = cum_dist[end] - cum_dist[start]
        gain = smoothed[end] - smoothed[start]
        if length_km >= min_length_km and gain >= min_gain_m:
            gradients = _segment_gradients(smoothed, cum_dist, start, end)
            climbs.append(
                {
                    "start_km": round(cum_dist[start], 2),
                    "end_km": round(cum_dist[end], 2),
                    "length_km": round(length_km, 2),
                    "elevation_gain_m": round(gain, 0),
                    "avg_gradient_pct": round(gain / (length_km * 10), 2),
                    "max_gradient_pct": round(max(gradients), 2) if gradients else None,
                }
            )

    for i in range(1, len(smoothed)):
        rising = smoothed[i] > smoothed[i - 1]

        if rising and not in_climb:
            in_climb = True
            climb_start_idx = i - 1
        elif not rising and in_climb:
            _check_and_add(climb_start_idx, i)
            in_climb = False

    # Flush any climb that runs to the end of the data
    if in_climb:
        _check_and_add(climb_start_idx, len(smoothed) - 1)

    return climbs


# ── internal helpers ──────────────────────────────────────────────────────────

def _haversine(a: tuple, b: tuple) -> float:
    """Return distance in km between two (lat, lon, ele) points."""
    R = 6371.0
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(h))


def _total_distance(points: list) -> float:
    return sum(_haversine(points[i - 1], points[i]) for i in range(1, len(points)))


def _elevation_gain(points: list) -> float:
    return sum(
        max(0, points[i][2] - points[i - 1][2]) for i in range(1, len(points))
    )


def _elevation_loss(points: list) -> float:
    return sum(
        max(0, points[i - 1][2] - points[i][2]) for i in range(1, len(points))
    )


def _moving_average(values: list[float], window: int) -> list[float]:
    result = []
    for i in range(len(values)):
        start = max(0, i - window // 2)
        end = min(len(values), i + window // 2 + 1)
        result.append(sum(values[start:end]) / (end - start))
    return result


def _segment_gradients(
    elevations: list[float], cum_dist: list[float], start: int, end: int
) -> list[float]:
    grads = []
    for i in range(start + 1, end):
        dist_m = (cum_dist[i] - cum_dist[i - 1]) * 1000
        if dist_m > 0:
            grads.append((elevations[i] - elevations[i - 1]) / dist_m * 100)
    return grads
