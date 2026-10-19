"""Storage for the official UCI team ranking and the dated rider results behind it.

Tables (the data contract shared with ``analysis.team_points``):

* ``uci_team_totals``      — official listing pages (``ranking`` =
                             'rankings/me/ranking-2026-2028'); the scraper also
                             stores each breakdown page's own season total
                             under ``ranking`` = 'team-breakdown/ranking-2026-2028'
                             (the listing is a snapshot taken earlier in the day)
* ``uci_team_breakdown``   — team/<slug>/results/ranking-2026-2028, one row per rider
* ``rider_uci_results``    — rider/<slug>/results rows for one season (plus GC
                             leader-jersey points, see below) — SCORING ROWS ONLY
* ``rider_uci_scrapes``    — one row per scraped rider and season: what his pages
                             showed (rows, first/last result date, complete or not)
* ``rider_uci_penalties``  — UCI sanction points from rider/<slug>/info/fines-penalties
                             (with the race_slug / race_class of the race they were
                             imposed in, resolved from the rider's full result list)
* ``rider_team_spells``    — which team a rider rode for when (mid-season transfers)

Plus one table outside the contract, used only for reconciliation:

* ``uci_team_recent_points`` — team/<slug>/results/latest-uci-points: the team's
                               UCI points per day for the last ~30 days

Scoring rows only: a rider's results page lists every race day, and five in
six of them are worth no UCI points (2026: 48,498 of 58,261 rows; dropping
them saves ~16 MB of table + indexes in a git-tracked DB).  Only rows with
``points_uci`` not NULL and not 0 are stored (leader-jersey rows always have
points);
``rider_uci_scrapes`` keeps what the dropped rows were needed for:

* a scraped rider with no row on a stage scored 0 there — up to
  ``last_date``, the last result his pages listed (``complete`` = 0, a partial
  scrape, proves nothing);
* ``analysis.team_points.data_through`` — the max of ``last_date`` (every row
  seen, zero-point ones included), so the "data through" date does not move
  back to the last *scoring* result;
* the rider's page name (<h1>) for riders without a scoring row.

A penalty borrowed its race_slug / race_class from the rider's result row of
that race, often a zero-point row: it is resolved when the penalty is stored
(``race_slug`` / ``race_class`` columns).  :func:`prune_zero_point_results`
converts a DB that still holds zero-point rows (backfilling both first).

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

import logging
import os
import sqlite3
from typing import Callable, Iterable, Optional

log = logging.getLogger(__name__)

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
CREATE TABLE IF NOT EXISTS rider_uci_scrapes (
    season INTEGER NOT NULL, rider_slug TEXT NOT NULL, rider_name TEXT,
    rows_seen INTEGER NOT NULL DEFAULT 0,
    scoring_rows INTEGER NOT NULL DEFAULT 0,
    first_date TEXT, last_date TEXT,
    complete INTEGER,
    scraped_at TEXT DEFAULT (datetime('now')),
    UNIQUE(season, rider_slug));
CREATE TABLE IF NOT EXISTS rider_uci_penalties (
    season INTEGER NOT NULL, rider_slug TEXT NOT NULL, date TEXT NOT NULL, race_name TEXT,
    penalty_points REAL NOT NULL,
    fine TEXT, article TEXT,
    race_slug TEXT, race_class TEXT,
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

# the rows rider_uci_results keeps (SQL and Python forms of the same rule)
SCORING_SQL = "points_uci IS NOT NULL AND points_uci != 0"

# columns added after the first release of a table: (table, column, type)
_ADDED_COLUMNS = (("rider_uci_penalties", "race_slug", "TEXT"),
                  ("rider_uci_penalties", "race_class", "TEXT"))


def ensure_uci_points_schema(conn: sqlite3.Connection) -> None:
    """Create the UCI-points tables and indexes if they do not exist, and add
    the columns newer code expects to tables created earlier (idempotent)."""
    conn.executescript(UCI_POINTS_DDL)
    for table, column, sqltype in _ADDED_COLUMNS:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sqltype}")
    conn.commit()


def is_scoring(row: dict) -> bool:
    """True when a rider_uci_results row is worth UCI points (and is stored)."""
    pts = row.get("points_uci")
    return pts is not None and pts != 0


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
                          rows: Iterable[dict], complete: Optional[bool] = None,
                          rider_name: Optional[str] = None) -> int:
    """Store one rider's season rows, replacing what was there; returns the rows stored.

    ``rows`` is the full merged set (results-page rows + leader-jersey and
    other ranking-only rows, see ``scraper.uci_points.merge_season_rows``),
    zero-point rows included: only the scoring ones (:func:`is_scoring`) are
    stored, and :func:`record_rider_scrape` notes what the pages showed.

    ``complete`` says how far the rows can be trusted:

    * True — the rider's pages were all read: the rows ARE his season.  Even
      an empty set replaces (clears) his stored rows: a rider who scored
      nothing, or whose only result was cancelled, keeps no stale points;
    * False — a partial scrape (a page failed): non-empty rows are stored but
      the scrape record says so, and the mismatch check
      (``db.database.find_stages_disagreeing_with_rider_results``) does not
      read a missing row as 0 points;
    * None (default, older callers) — non-empty rows replace; an empty set
      is "nothing scraped" and deletes nothing.

    An empty set with ``complete`` not True therefore never touches the DB
    (no delete, no scrape record).  A non-empty set always replaces: rows
    that were seen, even if none of them scored, are a scrape.
    """
    rows = list(rows)
    if not rows and complete is not True:
        return 0
    conn.execute("DELETE FROM rider_uci_results WHERE season = ? AND rider_slug = ?",
                 (season, rider_slug))
    scoring = [r for r in rows if is_scoring(r)]
    for r in scoring:
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
    record_rider_scrape(conn, season, rider_slug, rows, complete, rider_name)
    return len(scoring)


def record_rider_scrape(conn: sqlite3.Connection, season: int, rider_slug: str,
                        rows: list[dict], complete: Optional[bool] = None,
                        rider_name: Optional[str] = None) -> None:
    """Upsert the rider's ``rider_uci_scrapes`` row from the rows his pages showed.

    ``rows_seen`` / ``first_date`` / ``last_date`` count every row, zero-point
    ones included (``last_date`` is how far the pages reached, see the module
    docstring); ``scoring_rows`` the rows stored.  ``complete``: 1, 0 or NULL
    (unknown — older callers and scrapes backfilled by the prune).
    """
    dates = [r["date"] for r in rows if r.get("date")]
    names = [r["rider_name"] for r in rows if r.get("rider_name")]
    name = max(names) if names else rider_name  # MAX(rider_name), like the readers
    conn.execute(
        """
        INSERT INTO rider_uci_scrapes (season, rider_slug, rider_name, rows_seen, scoring_rows,
                                       first_date, last_date, complete, scraped_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(season, rider_slug) DO UPDATE SET
            rider_name = COALESCE(excluded.rider_name, rider_uci_scrapes.rider_name),
            rows_seen = excluded.rows_seen, scoring_rows = excluded.scoring_rows,
            first_date = excluded.first_date, last_date = excluded.last_date,
            complete = excluded.complete, scraped_at = excluded.scraped_at
        """,
        (season, rider_slug, name, len(rows), sum(1 for r in rows if is_scoring(r)),
         min(dates) if dates else None, max(dates) if dates else None,
         None if complete is None else int(bool(complete))),
    )


def has_rider_scrape(conn: sqlite3.Connection, season: int, rider_slug: str) -> bool:
    """True when the rider's season was stored before (a scrape record or any row)."""
    for table in ("rider_uci_scrapes", "rider_uci_results"):
        if conn.execute(f"SELECT 1 FROM {table} WHERE season = ? AND rider_slug = ? LIMIT 1",
                        (season, rider_slug)).fetchone():
            return True
    return False


def replace_rider_penalties(conn: sqlite3.Connection, season: int, rider_slug: str,
                            rows: Iterable[dict]) -> int:
    """Replace one rider's season penalties (rows: date, race_name, penalty_points, fine,
    article, and optionally race_slug / race_class — see the module docstring).

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
                                             penalty_points, fine, article, race_slug, race_class)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(season, rider_slug, date, race_name) DO UPDATE SET
                penalty_points = excluded.penalty_points, fine = excluded.fine,
                article = excluded.article, race_slug = excluded.race_slug,
                race_class = excluded.race_class
            """,
            (season, rider_slug, r["date"], r.get("race_name"), r["penalty_points"],
             r.get("fine"), r.get("article"), r.get("race_slug"), r.get("race_class")),
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


# ---------------------------------------------------------------------------
# maintenance: drop the zero-point rows of a DB stored before scoring-only
# ---------------------------------------------------------------------------

def _season_filter(season: Optional[int]) -> tuple[str, list]:
    return ("", []) if season is None else (" AND season = ?", [season])


def backfill_rider_scrapes(conn: sqlite3.Connection, season: Optional[int] = None) -> int:
    """Create the missing ``rider_uci_scrapes`` rows from the stored result rows.

    For riders stored before the table existed (while their zero-point rows
    are still there, so ``rows_seen`` / ``last_date`` are exact).  ``complete``
    stays NULL (unknown — the readers treat it like a complete scrape, as the
    pre-existing rows were trusted).  Existing scrape rows are left alone.
    Returns the rows created.
    """
    where, params = _season_filter(season)
    cur = conn.execute(
        f"""
        INSERT OR IGNORE INTO rider_uci_scrapes (season, rider_slug, rider_name, rows_seen,
            scoring_rows, first_date, last_date, complete, scraped_at)
        SELECT season, rider_slug, MAX(rider_name), COUNT(*),
               SUM(CASE WHEN {SCORING_SQL} THEN 1 ELSE 0 END), MIN(date), MAX(date), NULL,
               COALESCE(MAX(fetched_at), datetime('now'))
        FROM rider_uci_results WHERE 1 = 1{where} GROUP BY season, rider_slug
        """, params)
    return cur.rowcount


PenaltyRace = Callable[[Optional[str], str, list], tuple]


def _default_penalty_race() -> PenaltyRace:
    # analysis.team_points owns the race-name normalisation the ledger has
    # always used to place a penalty; imported lazily (pandas) and only here.
    from analysis.team_points import match_penalty_race
    return match_penalty_race


def resolve_penalty_races(conn: sqlite3.Connection, season: Optional[int] = None,
                          penalty_race: Optional[PenaltyRace] = None) -> int:
    """Fill ``race_slug`` / ``race_class`` of penalties that have neither, from the
    rider's stored result rows (all of them — run before deleting any).

    ``penalty_race(race_name, date, rows)`` -> (race_slug, race_class), rows
    being (date, race_name, race_slug, race_class) tuples in storage order;
    default :func:`analysis.team_points.match_penalty_race`.  Returns the
    penalties resolved.
    """
    penalty_race = penalty_race or _default_penalty_race()
    where, params = _season_filter(season)
    todo = conn.execute(
        "SELECT rowid, season, rider_slug, date, race_name FROM rider_uci_penalties "
        f"WHERE race_slug IS NULL AND race_class IS NULL{where}", params).fetchall()
    n = 0
    for rowid, ssn, slug, day, race_name in todo:
        rows = conn.execute(
            "SELECT date, race_name, race_slug, race_class FROM rider_uci_results "
            "WHERE season = ? AND rider_slug = ? ORDER BY rowid", (ssn, slug)).fetchall()
        race_slug, race_class = penalty_race(race_name, day, rows)
        if race_slug is None and race_class is None:
            continue
        conn.execute("UPDATE rider_uci_penalties SET race_slug = ?, race_class = ? WHERE rowid = ?",
                     (race_slug, race_class, rowid))
        n += 1
    return n


def prune_zero_point_results(conn: sqlite3.Connection, season: Optional[int] = None,
                             penalty_race: Optional[PenaltyRace] = None) -> dict:
    """Delete the rider_uci_results rows worth no UCI points (NULL or 0).

    ``season`` None = every season.  Before deleting, everything the readers
    took from those rows is kept elsewhere: the riders' scrape records
    (:func:`backfill_rider_scrapes`) and the penalties' races
    (:func:`resolve_penalty_races`).  Idempotent: a second run deletes
    nothing.  The schema check first commits any pending transaction; the
    backfill and the delete then run in one transaction the caller commits.

    Returns {deleted, kept, scrapes_backfilled, penalties_resolved}.
    """
    ensure_uci_points_schema(conn)
    where, params = _season_filter(season)
    backfilled = backfill_rider_scrapes(conn, season)
    resolved = resolve_penalty_races(conn, season, penalty_race)
    deleted = conn.execute(f"DELETE FROM rider_uci_results WHERE NOT ({SCORING_SQL}){where}",
                           params).rowcount
    kept = conn.execute(f"SELECT COUNT(*) FROM rider_uci_results WHERE 1 = 1{where}",
                        params).fetchone()[0]
    log.info("rider_uci_results%s: %d zero-point rows deleted, %d kept; %d scrape records "
             "backfilled, %d penalties' races resolved",
             "" if season is None else f" {season}", deleted, kept, backfilled, resolved)
    return {"deleted": deleted, "kept": kept, "scrapes_backfilled": backfilled,
            "penalties_resolved": resolved}


def vacuum_database(db_path: str) -> tuple[int, int]:
    """VACUUM the SQLite file at ``db_path``; returns its size (bytes) before and after.

    Deleting rows only frees pages inside the file; VACUUM rewrites it at its
    real size.  It cannot run inside a transaction, so it gets its own
    autocommit connection — call it after the writer's commit.  Needs free
    disk space for a temporary copy of the database.
    """
    before = os.path.getsize(db_path)
    conn = sqlite3.connect(db_path, isolation_level=None)  # autocommit: no implicit BEGIN
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()
    after = os.path.getsize(db_path)
    log.info("VACUUM %s: %.1f MB -> %.1f MB", db_path, before / 1e6, after / 1e6)
    return before, after
