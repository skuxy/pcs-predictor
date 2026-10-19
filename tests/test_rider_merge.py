"""Re-slugged rider detection + merge (db/rider_merge.py, main.py merge-riders).

Synthetic DBs: thomas-pidcock (profile row, early stages) and tom-pidcock
(stub, later stages) are one rider PCS re-slugged; romain-gregoire1 /
romain-gregoire play namesakes that must never be merged on names alone.
"""
import argparse
import json
import sqlite3
from contextlib import contextmanager

import pytest

import main
import scraper.utils
from db import database
from db.database import init_db
from db.rider_merge import (
    alias_map, build_plan, canonical_rows, check_pair, confirm_with_pcs, display_name,
    ensure_rider_merge_schema, find_bib_drift_pairs, find_name_candidates, first_name_relation,
    merge_pair, names_compatible, parse_cached_stage, record_alias, resolve_rider_slug, revalidate_plan,
)


# ── fixtures ─────────────────────────────────────────────────────────────────

def _open(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "merge.db")
    init_db(path)
    with _open(path) as conn:
        ensure_rider_merge_schema(conn)
    return path


@pytest.fixture
def conn(db_path):
    c = _open(db_path)
    yield c
    c.close()


def _race(conn, slug, year=2026, gender="men"):
    conn.execute("INSERT INTO races (pcs_slug, name, year, start_date, end_date, gender) VALUES (?,?,?,?,?,?)",
                 (slug, slug, year, f"{year}-01-01", f"{year}-12-31", gender))
    return conn.execute("SELECT id FROM races WHERE pcs_slug = ?", (slug,)).fetchone()[0]


def _stage(conn, race_id, slug, date, profile="flat"):
    conn.execute("INSERT INTO stages (race_id, pcs_slug, date, profile_type) VALUES (?,?,?,?)",
                 (race_id, slug, date, profile))
    return conn.execute("SELECT id FROM stages WHERE pcs_slug = ?", (slug,)).fetchone()[0]


