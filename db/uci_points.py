"""Storage for the official UCI team ranking and the dated rider results behind it.

Tables (the data contract shared with ``analysis.team_points``):

* ``uci_team_totals``      — official listing pages (``ranking`` =
                             'rankings/me/ranking-2026-2028'); the scraper also
                             stores each breakdown page's own season total
                             under ``ranking`` = 'team-breakdown/ranking-2026-2028'
                             (the listing is a snapshot taken earlier in the day)
* ``uci_team_breakdown``   — team/<slug>/results/ranking-2026-2028, one row per rider
* ``rider_uci_results``    — rider/<slug>/results rows for one season (plus GC
                             leader-jersey points, see below)
* ``rider_uci_penalties``  — UCI sanction points from rider/<slug>/info/fines-penalties
* ``rider_team_spells``    — which team a rider rode for when (mid-season transfers)

Plus one table outside the contract, used only for reconciliation:

* ``uci_team_recent_points`` — team/<slug>/results/latest-uci-points: the team's
                               UCI points per day for the last ~30 days

Slug conventions (match the rest of the DB): ``rider_slug`` is the bare PCS rider
slug (``tadej-pogacar``, same as ``riders.pcs_slug``); ``team_slug`` is the bare
PCS team slug WITH the season suffix (``uae-team-emirates-xrg-2026``);
``race_slug`` is ``race/<slug>/<year>`` (same as ``races.pcs_slug``);
``result_url`` is the PCS href exactly as the results page links it
(``race/tour-de-france-2026-stage-6``).

GC leader-jersey points: UCI awards points to the GC leader after every stage
of a stage race (Grand Tours 25, WorldTour stage races 10, ...).  PCS counts
them in the team ranking but does NOT list them on rider/<slug>/results; they
come from the rider's UCI world-ranking breakdown page and are stored as
``result_kind='other'``, ``status='leader'``,
``result_label='Leader jersey after Stage N'`` rows.  The same page adds the
few items UCI counts in the season although they are dated in the previous
calendar year (national championships held in late October), stored with
their real date.  See ``scraper.uci_points.merge_season_rows``.

Every write is an upsert, so re-running a scrape is idempotent.
"""
from __future__ import annotations

import sqlite3
from typing import Iterable

UCI_POINTS_DDL = """
CREATE TABLE IF NOT EXISTS uci_team_totals (
    ranking TEXT NOT NULL,
    ranking_date TEXT NOT NULL,
    rank INTEGER, team_slug TEXT NOT NULL, team_name TEXT, team_class TEXT, points REAL,
    fetched_at TEXT DEFAULT (datetime('now')),
    UNIQUE(ranking, ranking_date, team_slug));
CREATE TABLE IF NOT EXISTS uci_team_breakdown (
    season INTEGER NOT NULL, team_slug TEXT NOT NULL, team_name TEXT, team_class TEXT,
    rider_slug TEXT NOT NULL, rider_name TEXT, nth_best INTEGER,
    points_counted REAL, points_not_counted REAL, sanction_points REAL,
    fetched_at TEXT DEFAULT (datetime('now')),
    UNIQUE(season, team_slug, rider_slug));
CREATE TABLE IF NOT EXISTS rider_uci_results (
    season INTEGER NOT NULL, rider_slug TEXT NOT NULL, rider_name TEXT,
    date TEXT NOT NULL, race_name TEXT, race_slug TEXT,
    result_label TEXT,
    result_kind TEXT NOT NULL,
    race_class TEXT, position INTEGER, status TEXT,
    points_uci REAL, points_pcs REAL,
    result_url TEXT NOT NULL,
    fetched_at TEXT DEFAULT (datetime('now')),
    UNIQUE(season, rider_slug, date, result_url, result_label));
CREATE TABLE IF NOT EXISTS rider_uci_penalties (
    season INTEGER NOT NULL, rider_slug TEXT NOT NULL, date TEXT NOT NULL, race_name TEXT,
    penalty_points REAL NOT NULL,
    fine TEXT, article TEXT,
    UNIQUE(season, rider_slug, date, race_name));
CREATE TABLE IF NOT EXISTS rider_team_spells (
    season INTEGER NOT NULL, rider_slug TEXT NOT NULL, team_slug TEXT NOT NULL, team_name TEXT,
    start_date TEXT, end_date TEXT,
    UNIQUE(season, rider_slug, team_slug));
CREATE TABLE IF NOT EXISTS uci_team_recent_points (
    season INTEGER NOT NULL, team_slug TEXT NOT NULL, date TEXT NOT NULL,
    points REAL, indirect_points REAL,
    fetched_at TEXT DEFAULT (datetime('now')),
    UNIQUE(season, team_slug, date));
CREATE INDEX IF NOT EXISTS idx_rider_uci_results_rider ON rider_uci_results(season, rider_slug);
CREATE INDEX IF NOT EXISTS idx_rider_uci_results_date ON rider_uci_results(season, date);
CREATE INDEX IF NOT EXISTS idx_uci_team_breakdown_rider ON uci_team_breakdown(season, rider_slug);
"""

