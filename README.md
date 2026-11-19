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

A feature vector (184 columns) is built for each (rider, stage) pair using only results dated before the stage:

| Feature group | Examples |
|---------------|---------|
| **Rolling form** | avg position, top-10 rate, win rate, DNF rate — 30/60/90 day windows; days raced in the last 7/14 days |
| **Point-in-time strength** | PCS points over 90/365/730 days, finishing percentile, recency-weighted form, best result, level of races ridden |
| **Elo-style ratings** | overall, bunch sprint, small group, time trial, mountain, cobbles — a result counts for more against a stronger field |
| **Finish-type skills** | top-10 rate and points in stages that ended in a bunch sprint vs a small group (from how each past stage was actually decided) |
| **Profile affinity** | results on flat / hilly / mountain / TT stages, and on the same profile + surface as the target stage |
| **Stages like this one** | earlier results weighted by how close the stage's profile score and final-km gradient were to the target stage's |
| **Race history** | earlier editions of the same race; earlier stages of this edition; GC position and deficit before the stage |
| **Team role** | rank within the team by strength, share of the team's points, leader bib |
| **Standing in the field** | percentile of the key strength features among today's starters; quality of the field |
| **Stage context** | distance, elevation, profile score, final-km gradient, surface, stage number, previous stage, race tier |
| **Stage outcome** | a small classifier's P(sprint / breakaway / GC day), from the stage's profile and how earlier editions ended |

### 3. Train

Three binary classifiers per gender: does this rider finish in the top 10, on the podium, first?

| Component | Choice | Why |
|-----------|--------|-----|
| Model | `HistGradientBoostingClassifier`, leaf-wise trees with column subsampling | Handles missing values natively, strong on tabular data |
| Calibration | Isotonic regression (`CalibratedClassifierCV`, 5 stage-grouped folds) | Turns raw scores into meaningful probabilities; the five models are averaged |
| Validation | Walk-forward (`python main.py evaluate`) | Every test stage is predicted by models trained only on earlier stages |

### 4. Predict or backtest

For upcoming races the model loads the published start list and builds the same features from history only. For past races it joins against known results; a race covered by the last walk-forward run is shown with its saved out-of-sample predictions, and any other race is flagged when the model on disk was trained on it.

---

## Accuracy

Walk-forward test on the 2025 and 2026 seasons: every stage is predicted by models trained only on stages before its part of the season (`python main.py evaluate`; details and the effect of each change in [results/accuracy_2026-11.md](results/accuracy_2026-11.md)).

| Per stage | Men (947 stages) | Women (213 stages) | Random |
|-----------|------------------|--------------------|--------|
| Of the model's 10 picks, finished in the top 10 | 4.69 | 5.03 | 0.8–0.9 |
| Of its 3 podium picks, finished on the podium | 1.09 | 1.23 | |
| Winner was its first pick | 27% | 35% | |
| Winner among its first 3 picks | 49% | 57% | |
| AUC | 0.885 | 0.891 | 0.5 |

**Top-10 hits** (precision@10): of the model's top-10 picks for a stage, how many actually finished top-10. Ranking riders by their PCS points of the last year alone gets 3.32 for the men, by an Elo rating 3.37.

**AUC** (area under the ROC curve): probability that the model ranks an actual top-10 finisher above a rider outside the top 10. 0.5 = random, 1.0 = perfect.

The probabilities are calibrated: of the riders given P(top 10) between 0.3 and 0.4, 35% finished in the top 10. The winner figures are the noisy ones (about ±1 point for the men, ±3 for the women between runs).

These figures are for stages whose route is known (profile score, gradient of the final km, elevation). `predict` fetches the route of an upcoming stage from PCS; a stage PCS has not published a profile for is predicted as a typical stage of its kind and scores 4.25 top-10 hits (men) and 4.82 (women) in the same test (`python main.py evaluate --hide-route`).

### By terrain (men)

