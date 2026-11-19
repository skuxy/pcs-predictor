# How accurate is the predictor? (November 2026)

Measured on every stage of the 2025 and 2026 seasons, each predicted by models
that had only seen earlier stages. Results stored up to 2026-10-18; numbers as
of the run of 2026-11-17. A data fix on 2026-11-19 (stage distances) moved
them slightly; see "After the re-scrape of 19 November" right below the
tables.

| Men, per stage | Before | After the model rounds | With stage data | Now |
|---|---|---|---|---|
| Stages scored | 939 | 939 | 947 | 947 |
| Of the model's 10 picks, finished in the top 10 | 4.09 | 4.36 | 4.56 | **4.67** |
| Of its 3 podium picks, finished on the podium | 0.85 | 1.01 | 1.05 | 1.07 |
| Winner was its first pick | 21.2% | 27.3% | 26.1% | 26.7% |
| Winner among its first 3 picks | 36.9% | 47.6% | 48.8% | 49.1% |
| Winner among its first 10 picks | 64.0% | 69.2% | 69.4% | **71.0%** |
| AUC | 0.854 | 0.872 | 0.879 | **0.884** |

| Women, per stage (213 stages) | Before | After the model rounds | With stage data | Now |
|---|---|---|---|---|
| Of the model's 10 picks, finished in the top 10 | 4.64 | 4.85 | 4.91 | 5.02 |
| Of its 3 podium picks, finished on the podium | 1.04 | 1.16 | 1.17 | **1.24** |
| Winner was its first pick | 29.6% | 34.7% | 33.3% | 35.2% |
| Winner among its first 3 picks | 51.2% | 58.2% | 53.5% | 56.8% |
| Winner among its first 10 picks | 73.7% | 76.5% | 77.9% | 77.9% |
| AUC | 0.862 | 0.885 | 0.887 | **0.891** |

Bold in the last column: the change from the column before is outside its
95% interval. Picking ten riders at random gets 0.77 (men) and 0.92 (women)
top-10 hits.

### After the re-scrape of 19 November

`scrape-missing --year 2026` and `scrape-elevation` were run from the Mac with
the round-6 code, and the models retrained and re-measured on the result.

- **Distances:** all 67 stages that had results but distance 0 now have their
  distance (68 stage rows updated). The check for time trials stored as road
  stages (RUNBOOK) finds none among them.
- **No new results:** 67 stages of 2026 are still without one. PCS still
  answers HTTP 500 for the Tour of Holland, Simac Ladies Tour and
  International Tour of Hellas pages. Stage 3 of the Vuelta and stage 8 of
  the Volta a Portugal show no finishing positions on pages fetched after
  the race, so the stale cache was not the reason for those two.
- **One label changed:** Vuelta 2026 stage 19 is now `mountain` (was
  `hilly`), from the re-read race page.

| Per stage | Men, 17 Nov | Men, 19 Nov | Women, 17 Nov | Women, 19 Nov |
|---|---|---|---|---|
| Of the model's 10 picks, finished in the top 10 | 4.67 | 4.69 | 5.02 | 5.03 |
| Of its 3 podium picks, finished on the podium | 1.07 | 1.09 | 1.24 | 1.23 |
| Winner was its first pick | 26.7% | 27.1% | 35.2% | 35.2% |
| Winner among its first 3 picks | 49.1% | 48.9% | 56.8% | 57.3% |
| Winner among its first 10 picks | 71.0% | 71.5% | 77.9% | 77.9% |
| AUC | 0.884 | 0.885 | 0.891 | 0.891 |
| Route hidden (`evaluate --hide-route`), top-10 hits | 4.24 | 4.25 | 4.82 | 4.82 |

All of it is inside the noise between runs, as expected for a fix to 67 of the
1,160 test stages; the gain sits on hilly stages (4.32 to
4.36 hits, 3.67 to 3.70 with the route hidden). No paired interval was
computed for this step. The models, `model/evaluation*.json` and the README
carry the 19 November numbers; the tables and breakdowns in the rest of this
report are the run of 17 November.

**These numbers are for stages whose route is known** (profile score,
gradient of the final km, elevation), which is every finished stage. A stage
predicted before PCS publishes its route scores 4.24 hits (men) and 4.82
(women) — and until this round an upcoming stage normally had no route in
the database, published or not. See "A stage without a published route"
under Round 6.

- **Before** is the pipeline as it stood on `main` (87 features, depth-4
  trees, one top-10 model answering all three questions). The men's column
  was run with the original code; the women's with the original feature list
  and model settings on the current feature builder (on the men that variant
  scores 0.01 hits above the original).
