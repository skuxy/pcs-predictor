# How accurate is the predictor? (November 2026)

Measured on every stage of the 2025 and 2026 seasons, each predicted by models
that had only seen earlier stages. Results stored up to 2026-10-18; numbers as
of the run of 2026-11-17.

| Men, per stage | Before | After the model rounds | Now, with stage data |
|---|---|---|---|
| Stages scored | 939 | 939 | 947 |
| Of the model's 10 picks, finished in the top 10 | 4.09 | 4.36 | **4.56** |
| Of its 3 podium picks, finished on the podium | 0.85 | 1.01 | **1.05** |
| Winner was its first pick | 21.2% | 27.3% | 26.1% |
| Winner among its first 3 picks | 36.9% | 47.6% | 48.8% |
| Winner among its first 10 picks | 64.0% | 69.2% | 69.4% |
| AUC | 0.854 | 0.872 | **0.879** |

| Women, per stage (213 stages) | Before | After the model rounds | Now, with stage data |
|---|---|---|---|
| Of the model's 10 picks, finished in the top 10 | 4.64 | 4.85 | 4.91 |
| Of its 3 podium picks, finished on the podium | 1.04 | 1.16 | 1.17 |
| Winner was its first pick | 29.6% | 34.7% | 33.3% |
| Winner among its first 3 picks | 51.2% | 58.2% | 53.5% |
| Winner among its first 10 picks | 73.7% | 76.5% | 77.9% |
| AUC | 0.862 | 0.885 | 0.887 |

Picking ten riders at random gets 0.77 (men) and 0.92 (women) top-10 hits.

- **Before** is the pipeline as it stood on `main` (87 features, depth-4
  trees, one top-10 model answering all three questions). The men's column
  was run with the original code; the women's with the original feature list
  and model settings on the current feature builder (on the men that variant
  scores 0.01 hits above the original).
- **After the model rounds** (11 November): new features, ratings, podium and
  win models, new model settings, on the same data.
- **Now** (17 November): the same code after the scrape from the Mac —
  elevation for about 1,200 more stages, profile score and final-km gradient
  for about 850, 34 wrong stage labels corrected, and the 8 stages of 13–18
  October that PCS serves.

What holds and what does not, with 95% bootstrap intervals over stages (each
step paired on the stages both of its runs score):

| Step | Men | Women |
|---|---|---|
| Model rounds, top-10 hits | +0.26 [+0.19, +0.34] | +0.21 [+0.08, +0.35] |
| Model rounds, winner called | +6.1 points [+3.5, +8.6] | +5.2 points [−0.9, +11.3] |
| Model rounds, winner in first 3 | +10.8 points [+7.9, +13.5] | +7.0 points [+0.9, +13.2] |
| Stage data, top-10 hits | +0.20 [+0.13, +0.26] | +0.06 [−0.03, +0.15] |
| Stage data, winner called | −1.2 points [−3.2, +0.8] | −1.4 points [−6.1, +3.3] |
| Stage data, winner in first 3 | +1.2 points [−0.8, +3.2] | −4.7 points [−8.5, −0.5] |

- **Men:** both steps raised the top-10 hits beyond doubt; picking the winner
  improved with the model rounds and did not change with the stage data.
- **Women:** the top-10 gain of the model rounds holds, the stage data added
  nothing measurable, and the earlier gain in "winner among the first 3" did
  not hold up: of +7.0 points, 2.3 are left, which is inside the noise of 213
  stages.
- **How noisy the winner numbers are:** the Mac's own run on the same data
  (before four time-trial labels were corrected) gave 27.0% and 48.4% for the
  men and 35.2% and 57.3% for the women. Read "winner called" to about ±1
  point for the men and ±3 for the women.

## How it is measured

`python main.py evaluate` (code: `model/evaluate.py`).

