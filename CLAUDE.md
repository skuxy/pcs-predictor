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

python main.py train                     # stage classifier + top-10 / podium / win models on every stored result
python main.py train --gender women
python main.py evaluate                  # walk-forward accuracy (~13 min); writes model/evaluation.json + walkforward.csv.gz
python main.py evaluate --gender women

python main.py predict race/ronde-van-vlaanderen/2027 --cutoff 2027-04-04
python main.py backtest --race race/paris-nice/2026

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

3. **`features/builder.py`** — builds one row per (rider, stage) pair with 173 features (`FEATURE_COLS`). `_history_pairs` pairs every target row with that rider's earlier results (index ranges, no cross join); every rolling feature is a masked group-by over that frame. Groups: rolling form, fatigue, profile affinity, relevant-stage rolling, stage context, GC state (original 80); point-in-time strength (PCS points, finishing percentile, recency-weighted), finish-type skills (bunch sprint vs small group, from how past stages were actually decided), earlier editions of the race and earlier stages of this edition, team role (bib decade, leader bib), rank within today's field, and Elo-style ratings from `features/ratings.py`. `entries=` adds start-list rows for stages without a result, so upcoming stages use the same code as historic ones.

4. **`model/stage_classifier.py`** — predicts how a stage will be decided (sprint / breakaway / gc) from pre-race stage facts and how earlier editions of the race ended; its probabilities are features of the main model (`attach_stage_outcome`).

5. **`model/train.py`** — `HistGradientBoostingClassifier` (leaf-wise, column subsampling, handles NaN natively) wrapped in `CalibratedClassifierCV(method="isotonic")` with 5 stage-grouped folds; one model each for top 10, podium and win. Saves `model/trained_model[_women][_top3|_win].pkl`, `model/feature_names[_women].json`, `model/metrics[_women][_top3|_win].json`.

6. **`model/predict.py`** — `predict_race` (historic stages from their results, open stages from the PCS start list), `coherent_probabilities` (P(win) sums to 1 per stage, win ≤ podium ≤ top 10) and `backtest_predictions` (saved walk-forward predictions when the race is covered, else the model on disk with a seen / unseen flag). Feature alignment is enforced via `feature_names.json`.

7. **`model/evaluate.py`** — walk-forward evaluation: each test window is predicted by models fitted only on earlier stages. Per-stage metrics (top-10 hits, podium hits, winner's rank, AUC, nDCG), breakdowns, calibration; `python main.py evaluate` saves `model/evaluation[_women].json` and `model/walkforward[_women].csv.gz`. `model/backtest.py` prints one race.

8. **`ui/app.py`** — Streamlit UI with four tabs: Predictions (races with a stage still to ride), Historical Backtest (out-of-sample banner), Model (walk-forward accuracy, calibration, importances, DB stats) and Bet Tracker.

### Key Design Decisions

- **Binary targets**: top-10 finish (headline), podium and win. Calibrated probabilities are the output.
- **No leakage**: every feature is computed from results dated strictly before the stage (`tests/test_features_v2.py` rewrites a stage's own and later results and checks its features do not move). Rider-page facts dated at scrape time (`pcs_rank`, speciality) are not model inputs.
- **Accuracy claims come from `evaluate`**: the model on disk is trained on everything, so scoring it on a past race is in-sample. Use the walk-forward numbers.
- **Separate models for men/women**: different race calendars, class filters, and feature distributions.
- **DB + model tracked in git**: the primary mechanism for syncing state between machines is `git pull`/`git push`, not a separate data store.
- **HTML cache**: raw HTML is cached to `cache/` and never committed. Delete cache entries to force a re-fetch.

### Configuration (`config.py`)

Central place for `PCS_BASE` URL, `REQUEST_DELAY`, `DB_PATH`, `SCRAPE_YEARS`, `RACE_CLASSES` (men and women), and lists of cobbled/gravel race slugs used for surface tagging.
