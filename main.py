"""CLI entry point for the PCS predictor pipeline."""
import argparse
import datetime
import logging
import sys
from pathlib import Path

from db.database import (
    init_db, get_conn, upsert_race, upsert_stage, upsert_rider, insert_result,
    fill_result_teams, fill_stage_meta, find_implausible_uci_stages, repair_stage_points,
    delete_stage_rows, find_null_gap_stages, find_stages_disagreeing_with_rider_results,
)
from scraper.races import iter_races, fetch_race_stages, fetch_stage_meta, find_race_alias, is_missing_page
from scraper.utils import fetch, pcs_url
from scraper.results import fetch_stage_results
from scraper.riders import fetch_rider
from config import SCRAPE_YEARS, DB_PATH, WOMEN_RACE_CLASSES, WOMEN_CIRCUIT, MEN_CIRCUITS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def _stub_rider(slug: str, name: str) -> dict:
    return {
        "pcs_slug": slug, "name": name,
        "nationality": None, "dob": None, "team": None,
        "pcs_rank": None, "speciality": None,
        "weight_kg": None, "height_cm": None,
    }


def _within_race_window(conn, race_id: int, meta: dict) -> dict:
    """Drop a stage-page date that falls outside the race's dates (± 1 day)."""
    race = conn.execute("SELECT start_date, end_date FROM races WHERE id = ?", (race_id,)).fetchone()
    if meta.get("date") and race is not None and race["start_date"] and race["end_date"]:
        lo = (datetime.date.fromisoformat(race["start_date"]) - datetime.timedelta(days=1)).isoformat()
        hi = (datetime.date.fromisoformat(race["end_date"]) + datetime.timedelta(days=1)).isoformat()
        if not lo <= meta["date"] <= hi:
            meta = {**meta, "date": None}
    return meta


