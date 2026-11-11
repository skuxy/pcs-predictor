"""Point-in-time features: history pairing, finish types, start-list rows, no leakage."""
import sqlite3

import numpy as np
import pandas as pd

from features.builder import (
    FEATURE_COLS, LEGACY_FEATURE_COLS, ENTRY_STATUS, _history_pairs, build_features,
    race_base, race_sprint_history, race_tier, stage_finish_facts,
)


def test_feature_lists_are_consistent():
    assert len(FEATURE_COLS) == len(set(FEATURE_COLS))
    # rider-page facts dated at scrape time are not model inputs
    dropped = set(LEGACY_FEATURE_COLS) - set(FEATURE_COLS)
    assert dropped == {"pcs_rank", "spec_gc", "spec_sprinter", "spec_puncher", "spec_classics",
                       "spec_tt", "spec_climber"}


def test_race_tier_and_base():
    assert race_tier("2.UWT") == "WorldTour"
    assert race_tier("1.WWT") == "WorldTour"
    assert race_tier("1.Pro") == "ProSeries"
    assert race_tier("2.1") == ".1"
    assert race_tier(None) == ".1"
    s = race_base(pd.Series(["race/tour-de-france/2026", "race/gp-de-denain/2025"]))
    assert s.tolist() == ["race/tour-de-france", "race/gp-de-denain"]


def test_history_pairs_matches_a_brute_force_join():
    rng = np.random.default_rng(1)
    hist = pd.DataFrame({
        "rider_id": rng.integers(1, 6, 60),
        "hist_date": pd.to_datetime("2026-01-01") + pd.to_timedelta(rng.integers(0, 40, 60), unit="D"),
        "value": rng.normal(size=60),
    })
    targets = pd.DataFrame({
        "rider_id": [1, 2, 3, 4, 5, 9],
        "stage_id": [100, 101, 102, 103, 104, 105],
        "stage_date": pd.to_datetime(["2026-01-20", "2026-01-01", "2026-02-15", "2026-01-10", "2026-01-30",
                                      "2026-01-30"]),
    })
    got = _history_pairs(targets, hist)
    want = targets.merge(hist, on="rider_id")
    want = want[want["hist_date"] < want["stage_date"]]
    assert len(got) == len(want)
    assert (got["hist_date"] < got["stage_date"]).all()
    key = ["rider_id", "stage_id", "hist_date", "value"]
    pd.testing.assert_frame_equal(
        got[key].sort_values(key).reset_index(drop=True),
        want[key].sort_values(key).reset_index(drop=True))
    assert (got["_day_delta"] == (got["stage_date"] - got["hist_date"]).dt.days).all()
    # a rider without any result (9) contributes no rows
    assert 9 not in set(got["rider_id"])


def test_stage_finish_facts_types():
    rows = []
    for pos in range(1, 13):                      # stage 1: bunch sprint
        rows.append((1, pos, pos, "finished", 0))
    for pos in range(1, 13):                      # stage 2: solo winner
        rows.append((2, pos, pos, "finished", 0 if pos == 1 else 30 + pos))
    for pos in range(1, 13):                      # stage 3: five on the winner's time
        rows.append((3, pos, pos, "finished", 0 if pos <= 5 else 20))
    for pos in range(1, 13):                      # stage 4: no times stored
        rows.append((4, pos, pos, "finished", None))
    rows.append((1, 99, None, "dnf", None))
    res = pd.DataFrame(rows, columns=["stage_id", "rider_id", "position", "status", "time_seconds"])
    facts = stage_finish_facts(res)
    assert facts.loc[1, "finish_type"] == "bunch"
    assert facts.loc[2, "finish_type"] == "select"
    assert facts.loc[3, "finish_type"] == "mixed"
    assert facts.loc[4, "finish_type"] is None or pd.isna(facts.loc[4, "finish_type"])
    assert facts.loc[1, "n_fin"] == 12


def test_race_sprint_history_only_looks_at_earlier_editions():
    stages = pd.DataFrame({
        "id": [1, 2, 3, 4, 5],
        "race_slug": ["race/x/2024", "race/x/2025", "race/x/2025", "race/x/2026", "race/y/2026"],
        "date": pd.to_datetime(["2024-04-01", "2025-04-01", "2025-04-02", "2026-04-01", "2026-05-01"]),
        "profile_type": ["flat", "hilly", "itt", "hilly", "flat"],
        "finish_type": ["bunch", "select", "select", "bunch", "bunch"],
    })
    h = race_sprint_history(stages)
    assert np.isnan(h.loc[1, "race_bunch_rate_prev"]) and h.loc[1, "race_prev_stages"] == 0
    assert h.loc[2, "race_bunch_rate_prev"] == 1.0            # 2024 only
    assert h.loc[4, "race_bunch_rate_prev"] == 0.5            # 2024 bunch + 2025 select; the ITT is not counted
    assert h.loc[4, "race_prev_stages"] == 2
    assert np.isnan(h.loc[5, "race_bunch_rate_prev"])         # first edition of race y


def _features(**kw):
    df = build_features(gender="men", stage_clf=None, **kw)
    return df.set_index(["stage_id", "rider_id"]).sort_index()


