#!/usr/bin/env python3
"""
Check on real data that an upcoming race gets the features the model was trained on.

Works on a copy of the database: the race's results are deleted there (so its
stages are "open"), the riders who actually started are served as the PCS
start list, and the features built from that start list are compared with the
ones built from the stored result.  They must be identical: a feature may only
depend on what was known before the stage.  No network access, the real
database is not touched.

    python scripts/check_startlist_parity.py race/il-lombardia/2026
    python scripts/check_startlist_parity.py race/ronde-van-vlaanderen-we/2026 --gender women

Exit code 1 when a feature differs.
"""
import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("race", help="PCS race slug with stored results, e.g. race/il-lombardia/2026")
    parser.add_argument("--gender", default="men", choices=["men", "women"])
    args = parser.parse_args(argv)

    tmp = tempfile.mkdtemp(prefix="pcs-parity-")
    copy = os.path.join(tmp, "cycling.db")
    shutil.copy(os.environ.get("PCS_DB_PATH", ROOT / "data" / "cycling.db"), copy)
    os.environ["PCS_DB_PATH"] = copy          # read by config.py when the project is imported below

    import numpy as np
    import pandas as pd

    import model.predict as predict
    from features.builder import FEATURE_COLS, build_features

    try:
        with sqlite3.connect(copy) as conn:
            row = conn.execute("SELECT id, start_date FROM races WHERE pcs_slug = ?", (args.race,)).fetchone()
            if row is None:
                print(f"race not in the database: {args.race}")
                return 2
            race_id, start = row
            stage_ids = [r[0] for r in conn.execute(
                "SELECT id FROM stages WHERE race_id = ? ORDER BY date, id", (race_id,))]
            starters = pd.read_sql(
                """SELECT ri.pcs_slug AS slug, ri.name, x.bib, x.team_slug
                   FROM results x JOIN riders ri ON ri.id = x.rider_id WHERE x.stage_id = ?""",
                conn, params=(stage_ids[0],))
        if starters.empty:
            print(f"no stored result for the first stage of {args.race}")
            return 2

        # 1. features of the first stage from its stored result
        ref = build_features(cutoff_date=start, race_slug=args.race, gender=args.gender, stage_clf=None)
        ref = ref[ref["stage_id"] == stage_ids[0]].set_index("rider_id").sort_index()

        # 2. the same stage as an open one, from the start list
        with sqlite3.connect(copy) as conn:
            conn.execute(f"DELETE FROM results WHERE stage_id IN ({','.join('?' * len(stage_ids))})", stage_ids)
            conn.commit()
        predict.fetch_startlist = lambda slug: [
            {"slug": r.slug, "name": r.name, "team": None, "team_slug": r.team_slug,
             "bib": None if pd.isna(r.bib) else int(r.bib)}
            for r in starters.itertuples()]
        predict.refresh_open_stage_routes = lambda stage_ids: 0      # no request to PCS from this check
        entries, _ = predict.startlist_entries(args.race, start)
        got = build_features(cutoff_date=start, race_slug=args.race, gender=args.gender, stage_clf=None,
                             entries=entries)
        got = got[got["stage_id"] == stage_ids[0]].set_index("rider_id").sort_index()

        print(f"{args.race}: {len(ref)} riders from the result, {len(got)} from the start list, "
              f"{entries['stage_id'].nunique()} open stage(s), {len(FEATURE_COLS)} features")
        if list(ref.index) != list(got.index):
            print("the two paths disagree on who starts")
            return 1
        differing = {}
        for col in FEATURE_COLS:
            a, b = ref[col].astype(float), got[col].astype(float)
            same = (a.isna() & b.isna()) | np.isclose(a.fillna(0), b.fillna(0), rtol=1e-5, atol=1e-6)
            if not same.all():
                differing[col] = int((~same).sum())
        if differing:
            print("features that differ (riders affected):", differing)
            return 1
        print("every feature is identical on both paths")

        # 3. the full prediction on the copy (the models on disk have seen this race: a smoke test, not a score)
        try:
            df = predict.predict_race(args.race, start, gender=args.gender)
        except FileNotFoundError as exc:
            print(f"(no prediction shown: {exc})")
            return 0
        cols = [c for c in ("rider_name", "top10_prob", "top3_prob", "win_prob") if c in df.columns]
        print(df[df["stage_id"] == stage_ids[0]].head(10)[cols].round(3).to_string(index=False))
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