- **After the model rounds** (11 November): new features, ratings, podium and
  win models, new model settings, on the same data.
- **With stage data** (17 November, morning): the same code after the scrape
  from the Mac — elevation for about 1,200 more stages, profile score and
  final-km gradient for about 850, 34 wrong stage labels corrected, and the 8
  stages of 13–18 October that PCS serves.
- **Now** (17 November, round 6): 11 more features, a rider's earlier results
  weighted by how much the stage resembled today's (184 in all), on the same
  data.

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
| Round 6, top-10 hits | +0.11 [+0.06, +0.16] | +0.11 [0.00, +0.21] |
| Round 6, podium hits | +0.023 [−0.002, +0.049] | +0.070 [+0.005, +0.141] |
| Round 6, winner called | +0.6 points [−1.2, +2.3] | +1.9 points [−2.8, +6.6] |
| Round 6, winner in first 3 | +0.3 points [−1.5, +2.1] | +3.3 points [−0.9, +7.5] |

- **Men:** all three steps raised the top-10 hits beyond doubt. Picking the
  winner improved with the model rounds and has not moved since: 26–27% of
  first picks win.
- **Women:** the top-10 gain of the model rounds holds, the stage data added
  nothing measurable, and round 6 added 0.07 podium hits and probably some
  top-10 hits (the interval starts at zero). "Winner among the first 3" went
  +7.0, −4.7 and +3.3 points over the three steps; with 213 stages read it as
  "about 55%", not as a trend.
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
  a stage's own day and of every later stage and checks that the stage's 184
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
| Flat | 152 | 5.03 | 1.26 | 28% | 61% | 0.900 |
| Hilly | 556 | 4.32 | 0.96 | 26% | 46% | 0.870 |
| Time trial | 56 | 5.71 | 1.39 | 41% | 68% | 0.921 |
| Mountain | 183 | 5.10 | 1.16 | 23% | 43% | 0.904 |

| | Stages | Top-10 hits | Winner called | Winner in first 3 |
|---|---|---|---|---|
| WorldTour | 319 | 4.49 | 31% | 54% |
| ProSeries | 315 | 4.75 | 23% | 49% |
| .1 | 313 | 4.78 | 26% | 45% |
| One-day races | 218 | 4.56 | 28% | 46% |
| Stage-race stages | 729 | 4.70 | 26% | 50% |
| 2025 | 467 | 4.72 | 25% | 49% |
| 2026 | 480 | 4.63 | 28% | 49% |

Against rankings by a single number, on the same 947 stages:

| Ranking | Top-10 hits | Winner called | Winner in first 3 | AUC |
|---|---|---|---|---|
| The model | 4.67 | 27% | 49% | 0.884 |
| Elo rating (all stages) | 3.37 | 13% | 28% | 0.819 |
| PCS points, last 365 days | 3.32 | 19% | 36% | 0.805 |
| Top-10 rate, last 90 days | 3.03 | 12% | 23% | 0.753 |

- More than half of all stages have 5 or more hits (56%; 42% before the
  work, 54% before round 6); 12% have two or fewer (21%, 15%).
- On 11.7% of stages the winner sits outside the model's first 30 picks (16%
  before the work): breakaways and outsiders that results cannot foresee.
- Where the ten picks end up, per stage: 4.67 in the top 10, 1.92 in places
  11–20, 1.36 in 21–40, 1.72 beyond 40th, and 0.33 do not finish.
- When the stage's label matches what happened, the model is at its best:
  5.2 hits on flat stages that ended in a bunch sprint, 5.3 on mountain
  stages decided by a small group. The weak spot is still the "hilly" label,
  which covers 59% of the test stages and splits evenly between bunch sprints
  and small groups (4.4 and 4.3 hits) — though its distance to flat stages
  shrank from 0.95 to 0.71 hits in round 6.

### The probabilities can be read as probabilities

| P(top 10) given | Riders | Average given | Finished top 10 |
|---|---|---|---|
| 0.0–0.1 | 102,779 | 0.022 | 0.020 |
| 0.1–0.2 | 12,303 | 0.144 | 0.140 |
| 0.2–0.3 | 5,735 | 0.243 | 0.254 |
| 0.3–0.4 | 3,270 | 0.346 | 0.346 |
| 0.4–0.5 | 2,279 | 0.448 | 0.460 |
| 0.5–0.6 | 1,591 | 0.546 | 0.541 |
| 0.6–0.7 | 956 | 0.643 | 0.676 |
| 0.7–0.8 | 471 | 0.743 | 0.743 |
| 0.8–0.9 | 137 | 0.838 | 0.861 |
| 0.9–1.0 | 48 | 0.959 | 0.875 |