| Terrain | Stages | Top-10 hits | Winner called | AUC |
|---------|--------|-------------|---------------|-----|
| Time trial | 56 | 5.71 | 38% | 0.921 |
| Mountain | 184 | 5.11 | 23% | 0.904 |
| Flat | 152 | 5.02 | 28% | 0.900 |
| Hilly | 555 | 4.36 | 27% | 0.872 |

Time trials, flat and mountain stages are the most predictable. Hilly stages are hardest — the label covers both bunch sprints and punchy small-group finishes, and the fields are deep.

A backtest of a single race in the UI or with `main.py backtest` says whether its numbers are out of sample. The model on disk is trained on every stored result, so scoring it on a race it has seen says little.

---

## Team UCI points chart

A second deliverable next to the predictor: how the 2026 UCI points of the top
30 men's teams (official UCI team ranking, licence cycle 2026-2028) built up
week by week, with each team's biggest weeks and headline wins (Monuments,
World titles, Grand Tours, WorldTour wins) annotated with the riders and
results behind them (e.g. *Pogačar: Tour de France GC 1st + 5 stage wins*,
*Seixas wins Il Lombardia*). Tadej Pogačar's own points are plotted next to
the teams for comparison (alone he would rank 21st on all-rider points, 20th
against the official listing).

```bash
python main.py scrape-uci-points --season 2026 --teams all     # official ranking + every scoring rider's dated points
python main.py team-points --season 2026 --top 30 \
    --out-json out/team_points_2026_men.json --out-html out/team-points.html
```