def _rider(conn, slug, name, **profile):
    cols = ["pcs_slug", "name", *profile]
    conn.execute(f"INSERT INTO riders ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                 [slug, name, *profile.values()])
    return conn.execute("SELECT id FROM riders WHERE pcs_slug = ?", (slug,)).fetchone()[0]


def _res(conn, stage_id, rider_id, pos, bib=None, status="finished", **kw):
    data = {"stage_id": stage_id, "rider_id": rider_id, "position": pos, "status": status, "bib": bib, **kw}
    conn.execute(f"INSERT INTO results ({', '.join(data)}) VALUES ({', '.join('?' * len(data))})",
                 list(data.values()))
    return conn.execute("SELECT MAX(id) FROM results").fetchone()[0]


def _seed_reslug(conn, n_old=5, n_new=4):
    """One race, 9 stages (2026-03-01..09); Pidcock as thomas-pidcock on the
    first ``n_old`` stages, as tom-pidcock on the next ``n_new``; a teammate
    (bib 12) on every stage.  Returns a dict of ids."""
    race = _race(conn, "race/test-tour/2026")
    stages = [_stage(conn, race, f"race/test-tour/2026/stage-{i}", f"2026-03-{i:02d}")
              for i in range(1, n_old + n_new + 1)]
    old = _rider(conn, "thomas-pidcock", "Thomas  Pidcock", dob="1999-07-30",
                 nationality="Great Britain", team="Old Team", weight_kg=58.0, height_cm=170.0,
                 speciality="classics")
    new = _rider(conn, "tom-pidcock", "Pidcock Tom", team="Pinarello")
    mate = _rider(conn, "mate-one", "One Mate")
    for i, sid in enumerate(stages):
        who = old if i < n_old else new
        _res(conn, sid, who, i + 1, bib=11, team_slug="pinarello-2026", points_pcs=10 + i)
        _res(conn, sid, mate, 50 + i, bib=12, team_slug="pinarello-2026")
    conn.commit()
    return {"race": race, "stages": stages, "old": old, "new": new, "mate": mate}


def _count(conn, sql, *params):
    return conn.execute(sql, params).fetchone()[0]


# ── names ────────────────────────────────────────────────────────────────────

def test_first_name_relations():
    assert first_name_relation("tom", "thomas") == "variant"
    assert first_name_relation("romain", "romain") == "same"
    assert first_name_relation("lucas", "lukas") == "variant"
    assert first_name_relation("jared", "jacob") == "initial"
    assert first_name_relation("diana", "romain") is None


def test_names_compatible():
    assert names_compatible("thomas-pidcock", "Thomas  Pidcock", "tom-pidcock", "Pidcock Tom") == (True, "strong")
    assert names_compatible("david-lozano", "David  Lozano", "david-lozano-riba", "Lozano David")[0]
    assert names_compatible("filippo-magli2", "Magli Filippo", "filippo-magli", "MAGLI Filippo") == (True, "strong")
    # particles are not surnames
    ok, why = names_compatible("lucas-van-der-vreken", None, "lukas-van-der-valk", None)
    assert not ok and "surname" in why
    assert names_compatible("jared-scott", None, "jacob-scott", None) == (True, "weak")
    assert not names_compatible("tom-pidcock", None, "romain-gregoire", None)[0]
    # same tokens in another order, or a stored name that already is the new slug's
    assert names_compatible("enkhtaivan-bolor-erdene", "Bolor-erdene Enkhtaivan",
                            "bolor-erdene-enkhtaivan", None) == (True, "strong")
    assert names_compatible("gabriel-muller", "Mehmet Şampiyonbisiklet", "mehmet-sampiyonbisiklet",
                            "Şampiyonbisiklet Mehmet") == (True, "strong")
    # a marriage name is not a name match
    assert not names_compatible("megan-armitage", "Megan Armitage", "megan-monk", "MONK Megan")[0]


def test_display_name_prefers_slug_order():
    assert display_name("Thomas  Pidcock", "Pidcock Tom", "tom-pidcock") == "Tom Pidcock"
    assert display_name("Romain Grégoire", "Grégoire Romain", "romain-gregoire") == "Romain Grégoire"
    assert display_name("Magli Filippo", "MAGLI Filippo", "filippo-magli") == "Filippo Magli"
    assert display_name("David  Lozano", "Lozano David", "david-lozano-riba") == "David Lozano"
    assert display_name("Somebody", None, "new-slug") == "Somebody"
    assert display_name("Bolor-erdene Enkhtaivan", "ENKHTAIVAN Bolor-erdene",
                        "bolor-erdene-enkhtaivan") == "Bolor-erdene Enkhtaivan"
    assert display_name("Megan Armitage", "MONK Megan", "megan-monk") == "Megan Monk"


# ── merge ────────────────────────────────────────────────────────────────────

def test_merge_moves_everything_and_is_idempotent(conn):
    ids = _seed_reslug(conn)
    conn.execute("INSERT INTO upcoming_races (pcs_slug, name) VALUES ('race/next/2026', 'Next')")
    conn.execute("INSERT INTO upcoming_stages (upcoming_race_id, pcs_slug) VALUES (1, 'race/next/2026')")
    conn.execute("INSERT INTO predictions (stage_id, rider_id, top10_prob, model_version) VALUES (1, ?, 0.4, 'v1')",
                 (ids["old"],))
    conn.execute("INSERT INTO bets (stage_id, rider_id, rider_name, odds_decimal) VALUES (?, ?, 'Pidcock', 3.0)",
                 (ids["stages"][0], ids["old"]))
    conn.commit()

    dry = merge_pair(conn, "thomas-pidcock", "tom-pidcock", "test", dry_run=True)
    assert dry["action"] == "merge" and dry["results_moved"] == 5
    assert _count(conn, "SELECT COUNT(*) FROM riders WHERE pcs_slug = 'thomas-pidcock'") == 1
    assert alias_map(conn) == {}

    rep = merge_pair(conn, "thomas-pidcock", "tom-pidcock", "same stage+bib")
    conn.commit()
    assert rep["action"] == "merge" and rep["results_moved"] == 5 and rep["conflicts"] == []
    assert rep["predictions_moved"] == 1 and rep["bets_moved"] == 1
    assert _count(conn, "SELECT COUNT(*) FROM riders WHERE pcs_slug = 'thomas-pidcock'") == 0
    assert _count(conn, "SELECT COUNT(*) FROM results WHERE rider_id = ?", ids["new"]) == 9
    assert _count(conn, "SELECT COUNT(*) FROM (SELECT stage_id, rider_id FROM results "
                        "GROUP BY 1, 2 HAVING COUNT(*) > 1)") == 0
    assert _count(conn, "SELECT rider_id FROM predictions") == ids["new"]
    assert _count(conn, "SELECT rider_id FROM bets") == ids["new"]
    row = conn.execute("SELECT * FROM riders WHERE id = ?", (ids["new"],)).fetchone()
    assert (row["name"], row["dob"], row["nationality"], row["weight_kg"], row["speciality"]) == (
        "Tom Pidcock", "1999-07-30", "Great Britain", 58.0, "classics")
    assert row["team"] == "Pinarello"   # the canonical row's own value wins
    a = conn.execute("SELECT * FROM rider_slug_aliases").fetchone()
    assert (a["old_slug"], a["new_slug"], a["evidence"], a["valid_until"]) == (
        "thomas-pidcock", "tom-pidcock", "same stage+bib", "2026-03-05")

    again = merge_pair(conn, "thomas-pidcock", "tom-pidcock", "same stage+bib")
    assert again["action"] == "already merged" and again["results_moved"] == 0
    assert _count(conn, "SELECT COUNT(*) FROM results") == 18
    assert _count(conn, "SELECT COUNT(*) FROM rider_slug_aliases") == 1


def test_merge_renames_when_new_slug_is_not_stored(conn):
    race = _race(conn, "race/r/2026")
    s = _stage(conn, race, "race/r/2026/stage-1", "2026-04-01")
    old = _rider(conn, "david-lozano", "David  Lozano", dob="1988-12-21")
    _res(conn, s, old, 7, bib=21)
    rep = merge_pair(conn, "david-lozano", "david-lozano-riba", "bib")
    assert rep["action"] == "rename" and rep["results_moved"] == 1
    row = conn.execute("SELECT id, pcs_slug, name, dob FROM riders").fetchone()
    assert tuple(row) == (old, "david-lozano-riba", "David Lozano", "1988-12-21")
    assert resolve_rider_slug(conn, "david-lozano") == "david-lozano-riba"


def test_merge_resolves_same_stage_duplicates(conn):
    ids = _seed_reslug(conn)
    s5, s6 = ids["stages"][4], ids["stages"][5]
    # stage 5: old row (complete) + a sparse new row of the same rider (bib 11)
    sparse = _res(conn, s5, ids["new"], 5, bib=11)
    # stage 6: an equally complete old row inserted later -> the newer one wins
    later_old = _res(conn, s6, ids["old"], 6, bib=11, team_slug="pinarello-2026", points_pcs=15)
    conn.commit()
    assert check_pair(conn, "thomas-pidcock", "tom-pidcock")["ok"]   # same bib = one person
    rep = merge_pair(conn, "thomas-pidcock", "tom-pidcock", "bib")
    kept = {c["stage"]: c["kept"] for c in rep["conflicts"]}
    assert kept == {"race/test-tour/2026/stage-5": "old", "race/test-tour/2026/stage-6": "old"}
    assert _count(conn, "SELECT COUNT(*) FROM results WHERE id = ?", sparse) == 0
    assert _count(conn, "SELECT rider_id FROM results WHERE id = ?", later_old) == ids["new"]
    assert _count(conn, "SELECT COUNT(*) FROM results WHERE rider_id = ?", ids["new"]) == 9
    assert _count(conn, "SELECT COUNT(*) FROM (SELECT stage_id, rider_id FROM results "
                        "GROUP BY 1, 2 HAVING COUNT(*) > 1)") == 0


def test_chains_collapse(conn):
    race = _race(conn, "race/r/2026")
    s = [_stage(conn, race, f"race/r/2026/stage-{i}", f"2026-05-0{i}") for i in (1, 2, 3)]
    a = _rider(conn, "filippo-magli2", "Magli Filippo")
    b = _rider(conn, "filippo-magli1", "Magli Filippo")
    c = _rider(conn, "filippo-magli", "MAGLI Filippo")
    for sid, rid in zip(s, (a, b, c)):
        _res(conn, sid, rid, 10, bib=31)
    conn.commit()
    plan = build_plan(conn, offline_only=True, stage_parser=lambda slug: None, pairs=[
        {"old_slug": "filippo-magli2", "new_slug": "filippo-magli1", "evidence": "bib"},
        {"old_slug": "filippo-magli1", "new_slug": "filippo-magli", "evidence": "bib"}])
    by_old = {p["old_slug"]: p for p in plan["pairs"]}
    assert by_old["filippo-magli2"]["new_slug"] == "filippo-magli"
    assert by_old["filippo-magli2"]["chain"] == ["filippo-magli2", "filippo-magli1", "filippo-magli"]
    # alias table chains collapse too: a -> b recorded first, then b -> c
    merge_pair(conn, "filippo-magli2", "filippo-magli1", "bib")
    merge_pair(conn, "filippo-magli1", "filippo-magli", "bib")
    assert {k: v[0] for k, v in alias_map(conn).items()} == {
        "filippo-magli2": "filippo-magli", "filippo-magli1": "filippo-magli"}
    assert _count(conn, "SELECT COUNT(*) FROM results WHERE rider_id = ?", c) == 3
    assert _count(conn, "SELECT COUNT(*) FROM riders") == 1
    # PCS swapping back: the new canonical slug stops being an alias, no loop
    record_alias(conn, "filippo-magli", "filippo-magli2", "swap back", None)
    assert {k: v[0] for k, v in alias_map(conn).items()} == {
        "filippo-magli1": "filippo-magli2", "filippo-magli": "filippo-magli2"}
    assert resolve_rider_slug(conn, "filippo-magli2") == "filippo-magli2"


def _uci_rows(conn, slug, season, labels):
    conn.executemany("""INSERT INTO rider_uci_results (season, rider_slug, date, result_url, result_label,
                        result_kind, position, status, points_uci) VALUES (?,?,?,?,?,?,?,?,?)""",
                     [(season, slug, f"{season}-03-0{i}", f"race/test-tour-{season}-stage-{i}", label,
                       "stage", i, "finished", 10.0) for i, label in enumerate(labels, 1)])


def test_slug_keyed_tables_move_only_verified_rows(conn):
    ids = _seed_reslug(conn)
    # a season scraped under the old slug only: it moves as a whole
    _uci_rows(conn, "thomas-pidcock", 2026, [f"Stage {i}" for i in range(1, 6)])
    conn.execute("INSERT INTO rider_uci_scrapes (season, rider_slug, rows_seen, scoring_rows, last_date, complete) "
                 "VALUES (2026, 'thomas-pidcock', 9, 5, '2026-03-05', 1)")
    conn.execute("INSERT INTO rider_team_spells (season, rider_slug, team_slug) VALUES (2026, 'thomas-pidcock', 'x')")
    conn.execute("INSERT INTO uci_team_breakdown (season, team_slug, rider_slug) "
                 "VALUES (2026, 'pinarello-2026', 'thomas-pidcock')")
    rep = merge_pair(conn, "thomas-pidcock", "tom-pidcock", "bib")
    st = rep["slug_tables"]
    assert st["moved"] == {"rider_uci_results": 5, "rider_uci_scrapes": 1, "rider_team_spells": 1,
                           "uci_team_breakdown": 1}
    assert st["superseded"] == {}
    assert _count(conn, "SELECT COUNT(*) FROM rider_uci_results WHERE rider_slug = 'tom-pidcock'") == 5
    assert _count(conn, "SELECT COUNT(*) FROM rider_uci_scrapes WHERE rider_slug = 'tom-pidcock'") == 1
    for t in ("rider_uci_results", "rider_uci_scrapes", "rider_team_spells", "uci_team_breakdown"):
        assert _count(conn, f"SELECT COUNT(*) FROM {t} WHERE rider_slug = 'thomas-pidcock'") == 0
    assert ids


def test_slug_keyed_seasons_are_never_combined(conn):
    """Both slugs scraped the same season: two replace-sets, never unioned —
    PCS relabelled one result in between, and a union counted it twice."""
    _seed_reslug(conn)
    _uci_rows(conn, "thomas-pidcock", 2026, [f"Stage {i}" for i in range(1, 7)])
    _uci_rows(conn, "tom-pidcock", 2026, ["Stage 1 (ITT)"] + [f"Stage {i}" for i in range(2, 7)])
    conn.execute("INSERT INTO rider_uci_penalties (season, rider_slug, date, race_name, penalty_points) "
                 "VALUES (2026, 'thomas-pidcock', '2026-03-02', 'Test Tour', 25)")
    # an older season the new slug never had: moved
    _uci_rows(conn, "thomas-pidcock", 2025, ["Stage 1"])
    rep = merge_pair(conn, "thomas-pidcock", "tom-pidcock", "bib")
    st = rep["slug_tables"]
    assert st["superseded"] == {"rider_uci_results": 6, "rider_uci_penalties": 1}
    assert st["moved"] == {"rider_uci_results": 1}
    assert tuple(conn.execute("SELECT COUNT(*), SUM(points_uci) FROM rider_uci_results "
                              "WHERE rider_slug = 'tom-pidcock' AND season = 2026").fetchone()) == (6, 60.0)
    assert _count(conn, "SELECT COUNT(*) FROM rider_uci_results WHERE rider_slug = 'tom-pidcock' "
                        "AND season = 2025") == 1
    assert _count(conn, "SELECT COUNT(*) FROM rider_uci_results WHERE rider_slug = 'thomas-pidcock'") == 0


def test_slug_keyed_rows_of_a_namesake_stay(conn):
    """PCS gave the old slug to another rider: his UCI rows do not match ours."""
    _seed_reslug(conn)
    conn.executemany("""INSERT INTO rider_uci_results (season, rider_slug, date, result_url, result_label,
                        result_kind, position, status, points_uci) VALUES (?,?,?,?,?,?,?,?,?)""",
                     [(2026, "thomas-pidcock", f"2026-07-0{i}", f"race/other-2026-stage-{i}", f"Stage {i}",
                       "stage", 40 + i, "finished", 1.0) for i in range(1, 6)])
    rep = merge_pair(conn, "thomas-pidcock", "tom-pidcock", "bib")
    assert rep["slug_tables"]["left"] == {"rider_uci_results": 5}
    assert "another rider" in rep["slug_tables"]["reason"]
    assert _count(conn, "SELECT COUNT(*) FROM rider_uci_results WHERE rider_slug = 'thomas-pidcock'") == 5


# ── alias lookup ─────────────────────────────────────────────────────────────

def test_resolve_rider_slug_window_and_page_guard(conn):
    assert resolve_rider_slug(conn, "thomas-pidcock") == "thomas-pidcock"
    record_alias(conn, "thomas-pidcock", "tom-pidcock", "bib", "2026-03-23")
    assert resolve_rider_slug(conn, "thomas-pidcock") == "tom-pidcock"
    assert resolve_rider_slug(conn, "thomas-pidcock", fetched_on="2026-03-23") == "tom-pidcock"
    # a page fetched after the switch uses current slugs: the old one is someone else
    assert resolve_rider_slug(conn, "thomas-pidcock", fetched_on="2026-10-18") == "thomas-pidcock"
    # both slugs on one page: two riders
    assert resolve_rider_slug(conn, "thomas-pidcock", page_slugs={"thomas-pidcock", "tom-pidcock"}) \
        == "thomas-pidcock"
    rows = canonical_rows(conn, [{"rider_slug": "thomas-pidcock"}, {"rider_slug": "x"}])
    assert rows[0] == {"rider_slug": "tom-pidcock", "page_slug": "thomas-pidcock", "alias_by": "date"}
    assert rows[1] == {"rider_slug": "x"}


def test_canonical_rows_bib_settles_the_alias(conn):
    """valid_until (old row's last race, 2026-03-05) is no rename date: a page
    fetched later may still show the old slug for him.  In a race he has
    stored bibs in, the bib decides; without one, the page date."""
    ids = _seed_reslug(conn)
    merge_pair(conn, "thomas-pidcock", "tom-pidcock", "bib")
    stage = ids["stages"][-1]   # tom-pidcock wore bib 11 in this race
    late, early = "2026-06-01", "2026-03-04"

    def one(bib, fetched_on, sid=stage):
        return canonical_rows(conn, [{"rider_slug": "thomas-pidcock", "bib": bib},
                                     {"rider_slug": "mate-one", "bib": 12}], fetched_on, sid)[0]
    assert one(11, late) == {"rider_slug": "tom-pidcock", "page_slug": "thomas-pidcock", "bib": 11,
                             "alias_by": "bib"}
    # another bib: whoever PCS gave the slug to — even on a page from before the switch
    assert one(99, early) == {"rider_slug": "thomas-pidcock", "bib": 99, "alias_other_bib": [11]}
    # no bib to compare (team time trial): the page date decides; after it, unresolved
    assert one(None, early)["rider_slug"] == "tom-pidcock" and one(None, early)["alias_by"] == "date"
    assert one(None, late) == {"rider_slug": "thomas-pidcock", "bib": None, "alias_unresolved": "tom-pidcock"}
    # a race without his bib (no stage_id): the page date alone
    assert one(11, late, None)["alias_unresolved"] == "tom-pidcock"
    assert one(11, early, None)["rider_slug"] == "tom-pidcock"


def test_merge_pair_old_slug_gone_records_no_alias(conn):
    """A plan pair whose old slug is not in riders (plan from another DB copy,
    stale or edited): no alias without valid_until, no redirected alias."""
    race = _race(conn, "race/r/2026")
    s = _stage(conn, race, "race/r/2026/stage-3", "2026-03-03")
    rg = _rider(conn, "romain-gregoire", "Romain Grégoire")
    _res(conn, s, rg, 5, bib=11)
    conn.commit()
    rep = merge_pair(conn, "romain-gregoire1", "romain-gregoire", "plan")
    assert rep["action"] == "old slug not in riders" and alias_map(conn) == {}
    plan = {"pairs": [{"old_slug": "romain-gregoire1", "new_slug": "romain-gregoire", "confidence": "confirmed",
                       "evidence_type": ["bib"], "evidence": ["x"]}]}
    keep, dropped = revalidate_plan(conn, plan)
    assert keep == [] and "no alias maps it" in dropped[0]["reason"]
    # an alias to somebody else is never redirected by such a pair
    record_alias(conn, "romain-gregoire1", "other-rider", "bib", "2026-01-01")
    assert merge_pair(conn, "romain-gregoire1", "romain-gregoire", "plan")["action"] == "old slug not in riders"
    assert alias_map(conn) == {"romain-gregoire1": ("other-rider", "2026-01-01")}
    conn.execute("DELETE FROM rider_slug_aliases")
    # the alias the DB already holds: kept as a no-op
    record_alias(conn, "romain-gregoire1", "romain-gregoire", "bib", "2026-04-19")
    keep, dropped = revalidate_plan(conn, plan)
    assert [p["old_slug"] for p in keep] == ["romain-gregoire1"] and dropped == []
    assert merge_pair(conn, "romain-gregoire1", "romain-gregoire", "plan")["action"] == "already merged"
    assert alias_map(conn)["romain-gregoire1"] == ("romain-gregoire", "2026-04-19")


def test_merge_of_a_rider_without_results_records_no_alias(conn):
    old = _rider(conn, "x-old", "X Old")
    _rider(conn, "x-new", "X New")
    conn.execute("INSERT INTO bets (rider_id, rider_name, odds_decimal) VALUES (?, 'X', 2.0)", (old,))
    rep = merge_pair(conn, "x-old", "x-new", "pairs file")
    assert rep["action"] == "merge" and rep["bets_moved"] == 1 and rep["valid_until"] is None
    assert alias_map(conn) == {}


def test_resolve_without_alias_table(tmp_path):
    path = str(tmp_path / "plain.db")
    init_db(path)
    with _open(path) as c:
        assert resolve_rider_slug(c, "anyone") == "anyone"
        assert canonical_rows(c, [{"rider_slug": "anyone"}]) == [{"rider_slug": "anyone"}]


# ── detection ────────────────────────────────────────────────────────────────

def _page_row(slug, bib, pos, name=None):
    return {"rider_slug": slug, "rider_name": name or slug, "bib": bib, "position": pos, "status": "finished"}


def test_bib_drift_pairs_and_contradictions(conn):
    ids = _seed_reslug(conn)
    pages = {f"race/test-tour/2026/stage-{i}": ([_page_row("tom-pidcock", 11, i, "PIDCOCK Tom"),
                                                  _page_row("mate-one", 12, 49 + i)], "url")
             for i in (1, 2)}
    got = find_bib_drift_pairs(conn, stage_parser=lambda slug: pages.get(slug))
    assert list(got["pairs"]) == [("thomas-pidcock", "tom-pidcock")]
    assert got["pairs"][("thomas-pidcock", "tom-pidcock")]["stages"] == [
        "race/test-tour/2026/stage-1", "race/test-tour/2026/stage-2"]
    # a page with both slugs on it contradicts the pair
    pages["race/test-tour/2026/stage-7"] = ([_page_row("tom-pidcock", 11, 7),
                                             _page_row("thomas-pidcock", 99, 80)], "url")
    got = find_bib_drift_pairs(conn, stage_parser=lambda slug: pages.get(slug))
    assert got["pairs"] == {} and got["contradicted"][0]["old_slug"] == "thomas-pidcock"
    assert ids  # noqa


def test_bib_drift_after_a_merge(conn):
    """After the merge, an old cached page still showing the old slug is not
    a pair the other way round (which would rename the merged rider back);
    a later scrape that split him again is a pair."""
    ids = _seed_reslug(conn)
    merge_pair(conn, "thomas-pidcock", "tom-pidcock", "bib")
    old_pages = {f"race/test-tour/2026/stage-{i}": ([_page_row("thomas-pidcock", 11, i, "PIDCOCK Thomas"),
                                                      _page_row("mate-one", 12, 49 + i)], "url") for i in (1, 2)}
    got = find_bib_drift_pairs(conn, stage_parser=old_pages.get)
    assert got["pairs"] == {} and got["contradicted"] == []
    # stage 9 re-scraped from an old page: thomas-pidcock stored again with bib 11
    again = _rider(conn, "thomas-pidcock", "PIDCOCK Thomas")
    conn.execute("UPDATE results SET rider_id = ? WHERE rider_id = ? AND stage_id = ?",
                 (again, ids["new"], ids["stages"][8]))
    fresh = {"race/test-tour/2026/stage-9": ([_page_row("tom-pidcock", 11, 9, "PIDCOCK Tom"),
                                              _page_row("mate-one", 12, 58)], "url")}
    got = find_bib_drift_pairs(conn, stage_parser=fresh.get)
    assert list(got["pairs"]) == [("thomas-pidcock", "tom-pidcock")]


def test_parse_cached_stage_reads_only_the_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(scraper.utils, "CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(scraper.utils, "fetch", lambda *a, **k: pytest.fail("network fetch"))
    (tmp_path / "cache").mkdir()
    html = """<html><body><div class="resultCont" data-navid="1"><table class="results"><thead><tr>
      <th data-code="rnk">Rnk</th><th data-code="bib">BIB</th><th data-code="rider">Rider</th>
      <th data-code="riderteam">Team</th><th data-code="uci_pnt">UCI</th><th data-code="timelag">Time</th>
      </tr></thead><tbody>
      <tr><td>1</td><td>11</td><td><a href="rider/tom-pidcock">PIDCOCK Tom</a></td>
          <td><a href="team/pinarello-2026">Pinarello</a></td><td>100</td><td>4:00:00</td></tr>
      <tr><td>2</td><td>12</td><td><a href="rider/mate-one">MATE One</a></td>
          <td><a href="team/pinarello-2026">Pinarello</a></td><td>50</td><td>0:02</td></tr>
      </tbody></table></div></body></html>"""
    scraper.utils.cache_path(scraper.utils.pcs_url("race/test-tour/2026/stage-1")).write_text(html)
    rows, url = parse_cached_stage("race/test-tour/2026/stage-1")
    assert [(r["rider_slug"], r["bib"], r["position"]) for r in rows] == [
        ("tom-pidcock", 11, 1), ("mate-one", 12, 2)]
    assert parse_cached_stage("race/test-tour/2026/stage-2") is None


def test_name_candidates_need_consecutive_activity(conn):
    _seed_reslug(conn)
    race = _race(conn, "race/other/2026")
    s1 = _stage(conn, race, "race/other/2026/stage-1", "2026-04-01")
    s2 = _stage(conn, race, "race/other/2026/stage-2", "2026-04-02")
    # namesakes on the same stages: never candidates
    g1 = _rider(conn, "romain-gregoire1", "Romain Grégoire")
    g2 = _rider(conn, "romain-gregoire", "GRÉGOIRE Romain")
    for sid in (s1, s2):
        _res(conn, sid, g1, 3, bib=41)
        _res(conn, sid, g2, 90, bib=77)
    conn.commit()
    cands = find_name_candidates(conn)
    assert [(c["old_slug"], c["new_slug"], c["name_strength"]) for c in cands] == [
        ("thomas-pidcock", "tom-pidcock", "strong")]
    assert cands[0]["team"]["same_team"] and cands[0]["team"]["shared_teammates"] == 1
    chk = check_pair(conn, "romain-gregoire1", "romain-gregoire")
    assert not chk["ok"] and "different riders" in chk["reasons"][0]


def _seed_interleaved(conn):
    """andrea-ramirez-fregoso rides Feb and June 2023 (races stored as 'men',
    as the calendar scrape left them); andrea-ramirez, a stale scrape of the
    same rider, rides Itzulia Women in May 2023 — inside the other's span.
    andres-ramirez rides on one of andrea-ramirez-fregoso's days: never a
    candidate with her."""
    feb = _race(conn, "race/setmana-valenciana/2023", 2023, "men")
    may = _race(conn, "race/itzulia-women/2023", 2023, "women")
    jun = _race(conn, "race/vuelta-andalucia-women/2023", 2023, "men")
    st = {}
    for race, slug, days in ((feb, "race/setmana-valenciana/2023", ("02-16", "02-17", "02-18")),
                             (may, "race/itzulia-women/2023", ("05-12", "05-13", "05-14")),
                             (jun, "race/vuelta-andalucia-women/2023", ("06-01", "06-02"))):
        for i, d in enumerate(days, 1):
            st[(slug, i)] = _stage(conn, race, f"{slug}/stage-{i}", f"2023-{d}")
    new = _rider(conn, "andrea-ramirez-fregoso", "Ramirez Andrea")
    old = _rider(conn, "andrea-ramirez", "Ramirez Andrea")
    other = _rider(conn, "andres-ramirez", "Ramirez Andres")
    for i in (1, 2, 3):
        _res(conn, st[("race/setmana-valenciana/2023", i)], new, 40 + i, bib=55)
        _res(conn, st[("race/itzulia-women/2023", i)], old, 80 - i, bib=83)
    for i in (1, 2):
        _res(conn, st[("race/vuelta-andalucia-women/2023", i)], new, 30 + i, bib=61)
    _res(conn, st[("race/setmana-valenciana/2023", 2)], other, 12, bib=7)   # rides on her day
    conn.commit()
    return {"old": old, "new": new, "other": other}


def test_name_candidates_find_interleaved_splits(conn):
    ids = _seed_interleaved(conn)
    cands = {(c["old_slug"], c["new_slug"]): c for c in find_name_candidates(conn)}
    c = cands[("andrea-ramirez", "andrea-ramirez-fregoso")]
    assert c["interleaved"] and c["gender_differs"] and c["name_strength"] == "strong"
    assert c["gap_days"] < 0
    # andres-ramirez rode 2023-02-17 (Setmana) like andrea-ramirez-fregoso: never a candidate with her
    assert not any("andres-ramirez" in k and "andrea-ramirez-fregoso" in k for k in cands)
    assert check_pair(conn, "andrea-ramirez", "andrea-ramirez-fregoso")["ok"]
    # a day in two different races is two riders, whatever the names say
    _res(conn, conn.execute("SELECT id FROM stages WHERE pcs_slug = 'race/vuelta-andalucia-women/2023/stage-1'"
                            ).fetchone()[0], ids["old"], 50, bib=83)
    chk = check_pair(conn, "andrea-ramirez", "andrea-ramirez-fregoso")
    assert not chk["ok"] and "different riders" in chk["reasons"][0]   # same stage, different bib
    conn.execute("UPDATE stages SET date = '2023-02-16' WHERE pcs_slug = 'race/itzulia-women/2023/stage-1'")
    assert "1 day(s) in different races" in check_pair(conn, "andrea-ramirez", "andrea-ramirez-fregoso")[
        "reasons"][-1]


def test_interleaved_split_confirmed_on_the_new_slugs_page(conn):
    _seed_interleaved(conn)
    # rider.php?id=204333&p=results&xseason=2023: both rows' results, by race
    every = conn.execute(
        """SELECT s.date, x.position, s.pcs_slug FROM results x JOIN stages s ON s.id = x.stage_id
           JOIN riders rd ON rd.id = x.rider_id WHERE rd.pcs_slug LIKE 'andrea-ramirez%'
           ORDER BY s.date DESC""").fetchall()
    rows = "".join(
        f'<tr><td>{i}</td><td>{d}</td><td>{pos}</td><td><a href="{slug.replace('/2023/', '-2023-')}">'
        f'Race | Stage {slug[-1]}</a></td><td>2.1</td><td>100</td><td>-</td><td>-</td><td>0</td></tr>'
        for i, (d, pos, slug) in enumerate(every, 1))
    pages = {"rider/andrea-ramirez-fregoso/results":
             _HEAD.format(name="Andrea Ramirez", pid=204333) + rows + "</tbody></table>"}
    plan = build_plan(conn, stage_parser=lambda slug: None,
                      fetcher=lambda url: pages.get(url.split(".com/", 1)[1]))
    (p,) = [p for p in plan["pairs"] if p["old_slug"] == "andrea-ramirez"]
    assert (p["new_slug"], p["evidence_type"], p["rows_to_move"], p["action"]) == (
        "andrea-ramirez-fregoso", ["name+pcs"], 3, "merge")
    assert p["pcs_match"]["strict"] == 3


# fake PCS rider results pages (markup of rider/<slug>/results, Oct 2026)
_HEAD = """<h1>{name}</h1><form action="rider.php"><input type="hidden" name="id" value="{pid}"></form>
<table class="basic"><thead><tr><th>#</th><th>Date</th><th>Result</th><th>Race</th><th>Class</th>
<th>KMs</th><th>PCS points</th><th>UCI points</th><th>Vert. mtr</th></tr></thead><tbody>"""


def _rider_page(name, pid, results):
    rows = "".join(
        f'<tr><td>{i}</td><td>{d}</td><td>{pos}</td><td><a href="race/test-tour-2026-stage-{st}">'
        f'Test Tour | Stage {st}</a></td><td>2.UWT</td><td>150</td><td>-</td><td>-</td><td>0</td></tr>'
        for i, (d, pos, st) in enumerate(results, 1))
    return _HEAD.format(name=name, pid=pid) + rows + "</tbody></table>"


def _stage_rows(conn, slug):
    return [(r[0], r[1], int(r[2].rsplit("-", 1)[1])) for r in conn.execute(
        """SELECT s.date, x.position, s.pcs_slug FROM results x JOIN stages s ON s.id = x.stage_id
           JOIN riders rd ON rd.id = x.rider_id WHERE rd.pcs_slug = ? ORDER BY s.date DESC""", (slug,))]


def test_confirm_with_pcs_uses_only_the_new_slugs_page(conn):
    ids = _seed_reslug(conn)
    every = _stage_rows(conn, "tom-pidcock") + _stage_rows(conn, "thomas-pidcock")
    pages = {"rider/tom-pidcock/results": _rider_page("Tom Pidcock", 202331, every)}
    fetched = []

    def fetcher(url):
        fetched.append(url)
        return pages.get(url.split(".com/", 1)[1])
    res = confirm_with_pcs(conn, ids["old"], ids["new"], "tom-pidcock", fetcher)
    assert res["confirmed"] and res["matched"] == 5 and res["rate"] == 1.0 and res["pcs_id"] == "202331"
    assert res["new_rows_rate"] == 1.0
    assert all("thomas-pidcock" not in u for u in fetched)


def test_failed_confirmation_is_reported_not_merged(conn, db_path):
    ids = _seed_reslug(conn)
    # the new slug's page lists only its own results: a namesake, not a re-slug
    pages = {"rider/tom-pidcock/results": _rider_page("Tom Pidcock", 202331, _stage_rows(conn, "tom-pidcock"))}
    plan = build_plan(conn, stage_parser=lambda slug: None,
                      fetcher=lambda url: pages.get(url.split(".com/", 1)[1]))
    assert plan["pairs"] == []
    assert plan["unconfirmed"][0]["old_slug"] == "thomas-pidcock"
    assert "0/5" in plan["unconfirmed"][0]["reason"]
    assert _count(conn, "SELECT COUNT(*) FROM riders WHERE pcs_slug = 'thomas-pidcock'") == 1
    assert ids


def test_confirmed_name_pair_is_planned(conn):
    _seed_reslug(conn)
    every = _stage_rows(conn, "tom-pidcock") + _stage_rows(conn, "thomas-pidcock")
    pages = {"rider/tom-pidcock/results": _rider_page("Tom Pidcock", 202331, every)}
    plan = build_plan(conn, stage_parser=lambda slug: None,
                      fetcher=lambda url: pages.get(url.split(".com/", 1)[1]))
    (p,) = plan["pairs"]
    assert (p["old_slug"], p["new_slug"], p["evidence_type"], p["rows_to_move"], p["action"]) == (
        "thomas-pidcock", "tom-pidcock", ["name+pcs"], 5, "merge")
    assert plan["stats"]["pcs_confirmations"] == 1
    assert _count(conn, "SELECT COUNT(*) FROM riders WHERE pcs_slug = 'thomas-pidcock'") == 1  # plan only


def test_bib_pair_with_a_new_surname_needs_the_new_slugs_page(conn):
    """Marriage name: bib evidence alone is not enough, the new slug's PCS
    page must list the old row's results too."""
    race = _race(conn, "race/test-tour/2026", gender="women")
    stages = [_stage(conn, race, f"race/test-tour/2026/stage-{i}", f"2026-03-0{i}") for i in (1, 2, 3)]
    old = _rider(conn, "megan-armitage", "Megan Armitage")
    for i, sid in enumerate(stages, 1):
        _res(conn, sid, old, 10 + i, bib=21)
    conn.commit()
    pages = {f"race/test-tour/2026/stage-{i}": ([_page_row("megan-monk", 21, 10 + i, "MONK Megan")], "url")
             for i in (1, 2)}
    parser = pages.get

    offline = build_plan(conn, offline_only=True, stage_parser=parser)
    assert offline["pairs"] == [] and "needs PCS confirmation" in offline["unconfirmed"][0]["reason"]

    good = {"rider/megan-monk/results": _rider_page("Megan Monk", 999, _stage_rows(conn, "megan-armitage"))}
    plan = build_plan(conn, stage_parser=parser, fetcher=lambda url: good.get(url.split(".com/", 1)[1]))
    (p,) = plan["pairs"]
    assert (p["old_slug"], p["new_slug"], p["evidence_type"], p["action"], p["rows_to_move"]) == (
        "megan-armitage", "megan-monk", ["bib+pcs"], "rename", 3)
    assert p["kept_name"] == "Megan Monk"   # PCS's current name, not the stored maiden name

    other = {"rider/megan-monk/results": _rider_page("Megan Monk", 999, [("2026-03-01", 70, 1)])}
    plan = build_plan(conn, stage_parser=parser, fetcher=lambda url: other.get(url.split(".com/", 1)[1]))
    assert plan["pairs"] == [] and plan["rejected"][0]["old_slug"] == "megan-armitage"
    assert "PCS: only 0/3" in plan["rejected"][0]["reason"]

    merge_pair(conn, p["old_slug"], p["new_slug"], "bib+pcs", new_name=p["new_name_hint"])
    assert tuple(conn.execute("SELECT pcs_slug, name FROM riders WHERE id = ?", (old,)).fetchone()) == (
        "megan-monk", "Megan Monk")


def test_pairs_file_namesake_rejected(conn, tmp_path):
    race = _race(conn, "race/r/2026")
    s = _stage(conn, race, "race/r/2026/stage-1", "2026-04-01")
    g1 = _rider(conn, "romain-gregoire1", "Romain Grégoire")
    g2 = _rider(conn, "romain-gregoire", "Grégoire Romain")
    _res(conn, s, g1, 3, bib=41)
    _res(conn, s, g2, 90, bib=77)
    f = tmp_path / "pairs.json"
    f.write_text(json.dumps([{"old_slug": "romain-gregoire1", "new_slug": "romain-gregoire",
                              "evidence": "same stage+bib"}]))
    plan = build_plan(conn, offline_only=True, stage_parser=lambda slug: None,
                      pairs=main.load_pairs_file(str(f)))
    assert plan["pairs"] == [] and plan["rejected"][0]["evidence_type"] == "pairs-file"
    assert "different riders" in plan["rejected"][0]["reason"]


# ── main.py: scrape paths use the alias table ────────────────────────────────

@pytest.fixture
def cli(db_path, monkeypatch):
    @contextmanager
    def conn_cm(path=db_path):
        with database.get_conn(db_path) as c:
            yield c

    monkeypatch.setattr(main, "get_conn", conn_cm)
    monkeypatch.setattr(main, "init_db", lambda *a, **k: init_db(db_path))
    monkeypatch.setattr(main, "fetch", lambda url, use_cache=True: "<html><body>page</body></html>")
    monkeypatch.setattr(main, "find_race_alias", lambda *a, **k: None)
    monkeypatch.setattr(main, "_ALIASES", {})
    monkeypatch.setattr(main, "fetch_stage_meta", lambda slug: {})
    monkeypatch.setattr(main, "page_fetched_on", lambda slug: None)
    return db_path


def _merged_db(path):
    with _open(path) as c:
        ids = _seed_reslug(c)
        merge_pair(c, "thomas-pidcock", "tom-pidcock", "bib")
        race = c.execute("SELECT id FROM races").fetchone()[0]
        c.execute("INSERT INTO stages (race_id, pcs_slug, date) VALUES (?, 'race/test-tour/2026/stage-10', "
                  "'2026-03-10')", (race,))
        c.commit()
    return ids


def _old_page(slug):
    return [{"rider_slug": "thomas-pidcock", "rider_name": "PIDCOCK Thomas", "position": 1, "status": "finished",
             "time_seconds": 0, "points_pcs": 20, "points_uci": 80025, "uci_penalty": None, "bib": 11,
             "team_slug": "pinarello-2026", "team_name": "Pinarello"},
            {"rider_slug": "mate-one", "rider_name": "ONE Mate", "position": 2, "status": "finished",
             "time_seconds": 0, "points_pcs": 5, "points_uci": 5, "uci_penalty": None, "bib": 12,
             "team_slug": "pinarello-2026", "team_name": "Pinarello"}]


def test_scrape_path_maps_old_slug_to_merged_rider(cli, monkeypatch):
    ids = _merged_db(cli)
    stage_list = [{"pcs_slug": "race/test-tour/2026/stage-10", "stage_num": 10, "date": "2026-03-10",
                   "distance_km": None, "elevation_m": None, "profile_type": "flat", "surface": "road",
                   "departure": None, "arrival": None, "gpx_path": None}]
    monkeypatch.setattr(main, "fetch_race_stages", lambda slug: stage_list)
    monkeypatch.setattr(main, "fetch_stage_results", _old_page)
    profiles = []
    monkeypatch.setattr(main, "fetch_rider", lambda slug: profiles.append(slug) or None)
    main._scrape_race_results(ids["race"], "race/test-tour/2026", skip_riders=False)
    with _open(cli) as c:
        assert _count(c, "SELECT COUNT(*) FROM riders WHERE pcs_slug = 'thomas-pidcock'") == 0
        assert _count(c, """SELECT x.rider_id FROM results x JOIN stages s ON s.id = x.stage_id
                            WHERE s.pcs_slug = 'race/test-tour/2026/stage-10' AND x.position = 1""") == ids["new"]
    assert "thomas-pidcock" not in profiles   # the old slug's PCS page is never used


def test_scrape_path_leaves_old_slug_on_fresh_pages(cli, monkeypatch):
    """A page fetched after the switch, the old slug with another bib than
    the merged rider wore in this race: whoever PCS gave the slug to."""
    ids = _merged_db(cli)
    monkeypatch.setattr(main, "page_fetched_on", lambda slug: "2026-10-18")
    monkeypatch.setattr(main, "fetch_race_stages", lambda slug: [])
    monkeypatch.setattr(main, "fetch_stage_results",
                        lambda slug: [dict(r, bib=99) if r["bib"] == 11 else r for r in _old_page(slug)])
    main._scrape_race_results(ids["race"], "race/test-tour/2026", skip_riders=True)
    with _open(cli) as c:
        assert _count(c, "SELECT COUNT(*) FROM riders WHERE pcs_slug = 'thomas-pidcock'") == 1


def test_scrape_path_maps_old_slug_by_bib_after_the_window(cli, monkeypatch):
    """Fetched after valid_until but before PCS's rename: his bib in this race says it is him."""
    ids = _merged_db(cli)
    monkeypatch.setattr(main, "page_fetched_on", lambda slug: "2026-06-01")
    monkeypatch.setattr(main, "fetch_race_stages", lambda slug: [])
    monkeypatch.setattr(main, "fetch_stage_results", _old_page)
    main._scrape_race_results(ids["race"], "race/test-tour/2026", skip_riders=True)
    with _open(cli) as c:
        assert _count(c, "SELECT COUNT(*) FROM riders WHERE pcs_slug = 'thomas-pidcock'") == 0
        assert _count(c, """SELECT x.rider_id FROM results x JOIN stages s ON s.id = x.stage_id
                            WHERE s.pcs_slug = 'race/test-tour/2026/stage-10' AND x.position = 1""") == ids["new"]


def _marriage_db(path):
    """megan-armitage (1st, bib 11) renamed megan-monk on a 10-rider stage."""
    with _open(path) as c:
        race = _race(c, "race/t/2024", year=2024, gender="women")
        sid = _stage(c, race, "race/t/2024/stage-1", "2024-05-01")
        _res(c, sid, _rider(c, "megan-armitage", "Megan Armitage"), 1, bib=11)
        for i in range(2, 12):
            _res(c, sid, _rider(c, f"rider-{i}", f"Rider {i}"), i, bib=10 + i)
        c.commit()
        merge_pair(c, "megan-armitage", "megan-monk", "bib+pcs", new_name="MONK Megan")
        c.commit()
    return sid


def _marriage_page(bib):
    return [{"rider_slug": "megan-armitage", "rider_name": "ARMITAGE Megan", "position": 1, "status": "finished",
             "time_seconds": 0, "points_pcs": None, "points_uci": None, "uci_penalty": None, "bib": bib}] + \
           [{"rider_slug": f"rider-{i}", "rider_name": f"R {i}", "position": i, "status": "finished",
             "time_seconds": 0, "points_pcs": None, "points_uci": None, "uci_penalty": None, "bib": 10 + i}
            for i in range(2, 12)]


def test_refresh_never_splits_or_prunes_a_merged_rider(cli, monkeypatch):
    """A page fetched after valid_until (2024-05-01) but before PCS renamed
    her still lists megan-armitage: by bib it is her; without a bib the row
    is held — neither a new megan-armitage rider nor megan-monk's row pruned."""
    sid = _marriage_db(cli)
    monkeypatch.setattr(main, "page_fetched_on", lambda slug: "2025-03-01")
    rep = main._sync_stage_results(sid, _marriage_page(11), fetch_slug="race/t/2024/stage-1")
    assert (rep["inserted"], rep["pruned"], rep["held"]) == ([], [], {})
    assert "megan-monk" in rep["matched"]
    rep = main._sync_stage_results(sid, _marriage_page(None), fetch_slug="race/t/2024/stage-1")
    assert (rep["inserted"], rep["pruned"], rep["held"]) == ([], [], {"megan-armitage": "megan-monk"})
    assert rep["kept_unmatched"] == ["megan-monk"]
    with _open(cli) as c:
        assert [r[0] for r in c.execute("SELECT pcs_slug FROM riders WHERE pcs_slug LIKE 'megan%'")] == ["megan-monk"]
        assert _count(c, """SELECT rd.pcs_slug FROM results x JOIN riders rd ON rd.id = x.rider_id
                            WHERE x.stage_id = ? AND x.position = 1""", sid) == "megan-monk"


def test_repair_and_refresh_match_old_slug_rows(cli, monkeypatch):
    ids = _merged_db(cli)
    with _open(cli) as c:   # corrupt stage-1 points of the merged rider
        c.execute("UPDATE results SET points_uci = 80025 WHERE rider_id = ? AND stage_id = ?",
                  (ids["new"], ids["stages"][0]))
    page = [dict(r, points_uci=800 if r["rider_slug"] == "thomas-pidcock" else 5) for r in _old_page(None)]
    monkeypatch.setattr(main, "fetch_stage_results", lambda slug: page)
    main.cmd_repair_points(argparse.Namespace(year=2026, gender=None, races=None, dry_run=False))
    with _open(cli) as c:
        assert _count(c, "SELECT points_uci FROM results WHERE rider_id = ? AND stage_id = ?",
                      ids["new"], ids["stages"][0]) == 800
    rep = main._sync_stage_results(ids["stages"][1], page, fetch_slug="race/test-tour/2026/stage-2")
    assert rep["inserted"] == [] and "tom-pidcock" in rep["matched"]
    with _open(cli) as c:
        assert _count(c, "SELECT COUNT(*) FROM riders WHERE pcs_slug = 'thomas-pidcock'") == 0


def test_merge_riders_cli_dry_run_then_apply(cli, tmp_path):
    with _open(cli) as c:
        ids = _seed_reslug(c)
    pairs = tmp_path / "pairs.json"
    pairs.write_text(json.dumps([{"old_slug": "thomas-pidcock", "new_slug": "tom-pidcock",
                                  "evidence": "same stage+bib on race/x/2023"}]))
    out = tmp_path / "plan.json"
    main.main(["merge-riders", "--offline-only", "--dry-run", "--pairs", str(pairs), "--plan-out", str(out)])
    plan = json.loads(out.read_text())
    assert [(p["old_slug"], p["new_slug"], p["evidence_type"], p["rows_to_move"]) for p in plan["pairs"]] == [
        ("thomas-pidcock", "tom-pidcock", ["pairs-file"], 5)]
    assert plan["dry_run"] is True and "applied" not in plan
    with _open(cli) as c:
        assert _count(c, "SELECT COUNT(*) FROM riders WHERE pcs_slug = 'thomas-pidcock'") == 1

    main.main(["merge-riders", "--offline-only", "--pairs", str(pairs), "--plan-out", str(out)])
    plan = json.loads(out.read_text())
    assert plan["applied"][0]["action"] == "merge" and plan["applied"][0]["results_moved"] == 5
    with _open(cli) as c:
        assert _count(c, "SELECT COUNT(*) FROM riders WHERE pcs_slug = 'thomas-pidcock'") == 0
        assert _count(c, "SELECT COUNT(*) FROM results WHERE rider_id = ?", ids["new"]) == 9
    # re-run: nothing left to do
    main.main(["merge-riders", "--offline-only", "--pairs", str(pairs), "--plan-out", str(out)])
    plan = json.loads(out.read_text())
    assert plan["pairs"] == [] and "not in riders" in plan["rejected"][0]["reason"]


def test_merge_riders_cli_applies_a_reviewed_plan(cli, tmp_path):
    with _open(cli) as c:
        ids = _seed_reslug(c)
        g1 = _rider(c, "romain-gregoire1", "Romain Grégoire")
        g2 = _rider(c, "romain-gregoire", "Grégoire Romain")
        _res(c, ids["stages"][0], g1, 30, bib=41)
        c.commit()
    pairs = tmp_path / "pairs.json"
    pairs.write_text(json.dumps([{"old_slug": "thomas-pidcock", "new_slug": "tom-pidcock", "evidence": "bib"},
                                 {"old_slug": "romain-gregoire1", "new_slug": "romain-gregoire", "evidence": "bib"}]))
    reviewed = tmp_path / "reviewed.json"
    main.main(["merge-riders", "--offline-only", "--dry-run", "--pairs", str(pairs), "--plan-out", str(reviewed)])
    assert [p["old_slug"] for p in json.loads(reviewed.read_text())["pairs"]] == [
        "romain-gregoire1", "thomas-pidcock"]
    # after the review the "new" Grégoire shows up on the old one's stage with
    # another bib: two riders after all — the reviewed pair is dropped
    with _open(cli) as c:
        _res(c, ids["stages"][0], g2, 77, bib=99)
    out = tmp_path / "applied.json"
    main.main(["merge-riders", "--from-plan", str(reviewed), "--plan-out", str(out)])
    applied = json.loads(out.read_text())
    assert [r["old_slug"] for r in applied["applied"]] == ["thomas-pidcock"]
    assert applied["dropped_at_apply"][0]["old_slug"] == "romain-gregoire1"
    with _open(cli) as c:
        assert _count(c, "SELECT COUNT(*) FROM riders WHERE pcs_slug IN ('thomas-pidcock')") == 0
        assert _count(c, "SELECT COUNT(*) FROM riders WHERE pcs_slug LIKE 'romain-gregoire%'") == 2
