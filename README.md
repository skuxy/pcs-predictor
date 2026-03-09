# Cycling Race Predictor

Predicts professional cycling race outcomes by collecting historical results,
building rider profiles, and applying a machine-learning model to upcoming races.

Optionally, GPX files can be ingested to enrich race profiles with real elevation
and climb data.

---

## Project structure

```
pcs-predictor/
├── scraper/
│   ├── races.py       # race schedules & stage lists
│   ├── results.py     # stage result tables
│   ├── riders.py      # rider profile pages
│   └── gpx.py         # GPX file parsing & climb detection
├── db/
│   ├── schema.sql     # SQLite schema
│   └── database.py    # DB helpers (upsert, queries)
├── features/          # (Phase 2) feature engineering
├── model/             # (Phase 3) training & prediction
├── cache/html/        # cached raw HTML (git-ignored)
├── data/              # SQLite database (git-ignored)
├── config.py          # URLs, rate limits, years to collect
├── main.py            # CLI entry point
└── requirements.txt
```

---

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## Usage

### 1. Initialise the database
```bash
python main.py init
```

### 2. Collect data
Collect all configured years (see `config.py → SCRAPE_YEARS`):
```bash
python main.py scrape
```

Specific years only:
```bash
python main.py scrape --years 2024,2025
```

Skip fetching individual rider profile pages (much faster, less detail):
```bash
python main.py scrape --skip-riders
```

### 3. Ingest a GPX file
Attach GPX-derived climb data to a stage already in the database:
```bash
python main.py gpx path/to/stage.gpx --stage race/tour-de-france/2025/stage-17
```

---

## Configuration (`config.py`)

| Variable | Default | Description |
|----------|---------|-------------|
| `SCRAPE_YEARS` | `[2024, 2025]` | Years of results to collect |
| `RACE_CLASSES` | WT / HC / 2.1 | Race tiers to include |
| `REQUEST_DELAY` | `1.5` s | Pause between requests |
| `CACHE_DIR` | `cache/html` | Where to store cached HTML |
| `DB_PATH` | `data/cycling.db` | SQLite database location |

---

## Phases

| Phase | Status | Description |
|-------|--------|-------------|
| 1 — Data collection | ✅ implemented | Collect races, stages, results, rider profiles |
| 2 — Features | 🔜 next | Build rider form, speciality-match, team features |
| 3 — Model | 🔜 planned | Train gradient-boosted classifier on historical data |
| 4 — Predict | 🔜 planned | Score riders for upcoming races |

See [docs/phases.md](docs/phases.md) for detailed design notes on each phase.

---

## GPX support

Drop a `.gpx` file (downloadable from Strava, Komoot, or official race sites) for
any stage and ingest it with `python main.py gpx`. The parser will:

- Compute total distance, elevation gain/loss, and altitude range
- Detect significant climbs (configurable thresholds)
- Store per-climb stats (length, avg/max gradient) in `stage_climbs` table

These features feed directly into Phase 2 as richer alternatives to simplified
profile tags (`flat` / `hilly` / `mountain`).

See [docs/gpx.md](docs/gpx.md) for details.
