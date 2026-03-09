"""CLI entry point for the PCS predictor pipeline."""
import argparse
import logging
import sys
from pathlib import Path

from db.database import init_db, get_conn, upsert_race, upsert_stage, upsert_rider, insert_result
from scraper.races import iter_races, fetch_race_stages
from scraper.results import fetch_stage_results
from scraper.riders import fetch_rider
from config import SCRAPE_YEARS, DB_PATH

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def cmd_init(_args):
    """Initialise the database."""
    init_db()
    log.info("database initialised at %s", DB_PATH)


def cmd_scrape(args):
    """Scrape races, stages, results, and rider profiles."""
    init_db()
    years = [int(y) for y in args.years.split(",")] if args.years else SCRAPE_YEARS

    log.info("scraping races for years: %s", years)
    with get_conn() as conn:
        for race in iter_races(years):
            race_id = upsert_race(conn, race)
            log.info("race: %s (id=%d)", race["name"], race_id)

            stages = fetch_race_stages(race["pcs_slug"])
            for stage_data in stages:
                stage_data["race_id"] = race_id
                stage_id = upsert_stage(conn, stage_data)

                # Results
                results = fetch_stage_results(stage_data["pcs_slug"])
                riders_seen: dict[str, int] = {}

                for r in results:
                    slug = r["rider_slug"]
                    if slug not in riders_seen:
                        if not args.skip_riders:
                            rider = fetch_rider(slug)
                            if rider:
                                rider_id = upsert_rider(conn, rider)
                            else:
                                # Minimal placeholder
                                rider_id = upsert_rider(conn, {
                                    "pcs_slug": slug, "name": r["rider_name"],
                                    "nationality": None, "dob": None, "team": None,
                                    "pcs_rank": None, "speciality": None,
                                    "weight_kg": None, "height_cm": None,
                                })
                        else:
                            rider_id = upsert_rider(conn, {
                                "pcs_slug": slug, "name": r["rider_name"],
                                "nationality": None, "dob": None, "team": None,
                                "pcs_rank": None, "speciality": None,
                                "weight_kg": None, "height_cm": None,
                            })
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
                    })

    log.info("scraping complete")


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


def main():
    parser = argparse.ArgumentParser(description="PCS Race Predictor")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Initialise the database").set_defaults(func=cmd_init)

    p_scrape = sub.add_parser("scrape", help="Scrape PCS data")
    p_scrape.add_argument("--years", help="Comma-separated years, e.g. 2024,2025")
    p_scrape.add_argument("--skip-riders", action="store_true",
                          help="Don't fetch individual rider profiles (faster)")
    p_scrape.set_defaults(func=cmd_scrape)

    p_gpx = sub.add_parser("gpx", help="Ingest a GPX file")
    p_gpx.add_argument("gpx", help="Path to .gpx file")
    p_gpx.add_argument("--stage", dest="stage_slug",
                       help="PCS stage slug to attach climb data to")
    p_gpx.set_defaults(func=cmd_ingest_gpx)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