`team-points` writes a self-contained HTML page (no external requests; light
and dark mode; an *All riders* / *Best 20 riders (UCI rule)* toggle), the JSON
payload behind it, and a markdown summary on stdout. Data source: PCS's
official team ranking, each team's per-rider breakdown, and every
contributing rider's dated results, GC leader-jersey points and UCI sanctions.
The weekly ledger reproduces the per-team PCS points pages for 29 of the top
30 teams (MBH Bank's page counts two riders tied on 20th place). See [RUNBOOK.md](RUNBOOK.md#team-uci-points-weekly-chart)
for how it works and its caveats.

---

## UI

```bash
streamlit run ui/app.py
```

Four tabs:

- **Predictions** — every race that still has a stage to ride (upcoming or under way). Shows the top-N riders per stage with P(top 10), P(podium), P(win), fair odds, a CSV export and an EV calculator for pasted bookmaker odds. Out of season it says so and how to add the next calendar.
- **Historical Backtest** — pick any past race and compare the forecast with what happened: top-10 hits per stage, where the forecast ranked the winner, the actual top 10 with their forecast ranks. A banner says whether the numbers are out of sample (walk-forward, or a race after the model's training cutoff) or in sample.
- **Model** — the walk-forward accuracy (overall, by terrain, tier and season, against simple rankings), the reliability of the probabilities, what the current model was trained on and leans on, database row counts.
- **Bet Tracker** — log bets, settle them against stored results, P&L and ROI.

A static alternative, `python scripts/generate_dashboard.py`, writes `dashboard/index.html` with the upcoming races and the same headline accuracy.

---

## CLI reference

All commands run from the project root with the virtual environment active.

```bash
# Scrape a season
python main.py scrape --years 2026
python main.py scrape --years 2026 --gender women

# Backfill route data for newly scraped stages (elevation, final-km gradient, profile score, distance)
python main.py scrape-elevation

# Tag cobbled/gravel stages
python main.py tag-surface

# Train the stage classifier and the top-10 / podium / win models on every stored result
python main.py train
python main.py train --gender women

# Measure accuracy: walk-forward over 2025-2026 (saves model/evaluation*.json + walkforward*.csv.gz)
python main.py evaluate
python main.py evaluate --gender women

# Predict (stages on or after the cutoff; open stages from the PCS start list, their route fetched first)
python main.py predict race/ronde-van-vlaanderen/2027 --cutoff 2027-04-04

# Backtest one past race (says whether the numbers are out of sample)
python main.py backtest --race race/paris-nice/2026

# Fill in / repair results of finished races
python main.py scrape-missing --year 2026 --min-results 5
python main.py merge-riders --dry-run --plan-out /tmp/merge_plan.json   # riders PCS re-slugged (review the plan)
python main.py merge-riders --from-plan /tmp/merge_plan.json            # ... then merge them
python main.py repair-points --year 2026                                # also --year 2023/2024/2025
python main.py refresh-stages --year 2026 --null-gaps --uci-mismatch --dup-positions   # re-sync stale stages (RUNBOOK)
python main.py race-gender                                              # women's races stored as men's (page's race category)

# Team UCI points: scrape, then build the weekly chart (options are passed through
# to scripts/scrape_uci_points.py and scripts/team_points_report.py; add --help)
python main.py scrape-uci-points --season 2026 --teams all
python main.py team-points --season 2026 --top 30 --out-json out/tp.json --out-html out/team-points.html
# one-off: drop the zero-point rider rows an older DB stored, then shrink the file (no PCS traffic)
python main.py scrape-uci-points --prune-zero --all-seasons --vacuum
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
│   ├── uci_points.py      # UCI team ranking, team breakdowns, rider UCI points pages
│   └── gpx.py             # GPX file parsing & climb detection
├── db/
│   ├── schema.sql         # SQLite schema
│   ├── database.py        # upsert helpers, UCI points sanity checks + repair
│   ├── rider_merge.py     # re-slugged rider detection + merge, rider_slug_aliases (`main.py merge-riders`)
│   └── uci_points.py      # team UCI points tables (ranking, breakdown, scoring rider results, scrapes, sanctions, spells)
├── analysis/
│   ├── team_points.py     # weekly team ledger, jump detection + labels, chart payload
│   └── team_points_chart.py # self-contained HTML chart for the payload
├── scripts/
│   ├── scrape_uci_points.py   # `main.py scrape-uci-points`
│   ├── team_points_report.py  # `main.py team-points`
│   ├── generate_dashboard.py  # static dashboard/index.html
│   ├── check_startlist_parity.py # upcoming races get the trained-on features (no network)
│   └── ui_demo.py             # the UI with a finished race re-opened as an upcoming one
├── features/
│   ├── builder.py         # vectorised, point-in-time feature engineering (historic stages and start lists)
│   └── ratings.py         # Elo-style rider ratings, updated stage by stage
├── model/
│   ├── stage_classifier.py # how will the stage be decided: sprint / breakaway / GC day
│   ├── train.py           # training + calibration (top 10, podium, win)
│   ├── predict.py         # inference, start-list entries, backtest predictions
│   ├── evaluate.py        # walk-forward evaluation and its metrics (`main.py evaluate`)
│   └── backtest.py        # one race, printed (`main.py backtest`)
├── tests/                 # pytest test suite
├── results/accuracy_2026-11.md # how accurate the model is, and what each change was worth
├── data/cycling.db        # SQLite database (tracked in git, ~31 MB incl. the trimmed UCI points tables)
├── model/trained_model*.pkl # trained models (tracked in git): top 10, _top3, _win; _women
├── model/metrics*.json    # what each model was trained on, feature importances
├── model/evaluation*.json # walk-forward accuracy shown in the UI
├── model/walkforward*.csv.gz # out-of-sample predictions per rider and stage (UI backtests)
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

- **Hilly stages** are the hardest to predict — half of them end in a bunch sprint and half in a small group, and the fields are deep
- **Breakaway winners** are not predictable from results: on about one stage in eight the winner sits outside the model's first 30 picks
- **New riders** (sparse history) get mostly empty features and land in the middle of predictions
- **Rider profiles** (age, weight, height) are stored for a quarter of the riders only, so they carry no weight yet
- **Women's racing** has a quarter of the men's data, so its numbers are noisier
- **In a stage race** later stages are predicted with today's form and GC; re-run after each stage
- **A stage without a published profile** is predicted as a typical stage of its kind, at about 0.4 top-10 hits less (0.65 on hilly stages); the prediction says so, run it again closer to the race
