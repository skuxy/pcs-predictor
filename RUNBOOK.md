# Runbook — Common Operations

All commands are run from the project root with the virtual environment active:

```bash
cd ~/pcs-predictor
source .venv/bin/activate
```

---

## Launch the UI

```bash
streamlit run ui/app.py
```

Opens at http://localhost:8501. Use this for:
- Viewing and running predictions
- Running backtests on past races
- Checking model performance

---

## Scraping

### Scrape a new season

```bash
python main.py scrape --years 2026
python main.py scrape --years 2026 --gender women
```

Fetches all WorldTour races, stages, results, and rider profiles for the given year. Takes ~30–60 min per year due to rate limiting (1.5s between requests). HTML is cached in `cache/html/` so re-runs are fast.

### Update rider profiles (after a --skip-riders scrape)

If you previously scraped with `--skip-riders`, fill in speciality/rank:

```bash
python main.py scrape --years 2025,2026
```

This re-runs the full scrape but upserts are idempotent — it just adds the missing profile data.

### Add a race not in the WorldTour calendar (e.g. ProSeries 1.Pro)

These races (Milano-Torino, Nokere Koerse, GP Denain, Bredene Koksijde, etc.) aren't in the default scrape. Add them manually:

```python
from db.database import get_conn, upsert_race, upsert_stage

with get_conn() as conn:
    race_id = upsert_race(conn, {
        "pcs_slug": "race/nokere-koerse/2026",
        "name": "Danilith Nokere Koerse",
        "year": 2026,
        "start_date": "2026-03-19",
        "end_date": "2026-03-19",
        "class": "1.Pro",
        "country": "BE",
        "is_stage_race": 0,
        "gender": "men",
    })
    upsert_stage(conn, {
        "race_id": race_id,
        "pcs_slug": "race/nokere-koerse/2026",
        "stage_num": None,
        "date": "2026-03-19",
        "distance_km": None,
        "elevation_m": None,
        "profile_type": "hilly",
        "surface": "cobbled",
        "departure": None,
        "arrival": None,
        "gpx_path": None,
    })
```

### Backfill elevation data

After a scrape (which doesn't fetch individual stage pages):

```bash
python main.py scrape-elevation
```

Fetches `elevation_m` for all stages missing it. Uses the HTML cache, so most will be instant if already scraped. Retrain after.

### Tag cobbled/gravel stages

After adding new races, apply surface tags:

```bash
python main.py tag-surface
```

---

## Training

### Standard retrain

After scraping new data or making feature changes:

```bash
python main.py train --cutoff 2026-03-09 --val-race race/tirreno-adriatico/2026
```

The `--cutoff` date is the validation race start date. All data before this date is used for training; the val race is held out for evaluation.

### Train on a different validation race

```bash
python main.py train --cutoff 2026-08-23 --val-race race/vuelta-a-espana/2026
```

### Train women's model

```bash
python main.py train --gender women --cutoff 2026-03-01 --val-race race/strade-bianche-women/2026
```

Women's model is saved separately as `model/trained_model_women.pkl`.

---

## Predicting

### Predict an upcoming race (UI)

Use the **UI** — select the race from the dropdown and click **Run predictions**.

### Predict from the command line

```bash
python main.py predict race/ronde-van-vlaanderen/2026 --cutoff 2026-04-06
```

The `--cutoff` date controls which historical data is used for features — set it to the race start date.

### Predict a women's race

```bash
python main.py predict race/ronde-van-vlaanderen-women/2026 --cutoff 2026-04-06 --gender women
```

---

## Backtesting

### Backtest a past race (UI)

Use the **UI** — select the race from the History tab, set the cutoff date to the race start date, and click **Run backtest**.

### Backtest from the command line

```bash
python main.py backtest --race race/paris-nice/2026 --cutoff 2026-03-08
python main.py backtest --race race/giro-d-italia/2025 --cutoff 2025-05-09
```

---

## Database

### Share the database between machines

The database is tracked in git (`data/cycling.db`, ~5–10 MB). To sync:

```bash
git pull   # get latest DB + model
```

If you've scraped new data locally and want to push it:

```bash
git add data/cycling.db model/trained_model.pkl model/metrics.json
git commit -m "Update DB and model after 2026 scrape"
git push
```

### Check database contents

```bash
python -c "
from db.database import get_conn
import pandas as pd
with get_conn() as conn:
    for t in ['races','stages','results','riders']:
        n = conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
        print(f'{t}: {n:,}')
"
```

---

## Full pipeline (new season)

```bash
# 1. Scrape new data
python main.py scrape --years 2026
python main.py scrape --years 2026 --gender women

# 2. Backfill elevation
python main.py scrape-elevation

# 3. Tag any new cobbled/gravel races
python main.py tag-surface

# 4. Retrain
python main.py train --cutoff YYYY-MM-DD --val-race race/RACE/YEAR

# 5. Commit everything
git add data/cycling.db model/trained_model.pkl model/metrics.json model/feature_names.json
git commit -m "Season YYYY data + retrain"
git push

# 6. Launch UI
streamlit run ui/app.py
```