Brier score 0.0506 (0.0558 before the work), log loss 0.1769 (0.1981). Only
the very top is too confident: of the 48 riders given more than 0.9, 42
finished in the top 10. The model is also more decisive than before the work:
its first pick averages P(top 10) = 0.68 (was 0.54), and at Strade Bianche
2026 Pogačar gets 0.88 instead of 0.53.

The women's probabilities are slightly cautious in the middle (riders given
0.6–0.7 finished in the top 10 72% of the time) and too confident at the very
top (10 of the 13 riders given more than 0.9 finished in the top 10); Brier
0.0575, log loss 0.1956.

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

### Round 6 — stages like this one, and stages without a route (+0.011)

Production models, paired on all 947 men's stages: top-10 precision 0.4558 →
0.4671, Δ +0.0113 [+0.0065, +0.0164]; winner among the first ten picks +1.6
points [+0.1, +3.2]; first pick and first three unchanged (intervals at the
top).

**Where the model missed.** Before this round, 4.56 of its ten picks finished
in the top 10, 1.89 in places 11–20, 1.38 in 21–40, 1.85 beyond 40th and 0.33
did not finish. From the other side: 1.5 of each stage's top-10 finishers had
been ranked beyond 40th, and on every feature (results, ratings, points,
place in the team) those riders look like the average starter, not like the
picks. They are breakaway survivors and one-off rides, and results cannot
tell them apart beforehand. What can be gained is in ordering the thirty or
so riders who are plausible for the stage at hand.

**Adopted: results on stages like this one** (`SIMILAR_STAGE_COLS`, 11
features). Until now "the same kind of stage" meant the same label, and
"hilly" runs from Kuurne to Liège. The new features weigh every earlier
result of a rider by how close that stage's profile score was to today's (on
a square-root scale; one-year half-life, two-year window), and a second set
also by the gradient of the final km: finishing percentile, top-10 rate, PCS
points, the amount of such history, and the rider's place in today's field
on three of them. Time trials are compared with time trials only.

| Single model, reference 0.4526 | Δ precision@10 |
|---|---|
| **+ results on similar stages (11 features)** | **+0.0082 [+0.0029, +0.0133]** |
| + bunch finishes split into mass sprints (40+ riders on the winner's time) and reduced groups, with a rating for each (19) | −0.0002 [−0.0046, +0.0041] |
| + win rates by kind of finish (5) | +0.0031 [−0.0013, +0.0074] |
| + the same weeks of earlier seasons (4) | +0.0015 [−0.0026, +0.0060] |
| + level of today's field by kind of rider (6) | 0.0000 [−0.0042, +0.0043] |
| + all five groups (45) | +0.0098 [+0.0045, +0.0153] |
| Similar stages + narrower, wider and more recent kernels, podium rate, results against the rating (20 more) | +0.0027 [−0.0018, +0.0074] over similar stages alone |

Only the first line was adopted; nothing else is outside its interval.

- **Where the gain is** (production models, top-10 hits): hilly stages
  4.14 → 4.32 (+0.18 [+0.12, +0.25]) and time trials 5.50 → 5.71 (+0.21
  [+0.02, +0.41], 56 stages). Flat (−0.06 [−0.16, +0.04]) and mountain
  stages (+0.02 [−0.12, +0.15]) did not move. By profile score, road stages
  of 51–150 gained 0.17 [+0.09, +0.25]; the flattest (0–50) and the hardest
  (151 and up), where the label already says enough, did not (+0.05
  [−0.04, +0.14] and 0.00 [−0.10, +0.10]).
- **Importance.** The rider's place in today's field by points on similar
  stages (`sim_pts_frank`) is now the second most important feature for the
  men and the first for the women. `profile_score` itself fell from 3rd to
  29th: what it knows now arrives through these features.
- **Podium and win models.** Podium model: +0.034 podium hits [+0.001,
  +0.066]. Win model: no change (winner's reciprocal rank −0.004 [−0.018,
  +0.009]). With all 65 candidate features the win model gives the winner a
  higher probability (log-probability +0.049 [+0.014, +0.086]) but does not
  rank him higher (+0.006 [−0.010, +0.021]), so they stayed out.
- **Calling the kind of finish** (mass sprint, reduced group, small group or
  solo) from earlier editions and the make-up of the field: 66% → 71% right,
  62% → 67% on hilly stages. Not built into the pipeline: round 4 measured
  what a perfect answer would be worth (+0.05 hits).

#### A stage without a published route