- **Walk-forward.** Six test windows start on 2025-01-01, 2025-04-01,
  2025-07-01, 2026-01-01, 2026-04-01 and 2026-07-01. For each one the stage
  classifier and the three models are fitted on stages before the window and
  then predict every stage in it. Nothing is tuned on the test stages except
  the choices listed under "What each change was worth", which were compared
  on these same windows; treat the final numbers as slightly optimistic for
  that reason.
- **Features need no refitting.** Each is computed from results dated before
  the stage it describes. `tests/test_features_v2.py` rewrites the results of
  a stage's own day and of every later stage and checks that the stage's 173
  features do not change.
- **Per stage**, riders are ranked by P(top 10) for the top-10 hits, AUC and
  nDCG, by P(podium) for the podium hits and by P(win) for the winner's rank;
  the stage values are averaged. Team time trials are not scored (their
  "position" is the team's). A rider who did not finish counts as outside the
  top 10, so a pick that crashes is a miss.
- **Differences** are paired by stage, with a bootstrap 95% interval
  (`model.evaluate.paired_difference`).

### Why the earlier numbers could not be trusted

The README used to quote AUC 0.877 and precision@10 0.425, and the UI and
`main.py backtest` scored any past race with the model on disk. That model
was trained with cutoff 2026-08-11, so every race before that date was in its
training data: the "backtest" was partly a memory test. The honest figure for
that pipeline turned out close to the claim (0.409 / 0.854) — but it could not
have been known without this run, and a per-race backtest could be far off.
The UI and CLI now say whether a backtest is out of sample.

## Men: where it is good and where it is not

| Terrain | Stages | Top-10 hits | Podium hits | Winner called | Winner in first 3 | AUC |
|---|---|---|---|---|---|---|
| Flat | 152 | 5.09 | 1.28 | 28% | 61% | 0.898 |
| Hilly | 556 | 4.14 | 0.95 | 25% | 46% | 0.863 |
| Time trial | 56 | 5.50 | 1.30 | 34% | 66% | 0.915 |
| Mountain | 183 | 5.09 | 1.10 | 24% | 43% | 0.899 |

| | Stages | Top-10 hits | Winner called | Winner in first 3 |
|---|---|---|---|---|
| WorldTour | 319 | 4.46 | 31% | 54% |
| ProSeries | 315 | 4.57 | 22% | 46% |
| .1 | 313 | 4.65 | 26% | 47% |
| One-day races | 218 | 4.45 | 25% | 46% |
| Stage-race stages | 729 | 4.59 | 26% | 50% |
| 2025 | 467 | 4.57 | 25% | 48% |
| 2026 | 480 | 4.54 | 27% | 49% |

Against rankings by a single number, on the same 947 stages:

| Ranking | Top-10 hits | Winner called | Winner in first 3 | AUC |
|---|---|---|---|---|
| The model | 4.56 | 26% | 49% | 0.879 |
| Elo rating (all stages) | 3.37 | 13% | 28% | 0.819 |
| PCS points, last 365 days | 3.32 | 19% | 36% | 0.805 |
| Top-10 rate, last 90 days | 3.03 | 12% | 23% | 0.753 |

- More than half of all stages now have 5 or more hits (54%; 42% before the
  work, 49% before the stage data); 15% have two or fewer (21%, 18%).
- On 12.5% of stages the winner sits outside the model's first 30 picks (16%
  before the work): breakaways and outsiders that results cannot foresee.
- When the stage's label matches what happened, the model is at its best:
  5.3 hits on flat stages that ended in a bunch sprint, 5.4 on mountain
  stages decided by a small group. The weak spot is still the "hilly" label,
  which covers 59% of the test stages and splits evenly between bunch sprints
  and small groups (4.2 and 4.1 hits).

### The probabilities can be read as probabilities

