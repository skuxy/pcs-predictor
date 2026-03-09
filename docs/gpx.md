# GPX File Support

## Why GPX?

PCS assigns each stage one of five profile tags: `flat`, `hilly`, `mountain`,
`itt`, or `utt`. These are useful but coarse. A GPX file gives you the actual
elevation trace, which enables:

- Exact total elevation gain
- Climb-by-climb analysis (length, avg/max gradient, position in race)
- Altitude exposure (time above 2000 m)
- Whether the hardest climb is early or late in the stage

## Where to get GPX files

| Source | Notes |
|--------|-------|
| Official race websites | Often published a few days before the stage |
| [Veloviewer](https://veloviewer.com) | Good coverage of WT races |
| [Strava segments](https://www.strava.com) | Community-created routes |
| [Komoot](https://www.komoot.com) | Route planning exports |
| [CycleDiv](https://www.cyclediv.cc) | Historical stage GPX library |

## Ingesting a GPX file

```bash
python main.py gpx path/to/stage17.gpx --stage race/tour-de-france/2025/stage-17
```

The `--stage` argument must match the `pcs_slug` of a row in `stages` or
`upcoming_stages`.

## What the parser extracts

### Stage summary
| Field | Description |
|-------|-------------|
| `total_distance_km` | Track length from haversine distances |
| `total_elevation_gain_m` | Cumulative uphill metres |
| `total_elevation_loss_m` | Cumulative downhill metres |
| `max_elevation_m` | Highest point |
| `min_elevation_m` | Lowest point |

### Per-climb (stored in `stage_climbs`)
| Field | Description |
|-------|-------------|
| `climb_order` | 1 = first climb encountered |
| `start_km` | Distance from start where climb begins |
| `length_km` | Climb length |
| `elevation_gain_m` | Height gained |
| `avg_gradient_pct` | Mean gradient (%) |
| `max_gradient_pct` | Steepest 100 m section (%) |

## Climb detection algorithm

1. Elevation trace is smoothed with a moving average (window = 10 points) to
   remove GPS noise.
2. Contiguous uphill sections are identified.
3. A climb is retained if `length_km >= 2.0` **and** `elevation_gain_m >= 80`.
   These thresholds are configurable in `scraper/gpx.py`.

## Using GPX features in the model (Phase 2)

Phase 2 will expose these features per stage:
- `n_climbs` — total number of detected climbs
- `final_climb_gradient` — avg gradient of the last climb (often decisive)
- `elevation_above_2000m_pct` — fraction of stage at altitude (affects GC riders more)
- `total_elevation_gain_m` (replaces PCS `elevation_m` when GPX is available)