The calendar scrape stores a stage's profile icon and, sometimes, its
distance. Profile score, final-km gradient and elevation were written when
the result was scraped — or by a `scrape-elevation` run in between, which
nothing in the prediction path started and which read the stage page out of
the cache. The walk-forward test scores finished stages, which have all of
it; a prediction for next Sunday normally had none of it. Scoring the test
stages that way:

| Men, top-10 hits | Route known | Route not in the database |
|---|---|---|
| The model before this round (single model, 173 features) | 4.53 | 3.87 |
| … hilly / mountain | 4.11 / 5.12 | 3.43 / 3.85 |
| Now (production models, `evaluate --hide-route`) | 4.67 | 4.24 |
| … flat / hilly / time trial / mountain | 5.03 / 4.32 / 5.71 / 5.10 | 5.04 / 3.67 / 5.61 / 4.87 |
| Women, now | 5.02 | 4.82 |

So for an upcoming stage the accuracy quoted so far was about 0.7 hits too
high. Two changes:

1. **`predict` asks PCS for the route of the open stages first**
   (`model.predict.refresh_open_stage_routes`), and `scrape-elevation` covers
   stages up to three weeks ahead. Where PCS has the profile, the prediction
   is of the 4.67 kind.
2. **Where PCS has nothing yet, the stage is predicted as a typical stage of
   its icon** (`ROUTE_DEFAULTS`: median profile score, gradient and
   elevation per km of the stored flat, hilly, mountain and time-trial
   stages), and the CLI and the UI say so. Single-model precision with the
   route hidden: 0.3871 for the model before this round; 0.4130 with the
   similar-stage features centred on the typical profile score; 0.4212 with
   the route features filled in as well (+0.0081 [+0.0021, +0.0138]),
   against 0.4636 with the route. On stages that have a route the stand-in
   changes nothing (−0.0010 [−0.0052, +0.0030]). Training on a mix of known
   and hidden routes instead was worse on both kinds (known −0.0062
   [−0.0109, −0.0017], hidden 0.4178).

What remains without a route is 0.43 hits (0.65 on hilly stages): the profile
score is worth fetching. Two limits of these numbers:

- The test keeps each stage's icon and distance, which a stage list normally
  shows. A stage PCS lists with neither is stored as "profile unknown" (the
  default `mountain` label below) and gets no stand-in; the 30 such stages
  that were in the test before the Mac scrape scored 2.9 hits.
- The fetch at predict time and the cache rule below have only run against
  fake pages, because PCS blocks this machine. Their first real test is a
  `predict` from the Mac.

#### Other checks

- **Predicting before bibs are assigned** (leader bib unknown) costs
  nothing: Δ +0.0014 [−0.0006, +0.0033].
- **Calibration does not cost ranking.** Ranking by the boosted models' own
  scores instead of the calibrated probabilities: winner's reciprocal rank
  +0.0004 [−0.0045, +0.0053]. Two of the first three picks share exactly the
  same P(win) in 0.1% of stages.
- **How much a season of history is worth.** The 480 stages of 2026 with the
  database cut to start in 2025, 2024 or 2023 (the model before this round):
  4.43, 4.48 and 4.54 hits. Three seasons against one: +0.11 [+0.02, +0.19];
  the third on top of the second: +0.06 [−0.01, +0.13]. Picking winners does
  not depend on it (reciprocal rank −0.002 [−0.021, +0.017]).

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
  (`scripts/check_startlist_parity.py`), all 184 features equal the
  result-based ones for every starter.
- **An upcoming stage normally had no route in the database** (round 6):
  profile score, final-km gradient and elevation arrived with the result, so
  a real prediction was made without them, at about 0.7 hits below the
  quoted accuracy. `predict` now fetches them, and a stage PCS has no profile
  for is predicted as a typical stage of its icon.
- **A page cached before the stage was served for ever.** Cached pages never
  expired. A stage page fetched during a calendar scrape shows the start list
  where the result will be; the parser reads that as "no real finishing
  positions (all 'NR' placeholders)", a cancelled stage, and every later run
  got the same copy. In the Mac's log of 16 November, stage 3 of the Vuelta
  2026 and stage 8 of the Volta a Portugal 2026 were answered this way, out
  of the cache. A copy written on or before the stage's day is now fetched
  again when the stage's result or route is wanted
  (`scraper.utils.pages_newer_than`), `scrape` no longer asks for stages
  still to come, and the running season's calendar is refreshed daily. For
  two weeks after a stage a page without the result is asked for again on a
  later day, because PCS enters some results late.
