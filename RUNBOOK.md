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

### How PCS is fetched (Cloudflare, browser, cache)

ProCyclingStats sits behind a Cloudflare challenge: plain `requests` get a 403
"Just a moment..." page. `scraper/utils.fetch()` detects that once and from then
on fetches every page of the process through **one shared headless Chromium**
(Playwright), started lazily and reused for every URL (~1–2 s per page plus the
1.5 s `REQUEST_DELAY`). Pages are cached in `cache/html/` keyed by URL (run
commands from the project root), so re-runs are near-instant; delete a cache
file to force a re-fetch. Keep to **one fetching process at a time** and do not
lower `REQUEST_DELAY`.

Environment knobs:

| Variable | Effect |
|---|---|
| `HTTPS_PROXY` / `https_proxy` | Passed explicitly to Chromium (it ignores the env var), so egress-proxied machines work |
| `PCS_CHROMIUM_EXECUTABLE` | Use this Chromium binary instead of Playwright's bundled build |
| `PLAYWRIGHT_BROWSERS_PATH` | If the bundled build is missing, the newest `chromium-*/chrome-linux*/chrome` under this dir is used |
| `PCS_DB_PATH` | Database file to read/write (default `data/cycling.db`) — point it at a copy for experiments |

```bash
python3 -m playwright install chromium     # one-time, if no Chromium is provisioned
cp data/cycling.db /tmp/work.db
PCS_DB_PATH=/tmp/work.db python main.py scrape-missing --year 2026
```

PCS page layout (Oct 2026) — what the scrapers rely on:
- Result tables: column mapping comes from `th[data-code]` (`rnk`, `bib`, `rider`, `riderteam`, `uci_pnt`, `pnt`, `timelag`); a stage page has one `div.resultCont` per tab (stage, GC, points, ...) and the scraper always takes the **Stage** tab. Team time trials are a `ul.ttt-results` list and are stored per rider (team time + own gap, team UCI share).
- UCI sanctions appear in the UCI cell as `800 <span class="red">-25</span>`: `results.points_uci` holds the earned 800, `results.uci_penalty` the 25.
- A stage race's overview URL now shows the final stage; the stage list is read from `race/<slug>/<year>/gc/stages` (dates are MM/DD there). Links use `race/<name>-<year>-stage-N`; they are normalised back to the DB's `race/<name>/<year>/stage-N`.

### Fill in missing / incomplete results

```bash
python main.py scrape-missing --year 2026                 # races that ended before today with stages lacking results
python main.py scrape-missing --year 2026 --gender women  # only one calendar
python main.py scrape-missing --year 2026 --min-results 5 # also redo stages that only got a partial (<5 rows) table
python main.py scrape-missing --year 2026 --races race/il-lombardia/2026,race/tour-de-france/2026
```

Picks up unscraped **and partially scraped** races (some stages without
results) that ended before today; stages that already have results are skipped,
so it is safe to re-run (a re-run is all cache hits; races PCS has no results for
are recognised from their page and not re-fetched). Rider profiles are not
fetched (stub rows for new riders; existing rider rows are left untouched). New
rows carry `team_slug`, `team_name` and `uci_penalty`, and the stage's
date/distance/elevation/profile/towns are updated from the page the results came
from (the stored values can be a provisional route).

