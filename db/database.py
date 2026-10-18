import re
import sqlite3
import pathlib
from contextlib import contextmanager

import pandas as pd

from config import DB_PATH


def init_db(db_path: str = DB_PATH) -> None:
    """Create the database and apply schema."""
    pathlib.Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    schema = (pathlib.Path(__file__).parent / "schema.sql").read_text()
    with sqlite3.connect(db_path) as conn:
        conn.executescript(schema)
        # Migrations for columns added after initial schema
        for ddl in [
            "ALTER TABLE stages ADD COLUMN gradient_final_km REAL",
            "ALTER TABLE stages ADD COLUMN profile_score INTEGER",
            # Oct 2026: team of the result row + UCI sanction (penalty) points
            "ALTER TABLE results ADD COLUMN team_slug TEXT",
            "ALTER TABLE results ADD COLUMN team_name TEXT",
            "ALTER TABLE results ADD COLUMN uci_penalty INTEGER",
        ]:
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                pass  # column already exists
        # Data migration: PCS "Hills" category is now mapped to "climber"
        conn.execute(
            "UPDATE riders SET speciality='climber' WHERE speciality='puncher'"
        )

        # Add performance indexes (idempotent)
        for ddl in [
            "CREATE INDEX IF NOT EXISTS idx_results_stage ON results(stage_id)",
            "CREATE INDEX IF NOT EXISTS idx_results_rider ON results(rider_id)",
            "CREATE INDEX IF NOT EXISTS idx_stages_race_date ON stages(race_id, date)",
        ]:
            conn.execute(ddl)

        # Bet tracking table (added for CLV/ROI tracker)
        conn.execute("""CREATE TABLE IF NOT EXISTS bets (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            stage_id      INTEGER REFERENCES stages(id),
            rider_id      INTEGER REFERENCES riders(id),
            rider_name    TEXT NOT NULL,
            stage_date    TEXT,
            race_slug     TEXT,
            top10_prob    REAL,
            odds_decimal  REAL NOT NULL,
            stake         REAL DEFAULT 1.0,
            result        INTEGER,
            pnl           REAL,
            created_at    TEXT DEFAULT (datetime('now'))
        )""")

        # Team UCI points tables (official ranking, per-team breakdown, dated
        # rider results, sanctions, team spells) — written by
        # scripts/scrape_uci_points.py, read by analysis/team_points.py.
        from db.uci_points import ensure_uci_points_schema
        ensure_uci_points_schema(conn)


