"""
Try the UI's Predictions tab when no race is open or PCS cannot be reached.

Runs ui/app.py against a copy of the database in which one finished race is
turned into an upcoming one (results deleted, dates moved to tomorrow), with
the riders who actually started served as its start list.  The real database
and the production code are untouched; the predictions shown are for looking
at the screen, not for judging the model (it has seen this race).

    PCS_DEMO_RACE=race/il-lombardia/2026 streamlit run scripts/ui_demo.py
"""
import datetime
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
RACE = os.environ.get("PCS_DEMO_RACE", "race/il-lombardia/2026")

# Streamlit re-runs this file on every interaction: set the copy up once per process.
if "PCS_UI_DEMO_READY" not in os.environ:
    copy = os.path.join(tempfile.mkdtemp(prefix="pcs-ui-demo-"), "cycling.db")
    shutil.copy(os.environ.get("PCS_DB_PATH", ROOT / "data" / "cycling.db"), copy)
    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    with sqlite3.connect(copy) as conn:
        conn.row_factory = sqlite3.Row
        race = conn.execute("SELECT id, start_date FROM races WHERE pcs_slug = ?", (RACE,)).fetchone()
        if race is None:
            raise SystemExit(f"race not in the database: {RACE}")
        stages = conn.execute("SELECT id, date FROM stages WHERE race_id = ? ORDER BY date, id",
                              (race["id"],)).fetchall()
        starters = [dict(r) for r in conn.execute(
            """SELECT ri.pcs_slug AS slug, ri.name, x.team_name AS team, x.team_slug, x.bib
               FROM results x JOIN riders ri ON ri.id = x.rider_id WHERE x.stage_id = ?""", (stages[0]["id"],))]
        shift = tomorrow - datetime.date.fromisoformat(race["start_date"])
        for st in stages:
            moved = datetime.date.fromisoformat(st["date"]) + shift
            conn.execute("UPDATE stages SET date = ? WHERE id = ?", (moved.isoformat(), st["id"]))
            conn.execute("DELETE FROM results WHERE stage_id = ?", (st["id"],))
        last = datetime.date.fromisoformat(stages[-1]["date"]) + shift
        conn.execute("UPDATE races SET start_date = ?, end_date = ? WHERE id = ?",
                     (tomorrow.isoformat(), last.isoformat(), race["id"]))
        conn.commit()
    os.environ["PCS_DB_PATH"] = copy          # read by config.py when the project is imported below
    os.environ["PCS_UI_DEMO_READY"] = "1"

    import model.predict as predict
    predict.fetch_startlist = lambda slug: starters if slug == RACE else []

exec(compile((ROOT / "ui" / "app.py").read_text(), str(ROOT / "ui" / "app.py"), "exec"))