- **67 stages with results had no distance** (found in round 6; filled by
  the Mac run of 19 November). A stage stored from the stage list before PCS showed its
  distance kept `distance_km = 0` once it had results, because `scrape`
  skips such a stage: Tour de Suisse, Tour of Belgium, Tour of Slovenia, Tour
  of Turkey and others between April and July 2026, about one in nine of the
  2026 stages. For these the model sees a distance of 0 and no elevation per
  km. `scrape-elevation` now reads the distance from the stage page (Tour of
  Belgium stage 1: 188.3 km); it needs the Mac's page cache or PCS.
- **A stage race under way** only got predictions for the stages already
  ridden; the open stages are now predicted from the start list.
- **UI**: the Model tab showed "Validation AUC 0.0000" and never had feature
  importances (the boosted model has none built in; they are now measured by
  permutation); P(win) / P(top 3) columns were looked up under the wrong
  names; out of season the Predictions tab was empty without explanation.
- **Build cost**: the feature build paired every result of a rider with every
  other one before filtering by date. It now builds only the earlier-result
  pairs, a group of riders at a time: about 30 s and 3.5 GB for the men's
  260,217 rows with 184 features (9.5 GB before the grouping of round 6),
  against 46 s and 9.7 GB for 87 features originally.
- **Rider links with a discipline suffix.** PCS now links some riders as
  `rider/rein-taaramae/road`; the result parser stored that as a new rider
  without history (found on the Japan Cup page, fixed in
  `scraper/results.py` and the start-list parser, the one affected row
  repaired).

## What would move it further

In order of expected value. PCS could not be fetched from the cloud VM (see
RUNBOOK, "still on Cloudflare challenge page"); from the Mac it works.

1. **The route of an upcoming stage.** The largest measured gap is no longer
   in the model: 0.43 hits between a stage with its route and one without
   (0.65 on hilly stages). First, see what PCS really publishes before a
   race, with a `predict` from the Mac for the first races of 2027. Then, for
   races without a profile, a better stand-in than the icon's median: last
   year's edition of a one-day race is nearly the same route, and one-day
   races lose 0.38 hits without theirs.
2. **Hilly stages.** Still 59% of the calendar and the weakest group (4.32
   hits). Profile score and final-km gradient are now used for all they say;
   what they do not say is where the climbs are. Position and length of the
   last climbs (from the stage profile or GPX) would be the data to try.
3. **Picking winners.** "Winner called" has stood at 26–27% since the win
   model was added; stage data and similar stages did not move it, nor did
   win rates, the split of bunch finishes or ranking by uncalibrated scores
   in this round. One stage in eight or nine is won from outside the first 30
   picks. Bookmaker odds would show how much is left to find.
4. **Seasons before 2023.** Measured in round 6: the third season of history
   added 0.06 hits [−0.01, +0.13] on 2026 stages and nothing for winners; a
   fourth should add less. About 45 minutes of scraping per season.
5. **Rider profiles** for the 77% of riders without one (age mostly).
6. **The 2027 calendar** from January.

Done from the Mac on 19 November, with a retrain and re-measurement (see
"After the re-scrape of 19 November" at the top); as it was planned:

- `python main.py scrape-missing --year 2026` with the cache rule of round 6
  (at most about 100 pages). It fetches again the two stages that read as
  cancelled, if their copies predate the stage, and retries the three races
  PCS answered HTTP 500 for.
- `python main.py scrape-elevation` for the distance of the 67 stages above
  (from the page cache).

Not available: PCS answers HTTP 500 for the Tour of Holland 2026, Simac
Ladies Tour 2026 and International Tour of Hellas 2026 pages (17 stages), and
has no results for another 22 races in the 2026 calendar (43 stages, among
them Trofeo Baracchi and the Tour of Guangxi women's race). Two stages read
as cancelled, stage 8 of the Volta a Portugal and stage 3 of the Vuelta, but
both answers came out of the page cache (see "A page cached before the
stage"); the scrape of 19 November read both from pages fetched after the
race and found no finishing positions again. Five empty stage rows are
leftovers of stage lists PCS renumbered (Volta a Portugal `stage-11` and
`stage-12`; after a prologue was added, Boucles de la Mayenne, Tour of
Estonia and Deutschland Tour).

## Reproduce

```bash
python main.py evaluate                  # men, ~9 min on 6 cores; prints the tables above
python main.py evaluate --gender women   # ~2 min
python main.py evaluate --hide-route --top10-only   # the "route not in the database" column; prints only
```

The first two write `model/evaluation[_women].json` and
`model/walkforward[_women].csv.gz`. The "before" columns come from the same
harness with `features.builder.LEGACY_FEATURE_COLS` and the earlier model
settings (`max_depth=4, min_samples_leaf=20, class_weight="balanced",
max_iter=300`).