def _db_stages(race_id: int) -> list[dict]:
    """Stages already stored for a race (fallback when PCS gives no stage list)."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT pcs_slug, stage_num, date, distance_km, elevation_m, profile_type,
                      surface, departure, arrival, gpx_path
               FROM stages WHERE race_id = ? ORDER BY date, stage_num, id""",
            (race_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _checked_stage_list(race_id: int, race_slug: str, fetched: list[dict]) -> list[dict]:
    """Sanity-check a stage list from PCS against what the DB knows.

    Falls back to the stored stages when PCS gave nothing, when a known stage
    race came back as a one-day pseudo-stage, or when none of the fetched
    slugs matches a stored one (a slug-format change would otherwise insert a
    duplicate set of stages).  Stage dates outside the race window (± 1 day)
    are dropped so a misread date never overwrites a stored one.
    """
    with get_conn() as conn:
        race = conn.execute(
            "SELECT is_stage_race, start_date, end_date FROM races WHERE id = ?", (race_id,)
        ).fetchone()
    stored = _db_stages(race_id)
    stored_slugs = {s["pcs_slug"] for s in stored}
    fetched = [dict(s) for s in fetched or [] if s.get("pcs_slug")]

    reason = None
    if not fetched:
        reason = "no stage list from PCS"
    elif race is not None and race["is_stage_race"] and len(fetched) == 1 \
            and fetched[0]["pcs_slug"] == race_slug:
        reason = "stage race parsed as a one-day race"
    elif stored_slugs and not stored_slugs & {s["pcs_slug"] for s in fetched}:
        reason = "no fetched stage slug matches the stored stages"
    if reason:
        if stored:
            log.warning("%s for %s — using the %d stages stored in the DB", reason, race_slug, len(stored))
        else:
            log.warning("%s for %s", reason, race_slug)
        # Never store a one-day pseudo-stage for a stage race or a second,
        # differently-keyed set of stages: stick to what the DB has.
        return stored

    if race is not None and race["start_date"] and race["end_date"]:
        lo = (datetime.date.fromisoformat(race["start_date"]) - datetime.timedelta(days=1)).isoformat()
        hi = (datetime.date.fromisoformat(race["end_date"]) + datetime.timedelta(days=1)).isoformat()
        for s in fetched:
            if s.get("date") and not (lo <= s["date"] <= hi):
                log.warning("  stage %s: date %s outside race window %s..%s — ignored",
                            s["pcs_slug"], s["date"], race["start_date"], race["end_date"])
                s["date"] = None
    return fetched


_ALIASES: dict[int, str | None] = {}


def _race_alias(race_id: int, race_slug: str) -> str | None:
    """Current PCS slug of a race whose stored slug now returns PCS's empty
    "unknown slug" page (renamed race), or None.  Memoised per process."""
    if race_id in _ALIASES:
        return _ALIASES[race_id]
    alias = None
    html = fetch(pcs_url(race_slug))
    if html is None:
        # a failed fetch is not a renamed race: no alias, and not memoised
        log.warning("race %s: page could not be fetched — not looking for a renamed race", race_slug)
        return None
    if is_missing_page(html):
        with get_conn() as conn:
            race = conn.execute(
                "SELECT start_date, end_date, class, name FROM races WHERE id = ?", (race_id,)
            ).fetchone()
            if race is not None:
                alias = find_race_alias(race_slug, race["start_date"], race["end_date"], race["class"], race["name"])
            other = conn.execute("SELECT id FROM races WHERE pcs_slug = ? AND id != ?",
                                 (alias, race_id)).fetchone() if alias else None
        if other is not None:
            # a race we store under its own slug is a different race, never an alias
            log.warning("race %s: candidate %s is another stored race (id %d) — no alias",
                        race_slug, alias, other["id"])
            alias = None
        if alias:
            log.info("race %s is now %s on PCS", race_slug, alias)
        else:
            log.warning("race %s: PCS page is empty and no renamed race was found in the calendar", race_slug)
    _ALIASES[race_id] = alias
    return alias


def _alias_stage_list(race_id: int, race_slug: str, alias: str) -> list[dict]:
    """Stage list of a renamed race, keyed to the stored stages.

    Each stage keeps the PCS slug to fetch in ``fetch_slug``; ``pcs_slug``
    is the stored row it belongs to: a stored stage under the new slug, else
    the stored stage with the same suffix under the old race slug, else the
    new slug (a stage the DB did not know).
    """
    stored = {s["pcs_slug"] for s in _db_stages(race_id)}
    out = []
    for st in fetch_race_stages(alias):
        fetch_slug = st["pcs_slug"]
        suffix = fetch_slug[len(alias):].strip("/")
        old_slug = f"{race_slug}/{suffix}" if suffix else race_slug
        db_slug = next((c for c in (fetch_slug, old_slug) if c in stored), fetch_slug)
        out.append({**st, "pcs_slug": db_slug, "fetch_slug": fetch_slug})
    return out


def _fetch_slug(stage_slug: str, race_id: int, race_slug: str) -> str:
    """URL path to fetch a stored stage from (follows a race rename)."""
    alias = _race_alias(race_id, race_slug) if is_missing_page(fetch(pcs_url(stage_slug))) else None
    if alias and (stage_slug == race_slug or stage_slug.startswith(race_slug + "/")):
        return alias + stage_slug[len(race_slug):]
    return stage_slug


def _store_stage_results(conn, stage_id: int, results: list[dict], skip_riders: bool,
                         riders_seen: dict[str, int]) -> None:
    for r in results:
        slug = r["rider_slug"]
        if slug not in riders_seen:
            if not skip_riders:
                rider = fetch_rider(slug)
                rider_id = upsert_rider(conn, rider if rider else _stub_rider(slug, r["rider_name"]))
            else:
                existing = conn.execute("SELECT id FROM riders WHERE pcs_slug = ?", (slug,)).fetchone()
                rider_id = existing["id"] if existing else upsert_rider(conn, _stub_rider(slug, r["rider_name"]))
            riders_seen[slug] = rider_id

        insert_result(conn, {
            "stage_id": stage_id,
            "rider_id": riders_seen[slug],
            "position": r["position"],
            "status": r["status"],
            "time_seconds": r["time_seconds"],
            "points_pcs": r["points_pcs"],
            "points_uci": r["points_uci"],
            "bib": r["bib"],
            "team_slug": r.get("team_slug"),
            "team_name": r.get("team_name"),
            "uci_penalty": r.get("uci_penalty"),
        })


def _scrape_race_results(race_id: int, race_slug: str, skip_riders: bool,
                         min_results: int = 1) -> int:
    """Fetch and store results for all stages of one race. Returns total results stored.

    Skips stages that already have at least ``min_results`` results (idempotent
    re-runs; raise it to redo stages that only got a partial table).  After a
    stage is scraped, NULL stage metadata (distance, elevation, profile score,
    ...) is filled from the same — cached — stage page.
    """
    fetched = fetch_race_stages(race_slug)
    if not fetched:
        alias = _race_alias(race_id, race_slug)
        if alias:
            fetched = _alias_stage_list(race_id, race_slug, alias)
    stages = _checked_stage_list(race_id, race_slug, fetched)
    if not stages:
        log.warning("no stages found for %s", race_slug)
        return 0

    total = 0
    riders_seen: dict[str, int] = {}
    for stage_data in stages:
        if not stage_data.get("pcs_slug"):
            log.warning("skipping stage with empty slug in %s", race_slug)
            continue
        stage_data["race_id"] = race_id

        with get_conn() as conn:
            known = conn.execute(
                "SELECT race_id FROM stages WHERE pcs_slug = ?", (stage_data["pcs_slug"],)
            ).fetchone()
            if known is None:
                log.info("  new stage %s", stage_data["pcs_slug"])
            elif known["race_id"] != race_id:
                log.warning("  stage %s belongs to race id %d, not %d — skipping",
                            stage_data["pcs_slug"], known["race_id"], race_id)
                continue
            stage_id = upsert_stage(conn, stage_data)
            existing = conn.execute(
                "SELECT COUNT(*) FROM results WHERE stage_id = ?", (stage_id,)
            ).fetchone()[0]

        if existing >= max(min_results, 1):
            log.info("  stage %s already has %d results — skipping", stage_data["pcs_slug"], existing)
            total += existing
            continue

        fetch_slug = stage_data.get("fetch_slug") or stage_data["pcs_slug"]
        results = fetch_stage_results(fetch_slug)
        if not results:
            log.warning("no results for stage %s", stage_data["pcs_slug"])
            continue

        # Stage metadata from the same (cached) page the results came from: it
        # describes the stage as raced (the stored values may be a provisional
        # route — shortened stages, changed towns, a day's shift).
        meta = fetch_stage_meta(fetch_slug)
        with get_conn() as conn:
            _store_stage_results(conn, stage_id, results, skip_riders, riders_seen)
            if meta:
                fill_stage_meta(conn, stage_id, _within_race_window(conn, race_id, meta), overwrite=True)
        total += len(results)
        via = f" (from {fetch_slug})" if fetch_slug != stage_data["pcs_slug"] else ""
        log.info("  stage %s → %d results%s", stage_data["pcs_slug"], len(results), via)

    return total


def cmd_init(_args):
    """Initialise the database."""
    init_db()
    log.info("database initialised at %s", DB_PATH)


def cmd_scrape(args):
    """Scrape races, stages, results, and rider profiles."""
    init_db()
    years = [int(y) for y in args.years.split(",")] if args.years else SCRAPE_YEARS
    if args.gender == "women":
        circuits = {WOMEN_CIRCUIT: WOMEN_RACE_CLASSES}
    else:
        circuits = MEN_CIRCUITS

    log.info("scraping races for years: %s (gender=%s)", years, args.gender)
    for race in iter_races(years, circuits=circuits):
        with get_conn() as conn:
            race_id = upsert_race(conn, race)
        log.info("race: %s (id=%d)", race["name"], race_id)
        _scrape_race_results(race_id, race["pcs_slug"], args.skip_riders)

    log.info("scraping complete")


def cmd_train(args):
    """Train the stage outcome classifier then the top-10 probability model."""
    from model.stage_classifier import train as train_stage_clf
    from model.train import train
    train_stage_clf(cutoff_date=args.cutoff)
    train(train_cutoff=args.cutoff, val_race_slug=args.val_race, gender=args.gender, target=getattr(args, 'target', 'top10'))


def cmd_backtest(args):
    """Backtest predictions against a historic race."""
    from model.backtest import backtest
    backtest(args.race, args.cutoff, args.top_n, gender=args.gender)


def cmd_predict(args):
    """Predict top-10 probabilities for a race."""
    from model.predict import predict_race, print_predictions
    df = predict_race(args.race, args.cutoff, gender=args.gender)
    if df.empty:
        log.error("no predictions generated")
        return
    print_predictions(df, top_n=args.top_n)


def cmd_scrape_elevation(args):
    """Backfill elevation_m for stages that are missing it.

    Also fixes NULL date/distance/profile_type for one-day stages whose metadata
    wasn't scraped correctly (e.g. because _parse_infolist used div.bold instead
    of div.title).
    """
    from scraper.races import fetch_stage_elevation, fetch_race_stages, fetch_result_meta
    from db.database import get_conn

    # ── fix one-day stages with NULL date (broken metadata parse) ─────────────
    with get_conn() as conn:
        null_meta = conn.execute(
            """SELECT s.id, s.pcs_slug, r.is_stage_race
               FROM stages s JOIN races r ON s.race_id = r.id
               WHERE s.date IS NULL
               ORDER BY s.id"""
        ).fetchall()

    log.info("stages with NULL date: %d", len(null_meta))
    meta_fixed = 0
    for row in null_meta:
        stages = fetch_race_stages(row["pcs_slug"]) if not row["is_stage_race"] else []
        if stages and stages[0].get("date"):
            s = stages[0]
            with get_conn() as conn:
                conn.execute(
                    """UPDATE stages SET date=?, distance_km=?, elevation_m=?,
                       profile_type=?, departure=?, arrival=?
                       WHERE id=?""",
                    (s["date"], s["distance_km"], s["elevation_m"],
                     s["profile_type"], s["departure"], s["arrival"], row["id"]),
                )
            meta_fixed += 1
            log.info("  meta fixed: %s → %s", row["pcs_slug"], s["date"])
        else:
            log.debug("  no meta found: %s", row["pcs_slug"])

    log.info("meta backfill complete: %d/%d fixed", meta_fixed, len(null_meta))

    # ── backfill elevation_m ───────────────────────────────────────────────────
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, pcs_slug FROM stages
               WHERE elevation_m IS NULL AND pcs_slug NOT LIKE '%/result'
               ORDER BY id"""
        ).fetchall()

    log.info("stages missing elevation: %d", len(rows))
    updated = 0
    for row in rows:
        elev = fetch_stage_elevation(row["pcs_slug"])
        if elev:
            with get_conn() as conn:
                conn.execute(
                    "UPDATE stages SET elevation_m = ? WHERE id = ?",
                    (elev, row["id"]),
                )
            updated += 1
            log.info("  %s → %d m", row["pcs_slug"], elev)
        else:
            log.debug("  %s → no data", row["pcs_slug"])

    log.info("elevation backfill complete: %d/%d updated", updated, len(rows))

    # ── backfill gradient_final_km / profile_score / profile_type ─────────────
    with get_conn() as conn:
        grad_rows = conn.execute(
            """SELECT id, pcs_slug FROM stages
               WHERE gradient_final_km IS NULL
               ORDER BY id"""
        ).fetchall()

    log.info("stages missing gradient/profile meta: %d", len(grad_rows))
    meta_updated = 0
    for row in grad_rows:
        meta = fetch_result_meta(row["pcs_slug"])
        if meta.get("gradient_final_km") is not None or meta.get("profile_score") is not None:
            with get_conn() as conn:
                conn.execute(
                    """UPDATE stages SET
                       gradient_final_km = COALESCE(?, gradient_final_km),
                       profile_score     = COALESCE(?, profile_score),
                       profile_type      = COALESCE(profile_type, ?)
                       WHERE id = ?""",
                    (meta.get("gradient_final_km"), meta.get("profile_score"),
                     meta.get("profile_type"), row["id"]),
                )
            meta_updated += 1
            log.info("  %s → grad=%.1f%% score=%s profile=%s",
                     row["pcs_slug"],
                     meta.get("gradient_final_km") or 0,
                     meta.get("profile_score"),
                     meta.get("profile_type"))
        else:
            log.debug("  no meta: %s", row["pcs_slug"])

    log.info("gradient/profile backfill: %d/%d updated", meta_updated, len(grad_rows))


def cmd_scrape_race(args):
    """Scrape stages, results, and optionally rider profiles for a single race slug."""
    init_db()
    race_slug = args.race_slug

    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM races WHERE pcs_slug = ?", (race_slug,)
        ).fetchone()

    if not row:
        log.error(
            "Race '%s' not found in DB. Run 'scrape' first to populate the calendar, "
            "or check the slug.",
            race_slug,
        )
        return

    log.info("scraping %s", race_slug)
    total = _scrape_race_results(row["id"], race_slug, args.skip_riders)
    log.info("done: %d total results for %s", total, race_slug)


def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()] if value else []


def _race_filter_sql(gender: str | None, race_slugs: list[str], alias: str = "r") -> tuple[str, list]:
    sql, params = "", []
    if gender:
        sql += f" AND {alias}.gender = ?"
        params.append(gender)
    if race_slugs:
        sql += f" AND {alias}.pcs_slug IN ({','.join('?' * len(race_slugs))})"
        params.extend(race_slugs)
    return sql, params


def _missing_stage_counts(year: int, today: str, gender: str | None,
                          race_slugs: list[str], min_results: int) -> dict[str, int]:
    """Stages dated before today (or undated, in finished races) with fewer
    than ``min_results`` results, by race gender."""
    where, params = _race_filter_sql(gender, race_slugs)
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT r.gender, COUNT(*) FROM stages s JOIN races r ON r.id = s.race_id
            WHERE r.year = ? AND r.end_date < ?
              AND (s.date IS NULL OR s.date < ?)
              AND (SELECT COUNT(*) FROM results x WHERE x.stage_id = s.id) < ?
              {where}
            GROUP BY r.gender
            """,
            [year, today, today, max(min_results, 1), *params],
        ).fetchall()
    return {r[0]: r[1] for r in rows}


