import sqlite3
import pathlib
from contextlib import contextmanager
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
        ]:
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                pass  # column already exists


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
            date              = excluded.date,
            distance_km       = excluded.distance_km,
            elevation_m       = COALESCE(excluded.elevation_m, stages.elevation_m),
            profile_type      = excluded.profile_type,
            surface           = excluded.surface,
            departure         = excluded.departure,
            arrival           = excluded.arrival,
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
    conn.execute(
        """
        INSERT INTO results (stage_id, rider_id, position, status,
                             time_seconds, points_pcs, points_uci, bib)
        VALUES (:stage_id, :rider_id, :position, :status,
                :time_seconds, :points_pcs, :points_uci, :bib)
        ON CONFLICT(stage_id, rider_id) DO UPDATE SET
            position     = excluded.position,
            status       = excluded.status,
            time_seconds = excluded.time_seconds,
            points_pcs   = excluded.points_pcs,
            points_uci   = excluded.points_uci
        """,
        data,
    )
