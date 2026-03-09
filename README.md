# Cycling Race Predictor

Predicts professional cycling race outcomes using historical results and rider
profiles. Given an upcoming race and its stage profiles, the model outputs
top-10 finish probabilities for every rider in the start list.

Optionally, GPX files can be ingested to enrich stage profiles with real
elevation and climb data beyond simplified tags.

---

## How it works

1. **Collect** race results and rider profiles from the web into a local SQLite database
2. **Engineer features** — rolling form windows, profile affinity, stage context
3. **Train** a calibrated gradient-boosted classifier (top-10 finish probability)
4. **Backtest or predict** — compare predictions to actual results, or score an upcoming race

### Model

Binary classification: does this rider finish top-10? (chosen over exact position
prediction — position is noisy, top-10 is a cleaner signal)

| Component | Choice | Why |
|-----------|--------|-----|
| Model | `HistGradientBoostingClassifier` | Handles missing values natively, strong on tabular data |
| Calibration | Isotonic regression (`CalibratedClassifierCV`) | Turns raw scores into meaningful probabilities |
| Validation | Time-based split | Prevents future data leaking into training |

### Features

- **Rider form**: avg position / top-10 rate / win rate / DNF rate over 30, 60, 90 day windows
- **Profile affinity**: rider's historical avg position on flat / hilly / mountain / TT stages
- **Stage context**: distance, elevation, profile type, stage number, is stage race
- **Rider attributes**: PCS ranking, speciality, weight, height (when available)

### Results so far

| Backtest | AUC | Avg precision@10 | Notes |
|----------|-----|------------------|-------|
| Giro d'Italia 2024 | 0.835 | 0.40 | Pogačar correctly ranked #1 GC, 6 stage wins |
| UAE Tour 2026 | 0.797 | 0.40 | Del Toro highest avg P(top10) on GC leaderboard |

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
├── features/
│   └── builder.py     # vectorised feature engineering (~40s for 80k rows)
├── model/
│   ├── train.py       # training + calibration
│   ├── predict.py     # inference for any race
│   └── backtest.py    # per-stage precision/recall + GC leaderboard
├── tests/             # pytest test suite
├── docs/
│   ├── phases.md      # detailed design notes per phase
│   └── gpx.md         # GPX ingestion guide
├── cache/html/        # cached raw HTML (git-ignored)
├── data/              # SQLite database (git-ignored)
├── config.py          # URLs, rate limits, years to collect
├── main.py            # CLI entry point
└── requirements.txt
```

---

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## Usage

### 1. Initialise the database
```bash
python3 main.py init
```

### 2. Collect data
```bash
# Fast first pass — no rider profiles
python3 main.py scrape --years 2023,2024,2025 --skip-riders

# Full pass with rider profiles (slower, run overnight)
python3 main.py scrape --years 2023,2024,2025
```

### 3. Train
```bash
# Train on everything before a cutoff date
python3 main.py train --cutoff 2026-02-16
```

### 4. Backtest against a historic race
```bash
python3 main.py backtest --race race/uae-tour/2026 --cutoff 2026-02-16
python3 main.py backtest --race race/giro-d-italia/2024 --cutoff 2024-05-04
```

### 5. Predict an upcoming race
```bash
python3 main.py predict race/tour-de-france/2026 --cutoff 2026-06-28
```

### 6. Ingest a GPX file
```bash
python3 main.py gpx path/to/stage.gpx --stage race/tour-de-france/2025/stage-17
```

---

## Tests

```bash
pytest tests/
```

---

## Configuration (`config.py`)

| Variable | Default | Description |
|----------|---------|-------------|
| `SCRAPE_YEARS` | `[2023, 2024]` | Years of results to collect |
| `RACE_CLASSES` | WT / HC / 2.1 | Race tiers to include |
| `REQUEST_DELAY` | `1.5` s | Pause between requests (be polite) |
| `CACHE_DIR` | `cache/html` | Where to store cached HTML |
| `DB_PATH` | `data/cycling.db` | SQLite database location |

---

## Phases

| Phase | Status | Description |
|-------|--------|-------------|
| 1 — Data collection | ✅ done | Races, stages, results, rider profiles |
| 2 — Features | ✅ done | Rolling form, profile affinity, stage context |
| 3 — Model | ✅ done | Gradient boosted classifier, calibrated probabilities |
| 4 — Predict | 🔜 next | Start list scraping, truly future race prediction |
| 5 — GPX enrichment | 🔜 planned | Real climb data from GPX files |

See [docs/phases.md](docs/phases.md) for detailed design notes.

---

## GPX support

Drop a `.gpx` file for any stage and ingest it with `python3 main.py gpx`.
The parser will detect significant climbs and store gradient stats in the
`stage_climbs` table, giving the model richer data than simplified profile tags.

See [docs/gpx.md](docs/gpx.md) for details.