# status / label prefix that mark a GC leader-jersey row
LEADER_STATUS = "leader"
LEADER_LABEL_PREFIX = "Leader jersey"


def ensure_uci_points_schema(conn: sqlite3.Connection) -> None:
    """Create the UCI-points tables and indexes if they do not exist (idempotent)."""
    conn.executescript(UCI_POINTS_DDL)


def upsert_team_totals(conn: sqlite3.Connection, ranking: str, ranking_date: str,
                       rows: Iterable[dict]) -> int:
    """Upsert official listing rows (keys: rank, team_slug, team_name, team_class, points)."""
    n = 0
    for r in rows:
        conn.execute(
            """
            INSERT INTO uci_team_totals (ranking, ranking_date, rank, team_slug, team_name,
                                         team_class, points, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(ranking, ranking_date, team_slug) DO UPDATE SET
                rank = excluded.rank, team_name = excluded.team_name,
                team_class = excluded.team_class, points = excluded.points,
                fetched_at = excluded.fetched_at
            """,
            (ranking, ranking_date, r.get("rank"), r["team_slug"], r.get("team_name"),
             r.get("team_class"), r.get("points")),
        )
        n += 1
    return n


def upsert_team_breakdown(conn: sqlite3.Connection, team_slug: str, team_name: str | None,
                          team_class: str | None, rows: Iterable[dict],
                          replace: bool = True) -> int:
    """Upsert a team's per-rider breakdown rows.

    With ``replace`` (default) the team's existing rows for every season present
    in ``rows`` are deleted first, so a rider who dropped off the page does not
    linger.  An empty ``rows`` never deletes anything.
    """
    rows = list(rows)
    if replace and rows:
        for season in {r["season"] for r in rows}:
            conn.execute("DELETE FROM uci_team_breakdown WHERE season = ? AND team_slug = ?",
                         (season, team_slug))
    for r in rows:
        conn.execute(
            """
            INSERT INTO uci_team_breakdown (season, team_slug, team_name, team_class, rider_slug,
                rider_name, nth_best, points_counted, points_not_counted, sanction_points,
                fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(season, team_slug, rider_slug) DO UPDATE SET
                team_name = excluded.team_name, team_class = excluded.team_class,
                rider_name = excluded.rider_name, nth_best = excluded.nth_best,
                points_counted = excluded.points_counted,
                points_not_counted = excluded.points_not_counted,
                sanction_points = excluded.sanction_points, fetched_at = excluded.fetched_at
            """,
            (r["season"], team_slug, team_name, team_class, r["rider_slug"], r.get("rider_name"),
             r.get("nth_best"), r.get("points_counted"), r.get("points_not_counted"),
             r.get("sanction_points")),
        )
    return len(rows)


