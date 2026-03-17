# Cycling Race Predictor

Predicts professional cycling race outcomes using historical results and rider profiles. Given an upcoming race, the model outputs top-10 finish probabilities for every rider in the start list — viewable in a web UI or from the command line.

---

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Launch the UI
streamlit run ui/app.py
```

The UI opens at http://localhost:8501.

If you're pulling an existing database from git, you can skip straight to the UI or retrain:

```bash
git pull          # gets latest data/cycling.db + model/*.pkl
streamlit run ui/app.py
```

---

## How it works

### 1. Scrape

Race calendars, stage lists, results, and rider profiles are fetched from a public cycling statistics site and stored in a local SQLite database. HTML is cached on disk so re-runs are fast. Rate-limited to 1.5 s between requests.

### 2. Engineer features

A feature vector is built for each (rider, stage) pair using only data available before the race starts:

| Feature group | Examples |
|---------------|---------|
| **Rolling form** | avg position, top-10 rate, win rate, DNF rate — 30/60/90 day windows |
| **Profile affinity** | rider's historic avg position on flat / hilly / mountain / TT stages |
| **Stage context** | distance, elevation, elevation per km, stage number, is stage race |
| **Stage type** | profile type (flat / hilly / mountain / TT), surface (road / cobbled / gravel) |
| **Stage sequence** | whether the preceding stage was mountain or hilly (recovery signal) |
| **Rider attributes** | PCS ranking, riding speciality |

### 3. Train

Binary classifier: does this rider finish top-10?

| Component | Choice | Why |
|-----------|--------|-----|
| Model | `HistGradientBoostingClassifier` | Handles missing values natively, strong on tabular data |
| Calibration | Isotonic regression (`CalibratedClassifierCV`) | Turns raw scores into meaningful probabilities |
| Validation | Time-based split (held-out race) | Prevents future data leaking into training |

### 4. Predict or backtest

For upcoming races the model loads the published start list and computes probabilities from historical features only. For past races it joins against known results to evaluate accuracy.

---

## Accuracy

Overall performance on 2025 WorldTour season (held-out test set):

| Metric | Value |
|--------|-------|
| AUC | 0.877 |
| Avg precision@10 | 0.425 |

**AUC** (area under the ROC curve): probability that the model ranks an actual top-10 finisher above a non-finisher. 0.5 = random, 1.0 = perfect.

**Precision@10**: of the model's top-10 picks for a stage, how many actually finished top-10. Baseline is 10/~150 ≈ 0.07.

### By terrain

| Terrain | AUC | Avg precision@10 |
|---------|-----|-----------------|
| Mountain | 0.928 | 0.565 |
| Flat | 0.897 | 0.454 |
| Hilly | 0.833 | 0.339 |

Mountain stages are most predictable (GC riders dominate). Hilly stages are hardest — punchy finishes attract a wider mix of rider types.

### Selected race backtests

| Race | AUC | Avg p@10 | Highlight |
|------|-----|----------|-----------|
| Giro d'Italia 2024 | 0.835 | 0.40 | Pogačar ranked #1 GC, predicted in top-10 for 15/21 stages |
| Tirreno-Adriatico 2026 | 0.872 | 0.371 | Stage 1: 7/10 correct |
| Paris-Nice 2026 | 0.776 | 0.188 | Harder — mixed terrain, volatile sprinter results |

---

## UI

```bash
streamlit run ui/app.py
```

Three tabs:

- **Predictions** — pick any upcoming race from the database, set a cutoff date, run predictions. Shows top-N riders per stage with a probability heatmap.
- **Historical Backtest** — pick any past race, run the model as if it were race day, and compare against actual results. Shows AUC, precision@10, a GC leaderboard, and a per-stage table with correct picks highlighted in green.
- **Model** — current validation metrics, terrain accuracy breakdown, feature list, database row counts.

---

## CLI reference

All commands run from the project root with the virtual environment active.

```bash
# Scrape a season
python main.py scrape --years 2026
python main.py scrape --years 2026 --gender women

# Backfill elevation data for newly scraped stages
python main.py scrape-elevation

# Tag cobbled/gravel stages
python main.py tag-surface

# Train (cutoff = start date of the validation race)
python main.py train --cutoff 2026-03-09 --val-race race/tirreno-adriatico/2026
python main.py train --gender women --cutoff 2026-03-01 --val-race race/strade-bianche-women/2026

# Predict
python main.py predict race/ronde-van-vlaanderen/2026 --cutoff 2026-04-06

# Backtest
python main.py backtest --race race/paris-nice/2026 --cutoff 2026-03-08
```

See [RUNBOOK.md](RUNBOOK.md) for detailed recipes including sharing the database between machines, adding ProSeries races, and the full new-season pipeline.

---

## Project structure

```
pcs-predictor/
├── ui/
│   └── app.py             # Streamlit web UI
├── scraper/
│   ├── races.py           # race calendars, stage lists, elevation backfill
│   ├── results.py         # stage result tables
│   ├── riders.py          # rider profile pages
│   ├── startlist.py       # start list scraping (upcoming races)
│   └── gpx.py             # GPX file parsing & climb detection
├── db/
│   ├── schema.sql         # SQLite schema
│   └── database.py        # upsert helpers
├── features/
│   └── builder.py         # vectorised feature engineering
├── model/
│   ├── train.py           # training + calibration
│   ├── predict.py         # inference (results-based + start-list-based)
│   └── backtest.py        # evaluation metrics
├── tests/                 # pytest test suite
├── data/cycling.db        # SQLite database (tracked in git, ~10 MB)
├── model/trained_model.pkl  # trained model (tracked in git)
├── model/metrics.json     # latest validation metrics
├── model/feature_names.json # feature column list
├── config.py              # URLs, rate limits, cobbled/gravel race lists
├── main.py                # CLI entry point
├── RUNBOOK.md             # operational recipes
└── requirements.txt
```

---

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py init        # create the SQLite schema
```

---

## Known limitations

- **Hilly/punchy stages** are the hardest to predict (p@10 ≈ 0.34) — the field is wider and breakaways are harder to model
- **Breakaway success** is not modelled — riders who win from a break rank low because their recent results don't show obvious form
- **New riders** (sparse history) get mostly NaN features and land in the middle of predictions
- **Women's racing** has sparser historical data so accuracy is lower
- **Grand Tour fatigue** — cumulative tiredness across a 3-week race isn't explicitly modelled yet