Things the log will tell you about:
- **Renamed races.** PCS answers a dead slug with an empty page. The race is then
  looked up in the season calendar (same class, start and end date) and fetched
  from its new slug, but stored under the existing DB stages — e.g.
  `race/dauphine/2026` → `race/tour-auvergne-rhone-alpes/2026` ("race X is now Y
  on PCS"). Note that a later `scrape --years` will add the renamed race as a
  second race row.
- **Restructured stage lists.** When PCS re-numbers a race after the calendar
  scrape (a prologue / `stage-0` added), the new stages are inserted ("new
  stage ...") and stage dates follow PCS; stored stages PCS no longer lists are
  left alone (they stay empty).
- **Still empty** after a run usually means PCS itself has no results (empty
  table, no "Won how"), or a cancelled stage (all `NR`).

`--backfill-teams` additionally re-parses every stage of the selected races
(cache first, network otherwise) and fills `team_slug` / `team_name` /
`uci_penalty` on existing rows without touching any other column. A whole season
is ~650 pages — restrict it with `--races` unless the cache is warm:

```bash
python main.py scrape-missing --year 2026 --races race/paris-nice/2026,race/ronde-van-vlaanderen/2026 --backfill-teams
```

### Repair corrupted UCI points

Before Oct 2026 the parser glued a sanction's digits onto the earned points
(`800 -25` → 80025, `15 -25` → 1525) and stored a bare `-25` as 25 points.
`repair-points` finds stages whose stored `points_uci` are implausible (above 800,
not decreasing with position, points after non-scoring finishers, points on
DNF rows, a team time trial with fewer than 4 riders holding a share),
re-fetches them (cache first) and rewrites every matched row from the page —
`points_uci` + `uci_penalty`, and also position, status, time, PCS points and
bib, because PCS reorders stages after the first scrape and new points on a
stale order are inconsistent (Amstel Gold Race Women 2026, 20th/21st) — and
fills team columns, logging each change.

```bash
python main.py repair-points --year 2026 --dry-run   # log what would change
python main.py repair-points --year 2026
```

A second run should report 0 stages still flagged (stages whose rows can't be
matched to the page are listed).

### Refresh stale stages

`scrape-missing` skips any stage that already has results, so a stage stored
before a parser fix or before PCS corrected its order is never refreshed by it.
`refresh-stages` re-parses selected stages (cache first) and syncs every row:
stored rows take the page's values, page riders missing from the stage are
inserted, riders no longer on the page are removed (only when the page parse is
at least 90% the size of the stored stage), and a rider PCS re-slugged
(`thomas-pidcock` → `tom-pidcock`) keeps his stored row. NULL stage metadata is
filled from the page, and a time-trial profile (`itt`/`ttt`) overrides a wrong
stored one.

```bash
python main.py refresh-stages --year 2026 --null-gaps --uci-mismatch --dry-run
python main.py refresh-stages --year 2026 --null-gaps --uci-mismatch
python main.py refresh-stages --year 2026 --stages race/paris-nice/2026/stage-3
```

- `--null-gaps`: ranked finishers behind the winner with no time. Before Oct
  2026 the parser read only the hundredths of live time-trial cells
  (`0:28<font>,36</font>` → NULL), so every 2026 ITT/prologue lost its gaps.
- `--uci-mismatch`: stages whose stored positions / `points_uci` disagree with
  `rider_uci_results` (needs `scrape-uci-points` first; DB only, no traffic) —
  PCS corrections after the race, and team time trials stored from the GC tab.

The Oct 2026 run refreshed 120 stages (4,036 times, 342 positions, 251 UCI
points, 78 riders inserted, none removed).

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

## Team UCI points (weekly chart)

How the top 30 men's teams' UCI points evolved, week by week, with the reasons
for each team's big weeks; Tadej Pogačar's own points are plotted alongside
(the `--rider` option adds others).

```bash
# 1. scrape (writes data/cycling.db; PCS_DB_PATH / --db to work on a copy)
python main.py scrape-uci-points --season 2026 --teams all [--report /tmp/uci_reconciliation.json]

# 2. build the chart (reads the DB read-only)
python main.py team-points --season 2026 --top 30 \
    --out-json out/team_points_2026_men.json --out-html out/team-points.html \
    [--generated-at 2026-10-18] [--rider tadej-pogacar --rider mathieu-van-der-poel]
```

Both subcommands pass every option through to `scripts/scrape_uci_points.py` /
`scripts/team_points_report.py` (`python main.py team-points --help`). The HTML
page is self-contained (inline CSS/JS/SVG, no external requests); the markdown
summary (top-30 table, Pogačar line, reconciliation notes) goes to stdout.

**Cost.** A cold run fetches ~1,950 PCS pages (~45 min): the ranking listing, 34
team breakdowns, and for each of ~850 scoring riders the results page plus the
UCI-ranking page (plus fines pages / profiles where needed). With a warm
`cache/html` a re-run makes no requests and takes ~70 s. Cached pages never
expire: `--refresh` re-fetches the listing and breakdowns (a new listing date
then re-fetches every rider's ranking page), `--refresh-riders` also the results
pages.

**Data source and method.**
- `rankings/me/ranking-2026-2028` (official listing, 34 teams) → `uci_team_totals`;
  it defines the top 30. `team/<slug>/results/ranking-2026-2028` (per-rider
  points counted / not counted / sanctions) → `uci_team_breakdown`.
- Per rider, `rider/<slug>/results` gives every dated race day and
  `rider.php?id=<n>&p=results&s=uci-world-ranking&date=<listing date>` gives
  every point item, including two things the results page lacks: GC
  leader-jersey points (25/10/5 per day in Grand Tours / WorldTour / .Pro stage
  races) and items from October–December of the previous year →
  `rider_uci_results`. Sanctions → `rider_uci_penalties`; dated transfers from the
  rider profile → `rider_team_spells`.
- The ledger dates every item to its result date (GC and classification points
  to the race's final day), subtracts sanctions on their day, and attributes
  each item to the team whose spell covers the date. Weeks are ISO weeks
  (Monday start). *All riders* sums every rider; *Best 20* applies the UCI rule
  (a team's 20 best riders of the season, as of each week).
- Annotations: each team's biggest weeks (normally 3–6) plus the weeks of its
  headline wins (every Monument, elite World title and Grand Tour GC; WorldTour
  one-day and stage-race wins up to 10 annotations), labelled with the riders
  and results that made them; "wins" only for a 1st place; a team time trial
  is the team's win, never a rider's stage win; other wins worth 10% of the
  week are added to the label; in a stage race's final week the label counts
  the whole race's stage wins (*Pogačar: Tour de France GC 1st + 5 stage wins*)
  while the points stay that week's.
- The tooltip's "biggest race of the week" sums every charted rider's points
  per race (`weeks_meta` in the JSON), not only the annotated jumps.

**Caveats (2026 data).**
- The UCI 2026 season started on 2025-10-22 (calibrated from the data): 15
  results (716 pts) from Oct–Dec 2025 count. The JSON keeps every week from
  2025-10-20; the chart starts at the week of 2026-01-12 (`meta.axis_start`)
  with those points in each line's opening value. The results pages are the
  only source for some of them — the 2025-10-25 Israeli ITT championship
  (Raisberg 50, Kogut 30, Einhorn 10 for NSN, the Israel - Premier Tech
  franchise; Tene's 15 belong to the academy team he rode for in 2025) is
  missing from the riders' UCI-ranking pages.
- The official listing (e.g. UAE 30,607) is a snapshot taken during the day; the
  team breakdown pages (UAE 30,982.1) include that day's later results. The
  best-20 ledger reproduces the breakdown totals for 29 of the top 30 teams (32
  of 34 overall) and the listing for 32 of 34 when limited to the results it
  contained.
- PCS's team page total counts both riders tied on 20th place (Euskaltel
  +0.71, MBH +3 over the UCI best-20 sum, which our best-20 ledger follows), and
  subtracts a negative net it prints as '-' (Kern Pharma −10).
- Points a rider scored for a development team before moving up (e.g. UAE Gen Z,
  Tudor U23) are not in any team total, as on PCS.
- A rider whose sanctions exceed his points has a negative net; it lowers the
  all-rider total but rarely counts among the best 20, so a few teams' best-20
  line sits above their all-rider line.
- PCS renames rider and team slugs within a season
  (`ineos-grenadiers-2026` → `netcompany-ineos-2026`); the UCI tables key on the
  breakdown's current slugs.
- The UCI tables add ~21 MB to `data/cycling.db` (now ~49 MB, tracked in git).

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
python main.py scrape-missing --year 2026      # retry anything that was not finished yet
python main.py repair-points --year 2026       # only needed for data scraped before Oct 2026
python main.py refresh-stages --year 2026 --null-gaps   # ITT gaps stored before the Oct 2026 parser fix
python main.py scrape-uci-points --season 2026 --teams all   # team UCI points (optional)
python main.py refresh-stages --year 2026 --uci-mismatch     # stages PCS corrected after the scrape
python main.py team-points --season 2026 --top 30 --out-html out/team-points.html

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