def test_build_features_has_every_column_and_one_row_per_result(season_db):
    df = _features()
    assert not [c for c in FEATURE_COLS if c not in df.columns]
    with sqlite3.connect(season_db["path"]) as conn:
        n = conn.execute("SELECT COUNT(*) FROM results").fetchone()[0]
    assert len(df) == n
    assert df.index.is_unique


def test_stronger_riders_get_better_form_features(season_db):
    df = _features().reset_index()
    last = df[df["stage_id"] == season_db["stage_ids"][("gamma", 2026, 1)]].set_index("rider_id")
    assert last.loc[1, "elo_all"] > last.loc[30, "elo_all"]
    assert last.loc[1, "pts_365d"] > last.loc[30, "pts_365d"]
    assert last.loc[1, "elo_all_frank"] > 0.8 > 0.5 > last.loc[30, "elo_all_frank"]
    # same race a year earlier
    assert last.loc[1, "race_hist_n"] >= 1
    # team = bib decade: five riders each
    assert (last["team_size"] == 5).all()


def test_features_do_not_see_the_stage_or_anything_later(season_db):
    """Rewriting the results of a stage's own day and of every later stage
    must leave that day's features untouched."""
    sid = season_db["stage_ids"][("beta", 2026, 2)]
    before = _features().loc[sid]

    with sqlite3.connect(season_db["path"]) as conn:
        cutoff = conn.execute("SELECT date FROM stages WHERE id = ?", (sid,)).fetchone()[0]
        later = [r[0] for r in conn.execute("SELECT id FROM stages WHERE date >= ?", (cutoff,))]
        for stage_id in later:
            rows = conn.execute("SELECT id FROM results WHERE stage_id = ? ORDER BY id", (stage_id,)).fetchall()
            # reverse the finishing order, remove time gaps and points
            n = len(rows)
            for i, (rid,) in enumerate(rows):
                conn.execute(
                    "UPDATE results SET position = ?, status = 'finished', time_seconds = ?, points_pcs = ? WHERE id = ?",
                    (n - i, 500 + i, 100, rid))
        conn.commit()

    after = _features().loc[sid]
    cols = [c for c in FEATURE_COLS if not c.startswith(("p_sprint", "p_breakaway", "p_gc"))]
    pd.testing.assert_frame_equal(
        before[cols].astype(float), after[cols].astype(float), check_exact=False, rtol=1e-6)


def test_start_list_rows_get_features_but_never_count_as_history(season_db):
    with sqlite3.connect(season_db["path"]) as conn:
        conn.execute("INSERT INTO races (id, pcs_slug, name, year, start_date, end_date, class, is_stage_race, gender) "
                     "VALUES (100, 'race/alpha/2027', 'Alpha', 2027, '2027-03-10', '2027-03-10', '1.Pro', 0, 'men')")
        conn.execute("INSERT INTO stages (id, race_id, pcs_slug, date, distance_km, profile_type, surface) "
                     "VALUES (1000, 100, 'race/alpha/2027', '2027-03-10', 150, 'flat', 'road')")
        conn.commit()
    entries = pd.DataFrame({
        "stage_id": 1000,
        "rider_id": [1, 2, 30, -1],          # -1: a rider the database has never seen
        "bib": [1, 2, 11, np.nan],
        "team_slug": ["team-a", "team-a", "team-b", "team-c"],
    })
    df = build_features(cutoff_date="2027-01-01", race_slug="race/alpha/2027", gender="men",
                        stage_clf=None, entries=entries)
    assert len(df) == 4
    assert df["top10"].isna().all()
    df = df.set_index("rider_id")
    assert df.loc[1, "elo_all"] > df.loc[30, "elo_all"]
    assert df.loc[1, "race_hist_n"] == 3                      # alpha 2024, 2025, 2026
    assert np.isnan(df.loc[-1, "elo_all"]) and df.loc[-1, "n_results_all"] == 0
    assert df.loc[1, "bib_is_leader"] == 1 and df.loc[2, "bib_is_leader"] == 0
    assert df.loc[1, "team_size"] == 2

    # historic features are the same with or without the start-list rows
    sid = season_db["stage_ids"][("gamma", 2026, 1)]
    with_entries = build_features(gender="men", stage_clf=None, entries=entries)
    hist = with_entries[with_entries["stage_id"] == sid].set_index("rider_id").sort_index()
    plain = _features().loc[sid].sort_index()
    pd.testing.assert_frame_equal(hist[FEATURE_COLS].astype(float), plain[FEATURE_COLS].astype(float),
                                  check_exact=False, rtol=1e-6)
    assert (with_entries["status"] == ENTRY_STATUS).sum() == 4


def test_unread_mountain_label_is_treated_as_unknown(season_db):
    sid = season_db["stage_ids"][("beta", 2026, 3)]
    with sqlite3.connect(season_db["path"]) as conn:
        conn.execute("UPDATE stages SET profile_score = NULL, distance_km = 0 WHERE id = ?", (sid,))
        conn.commit()
    df = _features().loc[sid]
    assert df["profile_type"].isna().all()
    assert (df["is_mountain"] == 0).all()
