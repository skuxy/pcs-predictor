# Implementation Phases

## Phase 1 — Scraper ✅

**Goal:** Populate the SQLite database with ~1 year of historical race results.

### What is collected
- **Races** — name, year, class (WorldTour / HC / 2.1), country, date range
- **Stages** — distance, elevation, profile type, start/finish towns
- **Results** — finish position, time gap, PCS/UCI points, DNF/DNS status
- **Riders** — nationality, date of birth, team, weight, height, inferred speciality

### Key files
| File | Purpose |
|------|---------|
| `scraper/races.py` | Parse PCS race calendar and stage lists |
| `scraper/results.py` | Parse stage result tables |
| `scraper/riders.py` | Parse rider profile pages |
| `scraper/utils.py` | HTTP fetching, caching, rate limiting |
| `db/schema.sql` | Database schema |
| `db/database.py` | Upsert helpers |

### Rate limiting & caching
All HTTP responses are cached in `cache/html/` by URL hash. Re-running the
scraper is safe and will not re-fetch already-cached pages. A 1.5 s delay is
enforced between live requests.

---

## Phase 2 — Feature Engineering 🔜

**Goal:** Transform raw DB rows into ML-ready feature vectors, one row per
(rider, stage) pair.

### Planned features

#### Rider form
- Average finish position over last 30 / 60 / 90 days
- Best result in last 90 days
- Number of race days in last 30 days (fatigue proxy)
- DNF rate

#### Race-type affinity
- Rider's average position on flat / hilly / mountain stages separately
- Rider's TT performance vs field (time gap per km)
- Win rate by profile type

#### Physical match
- Stage elevation gain vs rider's historic comfort zone
- Longest climb gradient vs rider weight (watts/kg proxy)
- If GPX available: % of race above 2000 m, number of HC climbs

#### Contextual
- Days since last race (freshness)
- Team rank (proxy for lead-out / support quality)
- Race importance (Grand Tour vs one-day)

### Output
A `features/builder.py` module that produces a `pd.DataFrame` with all features
and a `position` target column, ready for model training.

---

## Phase 3 — Model Training 🔜

**Goal:** Learn from historical (rider, stage, features) → position data.

### Approach
- **Target**: top-10 finish probability (binary classification) — more reliable
  than exact position prediction.
- **Model**: Gradient Boosted Trees (`sklearn.ensemble.HistGradientBoostingClassifier`
  or `xgboost.XGBClassifier`) — handles mixed numeric/categorical features and
  missing values well.
- **Validation**: time-based split (train on earlier races, validate on later ones)
  to avoid data leakage.
- **Calibration**: `sklearn.calibration.CalibratedClassifierCV` to produce
  meaningful probabilities.

### Output artefacts
- `model/trained_model.pkl` — serialised model
- `model/feature_names.json` — ordered feature list (for inference alignment)
- `model/metrics.json` — validation AUC, precision, recall

---

## Phase 4 — Prediction 🔜

**Goal:** Given an upcoming race (already in `upcoming_stages` table), rank
likely competitors and score them.

### Workflow
1. Identify the start list for the upcoming stage (scrape PCS startlist page)
2. Build feature vectors for each rider using Phase 2 logic (using only past data)
3. Run the trained model → top-10 / podium / win probabilities
4. Output a ranked table

### Output formats
- Console table (rich)
- CSV export
- (Future) Web UI or Telegram bot

---

## GPX Integration

GPX files optionally replace or supplement PCS profile tags with precise
elevation data. The `stage_climbs` table stores detected climbs with gradient
statistics that Phase 2 can use as features.

See [gpx.md](gpx.md) for parsing details.