| P(top 10) given | Riders | Average given | Finished top 10 |
|---|---|---|---|
| 0.0–0.1 | 102,653 | 0.022 | 0.021 |
| 0.1–0.2 | 12,610 | 0.143 | 0.144 |
| 0.2–0.3 | 5,750 | 0.244 | 0.242 |
| 0.3–0.4 | 3,392 | 0.346 | 0.357 |
| 0.4–0.5 | 2,069 | 0.447 | 0.457 |
| 0.5–0.6 | 1,521 | 0.547 | 0.536 |
| 0.6–0.7 | 927 | 0.644 | 0.676 |
| 0.7–0.8 | 468 | 0.743 | 0.737 |
| 0.8–0.9 | 136 | 0.838 | 0.831 |
| 0.9–1.0 | 43 | 0.955 | 0.861 |

Brier score 0.0513 (0.0558 before the work), log loss 0.1797 (0.1981). Only
the very top is too confident: of the 43 riders given more than 0.9, 37
finished in the top 10. The model is also more decisive than before: its first
pick averages P(top 10) = 0.67 (was 0.54), and at Strade Bianche 2026 Pogačar
gets 0.91 instead of 0.53.

The women's probabilities are slightly cautious in the middle (riders given
0.5–0.6 finished in the top 10 61% of the time; Brier 0.0583, log loss
0.1987).

## What each change was worth

Single-model walk-forward runs on the same six windows (one boosted model
instead of the calibrated five, so absolute values sit about 0.005 below the
production figures). Δ is the change in top-10 precision (0.01 = 0.1 hits per
stage) against the stated reference, with its 95% interval.

### Round 1 — more to know about a rider (+0.022)

87 → 164 features, production model unchanged: precision@10 0.409 → 0.431,
Δ +0.022 [+0.015, +0.029]; podium hits 0.85 → 0.98; winner in the first three
37% → 44%.

| Group | What it adds |
|---|---|
| Point-in-time strength | PCS points over 90/365/730 days, finishing percentile, recency-weighted form, best result, level of races ridden |
| Finish-type skills | record in stages that ended in a bunch sprint vs a small group (derived from the stored time gaps of past stages), uphill finishes, time-trial seconds per km |
| Race history | earlier editions of the same race; earlier stages of the current edition |
| Team role | rank within the team, share of the team's points, leader bib |
| Standing in the field | percentile of the strength features among the day's starters; quality of the field |

Taking any one group out again changes precision by −0.002 to +0.001, all
inside the noise: the groups overlap, and it is the set that matters.

### Round 2 — ratings and objectives (+0.004, plus the win model)

- **Elo-style ratings** (`features/ratings.py`; overall, bunch, small group,
  time trial, mountain, cobbles): Δ +0.0037 [−0.0016, +0.0089]. Kept: small
  on its own, but it is the only strength measure that accounts for who was
  beaten, and its rankings pass a sanity check (Philipsen and Merlier lead
  the bunch rating, Pogačar and Vingegaard the mountain one).
- **Ranking and graded objectives** (LambdaRank, regression on a position
  score) did not improve the top 10 (Δ −0.006 to +0.002) but picked winners
  better (first pick wins 24–26% against 22%). Hence separate models for
  **win** and **podium** next to top 10: each question is answered by the
  probability trained for it. Ranked by P(win), the first pick wins 27.3% of
  stages; ranked by P(top 10), 23.9%.

### Round 3 — the model itself (+0.005)

| Variant (reference: depth-4 trees, class weights) | Δ precision@10 |
|---|---|
| **Leaf-wise trees (31 leaves), 70% of columns per split, L2 = 1, no class weights** | **+0.0054 [+0.0007, +0.0097]** |
| … without column subsampling | −0.0044 [−0.0081, −0.0005] vs the line above |
| … with class weights | −0.0031 [−0.0072, +0.0013] |
| … 15 or 63 leaves | −0.0032 |
| … learning rate 0.03 × 700 rounds | 0.0000 |
| … three seeds averaged | +0.0018 [−0.0018, +0.0058] |
| LightGBM, same idea | +0.0080 [+0.0027, +0.0130] |

Adopted the scikit-learn version (no new dependency; the calibrated
five-model average the pipeline already uses gives the bagging). Team time
trials no longer count as training rows (Δ +0.0029 [−0.0017, +0.0073]).

