-- Riders
CREATE TABLE IF NOT EXISTS riders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pcs_slug    TEXT UNIQUE NOT NULL,   -- e.g. "tadej-pogacar"
    name        TEXT NOT NULL,
    nationality TEXT,
    dob         TEXT,                   -- ISO date
    team        TEXT,
    pcs_rank    INTEGER,
    speciality  TEXT,                   -- climber/sprinter/puncher/tt/allrounder
    weight_kg   REAL,
    height_cm   REAL,
    updated_at  TEXT DEFAULT (datetime('now'))
);

-- Races (editions)
CREATE TABLE IF NOT EXISTS races (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pcs_slug    TEXT NOT NULL,          -- e.g. "tour-de-france/2024"
    name        TEXT NOT NULL,
    year        INTEGER NOT NULL,
    start_date  TEXT,
    end_date    TEXT,
    class       TEXT,                   -- 2.UWT etc.
    country     TEXT,
    is_stage_race INTEGER DEFAULT 0,
    UNIQUE(pcs_slug)
);

-- Stages / one-day races
CREATE TABLE IF NOT EXISTS stages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id         INTEGER NOT NULL REFERENCES races(id),
    stage_num       INTEGER,            -- NULL for one-day races
    pcs_slug        TEXT UNIQUE NOT NULL,
    date            TEXT,
    distance_km     REAL,
    elevation_m     INTEGER,
    profile_type    TEXT,               -- flat/hilly/mountain/itt/utt
    departure       TEXT,
    arrival         TEXT,
    gpx_path        TEXT                -- path to local GPX file if available
);

-- Results
CREATE TABLE IF NOT EXISTS results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    stage_id    INTEGER NOT NULL REFERENCES stages(id),
    rider_id    INTEGER NOT NULL REFERENCES riders(id),
    position    INTEGER,                -- NULL = DNF/DNS/OTL
    status      TEXT DEFAULT 'finished', -- finished/dnf/dns/otl/dsq
    time_seconds INTEGER,               -- seconds behind leader (0 for winner)
    points_pcs  INTEGER,
    points_uci  INTEGER,
    bib         INTEGER,
    UNIQUE(stage_id, rider_id)
);

-- GPX-derived climb features (populated from GPX files)
CREATE TABLE IF NOT EXISTS stage_climbs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    stage_id        INTEGER NOT NULL REFERENCES stages(id),
    climb_order     INTEGER,            -- 1 = first climb of the day
    name            TEXT,
    start_km        REAL,
    length_km       REAL,
    elevation_gain_m INTEGER,
    avg_gradient_pct REAL,
    max_gradient_pct REAL,
    category        TEXT                -- HC/1/2/3/4
);

-- Upcoming races (for prediction targets)
CREATE TABLE IF NOT EXISTS upcoming_races (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pcs_slug    TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    start_date  TEXT,
    end_date    TEXT,
    class       TEXT,
    country     TEXT,
    is_stage_race INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS upcoming_stages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    upcoming_race_id INTEGER NOT NULL REFERENCES upcoming_races(id),
    stage_num       INTEGER,
    pcs_slug        TEXT UNIQUE NOT NULL,
    date            TEXT,
    distance_km     REAL,
    elevation_m     INTEGER,
    profile_type    TEXT,
    departure       TEXT,
    arrival         TEXT,
    gpx_path        TEXT
);

-- Predictions
CREATE TABLE IF NOT EXISTS predictions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    stage_id        INTEGER REFERENCES upcoming_stages(id),
    rider_id        INTEGER REFERENCES riders(id),
    predicted_rank  INTEGER,
    top10_prob      REAL,
    podium_prob     REAL,
    win_prob        REAL,
    model_version   TEXT,
    created_at      TEXT DEFAULT (datetime('now')),
    UNIQUE(stage_id, rider_id, model_version)
);
