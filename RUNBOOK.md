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

A race's gender comes from its first stored stage page ("Race category: WE"), not from the calendar row, and re-runs keep it (see [Race gender](#race-gender)); `race-gender` is needed only for races scraped before Oct 2026.

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
- An annulled result keeps its row with the rank struck through (`<td><s> 6 </s></td>`) and the next rider ranked in its place; it is stored as status `dsq` with no position or time. The pre-Oct-2026 parser stored it as a finish (two 1st places on San Juan 2023 stage 5, Bossuyt's 2023 top-10s, Baudin's whole Giro 2023). When the struck row is the original winner, PCS still prints every gap below from his time: San Juan 2023 stage 5 lists López struck at 4:07:10, Ganna 1st at 4:07:40 and Higuita at "0:38", 8 s behind Ganna. The parser re-measures the gaps from the ranked winner (his cell shows his absolute time, a gap, or `,,` for the same time).
- Every result page states the race's gender ("Race category: WE" / "ME"); the calendar row does not always (see [Race gender](#race-gender)).
- In a team time trial list a rider who did not finish carries a red marker before his name (`<font class="red">DNS</font> SCHMID Mauro`); he is stored with that status (`dns` / `dnf` / `otl`), outside the ranking. The pre-Oct-2026 parser ranked him at the team's time and shifted everyone behind him (Paris-Nice 2026 stage 3, Dauphiné 2026 stage 3).
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

### Merge re-slugged riders

PCS renames rider slugs in mid-season (`thomas-pidcock` → `tom-pidcock`,
`filippo-magli2` → `filippo-magli`, `david-lozano` → `david-lozano-riba`, a
marriage name `megan-armitage` → `megan-monk`). Result pages link the slug in
use when they are fetched, so the stages scraped before the change sit under
the old `riders` row and every later stage creates a second row: one rider,
two ids, two half-careers. `merge-riders` finds such pairs and merges them.

```bash
python main.py merge-riders --dry-run --plan-out /tmp/merge_plan.json   # detect; writes the plan, merges nothing
python main.py merge-riders --from-plan /tmp/merge_plan.json            # apply exactly the reviewed pairs
```

Without `--dry-run` it detects and applies in one go. `--from-plan` re-checks
each pair against the DB first and drops one that no longer passes.
`--pairs FILE` adds pairs found elsewhere (`[{"old_slug", "new_slug",
"evidence"}]`), `--offline-only` makes no PCS request, `--year` limits the
search. Detection parses every cached stage page (~730, ~2.5 min).

Evidence (`db/rider_merge.py`):
- **Same stage, same bib** (offline): a cached stage page lists slug B with the
  bib the DB stores for slug A on that stage. Bibs are unique per rider per
  race. A pair is dropped when both slugs appear on one page.