### Round 4 — checks that decided what not to build

- **Knowing the finish type in advance** (bunch sprint or not, taken from the
  actual result as an oracle) is worth only +0.0045 [+0.0012, +0.0080]. A
  better stage classifier therefore cannot be the big lever; the current one
  calls a bunch finish with AUC 0.80 (0.77 within hilly stages).
- **Final GC of earlier stage races** (summed stage gaps): −0.0015
  [−0.0057, +0.0027]. Removed.
- **Rider-page attributes.** Dropping the speciality flags: +0.0017
  [−0.0027, +0.0062]; dropping age, weight and height too: +0.0015. The
  speciality flags and `pcs_rank` are no longer model inputs (they are dated
  at scrape time, so for an old race they look into the future); age, weight
  and height stay, they are point-in-time and cost nothing.
- **Training on the last two years only**: −0.0008 [−0.0053, +0.0040]. More
  old training rows alone would not help much.
- **Making the probabilities add up per stage** (one winner, three podium
  places, ten top-10 places). Shifting every rider's log-odds by one constant
  per stage beat both the models' raw outputs and a proportional rescaling on
  the 473 stages of 2026: P(top 10) log loss 0.18827 raw, 0.18816 rescaled,
  0.18745 shifted (Brier 0.05350 / 0.05342 / 0.05336). The rescaling also
  pushed favourites too high — riders it gave 0.8–0.9 finished in the top 10
  77% of the time — which the shift does not. Adopted in
  `model.predict.coherent_probabilities`.

### Round 5 — stage data from the Mac scrape (+0.020 for the men)

`scrape-missing` and `scrape-elevation`, run from the Mac on 16 November (PCS
answered plain requests there; no browser was needed), then the same code
retrained. Production models, paired on the 939 stages both runs score:
top-10 precision 0.4357 → 0.4552, Δ +0.0195 [+0.0131, +0.0259].

| Men's stages with a stored result | Before | After |
|---|---|---|
| Profile score | 59% | 97% |
| Elevation | 42% | 99% |
| Gradient of the final km | 59% | 98% |
| … all 2.1 / 2.Pro stage-race stages, profile score | 11–15% | 81–93% |
| Unread default `mountain` label | 31 | 0 |

- The gain sits where the data was missing: 2.1 and 2.Pro stage races went
  from 4.32 to 4.66 hits, WorldTour stage races from 4.42 to 4.47, one-day
  races from 4.35 to 4.44.
- Of the 44 stages (men and women) that carried the unread `mountain` default
  and have results, 33 were relabelled (flat or hilly) and 11 are mountain
  stages according to their page. The "profile unknown" group (2.9 hits) is
  gone.
- `profile_score` and `gradient_final_km` are now the 3rd and 6th most
  important features (permutation importance).
- It did not help pick winners (see the intervals at the top), and it did
  little for the women, whose WorldTour-heavy calendar already had the data
  (profile score 73% → 96% of stages with results).
- New results: Tour of Guangxi (6 stages), Veneto Classic, Chrono des
  Nations; 1,044 rows, 2 new riders.

## What was wrong or missing in the data and code

- **`pcs_rank` is NULL for all 6,478 riders**, and only 1,515 have a profile
  (speciality, date of birth; 830 a weight). The "rider attributes" carried
  almost no information.
- **"Hilly" is 56% of all rider-stage rows** and ends in a bunch sprint half
  the time (Kuurne, Almería, Gent–Wevelgem are "hilly").
- **A default `mountain` label**: a stage that came from the stage list and
  whose page was never read is stored as `mountain` with distance 0 — Tour of
  Belgium and Tour of Slovenia sprint stages, Chrono des Nations. 58% of them
  ended in a bunch sprint. 31 men's stages with results (30 in the test
  windows) and 5 women's test stages. Features and stage classifier now treat
  them as "profile unknown"; `scrape-elevation` replaces the label when it
  reads the page. Fixed by the 16 November scrape for every stage with
  results; the 126 stages still carrying it have no result (races PCS has
  nothing for).