def replace_rider_results(conn: sqlite3.Connection, season: int, rider_slug: str,
                          rows: Iterable[dict]) -> int:
    """Store one rider's complete set of season rows, replacing what was there.

    ``rows`` is the full merged set (results-page rows + leader-jersey and
    other ranking-only rows, see ``scraper.uci_points.merge_season_rows``).
    An empty ``rows`` is treated as "nothing scraped" and deletes nothing.
    """
    rows = list(rows)
    if rows:
        conn.execute("DELETE FROM rider_uci_results WHERE season = ? AND rider_slug = ?",
                     (season, rider_slug))
    for r in rows:
        conn.execute(
            """
            INSERT INTO rider_uci_results (season, rider_slug, rider_name, date, race_name,
                race_slug, result_label, result_kind, race_class, position, status,
                points_uci, points_pcs, result_url, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(season, rider_slug, date, result_url, result_label) DO UPDATE SET
                rider_name = excluded.rider_name, race_name = excluded.race_name,
                race_slug = excluded.race_slug, result_kind = excluded.result_kind,
                race_class = excluded.race_class, position = excluded.position,
                status = excluded.status, points_uci = excluded.points_uci,
                points_pcs = excluded.points_pcs, fetched_at = excluded.fetched_at
            """,
            (season, rider_slug, r.get("rider_name"), r["date"], r.get("race_name"),
             r.get("race_slug"), r.get("result_label") or "", r["result_kind"],
             r.get("race_class"), r.get("position"), r.get("status"), r.get("points_uci"),
             r.get("points_pcs"), r["result_url"]),
        )
    return len(rows)


def replace_rider_penalties(conn: sqlite3.Connection, season: int, rider_slug: str,
                            rows: Iterable[dict]) -> int:
    """Replace one rider's season penalties (rows: date, race_name, penalty_points, fine, article).

    Several sanctions on the same day in the same race (Kuss, TdF 2026 stage 20:
    25 + 50 points) share the table's unique key, so they are summed into one
    row whose ``fine`` / ``article`` list both, joined with ' + '.
    """
    merged: dict = {}
    for r in rows:
        key = (r["date"], r.get("race_name"))
        if key not in merged:
            merged[key] = dict(r)
            continue
        m = merged[key]
        m["penalty_points"] = (m["penalty_points"] or 0) + (r["penalty_points"] or 0)
        for k in ("fine", "article"):
            if r.get(k):
                m[k] = f"{m[k]} + {r[k]}" if m.get(k) else r[k]
    rows = list(merged.values())
    conn.execute("DELETE FROM rider_uci_penalties WHERE season = ? AND rider_slug = ?",
                 (season, rider_slug))
    for r in rows:
        conn.execute(
            """
            INSERT INTO rider_uci_penalties (season, rider_slug, date, race_name,
                                             penalty_points, fine, article)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(season, rider_slug, date, race_name) DO UPDATE SET
                penalty_points = excluded.penalty_points, fine = excluded.fine,
                article = excluded.article
            """,
            (season, rider_slug, r["date"], r.get("race_name"), r["penalty_points"],
             r.get("fine"), r.get("article")),
        )
    return len(rows)


def replace_rider_spells(conn: sqlite3.Connection, season: int, rider_slug: str,
                         spells: Iterable[dict]) -> int:
    """Replace one rider's team spells (keys: team_slug, team_name, start_date, end_date)."""
    spells = list(spells)
    if not spells:
        return 0
    conn.execute("DELETE FROM rider_team_spells WHERE season = ? AND rider_slug = ?",
                 (season, rider_slug))
    for s in spells:
        conn.execute(
            """
            INSERT INTO rider_team_spells (season, rider_slug, team_slug, team_name,
                                           start_date, end_date)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(season, rider_slug, team_slug) DO UPDATE SET
                team_name = excluded.team_name, start_date = excluded.start_date,
                end_date = excluded.end_date
            """,
            (season, rider_slug, s["team_slug"], s.get("team_name"), s.get("start_date"),
             s.get("end_date")),
        )
    return len(spells)


def upsert_team_recent_points(conn: sqlite3.Connection, season: int, team_slug: str,
                              rows: Iterable[dict]) -> int:
    """Upsert latest-uci-points rows (keys: date, points, indirect_points)."""
    n = 0
    for r in rows:
        conn.execute(
            """
            INSERT INTO uci_team_recent_points (season, team_slug, date, points,
                                                indirect_points, fetched_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(season, team_slug, date) DO UPDATE SET
                points = excluded.points, indirect_points = excluded.indirect_points,
                fetched_at = excluded.fetched_at
            """,
            (season, team_slug, r["date"], r.get("points"), r.get("indirect_points")),
        )
        n += 1
    return n
