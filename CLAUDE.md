# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A professional cycling race predictor that scrapes ProCyclingStats (PCS), engineers features, trains a gradient-boosted ML model, and outputs top-10 finish probabilities for every rider in a start list. Supports men's and women's races with separate trained models for each.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 -m playwright install chromium  # one-time: headless browser for Cloudflare bypass

python main.py init   # create SQLite schema (only needed on first run)
```

The database (`data/cycling.db`) and trained model artifacts (`model/*.pkl`, `model/*.json`) are tracked in git so you can `git pull` to get the latest state without re-scraping.

## Common Commands

```bash
# Run tests
pytest tests/

# Run a single test file
pytest tests/test_gpx.py

# Launch the web UI
streamlit run ui/app.py   # opens at http://localhost:8501

# CLI pipeline
python main.py scrape --years 2026 [--gender women] [--skip-riders]
python main.py scrape-elevation          # backfill NULL elevation_m values
python main.py tag-surface               # tag cobbled/gravel stages

python main.py train --cutoff 2026-03-09 --val-race race/tirreno-adriatico/2026
python main.py train --gender women --cutoff 2026-03-01 --val-race race/strade-bianche-women/2026

python main.py predict race/ronde-van-vlaanderen/2026 --cutoff 2026-04-06
python main.py backtest --race race/paris-nice/2026 --cutoff 2026-03-08

# Import a GPX file for climb data on a specific stage
python main.py gpx path/to/stage.gpx --stage race/tour-de-france/2026/stage-17

# Scrape results for a single race (race must already exist in DB from a prior `scrape`)
python main.py scrape-race race/milano-sanremo/2026
python main.py scrape-race race/milano-sanremo/2026 --skip-riders  # faster, no profile updates
```

See `RUNBOOK.md` for operational recipes (new-season pipeline, sharing the DB, adding ProSeries races).

## Architecture

Data flows strictly left-to-right: **scrape → SQLite → feature engineering → model → predictions**.

### Pipeline

1. **`scraper/`** — fetches HTML from PCS, caches to `cache/html/` (not tracked in git), rate-limited to 1.5 s per request. Each module handles a different page type: `races.py` (calendars, stage lists), `results.py` (stage results), `riders.py` (profiles), `startlist.py` (upcoming races), `gpx.py` (GPX climb detection).

2. **`db/`** — SQLite via stdlib `sqlite3`. All writes use upsert semantics. Key tables: `riders`, `races`, `stages`, `results`, `stage_climbs`, `upcoming_races`/`upcoming_stages`/`predictions`. Schema is in `db/schema.sql`.

3. **`features/builder.py`** — builds one row per (rider, stage) pair using 42 features. All rolling window computations are vectorised pandas (cross-merge + groupby), not per-rider Python loops. Features are grouped into: rolling form (30/60/90d windows), fatigue (race days in last 7/14d), profile affinity (mountain/flat/hilly/TT), relevant-stage rolling (profile+surface matched), stage context (distance, elevation, gradient), stage type one-hots, and rider attributes.

4. **`model/train.py`** — `HistGradientBoostingClassifier` (handles NaN natively) wrapped in `CalibratedClassifierCV(method="isotonic")`. Time-based validation split: train on all data before a cutoff date, validate on a single held-out race. Saves `model/trained_model[_women].pkl`, `model/feature_names[_women].json`, `model/metrics[_women].json`.

5. **`model/predict.py`** — inference for both historic races (joined against known results) and upcoming races (start-list based). Feature alignment is enforced via `feature_names.json`.

6. **`model/backtest.py`** — evaluates predictions with AUC, precision@10, and a GC leaderboard reconstruction.

7. **`ui/app.py`** — Streamlit UI with three tabs: Predictions (upcoming races), Historical Backtest, and Model (metrics + DB stats).

### Key Design Decisions

- **Binary target**: top-10 finish (not exact position). Calibrated probabilities are the output.
- **No leakage**: rolling features are always computed strictly before the stage date.
- **Separate models for men/women**: different race calendars, class filters, and feature distributions.
- **DB + model tracked in git**: the primary mechanism for syncing state between machines is `git pull`/`git push`, not a separate data store.
- **HTML cache**: raw HTML is cached to `cache/` and never committed. Delete cache entries to force a re-fetch.

### Configuration (`config.py`)

Central place for `PCS_BASE` URL, `REQUEST_DELAY`, `DB_PATH`, `SCRAPE_YEARS`, `RACE_CLASSES` (men and women), and lists of cobbled/gravel race slugs used for surface tagging.