def cmd_scrape_missing(args):
    """Scrape finished races whose results are missing or incomplete.

    A race qualifies when it ended before today and has no stages yet, or at
    least one stage with fewer than ``--min-results`` results (default 1, i.e.
    zero-result stages: unscraped races and PARTIALLY scraped ones).  Stages
    that already have results are skipped.  ``--backfill-teams`` then
    re-parses every stage of the selected races (cache first) and fills
    team_slug / team_name / uci_penalty on existing rows.
    """
    init_db()
    today = datetime.date.today().isoformat()
    year = args.year
    gender = getattr(args, "gender", None)
    race_slugs = _csv(getattr(args, "races", None))
    min_results = max(getattr(args, "min_results", 1) or 1, 1)
    where, params = _race_filter_sql(gender, race_slugs)

    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT r.id, r.pcs_slug, r.name, r.end_date, r.gender,
                   (SELECT COUNT(*) FROM stages s WHERE s.race_id = r.id) AS n_stages,
                   (SELECT COUNT(*) FROM stages s WHERE s.race_id = r.id
                      AND (SELECT COUNT(*) FROM results x WHERE x.stage_id = s.id) < ?) AS n_missing
            FROM races r
            WHERE r.year = ?
              AND r.end_date < ?
              {where}
              AND (NOT EXISTS (SELECT 1 FROM stages s WHERE s.race_id = r.id)
                   OR EXISTS (SELECT 1 FROM stages s WHERE s.race_id = r.id
                              AND (SELECT COUNT(*) FROM results x WHERE x.stage_id = s.id) < ?))
            ORDER BY r.end_date, r.id
            """,
            [min_results, year, today, *params, min_results],
        ).fetchall()

    before = _missing_stage_counts(year, today, gender, race_slugs, min_results)
    log.info("found %d races with missing results (year=%d, ended before %s, gender=%s); "
             "stages missing by gender: %s", len(rows), year, today, gender or "all", before)
    for i, row in enumerate(rows, 1):
        log.info("[%d/%d] %s (%s, %s, ended %s, %d/%d stages missing)", i, len(rows), row["name"],
                 row["pcs_slug"], row["gender"], row["end_date"], row["n_missing"], row["n_stages"])
        try:
            _scrape_race_results(row["id"], row["pcs_slug"], skip_riders=True, min_results=min_results)
        except Exception:
            log.exception("failed to scrape %s — continuing", row["pcs_slug"])

    after = _missing_stage_counts(year, today, gender, race_slugs, min_results)
    log.info("scrape-missing complete; stages still missing by gender: %s (before: %s)", after, before)

    if getattr(args, "backfill_teams", False):
        _backfill_teams(year, today, gender, race_slugs)


def _backfill_teams(year: int, today: str, gender: str | None, race_slugs: list[str]) -> None:
    """Re-parse stages of finished races (cache first) and fill team_slug /
    team_name / uci_penalty where NULL.  No other column is touched."""
    where, params = _race_filter_sql(gender, race_slugs)
    with get_conn() as conn:
        stages = conn.execute(
            f"""
            SELECT s.id, s.pcs_slug, r.id AS race_id, r.pcs_slug AS race_slug
            FROM stages s JOIN races r ON r.id = s.race_id
            WHERE r.year = ? AND r.end_date < ? {where}
              AND EXISTS (SELECT 1 FROM results x WHERE x.stage_id = s.id
                          AND (x.team_slug IS NULL OR x.team_name IS NULL))
            ORDER BY s.date, s.id
            """,
            [year, today, *params],
        ).fetchall()
    log.info("backfill-teams: %d stages with rows lacking team data", len(stages))
    filled_total = 0
    for i, st in enumerate(stages, 1):
        results = fetch_stage_results(_fetch_slug(st["pcs_slug"], st["race_id"], st["race_slug"]))
        if not results:
            log.warning("  [%d/%d] %s: no results parsed — skipped", i, len(stages), st["pcs_slug"])
            continue
        filled = unmatched = 0
        with get_conn() as conn:
            ids = {r["pcs_slug"]: r["id"] for r in conn.execute(
                """SELECT rd.pcs_slug, rd.id FROM results x JOIN riders rd ON rd.id = x.rider_id
                   WHERE x.stage_id = ?""", (st["id"],)).fetchall()}
            for r in results:
                rider_id = ids.get(r["rider_slug"])
                if rider_id is None:
                    unmatched += 1
                    continue
                filled += fill_result_teams(conn, st["id"], rider_id, r.get("team_slug"),
                                            r.get("team_name"), r.get("uci_penalty"))
        filled_total += filled
        log.info("  [%d/%d] %s: %d rows filled (%d page rows not in DB)", i, len(stages),
                 st["pcs_slug"], filled, unmatched)
    log.info("backfill-teams complete: %d rows filled", filled_total)


def cmd_repair_points(args):
    """Re-fetch stages whose stored UCI points look corrupted and fix them.

    The pre-Oct-2026 parser glued a sanction's digits onto the earned value
    ('800 -25' → 80025) and stored bare sanctions as points.  Suspicious
    stages (see db.database.uci_points_anomalies) are re-fetched (HTML cache
    first) and re-parsed; every matched row takes the page's position,
    status, points_uci and uci_penalty (and time / PCS points / bib when the
    page has them) and NULL team fields are filled — positions too, because
    PCS reorders stages after the first scrape and new points on a stale order
    are inconsistent.  Every change is logged.
    """
    init_db()
    race_slugs = _csv(getattr(args, "races", None))
    dry_run = getattr(args, "dry_run", False)
    with get_conn() as conn:
        suspects = find_implausible_uci_stages(conn, args.year, getattr(args, "gender", None), race_slugs)
    log.info("repair-points: %d stages with implausible UCI points (year=%d)%s",
             len(suspects), args.year, " [dry run]" if dry_run else "")
    changed_rows = teams = 0
    for i, st in enumerate(suspects, 1):
        log.info("[%d/%d] %s (%s)", i, len(suspects), st["pcs_slug"], ", ".join(st["reasons"]))
        results = fetch_stage_results(_fetch_slug(st["pcs_slug"], st["race_id"], st["race_slug"]))
        if not results:
            log.warning("  no results parsed — left unchanged")
            continue
        with get_conn() as conn:
            rep = repair_stage_points(conn, st["stage_id"], results, dry_run=dry_run)
        for slug, old_pts, new_pts, old_pen, new_pen in rep["changed"]:
            log.info("  %s: points_uci %s -> %s, uci_penalty %s -> %s", slug, old_pts, new_pts, old_pen, new_pen)
        for slug, old_pos, new_pos, old_status, new_status in rep["moved"]:
            log.info("  %s: position %s (%s) -> %s (%s)", slug, old_pos, old_status, new_pos, new_status)
        if rep["unmatched_db"]:
            log.warning("  %d DB rows not on the page (left unchanged): %s", len(rep["unmatched_db"]),
                        ", ".join(rep["unmatched_db"][:10]))
        changed_rows += len(rep["changed"])
        teams += rep["teams_filled"]
    with get_conn() as conn:
        remaining = find_implausible_uci_stages(conn, args.year, getattr(args, "gender", None), race_slugs)
    log.info("repair-points complete: %d rows changed, %d rows got team data; %d stages still flagged%s",
             changed_rows, teams, len(remaining), " [dry run]" if dry_run else "")
    for st in remaining:
        log.info("  still flagged: %s (%s)", st["pcs_slug"], ", ".join(st["reasons"]))


def _surname_tokens(name: str | None, upper_only: bool = False) -> set[str]:
    """Accent-free lower-case name tokens (≥ 3 letters); ``upper_only`` keeps
    only the PCS surname tokens of a 'PIDCOCK Tom' style name."""
    import re
    import unicodedata
    toks = [t for t in re.split(r"[\s\-']+", name or "") if t]
    if upper_only:
        upper = [t for t in toks if t.isupper()]
        toks = upper or toks
    out = set()
    for t in toks:
        t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().lower()
        if len(t) >= 3:
            out.add(t)
    return out


def _slug_aliases(page_rows: list[dict], stored: dict[str, str]) -> dict[str, str]:
    """Page rider slug -> stored rider slug for riders PCS re-slugged
    (``thomas-pidcock`` stored, ``tom-pidcock`` on today's pages).

    ``page_rows`` are page riders without a stored row on the stage,
    ``stored`` maps the stage's stored slugs that are not on the page to the
    rider's stored name.  A pair is an alias when the page rider's surname
    tokens all appear in the stored rider's name or slug and the match is
    unique on both sides.
    """
    tokens = {s: _surname_tokens(n) | _surname_tokens(s.replace("-", " ")) for s, n in stored.items()}
    cands = {}
    for r in page_rows:
        sur = _surname_tokens(r.get("rider_name"), upper_only=True)
        hits = [s for s, tok in tokens.items() if sur and sur <= tok]
        if len(hits) == 1:
            cands[r["rider_slug"]] = hits[0]
    targets = [v for v in cands.values()]
    return {k: v for k, v in cands.items() if targets.count(v) == 1}


def _sync_stage_results(stage_id: int, results: list[dict], dry_run: bool = False) -> dict:
    """Make one stored stage equal a fresh parse of its page.

    * stored rows of riders on the page: every field refreshed
      (``repair_stage_points``);
    * page riders with no stored row: inserted (stub riders as in
      ``scrape-missing``), unless PCS merely re-slugged a stored rider
      (``_slug_aliases``) — then the stored row is updated in place and the
      rider keeps his id and history;
    * stored riders no longer on the page: deleted, but only when the page
      parse is at least 90% the size of the stored stage (a truncated top-N
      fallback table never deletes rows).

    Returns repair_stage_points' report plus inserted / pruned / aliases.
    """
    with get_conn() as conn:
        stored = {r[0]: r[1] for r in conn.execute(
            """SELECT rd.pcs_slug, rd.name FROM results x JOIN riders rd ON rd.id = x.rider_id
               WHERE x.stage_id = ?""", (stage_id,))}
        page_slugs = {r["rider_slug"] for r in results}
        aliases = _slug_aliases([r for r in results if r["rider_slug"] not in stored],
                                {s: n for s, n in stored.items() if s not in page_slugs})
        rows = [dict(r, rider_slug=aliases.get(r["rider_slug"], r["rider_slug"])) for r in results]
        rep = repair_stage_points(conn, stage_id, rows, dry_run=dry_run)
        new_rows = [r for r in rows if r["rider_slug"] not in rep["matched"]]
        prune = rep["unmatched_db"] if len(rows) >= 0.9 * len(stored) else []
        if not dry_run:
            _store_stage_results(conn, stage_id, new_rows, skip_riders=True, riders_seen={})
            delete_stage_rows(conn, stage_id, prune)
    rep.update({"inserted": [r["rider_slug"] for r in new_rows], "pruned": prune,
                "kept_unmatched": [s for s in rep["unmatched_db"] if s not in prune],
                "aliases": aliases})
    return rep


def cmd_refresh_stages(args):
    """Re-parse stored stages from their (cached) pages and sync every row.

    Selectors (combine freely; at least one):
      --null-gaps     stages with a ranked finisher behind the winner whose
                      time_seconds is NULL (pre-Oct-2026 time-trial parser bug)
      --uci-mismatch  stages whose stored positions / points_uci disagree with
                      rider_uci_results (PCS corrected the order after the
                      first scrape; team time trials stored from the GC tab)
      --stages        explicit stage slugs
    ``scrape-missing`` skips any stage that already has results, so these
    stages are never refreshed otherwise.  Pages come from the HTML cache when
    present.  Each stage is synced with ``_sync_stage_results`` (rows updated,
    new riders inserted, riders no longer on the page removed); every change
    is logged.  ``--dry-run`` logs without writing.
    """
    init_db()
    race_slugs = _csv(getattr(args, "races", None))
    gender = getattr(args, "gender", None)
    dry_run = getattr(args, "dry_run", False)
    selected: dict[int, dict] = {}
    with get_conn() as conn:
        if getattr(args, "null_gaps", False):
            for st in find_null_gap_stages(conn, args.year, gender, race_slugs):
                selected.setdefault(st["stage_id"], st)
        if getattr(args, "uci_mismatch", False):
            for st in find_stages_disagreeing_with_rider_results(conn, args.year, gender, race_slugs):
                prev = selected.setdefault(st["stage_id"], st)
                if prev is not st:
                    prev["reasons"] = sorted(set(prev["reasons"]) | set(st["reasons"]))
        for slug in _csv(getattr(args, "stages", None)):
            row = conn.execute(
                """SELECT s.id, s.pcs_slug, s.date, ra.pcs_slug, ra.id FROM stages s
                   JOIN races ra ON ra.id = s.race_id WHERE s.pcs_slug = ?""", (slug,)).fetchone()
            if row is None:
                log.warning("stage %s is not in the DB — skipped", slug)
                continue
            selected.setdefault(row[0], {"stage_id": row[0], "pcs_slug": row[1], "date": row[2],
                                         "race_slug": row[3], "race_id": row[4], "reasons": ["requested"]})
    todo = sorted(selected.values(), key=lambda st: (st["date"] or "", st["stage_id"]))
    log.info("refresh-stages: %d stages selected (year=%d)%s", len(todo), args.year,
             " [dry run]" if dry_run else "")
    totals = {"stages": 0, "points": 0, "moved": 0, "retimed": 0, "inserted": 0, "pruned": 0}
    for i, st in enumerate(todo, 1):
        log.info("[%d/%d] %s (%s)", i, len(todo), st["pcs_slug"], ", ".join(st["reasons"]))
        results = fetch_stage_results(_fetch_slug(st["pcs_slug"], st["race_id"], st["race_slug"]))
        if not results:
            log.warning("  no results parsed — left unchanged")
            continue
        rep = _sync_stage_results(st["stage_id"], results, dry_run=dry_run)
        meta = fetch_stage_meta(_fetch_slug(st["pcs_slug"], st["race_id"], st["race_slug"]))
        if meta and not dry_run:
            with get_conn() as conn:
                # NULL columns only — except a time-trial profile, which the
                # first scrape may have missed (Ses Salines-Felanitx 2026, a
                # one-day TTT, was stored as 'flat')
                fill_stage_meta(conn, st["stage_id"], _within_race_window(conn, st["race_id"], meta))
                if meta.get("profile_type") in ("itt", "ttt"):
                    cur = conn.execute("UPDATE stages SET profile_type = ? WHERE id = ? AND "
                                       "COALESCE(profile_type, '') != ?",
                                       (meta["profile_type"], st["stage_id"], meta["profile_type"]))
                    if cur.rowcount:
                        log.info("  profile_type -> %s", meta["profile_type"])
        for slug, old_pts, new_pts, old_pen, new_pen in rep["changed"]:
            log.info("  %s: points_uci %s -> %s, uci_penalty %s -> %s", slug, old_pts, new_pts, old_pen, new_pen)
        for slug, old_pos, new_pos, old_status, new_status in rep["moved"]:
            log.info("  %s: position %s (%s) -> %s (%s)", slug, old_pos, old_status, new_pos, new_status)
        for new, old in rep["aliases"].items():
            log.info("  %s on the page is stored rider %s — row updated in place", new, old)
        if rep["inserted"]:
            log.info("  inserted %d riders: %s", len(rep["inserted"]), ", ".join(rep["inserted"][:10]))
        if rep["pruned"]:
            log.info("  removed %d riders no longer on the page: %s", len(rep["pruned"]), ", ".join(rep["pruned"]))
        if rep["kept_unmatched"]:
            log.warning("  %d stored riders not on a short page parse (kept): %s",
                        len(rep["kept_unmatched"]), ", ".join(rep["kept_unmatched"][:10]))
        log.info("  %d points, %d positions, %d times changed; %d inserted, %d removed",
                 len(rep["changed"]), len(rep["moved"]), rep["retimed"], len(rep["inserted"]), len(rep["pruned"]))
        totals["stages"] += 1
        for k, v in (("points", len(rep["changed"])), ("moved", len(rep["moved"])), ("retimed", rep["retimed"]),
                     ("inserted", len(rep["inserted"])), ("pruned", len(rep["pruned"]))):
            totals[k] += v
    log.info("refresh-stages complete: %s%s", totals, " [dry run]" if dry_run else "")


def _load_script(name: str):
    """Import scripts/<name>.py by path (scripts/ is not a package)."""
    import importlib.util
    path = Path(__file__).resolve().parent / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"pcs_scripts_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cmd_scrape_uci_points(args):
    """Scrape the official UCI team ranking and every scoring rider's dated UCI points.

    Delegates to scripts/scrape_uci_points.py run(); every argument after the
    subcommand is passed through unchanged (see that script's --help).  The
    DB defaults to config.DB_PATH (PCS_DB_PATH honoured).
    """
    rc = _load_script("scrape_uci_points").run(args.script_args)
    if rc:
        sys.exit(rc)


def cmd_team_points(args):
    """Weekly team UCI points payload (JSON) + HTML chart + markdown summary.

    Delegates to scripts/team_points_report.py run(); every argument after the
    subcommand is passed through unchanged (see that script's --help).  The
    DB (default config.DB_PATH) is opened read-only.
    """
    rc = _load_script("team_points_report").run(args.script_args)
    if rc:
        sys.exit(rc)


def cmd_tag_surface(args):
    """Backfill surface column for existing stages based on race slug."""
    from db.database import get_conn
    from config import COBBLED_RACE_SLUGS, GRAVEL_RACE_SLUGS

    with get_conn() as conn:
        stages = conn.execute(
            "SELECT s.id, s.pcs_slug FROM stages s"
        ).fetchall()

        cobbled = updated = 0
        for row in stages:
            slug = row["pcs_slug"]
            surface = "road"
            if any(p in slug for p in COBBLED_RACE_SLUGS):
                surface = "cobbled"
            elif any(p in slug for p in GRAVEL_RACE_SLUGS):
                surface = "gravel"
            if surface != "road":
                conn.execute(
                    "UPDATE stages SET surface = ? WHERE id = ?",
                    (surface, row["id"]),
                )
                cobbled += 1
            updated += 1

    log.info("surface tagging complete: %d/%d non-road stages", cobbled, updated)


def cmd_ingest_gpx(args):
    """Parse a GPX file and attach climb data to a stage."""
    from scraper.gpx import parse_gpx
    from db.database import get_conn

    gpx_path = Path(args.gpx)
    if not gpx_path.exists():
        log.error("file not found: %s", gpx_path)
        sys.exit(1)

    data = parse_gpx(gpx_path)
    log.info(
        "GPX summary — distance: %.1f km, gain: %.0f m, climbs: %d",
        data.get("total_distance_km", 0),
        data.get("total_elevation_gain_m", 0),
        len(data.get("climbs", [])),
    )

    if args.stage_slug:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT id FROM stages WHERE pcs_slug = ?", (args.stage_slug,)
            ).fetchone()
            if not row:
                row = conn.execute(
                    "SELECT id FROM upcoming_stages WHERE pcs_slug = ?", (args.stage_slug,)
                ).fetchone()
            if not row:
                log.error("stage slug not found in DB: %s", args.stage_slug)
                sys.exit(1)

            stage_id = row["id"]
            # Store path reference
            conn.execute(
                "UPDATE stages SET gpx_path = ? WHERE id = ?", (str(gpx_path), stage_id)
            )
            # Insert climb records
            conn.execute("DELETE FROM stage_climbs WHERE stage_id = ?", (stage_id,))
            for i, climb in enumerate(data.get("climbs", []), start=1):
                conn.execute(
                    """INSERT INTO stage_climbs
                       (stage_id, climb_order, start_km, length_km,
                        elevation_gain_m, avg_gradient_pct, max_gradient_pct)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (stage_id, i, climb["start_km"], climb["length_km"],
                     climb["elevation_gain_m"], climb["avg_gradient_pct"],
                     climb["max_gradient_pct"]),
                )
        log.info("climbs stored for stage %s", args.stage_slug)


def main(argv=None):
    parser = argparse.ArgumentParser(description="PCS Race Predictor")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Initialise the database").set_defaults(func=cmd_init)

    p_scrape = sub.add_parser("scrape", help="Scrape PCS data")
    p_scrape.add_argument("--years", help="Comma-separated years, e.g. 2024,2025")
    p_scrape.add_argument("--skip-riders", action="store_true",
                          help="Don't fetch individual rider profiles (faster)")
    p_scrape.add_argument("--gender", default="men", choices=["men", "women"],
                          help="Which calendar to scrape (default: men)")
    p_scrape.set_defaults(func=cmd_scrape)

    p_train = sub.add_parser("train", help="Train the prediction model")
    p_train.add_argument("--cutoff", default="2024-05-04",
                         help="Train on data before this date (default: Giro 2024 start)")
    p_train.add_argument("--val-race", default="race/giro-d-italia/2024")
    p_train.add_argument("--gender", default="men", choices=["men", "women"])
    p_train.add_argument("--target", default="top10", choices=["top10", "top3", "win"],
                         help="Prediction target: top10 (default), top3, or win")
    p_train.set_defaults(func=cmd_train)

    p_bt = sub.add_parser("backtest", help="Backtest against a historic race")
    p_bt.add_argument("--race",   default="race/giro-d-italia/2024")
    p_bt.add_argument("--cutoff", default="2024-05-04")
    p_bt.add_argument("--top-n",  type=int, default=10, dest="top_n")
    p_bt.add_argument("--gender", default="men", choices=["men", "women"])
    p_bt.set_defaults(func=cmd_backtest)

    p_pred = sub.add_parser("predict", help="Predict top-10 for an upcoming race")
    p_pred.add_argument("race",   help="Race slug, e.g. race/tour-de-france/2026")
    p_pred.add_argument("--cutoff", required=True, help="Feature cutoff date (race start)")
    p_pred.add_argument("--top-n", type=int, default=15, dest="top_n")
    p_pred.add_argument("--gender", default="men", choices=["men", "women"])
    p_pred.set_defaults(func=cmd_predict)

    p_gpx = sub.add_parser("gpx", help="Ingest a GPX file")
    p_gpx.add_argument("gpx", help="Path to .gpx file")
    p_gpx.add_argument("--stage", dest="stage_slug",
                       help="PCS stage slug to attach climb data to")
    p_gpx.set_defaults(func=cmd_ingest_gpx)

    sub.add_parser("scrape-elevation", help="Backfill elevation_m for all stages") \
       .set_defaults(func=cmd_scrape_elevation)

    sub.add_parser("tag-surface", help="Backfill cobbled/gravel surface tags") \
       .set_defaults(func=cmd_tag_surface)

    p_scrape_race = sub.add_parser(
        "scrape-race",
        help="Scrape stages + results for a single race slug (race must already be in DB)",
    )
    p_scrape_race.add_argument("race_slug", help="PCS race slug, e.g. race/milano-sanremo/2026")
    p_scrape_race.add_argument("--skip-riders", action="store_true",
                               help="Don't fetch individual rider profiles (faster)")
    p_scrape_race.set_defaults(func=cmd_scrape_race)

    p_scrape_missing = sub.add_parser(
        "scrape-missing",
        help="Scrape finished races in DB whose results are missing or incomplete",
    )
    p_scrape_missing.add_argument("--year", type=int, default=2026,
                                  help="Calendar year to backfill (default: 2026)")
    p_scrape_missing.add_argument("--gender", choices=["men", "women"],
                                  help="Only races of this gender (default: both)")
    p_scrape_missing.add_argument("--races", help="Comma-separated race slugs to restrict to, "
                                                  "e.g. race/il-lombardia/2026")
    p_scrape_missing.add_argument("--min-results", type=int, default=1, dest="min_results",
                                  help="Re-scrape stages with fewer results than this (default 1 = "
                                       "only stages with no results)")
    p_scrape_missing.add_argument("--backfill-teams", action="store_true", dest="backfill_teams",
                                  help="Also re-parse every stage of the selected races (cache first) "
                                       "and fill team_slug/team_name/uci_penalty on existing rows")
    p_scrape_missing.set_defaults(func=cmd_scrape_missing)

    p_repair = sub.add_parser(
        "repair-points",
        help="Re-fetch stages with implausible UCI points (old penalty parsing bug) and fix them",
    )
    p_repair.add_argument("--year", type=int, required=True, help="Season to check, e.g. 2026")
    p_repair.add_argument("--gender", choices=["men", "women"], help="Only races of this gender")
    p_repair.add_argument("--races", help="Comma-separated race slugs to restrict to")
    p_repair.add_argument("--dry-run", action="store_true", dest="dry_run",
                          help="Log the changes without writing them")
    p_repair.set_defaults(func=cmd_repair_points)

    p_refresh = sub.add_parser(
        "refresh-stages",
        help="Re-parse stored stages (cache first) and sync every row: NULL time-trial gaps, "
             "orders PCS corrected after the first scrape, explicit stages",
    )
    p_refresh.add_argument("--year", type=int, required=True, help="Season, e.g. 2026")
    p_refresh.add_argument("--gender", choices=["men", "women"], help="Only races of this gender")
    p_refresh.add_argument("--races", help="Comma-separated race slugs to restrict to")
    p_refresh.add_argument("--null-gaps", action="store_true", dest="null_gaps",
                           help="Stages with ranked finishers lacking a time gap")
    p_refresh.add_argument("--uci-mismatch", action="store_true", dest="uci_mismatch",
                           help="Stages whose rows disagree with rider_uci_results (scrape-uci-points)")
    p_refresh.add_argument("--stages", help="Comma-separated stage slugs to refresh")
    p_refresh.add_argument("--dry-run", action="store_true", dest="dry_run",
                           help="Log the changes without writing them")
    p_refresh.set_defaults(func=cmd_refresh_stages)

    # Team UCI points: both delegate to scripts/*.py run(argv) — every option
    # after the subcommand is forwarded verbatim (python main.py team-points --help).
    p_uci = sub.add_parser(
        "scrape-uci-points", add_help=False,
        help="Scrape the official UCI team ranking + each scoring rider's dated UCI points "
             "(scripts/scrape_uci_points.py; e.g. --season 2026 --teams all)",
    )
    p_uci.set_defaults(func=cmd_scrape_uci_points, passthrough=True)

    p_tp = sub.add_parser(
        "team-points", add_help=False,
        help="Weekly team UCI points chart: JSON payload, HTML page, markdown summary "
             "(scripts/team_points_report.py; e.g. --season 2026 --top 30 --out-html x.html)",
    )
    p_tp.set_defaults(func=cmd_team_points, passthrough=True)

    argv = sys.argv[1:] if argv is None else list(argv)
    args, extra = parser.parse_known_args(argv)
    if getattr(args, "passthrough", False):
        args.script_args = extra  # options after the subcommand, --help included
    elif extra:
        parser.error("unrecognized arguments: " + " ".join(extra))
    args.func(args)


if __name__ == "__main__":
    main()
