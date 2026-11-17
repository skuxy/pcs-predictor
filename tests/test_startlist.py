"""Start-list parsing and the start-list → feature-row path of predict_race."""
import sqlite3

import numpy as np

import model.predict as predict
from scraper import utils as predict_utils
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


def _route_fetcher(pages, seen):
    def fake(slug):
        seen.append((slug, predict_utils._stale_through))
        return dict(pages.get(slug, {}))
    return fake


def test_open_stages_get_their_route_before_predicting(season_db, monkeypatch):
    """The calendar scrape stores no profile score, gradient or elevation for a
    stage still to come; they are fetched (fresh) when the race is predicted."""
    _add_upcoming_race(season_db["path"])
    with sqlite3.connect(season_db["path"]) as conn:
        conn.execute("UPDATE stages SET distance_km = 0 WHERE id = 1001")
        conn.commit()
    seen = []
    pages = {
        "race/beta/2027/stage-1": {"date": "2027-06-30", "distance_km": 151.2, "elevation_m": 900, "profile_type": "flat",
                                   "gradient_final_km": 0.0, "profile_score": 0, "departure": "A", "arrival": "B"},
        "race/beta/2027/stage-2": {"distance_km": 0, "elevation_m": None, "profile_type": None,
                                   "gradient_final_km": None, "profile_score": None},   # PCS has nothing yet
    }
    monkeypatch.setattr(predict, "fetch_stage_meta", _route_fetcher(pages, seen))
    assert predict.refresh_open_stage_routes([1000, 1001]) == 1
    # each page asked for inside pages_newer_than(stage date): a pre-race copy in the cache is not used
    assert seen == [("race/beta/2027/stage-1", "2027-05-01"), ("race/beta/2027/stage-2", "2027-05-02")]
    with sqlite3.connect(season_db["path"]) as conn:
        one = conn.execute("SELECT date, distance_km, elevation_m, gradient_final_km, profile_score, arrival "
                           "FROM stages WHERE id = 1000").fetchone()
        two = conn.execute("SELECT distance_km, profile_score, profile_type FROM stages WHERE id = 1001").fetchone()
    assert one == ("2027-05-01", 151.2, 900, 0.0, 0, "B")       # a zero score / gradient is a fact; the date is kept
    assert two == (0, None, "hilly")                              # nothing invented, the icon's profile stays

    # complete stages are not asked for again
    seen.clear()
    assert predict.refresh_open_stage_routes([1000]) == 0 and seen == []
    assert predict.refresh_open_stage_routes([]) == 0


def test_route_refresh_reads_the_result_page_of_a_one_day_race_and_survives_errors(season_db, monkeypatch):
    with sqlite3.connect(season_db["path"]) as conn:
        conn.execute("INSERT INTO races (id, pcs_slug, name, year, start_date, end_date, class, is_stage_race, gender) "
                     "VALUES (101, 'race/alpha/2027', 'Alpha', 2027, '2027-03-10', '2027-03-10', '1.Pro', 0, 'men')")
        conn.execute("INSERT INTO stages (id, race_id, pcs_slug, date, distance_km, profile_type, surface) "
                     "VALUES (1010, 101, 'race/alpha/2027', '2027-03-10', 150, 'flat', 'road')")
        conn.commit()
    seen = []
    pages = {"race/alpha/2027": {"distance_km": 150.0},
             "race/alpha/2027/result": {"profile_score": 17, "gradient_final_km": 0.4, "elevation_m": 1100}}
    monkeypatch.setattr(predict, "fetch_stage_meta", _route_fetcher(pages, seen))
    assert predict.refresh_open_stage_routes([1010]) == 1
    assert [slug for slug, _ in seen] == ["race/alpha/2027", "race/alpha/2027/result"]
    with sqlite3.connect(season_db["path"]) as conn:
        assert conn.execute("SELECT profile_score, gradient_final_km, elevation_m FROM stages WHERE id = 1010"
                            ).fetchone() == (17, 0.4, 1100)
        conn.execute("UPDATE stages SET profile_score = NULL WHERE id = 1010")
        conn.commit()

    def broken(slug):
        raise RuntimeError("PCS is down")
    monkeypatch.setattr(predict, "fetch_stage_meta", broken)
    assert predict.refresh_open_stage_routes([1010]) == 0        # logged, not raised


def test_route_refresh_keeps_a_time_trial_label_and_an_unread_default(season_db, monkeypatch):
    """Before a stage its page has no "Won how" and shows a road icon: a
    stored time-trial label (from the stage's name) must survive the refresh.
    And the stage-list default ('mountain', no score, no distance) does not
    get a distance without the icon that says what the stage is."""
    _add_upcoming_race(season_db["path"])
    with sqlite3.connect(season_db["path"]) as conn:
        conn.execute("UPDATE stages SET profile_type = 'itt', distance_km = 0 WHERE id = 1000")
        conn.execute("UPDATE stages SET profile_type = 'mountain', distance_km = 0, profile_score = NULL WHERE id = 1001")
        conn.commit()
    pages = {
        "race/beta/2027/stage-1": {"distance_km": 12.4, "profile_type": "flat", "profile_score": 3,
                                   "gradient_final_km": 0.2, "elevation_m": 60},
        "race/beta/2027/stage-2": {"distance_km": 163.0, "profile_type": None, "profile_score": None},
    }
    monkeypatch.setattr(predict, "fetch_stage_meta", _route_fetcher(pages, []))
    assert predict.refresh_open_stage_routes([1000, 1001]) == 1
    with sqlite3.connect(season_db["path"]) as conn:
        assert conn.execute("SELECT profile_type, distance_km, profile_score FROM stages WHERE id = 1000"
                            ).fetchone() == ("itt", 12.4, 3)
        assert conn.execute("SELECT profile_type, distance_km FROM stages WHERE id = 1001").fetchone() == ("mountain", 0)

    # with its icon the default label is replaced, distance and all
    pages["race/beta/2027/stage-2"] = {"distance_km": 163.0, "profile_type": "hilly"}
    assert predict.refresh_open_stage_routes([1001]) == 1
    with sqlite3.connect(season_db["path"]) as conn:
        assert conn.execute("SELECT profile_type, distance_km FROM stages WHERE id = 1001").fetchone() == ("hilly", 163.0)


def test_route_refresh_does_not_fail_on_a_locked_database(season_db, monkeypatch):
    _add_upcoming_race(season_db["path"])
    with sqlite3.connect(season_db["path"]) as conn:
        conn.execute("UPDATE stages SET profile_score = NULL WHERE id = 1000")
        conn.commit()
    monkeypatch.setattr(predict, "fetch_stage_meta", _route_fetcher({"race/beta/2027/stage-1": {"profile_score": 40}}, []))

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(predict, "fill_stage_meta", locked)
    assert predict.refresh_open_stage_routes([1000]) == 0          # logged, the prediction goes ahead
