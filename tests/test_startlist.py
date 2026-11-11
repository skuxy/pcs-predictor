"""Start-list parsing and the start-list → feature-row path of predict_race."""
import sqlite3

import numpy as np

import model.predict as predict
from scraper.startlist import parse_startlist

# Shape of PCS's start-list page (ul.startlist_v4: one <li> per team holding
# the team link and a nested list of riders, each with its bib).
STARTLIST_HTML = """
<html><body>
<div class="sidebar"><a href="rider/not-a-starter">Somebody Else</a></div>
<ul class="startlist_v4">
  <li>
    <div class="ridersCont">
      <a class="team" href="team/alpha-racing-2027">Alpha Racing (WT)</a>
      <ul>
        <li><span class="bib">1</span><span class="flag si"></span><a href="rider/rider-1">ONE Rider</a></li>
        <li><span class="bib">2</span><span class="flag be"></span><a href="rider/rider-2">TWO Rider</a></li>
      </ul>
    </div>
  </li>
  <li>
    <div class="ridersCont">
      <a class="team" href="team/beta-cycling-2027">Beta Cycling (PRT)</a>
      <ul>
        <li><span class="bib">11</span><a href="rider/rider-30/road">THIRTY Rider</a></li>
        <li><span class="bib"></span><a href="rider/new-kid">KID New</a></li>
        <li><span class="bib">11</span><a href="rider/rider-30">THIRTY Rider</a></li>
      </ul>
    </div>
  </li>
</ul>
</body></html>
"""


def test_parse_startlist_reads_team_and_bib():
    riders = parse_startlist(STARTLIST_HTML)
    assert [r["slug"] for r in riders] == ["rider-1", "rider-2", "rider-30", "new-kid"]   # no sidebar, no duplicate
    assert riders[0] == {"slug": "rider-1", "name": "ONE Rider", "team": "Alpha Racing (WT)",
                         "team_slug": "alpha-racing-2027", "bib": 1}
    assert riders[2]["team_slug"] == "beta-cycling-2027" and riders[2]["bib"] == 11
    assert riders[3]["bib"] is None


def test_parse_startlist_without_teams_or_bibs():
    html = '<ul class="startlist_v4"><li><a href="rider/solo-rider">RIDER Solo</a></li></ul>'
    assert parse_startlist(html) == [
        {"slug": "solo-rider", "name": "RIDER Solo", "team": None, "team_slug": None, "bib": None}]


def _add_upcoming_race(path, with_first_stage_result=False):
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO races (id, pcs_slug, name, year, start_date, end_date, class, is_stage_race, gender) "
                     "VALUES (100, 'race/beta/2027', 'Beta', 2027, '2027-05-01', '2027-05-02', '2.UWT', 1, 'men')")
        conn.execute("INSERT INTO stages (id, race_id, stage_num, pcs_slug, date, distance_km, profile_type, surface) "
                     "VALUES (1000, 100, 1, 'race/beta/2027/stage-1', '2027-05-01', 150, 'flat', 'road')")
        conn.execute("INSERT INTO stages (id, race_id, stage_num, pcs_slug, date, distance_km, profile_type, surface) "
                     "VALUES (1001, 100, 2, 'race/beta/2027/stage-2', '2027-05-02', 160, 'hilly', 'road')")
        if with_first_stage_result:
            for pos, rider in enumerate((1, 2), 1):
                conn.execute("INSERT INTO results (stage_id, rider_id, position, status, time_seconds, bib) "
                             "VALUES (1000, ?, ?, 'finished', 0, ?)", (rider, pos, pos))
            conn.execute("INSERT INTO results (stage_id, rider_id, position, status, bib) VALUES (1000, 30, NULL, 'dnf', 11)")
        conn.commit()


def test_startlist_entries_cover_every_open_stage(season_db, monkeypatch):
    _add_upcoming_race(season_db["path"])
    monkeypatch.setattr(predict, "fetch_startlist", lambda slug: parse_startlist(STARTLIST_HTML))
    entries, names = predict.startlist_entries("race/beta/2027", "2027-05-01")
    assert sorted(entries["stage_id"].unique()) == [1000, 1001]
    assert len(entries) == 8
    one = entries[entries["stage_id"] == 1000].set_index("rider_id")
    assert set(one.index) == {1, 2, 30, -1}                  # new-kid is not in the database
    assert one.loc[1, "bib"] == 1 and one.loc[1, "team_slug"] == "alpha-racing-2027"
    assert np.isnan(one.loc[-1, "bib"])
    assert names[-1] == "KID New" and names[1] == "ONE Rider"


def test_startlist_entries_skip_ridden_stages_and_abandons(season_db, monkeypatch):
    _add_upcoming_race(season_db["path"], with_first_stage_result=True)
    calls = []
    monkeypatch.setattr(predict, "fetch_startlist",
                        lambda slug: calls.append(slug) or parse_startlist(STARTLIST_HTML))
    entries, _ = predict.startlist_entries("race/beta/2027", "2027-05-02")
    assert sorted(entries["stage_id"].unique()) == [1001]
    # rider 30 did not finish stage 1; the unknown rider stays (nothing says he left)
    assert set(entries["rider_id"]) == {1, 2, -1}
    # nothing left to predict once every stage has a result: no request is made
    calls.clear()
    with sqlite3.connect(season_db["path"]) as conn:
        conn.execute("INSERT INTO results (stage_id, rider_id, position, status) VALUES (1001, 1, 1, 'finished')")
        conn.commit()
    entries, names = predict.startlist_entries("race/beta/2027", "2027-05-02")
    assert entries.empty and names == {} and calls == []


def test_startlist_entries_unknown_race(season_db, monkeypatch):
    monkeypatch.setattr(predict, "fetch_startlist", lambda slug: (_ for _ in ()).throw(AssertionError("no fetch")))
    entries, names = predict.startlist_entries("race/nowhere/2027", "2027-01-01")
    assert entries.empty and names == {}