@contextmanager
def get_conn(db_path: str = DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert_rider(conn: sqlite3.Connection, data: dict) -> int:
    conn.execute(
        """
        INSERT INTO riders (pcs_slug, name, nationality, dob, team, pcs_rank,
                            speciality, weight_kg, height_cm)
        VALUES (:pcs_slug, :name, :nationality, :dob, :team, :pcs_rank,
                :speciality, :weight_kg, :height_cm)
        ON CONFLICT(pcs_slug) DO UPDATE SET
            name        = excluded.name,
            nationality = excluded.nationality,
            dob         = excluded.dob,
            team        = excluded.team,
            pcs_rank    = excluded.pcs_rank,
            speciality  = excluded.speciality,
            weight_kg   = excluded.weight_kg,
            height_cm   = excluded.height_cm,
            updated_at  = datetime('now')
        """,
        data,
    )
    row = conn.execute(
        "SELECT id FROM riders WHERE pcs_slug = ?", (data["pcs_slug"],)
    ).fetchone()
    return row["id"]


def upsert_race(conn: sqlite3.Connection, data: dict) -> int:
    conn.execute(
        """
        INSERT INTO races (pcs_slug, name, year, start_date, end_date, class,
                           country, is_stage_race, gender)
        VALUES (:pcs_slug, :name, :year, :start_date, :end_date, :class,
                :country, :is_stage_race, :gender)
        ON CONFLICT(pcs_slug) DO UPDATE SET
            name          = excluded.name,
            start_date    = excluded.start_date,
            end_date      = excluded.end_date,
            class         = excluded.class,
            country       = excluded.country,
            is_stage_race = excluded.is_stage_race,
            gender        = excluded.gender
        """,
        data,
    )
    row = conn.execute(
        "SELECT id FROM races WHERE pcs_slug = ?", (data["pcs_slug"],)
    ).fetchone()
    return row["id"]


def upsert_stage(conn: sqlite3.Connection, data: dict) -> int:
    """Insert or update a stage keyed by pcs_slug.

    On update a NULL in ``data`` never erases a known value (the live PCS
    stage list has no distance column, for instance); stage_num is set on
    insert only.
    """
    data = {**data,
            "gradient_final_km": data.get("gradient_final_km"),
            "profile_score":     data.get("profile_score")}
    conn.execute(
        """
        INSERT INTO stages (race_id, stage_num, pcs_slug, date, distance_km,
                            elevation_m, profile_type, surface, departure, arrival, gpx_path,
                            gradient_final_km, profile_score)
        VALUES (:race_id, :stage_num, :pcs_slug, :date, :distance_km,
                :elevation_m, :profile_type, :surface, :departure, :arrival, :gpx_path,
                :gradient_final_km, :profile_score)
        ON CONFLICT(pcs_slug) DO UPDATE SET
            date              = COALESCE(excluded.date, stages.date),
            distance_km       = COALESCE(excluded.distance_km, stages.distance_km),
            elevation_m       = COALESCE(excluded.elevation_m, stages.elevation_m),
            profile_type      = COALESCE(excluded.profile_type, stages.profile_type),
            surface           = excluded.surface,
            departure         = COALESCE(excluded.departure, stages.departure),
            arrival           = COALESCE(excluded.arrival, stages.arrival),
            gradient_final_km = COALESCE(excluded.gradient_final_km, stages.gradient_final_km),
            profile_score     = COALESCE(excluded.profile_score, stages.profile_score)
        """,
        data,
    )
    row = conn.execute(
        "SELECT id FROM stages WHERE pcs_slug = ?", (data["pcs_slug"],)
    ).fetchone()
    return row["id"]


def insert_result(conn: sqlite3.Connection, data: dict) -> None:
    """Insert or update one result row keyed by (stage_id, rider_id).

    ``team_slug`` / ``team_name`` / ``uci_penalty`` are optional in ``data``;
    on conflict a NULL never overwrites a stored team, and a NULL penalty
    keeps the stored one.
    """
    data = {"team_slug": None, "team_name": None, "uci_penalty": None, **data}
    conn.execute(
        """
        INSERT INTO results (stage_id, rider_id, position, status,
                             time_seconds, points_pcs, points_uci, bib,
                             team_slug, team_name, uci_penalty)
        VALUES (:stage_id, :rider_id, :position, :status,
                :time_seconds, :points_pcs, :points_uci, :bib,
                :team_slug, :team_name, :uci_penalty)
        ON CONFLICT(stage_id, rider_id) DO UPDATE SET
            position     = excluded.position,
            status       = excluded.status,
            time_seconds = excluded.time_seconds,
            points_pcs   = excluded.points_pcs,
            points_uci   = excluded.points_uci,
            team_slug    = COALESCE(excluded.team_slug, results.team_slug),
            team_name    = COALESCE(excluded.team_name, results.team_name),
            uci_penalty  = COALESCE(excluded.uci_penalty, results.uci_penalty)
        """,
        data,
    )


def fill_result_teams(conn: sqlite3.Connection, stage_id: int, rider_id: int,
                      team_slug, team_name, uci_penalty) -> int:
    """Fill team_slug / team_name / uci_penalty of an existing result where
    they are NULL; every other column is left alone.  Returns rows changed."""
    cur = conn.execute(
        """
        UPDATE results SET
            team_slug   = COALESCE(team_slug, ?),
            team_name   = COALESCE(team_name, ?),
            uci_penalty = COALESCE(uci_penalty, ?)
        WHERE stage_id = ? AND rider_id = ?
          AND ((team_slug IS NULL AND ? IS NOT NULL)
            OR (team_name IS NULL AND ? IS NOT NULL)
            OR (uci_penalty IS NULL AND ? IS NOT NULL))
        """,
        (team_slug, team_name, uci_penalty, stage_id, rider_id,
         team_slug, team_name, uci_penalty),
    )
    return cur.rowcount


_STAGE_META_FIELDS = ("date", "distance_km", "elevation_m", "profile_type", "departure",
                      "arrival", "gradient_final_km", "profile_score")


def fill_stage_meta(conn: sqlite3.Connection, stage_id: int, meta: dict,
                    overwrite: bool = False) -> None:
    """Update stage columns (date, distance, elevation, profile, towns,
    gradient, profile score) from a parsed stage page.

    Default: only NULL columns are filled.  ``overwrite=True`` lets every
    non-NULL page value replace the stored one — used right after scraping the
    stage's results from that same page (the stored values may come from a
    provisional route).  A NULL from the page never erases a value.
    """
    sets = ",\n            ".join(
        f"{c} = COALESCE(:{c}, {c})" if overwrite else f"{c} = COALESCE({c}, :{c})"
        for c in _STAGE_META_FIELDS)
    conn.execute(
        f"""
        UPDATE stages SET
            {sets}
        WHERE id = :stage_id
        """,
        {key: meta.get(key) for key in _STAGE_META_FIELDS} | {"stage_id": stage_id},
    )


# ── UCI points sanity (repair-points) ────────────────────────────────────────

# Highest UCI placing value a single stage / one-day result can carry
# (Monuments: 800).  GC and classification points are not stored per stage.
MAX_RESULT_UCI_POINTS = 800


def uci_points_anomalies(rows, is_ttt: bool = False,
                         max_points: float = MAX_RESULT_UCI_POINTS) -> set[str]:
    """Reasons why a stage's stored UCI points look corrupted.

    ``rows``: iterable of (position, status, points_uci) for ONE stage.
    The pre-Oct-2026 parser concatenated the digits of a sanction into the
    earned value ('800 -25' → 80025, '15 -25' → 1525) and stored a bare
    sanction as points ('-25' → 25 for a rider outside the scoring places).
    Checks:
      above_cap        any value > max_points
      non_monotonic    a finisher has more points than a better-placed one
      after_gap        points again after a better-placed finisher had none
      non_finisher     DNF/DNS/... row with points
    The two order checks are skipped for team time trials (team shares are
    not ordered by individual position); a TTT is instead checked for
      ttt_shares_missing  fewer than 4 finishers with points (all of them in
                          a smaller field): every rider of the winning team
                          gets a share, so 0-3 scorers means the shares were
                          never parsed — Paris-Nice 2026 stage 3 held the GC
                          tab, one rider with 10 points
    """
    reasons: set[str] = set()
    finishers = []
    for position, status, pts in rows:
        if pts is not None and pts > max_points:
            reasons.add("above_cap")
        if status != "finished" and pts:
            reasons.add("non_finisher")
        if status == "finished" and position is not None:
            finishers.append((position, pts))
    if is_ttt:
        if finishers and sum(1 for _, pts in finishers if pts) < min(4, len(finishers)):
            reasons.add("ttt_shares_missing")
        return reasons
    run_min = None
    gap = False
    for _, pts in sorted(finishers, key=lambda t: t[0]):
        if not pts:
            gap = True
            continue
        if gap:
            reasons.add("after_gap")
        if run_min is not None and pts > run_min:
            reasons.add("non_monotonic")
        run_min = pts if run_min is None else min(run_min, pts)
    return reasons


def find_implausible_uci_stages(conn: sqlite3.Connection, year: int,
                                gender: str | None = None,
                                race_slugs: list[str] | None = None) -> list[dict]:
    """Stages of ``year`` whose stored points_uci look corrupted.

    Returns dicts {stage_id, pcs_slug, race_slug, race_id, date, reasons}
    ordered by date.
    """
    sql = """
        SELECT s.id AS stage_id, s.pcs_slug, s.profile_type, s.date,
               ra.pcs_slug AS race_slug, ra.id AS race_id,
               x.position, x.status, x.points_uci
        FROM results x
        JOIN stages s ON s.id = x.stage_id
        JOIN races ra ON ra.id = s.race_id
        WHERE ra.year = ?
    """
    params: list = [year]
    if gender:
        sql += " AND ra.gender = ?"
        params.append(gender)
    if race_slugs:
        sql += f" AND ra.pcs_slug IN ({','.join('?' * len(race_slugs))})"
        params.extend(race_slugs)
    sql += " ORDER BY s.date, s.id"
    by_stage: dict[int, dict] = {}
    for row in conn.execute(sql, params).fetchall():
        st = by_stage.setdefault(row[0], {
            "stage_id": row[0], "pcs_slug": row[1], "profile_type": row[2],
            "date": row[3], "race_slug": row[4], "race_id": row[5], "rows": []})
        st["rows"].append((row[6], row[7], row[8]))
    out = []
    for st in by_stage.values():
        is_ttt = st["profile_type"] == "ttt" or any(
            isinstance(p, float) and not p.is_integer() for _, _, p in st["rows"])
        reasons = uci_points_anomalies(st["rows"], is_ttt=is_ttt)
        if reasons:
            out.append({"stage_id": st["stage_id"], "pcs_slug": st["pcs_slug"],
                        "race_slug": st["race_slug"], "race_id": st["race_id"],
                        "date": st["date"], "reasons": sorted(reasons)})
    return out


def repair_stage_points(conn: sqlite3.Connection, stage_id: int, parsed: list[dict],
                        dry_run: bool = False) -> dict:
    """Bring a stage's stored rows in line with a fresh parse, matched by rider slug.

    Every matched row takes the page's position, status, points_uci and
    uci_penalty; time_seconds, points_pcs and bib follow the page when it
    has a value (a non-finisher's time is cleared); NULL team fields are
    filled.  Positions are refreshed together with the points: PCS reorders
    stages after the first scrape (Amstel Gold Race Women 2026: Chabbey and
    Fisher-Black swapped 20th/21st), and copying only the points onto the
    stale order wrote a non-monotonic pair.

    Returns {"changed": [(rider_slug, old_pts, new_pts, old_pen, new_pen)],
             "moved": [(rider_slug, old_pos, new_pos, old_status, new_status)],
             "retimed": int, "teams_filled": int, "unmatched_db": [slug],
             "unmatched_page": int, "matched": {slug: result id}}.
    """
    fresh = {r["rider_slug"]: r for r in parsed}
    rows = conn.execute(
        """SELECT x.id, rd.pcs_slug, x.points_uci, x.uci_penalty, x.team_slug, x.team_name,
                  x.position, x.status, x.time_seconds, x.points_pcs, x.bib
           FROM results x JOIN riders rd ON rd.id = x.rider_id
           WHERE x.stage_id = ?""", (stage_id,)).fetchall()
    report = {"changed": [], "moved": [], "retimed": 0, "teams_filled": 0,
              "unmatched_db": [], "unmatched_page": 0, "matched": {}}
    for (res_id, slug, old_pts, old_pen, old_team, old_team_name,
         old_pos, old_status, old_time, old_pcs, old_bib) in rows:
        new = fresh.get(slug)
        if new is None:
            report["unmatched_db"].append(slug)
            continue
        report["matched"][slug] = res_id
        new_pts, new_pen = new.get("points_uci"), new.get("uci_penalty")
        if "status" in new:  # parser rows always carry position + status
            new_pos, new_status = new.get("position"), new.get("status") or old_status
        else:
            new_pos, new_status = old_pos, old_status
        if new_status != "finished":
            new_time = None
        else:
            new_time = new.get("time_seconds") if new.get("time_seconds") is not None else old_time
        new_pcs = new.get("points_pcs") if new.get("points_pcs") is not None else old_pcs
        new_bib = new.get("bib") if new.get("bib") is not None else old_bib
        points_changed = (old_pts, old_pen) != (new_pts, new_pen)
        moved = (old_pos, old_status) != (new_pos, new_status)
        retimed = old_time != new_time
        other = (old_pcs, old_bib) != (new_pcs, new_bib)
        team_fill = (old_team is None and new.get("team_slug") is not None) or \
                    (old_team_name is None and new.get("team_name") is not None)
        if points_changed:
            report["changed"].append((slug, old_pts, new_pts, old_pen, new_pen))
        if moved:
            report["moved"].append((slug, old_pos, new_pos, old_status, new_status))
        if retimed:
            report["retimed"] += 1
        if team_fill:
            report["teams_filled"] += 1
        if (points_changed or moved or retimed or other or team_fill) and not dry_run:
            conn.execute(
                """UPDATE results SET points_uci = ?, uci_penalty = ?, position = ?, status = ?,
                       time_seconds = ?, points_pcs = ?, bib = ?,
                       team_slug = COALESCE(team_slug, ?),
                       team_name = COALESCE(team_name, ?)
                   WHERE id = ?""",
                (new_pts, new_pen, new_pos, new_status, new_time, new_pcs, new_bib,
                 new.get("team_slug"), new.get("team_name"), res_id))
    report["unmatched_page"] = len(set(fresh) - set(report["matched"]))
    return report


def delete_stage_rows(conn: sqlite3.Connection, stage_id: int, rider_slugs: list[str]) -> int:
    """Delete the result rows of ``rider_slugs`` on one stage; returns rows deleted."""
    n = 0
    for slug in rider_slugs:
        n += conn.execute(
            """DELETE FROM results WHERE stage_id = ?
                 AND rider_id IN (SELECT id FROM riders WHERE pcs_slug = ?)""",
            (stage_id, slug)).rowcount
    return n


def find_null_gap_stages(conn: sqlite3.Connection, year: int, gender: str | None = None,
                         race_slugs: list[str] | None = None) -> list[dict]:
    """Stages of ``year`` with a ranked finisher behind the winner whose
    time_seconds is NULL — the signature of the pre-Oct-2026 time-trial
    parser, which read only the hundredths font (``0:28<font>,36</font>``).

    Returns dicts {stage_id, pcs_slug, race_slug, race_id, date, reasons}.
    """
    sql = """
        SELECT s.id, s.pcs_slug, ra.pcs_slug, ra.id, s.date, COUNT(*)
        FROM results x JOIN stages s ON s.id = x.stage_id JOIN races ra ON ra.id = s.race_id
        WHERE ra.year = ? AND x.status = 'finished' AND x.position > 1 AND x.time_seconds IS NULL
    """
    params: list = [year]
    if gender:
        sql += " AND ra.gender = ?"
        params.append(gender)
    if race_slugs:
        sql += f" AND ra.pcs_slug IN ({','.join('?' * len(race_slugs))})"
        params.extend(race_slugs)
    sql += " GROUP BY s.id ORDER BY s.date, s.id"
    return [{"stage_id": r[0], "pcs_slug": r[1], "race_slug": r[2], "race_id": r[3], "date": r[4],
             "reasons": [f"null_gaps({r[5]})"]} for r in conn.execute(sql, params).fetchall()]


def _rider_result_stage_key(race_slug: str | None, result_url: str | None) -> tuple[str | None, str]:
    """(stored stage slug, suffix) of a rider_uci_results row.

    ``race/paris-nice/2026`` + ``race/paris-nice-2026-stage-3`` →
    (``race/paris-nice/2026/stage-3``, ``stage-3``); a one-day race
    (``...-2026-result`` or the bare race link) → (``race/<name>/2026``, '').
    """
    m = re.match(r"^race/(.+)/(\d{4})$", race_slug or "")
    url = (result_url or "").split("?")[0].strip("/")
    if not m:
        return None, ""
    prefix = f"race/{m.group(1)}-{m.group(2)}"
    if url in (prefix, race_slug):
        return race_slug, ""
    for p in (prefix + "-", race_slug + "/"):
        if url.startswith(p):
            suffix = url[len(p):]
            return (race_slug, "") if suffix == "result" else (f"{race_slug}/{suffix}", suffix)
    return None, ""


def find_stages_disagreeing_with_rider_results(conn: sqlite3.Connection, year: int,
                                               gender: str | None = None,
                                               race_slugs: list[str] | None = None) -> list[dict]:
    """Stages of ``year`` whose stored rows disagree with ``rider_uci_results``.

    rider_uci_results (scripts/scrape_uci_points.py) holds the same results
    as listed on each scoring rider's PCS results page, fetched weeks after
    the stage pages; PCS corrects stage orders after the race (TdF 2026
    stage 6: Skjelmose / Lenny Martínez 8th/9th swapped; Paris-Roubaix: a
    rider re-classified from DNF to 93rd) and a stage scraped before the
    correction keeps the old order.  A stage is returned when a stored
    rider's points_uci differs, or his position differs (team time trials —
    profile 'ttt', fractional shares or a '(TTT)' label — excluded: the rider
    pages give the team's place).  Stages of renamed races
    (``race/dauphine`` stored, ``race/tour-auvergne-rhone-alpes`` on the rider
    page) are matched by date + stage suffix + shared riders.  DB only, no
    PCS traffic.  Returns dicts like :func:`find_implausible_uci_stages`.
    """
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'rider_uci_results'").fetchone()
    if not has_table:
        return []
    where, params = "", [year]
    if gender:
        where += " AND ra.gender = ?"
        params.append(gender)
    if race_slugs:
        where += f" AND ra.pcs_slug IN ({','.join('?' * len(race_slugs))})"
        params.extend(race_slugs)
    stored: dict[str, dict] = {}
    by_date_suffix: dict[tuple, list[str]] = {}
    for sid, slug, rslug, rid, sdate, prof, rider, pos, status, pts in conn.execute(f"""
            SELECT s.id, s.pcs_slug, ra.pcs_slug, ra.id, s.date, s.profile_type,
                   rd.pcs_slug, x.position, x.status, x.points_uci
            FROM results x JOIN stages s ON s.id = x.stage_id JOIN races ra ON ra.id = s.race_id
            JOIN riders rd ON rd.id = x.rider_id
            WHERE ra.year = ? {where}""", params):
        st = stored.get(slug)
        if st is None:
            st = stored[slug] = {"stage_id": sid, "pcs_slug": slug, "race_slug": rslug, "race_id": rid,
                                 "date": sdate, "ttt": prof == "ttt", "rows": {}}
            suffix = slug[len(rslug):].strip("/")
            by_date_suffix.setdefault((sdate, suffix), []).append(slug)
        st["rows"][rider] = (pos, status, pts)
        if isinstance(pts, float) and not pts.is_integer():
            st["ttt"] = True  # team time-trial shares (Ses Salines-Felanitx is stored as 'flat')
    rur: dict[str, dict] = {}
    for rider, rslug, url, label, pos, status, pts, day in conn.execute(
            """SELECT rider_slug, race_slug, result_url, result_label, position, status, points_uci, date
               FROM rider_uci_results WHERE season = ? AND result_kind IN ('stage', 'oneday')""",
            (year,)):
        key, suffix = _rider_result_stage_key(rslug, url)
        if key is None:
            continue
        g = rur.setdefault(key, {"suffix": suffix, "date": day, "ttt": False, "rows": {}})
        g["ttt"] = g["ttt"] or "(TTT)" in (label or "")
        g["rows"][rider] = (pos, status, pts)
    out = []
    for key, g in rur.items():
        st = stored.get(key)
        if st is None:  # renamed race: same day, same stage, most shared riders
            cands = [stored[c] for c in by_date_suffix.get((g["date"], g["suffix"]), [])]
            scored = sorted(((len(set(c["rows"]) & set(g["rows"])), c["pcs_slug"]) for c in cands),
                            reverse=True)
            if not scored or scored[0][0] < 3 or (len(scored) > 1 and scored[1][0] == scored[0][0]):
                continue
            st = stored[scored[0][1]]
        reasons: set[str] = set()
        for rider, (pos, status, pts) in g["rows"].items():
            if rider not in st["rows"]:
                continue
            db_pos, db_status, db_pts = st["rows"][rider]
            if round(float(db_pts or 0), 2) != round(float(pts or 0), 2):
                reasons.add("uci_points_differ")
            if not (st["ttt"] or g["ttt"]) and status != "dsq" and pos != db_pos:
                reasons.add("positions_differ")
        if reasons:
            out.append({k: st[k] for k in ("stage_id", "pcs_slug", "race_slug", "race_id", "date")}
                       | {"reasons": sorted(reasons)})
    return sorted(out, key=lambda r: (r["date"] or "", r["stage_id"]))


# ── DB loaders (used by model/predict.py for start-list predictions) ──────────

def _load_results(conn) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT id, stage_id, rider_id, position, status, time_seconds, bib FROM results", conn
    )


def _load_stages(conn) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT id, race_id, stage_num, date, distance_km, elevation_m, profile_type, surface, gradient_final_km, profile_score FROM stages",
        conn,
    )


def _load_races(conn) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT id, pcs_slug, name, year, is_stage_race FROM races", conn
    )


def _load_riders(conn) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT id, pcs_slug, name, team, pcs_rank, speciality, weight_kg, height_cm, dob FROM riders",
        conn,
    )