- **The new slug's own PCS results pages**: candidates with compatible names
  that never raced on the same day — one row's results all before the
  other's (gap ≤ 200 days), or **interleaved** (an old cached page scraped
  between newer ones: `andrea-ramirez`, Itzulia Women 2023, sits inside
  `andrea-ramirez-fregoso`'s 2023–2026) — are confirmed only when
  `rider/<new>/results` (season pages via
  `rider.php?id=<n>&p=results&xseason=YYYY&offset=N`) lists ≥ 80% of the old
  row's finishes with the same date and position (the race too below 5
  rows). The new slug is the one with the later last result. Gender is not a
  gate (some women's races were stored as men's, see
  [Race gender](#race-gender)), only an ordering hint. A bib pair whose
  names differ (marriage name) needs this confirmation too.
- **Every pair**: compatible names, no stage holding both slugs as different
  riders (different bib or position), no day on which they rode different
  races.
- **Never the old slug's page.** PCS reuses slugs: `rider/thomas-pidcock` is
  "Page not found", and `rider/romain-gregoire1` is now a *different* Romain
  Grégoire (PCS swapped the namesakes). Name similarity alone is not evidence
  either; unconfirmed candidates are reported, never merged.

A merge runs in one transaction per pair: results, predictions and bets move
to the new row (a stage holding both rows keeps the more complete one, and is
reported), NULL profile fields are filled from the old row, the name becomes
"First Last" in slug order, and the old row is deleted — or renamed when the
new slug is not stored yet. Rows of the slug-keyed UCI tables
(`rider_uci_results`, `rider_uci_scrapes`, `rider_uci_penalties`,
`rider_team_spells`, `uci_team_breakdown`) are re-keyed only when they are
shown to be this rider's, season by season and never combined: a season the
new slug was scraped in too keeps only the new slug's rows (its scrape came
after the rename; a union counted a relabelled result twice), a season only
the old slug has moves whole. The pair is recorded in `rider_slug_aliases`
(`old_slug`, `new_slug`, `evidence`, `merged_at`, `valid_until`).

Every scrape path (`scrape`, `scrape-missing`, `--backfill-teams`,
`repair-points`, `refresh-stages`) maps an old slug on a page to the merged
rider through that table:
- by **bib**, whatever the page's date: the old slug carries a bib the merged
  rider wore in that race (any stored stage of it) — and a different bib
  means another rider, never mapped;
- else by **page date**: pages fetched (cache file date) on or before
  `valid_until`, the old slug's last stored result. That is the only date
  known to be before PCS's rename; a later page may still show the old slug
  for him (rename not done yet) or for a namesake (PCS reuses slugs).
  Such a row is held by `refresh-stages` (neither inserted nor allowed to
  remove the merged rider's row) and stored under the old slug by the
  other paths (a re-split that the next `merge-riders` finds by bib).

A re-run reports "already merged" when the alias table already maps the
pair; a pair whose old slug is gone without such an alias (a plan built on
another DB copy, stale or edited) is dropped by `--from-plan` and never
writes an alias. `merge_pair` never writes an alias without a `valid_until`.

Run it **before** `repair-points` / `refresh-stages` on old seasons (they
match rows by slug: without the merge, Pidcock's 2023 Liège 64025 stayed
corrupt) and once more afterwards to catch pairs the repair exposed.

The Oct 2026 runs merged 11 pairs, with no stage holding both rows. The
first run found 10 by bib on cached pages; the marriage-name pair was
confirmed on PCS (`rider/megan-monk` lists 44/44 of the old row's finishes),
and the four known pairs were cross-checked there too (96/96 to 225/225).
`andrea-ramirez` was missed: her three Itzulia Women 2023 rows sit inside
`andrea-ramirez-fregoso`'s 2023–2026 span (and most of the latter's races
were stored as men's), and the name search then only took consecutive,
same-gender rows. Detection now takes interleaved rows too; the second run
found her by bib (Itzulia Women 2023 stages 1–2, bib 83, pages fetched
later) and the name path confirms it on `rider.php?id=204333&p=results&xseason=2023`
(2/2 finishes, the third row is a DNF):

| Old slug | New slug | Action | Results moved |
|---|---|---|---|
| thomas-pidcock | tom-pidcock | merge | 188 |
| romain-gregoire1 | romain-gregoire | merge | 212 |
| filippo-magli2 | filippo-magli | merge | 239 |
| david-lozano | david-lozano-riba | merge | 108 |
| gabriel-muller | mehmet-sampiyonbisiklet | merge | 40 |
| henrique-bravo1 | henrique-ribeiro-bravo | merge | 2 |
| andrea-ramirez | andrea-ramirez-fregoso | merge | 3 |
| megan-armitage | megan-monk | rename | 50 |
| andres-liber-mancipe | andres-libardo-mancipe-puin | rename | 19 |
| diana-rodriguez | diana-valeria-rodriguez-galvan | rename | 11 |
| enkhtaivan-bolor-erdene | bolor-erdene-enkhtaivan | rename | 17 |

889 results moved, none lost; riders 6,476 → 6,469. The second run checked
117 name candidates on PCS (77 interleaved, 40 consecutive; 74 pages fetched,
~5 min cold, ~3 min from the cache): none confirmed. The strong-named ones
were rejected by the new slug's page (`matthew-walls1` 0/6,
`marta-lopez-ruiz` 0/2) or have no stored finish to compare
(`joao-silva8`, `tiago-caetanita-silva`, `marta-lopez-fernandez`: DNF rows
only). A run after the stage refreshes found no new pair.

Limits of the detection: a pair is found only by bib on a cached page that
shows the other slug, or by names (same surname token, compatible first
names) — a rider whose surname changed is found by bib alone; a candidate
whose old row has no finish with a position cannot be confirmed.

### Repair corrupted UCI points

Before Oct 2026 the parser glued a sanction's digits onto the earned points
(`800 -25` → 80025, `15 -25` → 1525) and stored a bare `-25` as 25 points.
`repair-points` finds stages whose stored `points_uci` are implausible (above
800, not decreasing with position, points after non-scoring finishers, points
on DNF rows, a team time trial with fewer than 4 riders holding a share, a
single scorer among 20+ finishers — a TTT stored from the GC tab under a road
profile), re-fetches them (cache first) and rewrites every matched row from
the page — `points_uci` + `uci_penalty`, and also position, status, time, PCS
points and bib, because PCS reorders stages after the first scrape and new
points on a stale order are inconsistent (Amstel Gold Race Women 2026,
20th/21st) — and fills team columns, logging each change. A stage whose page
is a team time trial gets `profile_type = 'ttt'`.

```bash
python main.py repair-points --year 2026 --dry-run   # log what would change
python main.py repair-points --year 2026
```

Rows are matched by rider slug. The log lists the unmatched rows on both sides
with their bibs: one bib under two slugs is a re-slugged rider (run
`merge-riders`, then repair again). Page riders missing from the DB are not
inserted (use `refresh-stages` for that). A second run lists the stages still
flagged.

**Older seasons (2023–2025).** Merge first, repair, then check for new pairs:

```bash
python main.py merge-riders --dry-run --plan-out /tmp/merge_plan.json   # review, then:
python main.py merge-riders --from-plan /tmp/merge_plan.json
python main.py repair-points --year 2023    # then 2024, 2025
python main.py merge-riders --dry-run       # expect no new pair
```

The Oct 2026 run (all pages cached, no PCS request, ~30–45 s per season):

| | 2023 | 2024 | 2025 |
|---|---|---|---|
| Stages flagged / repaired | 123 | 126 | 105 |
| Rows with new `points_uci` / `uci_penalty` | 985 | 753 | 769 |
| Rows with new position / status | 637 | 394 | 518 |
| … of them annulled results now `dsq` | 31 | 6 | 16 |
| Rows retimed | 3,519 | 2,740 | 2,673 |
| Rows given team columns | 17,007 | 17,490 | 14,577 |
| Still flagged afterwards | 1 | 0 | 0 |

The largest stored value went from 64025 to 800. Most moves and retimes are
the 11 team time trials stored from the GC tab, now one row per rider with
the team's share, and old-parser artefacts (ditto rows stored as a 0 gap,
0 times on DNF rows). The three La Vuelta Femenina TTTs (stage 1, 2023–2025,
stored as `flat` with the GC leader's 8 points) now carry the 7.14-point
shares and profile `ttt`.

What stays as it is:
- **Japan Cup 2023** remains flagged (`after_gap`): Zambanini is 17th on the
  page, not struck, with no UCI points — PCS's own page.
- **Sanctions PCS no longer shows** (39 rows): the page is taken as is,
  so the earned points stay and `uci_penalty` is NULL (Pidcock's Lombardia
  2025: stored 28015, now 280).
- Women's stages with a 0 gap after positive gaps (an old ditto artefact) and
  0 times on DNF rows are fixed only on the stages repaired; `--null-gaps`
  does not detect zeros.
- Annulled results on stages the repair did not flag stayed stored as
  finishes, each sharing its position with the rider PCS ranked in its
  place: Baudin on 12 more Giro 2023 stages, López on San Juan 2023 stages
  1/3/4/6/7, Franco at Tour El Salvador 2024 (stages 0/1/2/4), Dostiyev at
  Hainan 2024 (1, 5), Chía at Hainan 2025 (5). `refresh-stages
  --dup-positions` (below) turned those 24 rows into `dsq`.
- 2023–2025 stages with NULL gaps behind the winner (pre-Oct-2026 parsers;
  86 / 99 / 98 stages, 8,370 rows, time trials and road stages) were not
  refreshed in bulk; `refresh-stages --year Y --null-gaps` does it (~280
  pages, mostly not cached).

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
python main.py refresh-stages --year 2023 --dup-positions
python main.py refresh-stages --year 2026 --stages race/paris-nice/2026/stage-3
```

- `--null-gaps`: ranked finishers behind the winner with no time. Before Oct
  2026 the parser read only the hundredths of live time-trial cells
  (`0:28<font>,36</font>` → NULL), so every 2026 ITT/prologue lost its gaps.
- `--uci-mismatch`: stages whose stored positions / `points_uci` disagree with
  `rider_uci_results` (needs `scrape-uci-points` first; DB only, no traffic) —
  PCS corrections after the race, and team time trials stored from the GC tab.
  `rider_uci_results` keeps scoring rows only, so a rider whose pages were
  scraped (`rider_uci_scrapes`, complete) and who has no row on a stage scored
  0 there: a stored row giving him points is flagged, up to the last result
  his pages listed (later stages, riders never scraped and partial scrapes are
  not judged). Positions are compared only where the rider page has a scoring
  row — an order corrected among riders outside the points is not detected.
  On the pre-refresh 2026 results (git `1984a35`) that keeps all 52 stages
  with a points disagreement and drops 12 of 65 flagged stages, each a
  positions-only swap of non-scoring riders (7 of them between 79th and 97th;
  the highest a 9th/10th swap in two 2.Pro stages that pay no points there).
- `--dup-positions`: stages where two finished rows share a position (team
  time trials excluded; DB only). That is an annulled result the
  pre-Oct-2026 parser stored as a finish next to the rider PCS ranked in its
  place — `repair-points` fixes them only on the stages it flags for UCI
  anomalies, so a rider could be `dsq` on some stages of a race and a
  duplicate "finisher" on the rest (Baudin, Giro 2023). PCS prints real ties
  with one rank too (time trials timed to the second); those stages stay
  selected and a refresh leaves them unchanged.
- A merged rider's old slug on a page (an old cached copy) is mapped to him
  when it carries the bib he wore in that race, else when the page was
  fetched on or before the alias's `valid_until`. When neither settles it,
  the row is held: not inserted, and the merged rider's stored row is not
  removed (see [Merge re-slugged riders](#merge-re-slugged-riders)).

The Oct 2026 run refreshed 120 stages (4,036 times, 342 positions, 251 UCI
points, 78 riders inserted, none removed). A second run, `--dup-positions`
for 2023–2026 plus San Juan 2023 stage 5 (34 stages, 29 pages fetched),
stored 24 annulled results as `dsq`, re-measured San Juan 2023 stage 5's
135 gaps from Ganna (they were 30 s too large, measured from López's struck
time) and filled the NULL gaps of the seven time trials among the selected
stages (1,455 times; Giro Women 2025 stage 1 became `itt`). What stays
selected are ties PCS prints itself, one rank for two riders on the same
second, all time trials: Vuelta 2023 stage 10, Romandie 2024 prologue (5
ties), Suisse 2024 stage 1, Tirreno 2025 stage 1, Giro 2025 stage 10, Giro
Women 2025 stage 1, TdF 2025 stage 13, Suisse 2026 stage 4, Vuelta 2026
stage 1. A placeholder row PCS lists without a rider (`<a href="rider/">`,
San Juan 2023 stage 4) is skipped by the parser. After the team-time-trial parser
fix, `--stages race/paris-nice/2026/stage-3,race/dauphine/2026/stage-3`
turned the two DNS riders the old parser had ranked (Ballerstedt 62nd,
Pescador 13th) into `dns` and moved the 227 riders behind them up one place.
A team time trial page always sets the stage's profile to `ttt` (the page's
own "Won how" may say `itt`).

### Race gender

The calendar scrape guesses a race's gender from its calendar row (class
`*.WWT`, or a women's marker in the name or slug: Women, Féminine, Feminas,
Donne, `-we`, ...). The men's calendar circuits (circuit 16 above all) also
list women's races of the same classes, some with no marker at all (Tour El
Salvador, GP Oetingen, GP Lucien Van Impe), and those were stored as `men` —
their results fed the men's model and men's stats. Every result page states
the real one ("Race category: WE" / "ME"): `scrape`, `scrape-missing` and
`refresh-stages` set `races.gender` from the page they store results from.
The calendar guess is stored only when a race is first inserted
(`db.database.upsert_race` never overwrites a stored gender), so a re-run of
`scrape` — which skips stages that already have results and reads no page
for them — keeps a gender a page set. `race-gender` corrects races whose
results were stored before the page check existed:

```bash
python main.py race-gender --dry-run          # races whose riders contradict their gender (DB only), checked on PCS
python main.py race-gender                    # apply
python main.py race-gender --year 2026 --all  # check every race of a season (one page per race)
```

Candidates (`db.database.find_race_gender_suspects`): a race stored `men`
where 30% or more of its riders also rode a race stored `women` (a real
men's race has none — on the Oct 2026 DB 15 races had 31–100%, none between
2 and 30%), or a race stored `women` whose riders ride mostly men's races.
The page decides; a race whose page has no race category is reported and
left alone. Retrain both models afterwards.

The Oct 2026 run (15 pages, ~20 s) moved 15 races from `men` to `women`:
Tour El Salvador 2024–2026, GP Oetingen 2025–2026, Vuelta CV Feminas
2025–2026, Setmana Valenciana Fèmines 2023, Vuelta Andalucía (women) 2023,
Elsy Jacobs 2023, GP Ciudad de Eibar 2025, Clásica de Almería WE 2026, Giro
dell'Emilia Donne 2026, Fourmies Féminine 2026, GP Lucien Van Impe 2026.

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
  `rider_uci_results`, **scoring rows only** (`points_uci` not NULL and not 0;
  five in six race days score nothing). What the pages showed is kept per
  rider in `rider_uci_scrapes`: rows seen, first/last result date, complete or
  not, page name. Sanctions → `rider_uci_penalties`, each with the
  `race_slug` / `race_class` of its race (matched against the full result
  list, zero-point rows included, before those are dropped); dated transfers
  from the rider profile → `rider_team_spells`.
- A complete scrape replaces a rider's rows even when none of them scores
  (stale points are cleared); when a page fails, the stored season — empty or
  not — is kept, and a rider never stored before gets his partial rows with
  `complete = 0` in `rider_uci_scrapes`. On such a degraded run a sanction
  is placed with the partial pages' rows plus the stored ones; one imposed in
  a race worth 0 to him (neither holds it) keeps the race — and the race name,
  rather than the ranking page's 'Sanctions' placeholder — it was stored with.
  Without the ranking page the fines page is not the whole truth (PCS lists
  some sanctions only on the ranking page: 13 of the 118 riders with
  sanctions in Oct 2026, e.g. Đurić, Pellizzari, Unai Ramos), so the stored
  sanctions are kept; a day's fines-page sanctions replace them only when
  they add up to at least as much (a sanction imposed since the last scrape
  is added, none is counted twice).
- `meta.data_through` is the last result date the rider pages reach: the max
  of `rider_uci_scrapes.last_date` (zero-point rows included), any stored row
  and any sanction — not the last *scoring* result.
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
- The UCI tables take ~4 MB of `data/cycling.db` (tracked in git). Until Oct
  2026 every race day was stored (58,261 rows, ~20 MB with the indexes); see
  [Trim the UCI tables](#trim-the-uci-tables) to convert a DB stored before.

### Trim the UCI tables

Policy: `rider_uci_results` keeps **scoring rows only** — the scraper never
stores a zero / NULL row, and what a rider's pages showed (rows seen, last
date, complete or not) lives in `rider_uci_scrapes`. A database stored before
that rule is converted in place (no PCS traffic, no scrape; ~2 s):

```bash
python main.py scrape-uci-points --prune-zero --all-seasons --vacuum   # or --season 2026
```

`--prune-zero` first backfills `rider_uci_scrapes` (from the rows about to
go, `complete` NULL) and the sanctions' races, then deletes the zero / NULL
rows; it is idempotent. `--vacuum` rewrites the file (deleted rows only free
pages inside it; needs free disk for a temporary copy) and prints the size
before and after. Run it as the last write of a maintenance session: VACUUM
only reclaims what was deleted before it.

On the Oct 2026 DB: 48,498 of 58,261 rows deleted (9,763 kept, 851 scrape
records backfilled, 127 sanctions' races resolved). The file was 49.5 MB
before that day's maintenance, 52.0 MB after the rider merges and the
2023–2025 repairs (team columns filled on ~49k rows) and 31.1 MB after the
prune + VACUUM (31.3 MB after the follow-up merge, stage refreshes and race
genders). The chart payload, HTML and markdown summary built from the
trimmed DB are byte-identical to the ones built before, and so is a re-scrape
from the HTML cache.

---

## Database

### Share the database between machines

The database is tracked in git (`data/cycling.db`, ~31 MB with the UCI points
tables trimmed — GitHub warns above 50 MB). To sync:

```bash
git pull   # get latest DB + model
```

If you've scraped new data locally and want to push it:

```bash
git add data/cycling.db model/trained_model.pkl model/metrics.json
git commit -m "Update DB and model after 2026 scrape"
git push
```

### Rider slugs and aliases

`riders.pcs_slug` is the rider's slug on PCS today; `results`, `predictions`
and `bets` reference `riders.id`. The UCI tables (`rider_uci_results`,
`rider_uci_scrapes`, `rider_uci_penalties`, `rider_team_spells`,
`uci_team_breakdown`) are keyed by the slug text the 2026 breakdown pages
used. `rider_slug_aliases` maps the old slugs of merged riders to the current
ones (see [Merge re-slugged riders](#merge-re-slugged-riders)); `init_db`
creates it.

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
python main.py merge-riders --dry-run --plan-out /tmp/merge_plan.json   # riders PCS re-slugged: review the plan,
python main.py merge-riders --from-plan /tmp/merge_plan.json            # then apply it
python main.py repair-points --year 2026       # only needed for data scraped before Oct 2026
python main.py refresh-stages --year 2026 --null-gaps --dup-positions   # ITT gaps / annulled results stored before the Oct 2026 parser fixes
python main.py race-gender                     # only for races scraped before Oct 2026 (the calendar stored some women's races as men's)
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