- **Stage data was thin below the WorldTour.** Profile score and elevation
  were stored for 97% of WorldTour stage-race stages but 11–15% of 2.1 and
  2.Pro ones (977 stages). Filled by the 16 November scrape (Round 5).
- **Time trials stored as road stages.** A one-day time trial whose page has
  no "Won how" line gets its label from the parcours icon: Chrono des Nations
  2023 and 2026 were `flat`, and stage 2 of the Tour de Suisse Women 2023 and
  2024 `hilly` / `mountain`. Found by their results (under 60 km, nobody on
  the winner's time, ten different times in the top 10) and set to `itt` by
  hand; the parser is unchanged.
- **Upcoming races used different features from training.** The start-list
  path rebuilt the features rider by rider with other definitions (DNF rate
  over the whole career instead of 90 days, rates over finished rides only,
  team time trials included, team never read from the start list). Both paths
  now run through `build_features`; with Il Lombardia 2026 and the Tour de
  France 2026 re-opened as races still to come
  (`scripts/check_startlist_parity.py`), all 173 features equal the
  result-based ones for every starter.
- **A stage race under way** only got predictions for the stages already
  ridden; the open stages are now predicted from the start list.
- **UI**: the Model tab showed "Validation AUC 0.0000" and never had feature
  importances (the boosted model has none built in; they are now measured by
  permutation); P(win) / P(top 3) columns were looked up under the wrong
  names; out of season the Predictions tab was empty without explanation.
- **Build cost**: the feature build paired every result of a rider with every
  other one before filtering by date. It now builds only the earlier-result
  pairs: about 30 s and 9.5 GB for the men's 259,173 rows with 173 features,
  against 46 s and 9.7 GB for 87 before.
- **Rider links with a discipline suffix.** PCS now links some riders as
  `rider/rein-taaramae/road`; the result parser stored that as a new rider
  without history (found on the Japan Cup page, fixed in
  `scraper/results.py` and the start-list parser, the one affected row
  repaired).

## What would move it further

In order of expected value. PCS could not be fetched from the cloud VM (see
RUNBOOK, "still on Cloudflare challenge page"); from the Mac it works.

1. **Seasons before 2023.** Ratings and one- and two-year windows are still
   filling up during 2023–2024, which is half the training data. About 45
   minutes of scraping per season.
2. **Telling a hilly sprint from a hilly selection.** Hilly stages are 59% of
   the calendar and the weakest group (4.1 hits). The oracle test puts the
   ceiling of perfect finish typing at about +0.05 hits overall, so this is a
   small lever; climbs near the finish (from GPX or the stage profile) would
   be the data to try.
3. **Picking winners.** Top-10 accuracy rose with every step, "winner called"
   has stood at 26–27% since the win model was added. One stage in eight is
   won from outside the first 30 picks.
4. **Rider profiles** for the 77% of riders without one (age mostly).
5. **The 2027 calendar** from January, and bookmaker odds as a benchmark.

Not available: PCS answers HTTP 500 for the Tour of Holland 2026, Simac
Ladies Tour 2026 and International Tour of Hellas 2026 pages (17 stages), and
has no results for another 22 races in the 2026 calendar (43 stages, among
them Trofeo Baracchi and the Tour of Guangxi women's race). Four stages were
cancelled (three at the Volta a Portugal, one at the Vuelta), and three empty
stage rows are leftovers of races PCS renumbered after a prologue was added
(Boucles de la Mayenne, Tour of Estonia, Deutschland Tour).

## Reproduce

```bash
python main.py evaluate                  # men, ~13 min on 8 cores; prints the tables above
python main.py evaluate --gender women   # ~2 min
```

Both write `model/evaluation[_women].json` and
`model/walkforward[_women].csv.gz`. The "before" columns come from the same
harness with `features.builder.LEGACY_FEATURE_COLS` and the earlier model
settings (`max_depth=4, min_samples_leaf=20, class_weight="balanced",
max_iter=300`).
