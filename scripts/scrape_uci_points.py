#!/usr/bin/env python3
"""Scrape the official UCI team ranking and every contributing rider's dated UCI points.

    python scripts/scrape_uci_points.py --season 2026 --teams top30
    python scripts/scrape_uci_points.py --season 2026 --teams all --db /tmp/copy.db \\
        --report /tmp/uci_reconciliation.json

Steps (every page goes through scraper.utils.fetch: HTML cache + rate limit, so
a re-run only fetches what is not cached yet; every write is an upsert or a
per-rider replace, so re-runs are idempotent):

1. rankings/me/ranking-<cycle>               -> uci_team_totals
2. team/<slug>/results/ranking-<cycle>       -> uci_team_breakdown      (per team)
   team/<slug>/results/latest-uci-points     -> uci_team_recent_points  (reconciliation aid)
3. per rider with points, biggest first:
   rider/<slug>/results (season rows)        -> rider_uci_results
   rider.php?...&s=uci-world-ranking&date=D  -> points of every row, plus the
       GC leader-jersey points and previous-year items the results page lacks
       (D = the official listing's date; the page covers the 52 weeks to D)
4. the UCI season start (late October of the previous year) is calibrated
   from those pages against the breakdown totals (or given: --season-start)
5. per rider: merge and store; then
   rider/<slug>/info/fines-penalties         -> rider_uci_penalties (riders with sanctions)
   rider/<slug> profile, only when needed    -> rider_team_spells with transfer dates
       (rider in two teams' breakdowns, or his points do not reconcile with the
       breakdown — e.g. he scored for a team outside the ranking before moving)
6. per-rider and per-team reconciliation report (log + optional JSON).

A page that fails to fetch is logged and skipped; the run carries on and exits 2.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

log = logging.getLogger("scrape_uci_points")

TOLERANCE = 0.5  # points; PCS rounds fractional team-time-trial shares to 2 decimals


# ---------------------------------------------------------------------------
# spells
# ---------------------------------------------------------------------------

def _note_date(note: str, season: int):
    """The date in a team-history note (' as from 01/08', ' until 31/07', '2026-08-01')."""
    note = note or ""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", note)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?", note)
    if m:
        yr = int(m.group(3)) if m.group(3) else season
        yr = yr + 2000 if yr < 100 else yr
        try:
            return date(yr, int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    return None


def _team_base(slug: str) -> str:
    """'bahrain-victorious-2026' -> 'bahrain-victorious' (the franchise across seasons)."""
    return re.sub(r"-\d{4}$", "", slug or "")


def _season_spells(history: list[dict], season: int, fallback_teams: list[dict]) -> list[dict]:
    entries = [h for h in history if h.get("season") == season]
    if not entries:
        return [{"team_slug": t["team_slug"], "team_name": t.get("team_name"),
                 "start_date": None, "end_date": None} for t in fallback_teams]
    if len(entries) == 1:
        e = entries[0]
        return [{"team_slug": e["team_slug"], "team_name": e["team_name"],
                 "start_date": None, "end_date": None}]
    spells = []
    for e in entries:
        note = (e.get("note") or "").lower()
        d = _note_date(note, season)
        start = end = None
        if d is not None:
            if "until" in note or "till" in note:
                end = d
            else:
                start = d
        spells.append({"team_slug": e["team_slug"], "team_name": e["team_name"],
                       "start_date": start, "end_date": end})
    spells.sort(key=lambda s: s["start_date"] or s["end_date"] or date(season, 1, 1))
    for a, b in zip(spells, spells[1:]):
        if a["end_date"] is None and b["start_date"] is not None:
            a["end_date"] = b["start_date"] - timedelta(days=1)
        if b["start_date"] is None and a["end_date"] is not None:
            b["start_date"] = a["end_date"] + timedelta(days=1)
    for s in spells:
        s["start_date"] = s["start_date"].isoformat() if s["start_date"] else None
        s["end_date"] = s["end_date"].isoformat() if s["end_date"] else None
    return spells


def spells_from_history(history: list[dict], season: int, fallback_teams: list[dict],
                        season_start: str | None = None) -> list[dict]:
    """rider_team_spells rows for ``season`` from the profile's team history.

    PCS marks a mid-season move on the profile (``ul.rdr-teams2``) with two
    entries for the season: ``XDS Astana Team (WT) as from 01/08`` and
    ``Netcompany INEOS (WT) until 31/07``.  One team -> one open spell (NULL
    dates).  Several -> 'until' closes a spell, 'as from' opens one, and the
    neighbouring bound is filled in (until 31/07 <-> from 01/08).  Without any
    history entry for the season: one open spell per breakdown team.

    When the UCI season starts in the previous October (``season_start``) and
    the rider changed teams over the winter, the late-previous-year points
    belong to his old team (Chih-Hao Tu: Taiwanese championships of November
    2025 count for Bahrain - Victorious, his 2026 points for Astemo Utsunomiya
    Blitzen).  The old team (his last entry of ``season - 1``) is mapped to the
    breakdown team of the same franchise (slug without the year) — or to the
    one breakdown team no current-season spell covers (a renamed franchise) —
    and gets a spell from ``season_start`` to 31 December.
    """
    spells = _season_spells(history, season, fallback_teams)
    jan1 = f"{season}-01-01"
    prev = [h for h in history if h.get("season") == season - 1]
    if not season_start or season_start >= jan1 or not prev or not spells:
        return spells
    last = next((h for h in prev if "from" in (h.get("note") or "").lower()), prev[0])
    mapped = next((t for t in fallback_teams
                   if _team_base(t["team_slug"]) == _team_base(last["team_slug"])), None)
    if mapped is None:
        covered = {s["team_slug"] for s in spells}
        orphans = [t for t in fallback_teams if t["team_slug"] not in covered]
        mapped = orphans[0] if len(orphans) == 1 else \
            {"team_slug": last["team_slug"], "team_name": last.get("team_name")}
    first = min(spells, key=lambda s: s["start_date"] or "")
    if any(s["team_slug"] == mapped["team_slug"] for s in spells):
        return spells  # same team before and after New Year (or ambiguous): nothing to split
    if first["start_date"] is None:
        first["start_date"] = jan1
    return [{"team_slug": mapped["team_slug"], "team_name": mapped.get("team_name"),
             "start_date": season_start, "end_date": f"{season - 1}-12-31"}] + spells


def team_for_date(spells: list[dict], day: str):
    """Team slug whose spell covers ``day`` (NULL bounds are open); first spell as fallback."""
    for s in spells:
        if (s["start_date"] is None or s["start_date"] <= day) and \
           (s["end_date"] is None or day <= s["end_date"]):
            return s["team_slug"]
    return spells[0]["team_slug"] if spells else None


# ---------------------------------------------------------------------------
# reconciliation
# ---------------------------------------------------------------------------

def _load_spells(conn: sqlite3.Connection, season: int) -> dict:
    spells = defaultdict(list)
    for slug, team, name, start, end in conn.execute(
            "SELECT rider_slug, team_slug, team_name, start_date, end_date FROM rider_team_spells "
            "WHERE season = ?", (season,)):
        spells[slug].append({"team_slug": team, "team_name": name,
                             "start_date": start, "end_date": end})
    return spells


def rider_reconciliation(conn: sqlite3.Connection, season: int) -> list[dict]:
    """Per rider: our ledger vs the breakdown's counted + not counted.

    PCS rule ("the sanctions are already subtracted"), verified on Pogačar
    2026: 8736.75 (results page) + 620 (GC leader jersey) - 25 (sanction) =
    9331.75 counted.  So ``counted + not_counted = results + leader-jersey
    points + previous-October items - sanction points``, per team: points a
    rider scored before joining a ranked team (``rider_team_spells``) are his
    old team's and are reported as ``other_teams``, not as a difference.
    """
    jan1 = f"{season}-01-01"
    spells = _load_spells(conn, season)
    official: dict = defaultdict(float)
    sanction: dict = defaultdict(float)
    names, teams = {}, defaultdict(list)
    for slug, name, team, counted, nc, sanc in conn.execute(
            "SELECT rider_slug, rider_name, team_slug, points_counted, points_not_counted, "
            "sanction_points FROM uci_team_breakdown WHERE season = ?", (season,)):
        official[slug] += (counted or 0) + (nc or 0)
        sanction[slug] += sanc or 0
        names[slug] = name
        teams[slug].append(team)
    parts = defaultdict(lambda: {"results": 0.0, "leader": 0.0, "previous_year": 0.0,
                                 "penalties": 0.0, "attributed": 0.0, "all": 0.0})
    scraped = set()
    for slug, day, pts, status in conn.execute(
            "SELECT rider_slug, date, COALESCE(points_uci, 0), COALESCE(status, '') "
            "FROM rider_uci_results WHERE season = ?", (season,)):
        scraped.add(slug)
        p = parts[slug]
        key = "leader" if status == "leader" else ("previous_year" if day < jan1 else "results")
        p[key] += pts
        p["all"] += pts
        if team_for_date(spells.get(slug, []), day) in teams[slug] or not spells.get(slug):
            p["attributed"] += pts
    for slug, day, pts in conn.execute(
            "SELECT rider_slug, date, penalty_points FROM rider_uci_penalties WHERE season = ?",
            (season,)):
        p = parts[slug]
        p["penalties"] += pts
        p["all"] -= pts
        if team_for_date(spells.get(slug, []), day) in teams[slug] or not spells.get(slug):
            p["attributed"] -= pts
    out = []
    for slug in official:
        p = parts[slug]
        diff = p["attributed"] - official[slug]
        # PCS prints a negative season net (sanctions > points) as '-': read as 0
        clamped = official[slug] == 0 and p["attributed"] < 0 and sanction[slug] > 0
        out.append({
            "rider_slug": slug, "rider_name": names[slug], "teams": teams[slug],
            "official": round(official[slug], 2), "sanction": sanction[slug],
            "results": round(p["results"], 2), "leader": round(p["leader"], 2),
            "previous_year": round(p["previous_year"], 2), "penalties": round(p["penalties"], 2),
            "ledger": round(p["attributed"], 2),
            "other_teams": round(p["all"] - p["attributed"], 2),
            "diff": 0.0 if clamped else round(diff, 2),
            "negative_net_shown_as_dash": clamped,
            "scraped": slug in scraped,
        })
    return out


def team_reconciliation(conn: sqlite3.Connection, season: int, ranking: str) -> list[dict]:
    """Per team: our ledger (all riders / best 20, by spell) vs breakdown vs official listing.

    ``breakdown_total`` is the breakdown page's own season total (stored under
    the ``team-breakdown/ranking-<cycle>`` ranking key); it can differ from
    ``breakdown_counted_sum`` because PCS prints a negative counted value as
    '-' (Kern Pharma: Unai Ramos 0 earned - 10 sanction is counted as -10).
    ``ledger_best20`` keeps negative riders, so it follows the page total.
    """
    listing_date = conn.execute(
        "SELECT MAX(ranking_date) FROM uci_team_totals WHERE ranking = ?", (ranking,)).fetchone()[0]
    listing = {r[0]: r for r in conn.execute(
        "SELECT team_slug, rank, team_name, team_class, points FROM uci_team_totals "
        "WHERE ranking = ? AND ranking_date = ?", (ranking, listing_date))}
    page_total = dict(conn.execute(
        "SELECT team_slug, points FROM uci_team_totals WHERE ranking = ? AND ranking_date = ?",
        (ranking.replace("rankings/me/", "team-breakdown/"), listing_date)).fetchall())
    breakdown: dict = {}
    for team, counted, nc, sanc in conn.execute(
            "SELECT team_slug, points_counted, points_not_counted, sanction_points "
            "FROM uci_team_breakdown WHERE season = ?", (season,)):
        b = breakdown.setdefault(team, {"counted": 0.0, "all": 0.0, "sanction": 0.0, "riders": 0})
        b["counted"] += counted or 0
        b["all"] += (counted or 0) + (nc or 0)
        b["sanction"] += sanc or 0
        b["riders"] += 1
    spells = _load_spells(conn, season)
    rider_team_pts: dict = defaultdict(float)
    for slug, day, pts in conn.execute(
            "SELECT rider_slug, date, points_uci FROM rider_uci_results "
            "WHERE season = ? AND points_uci IS NOT NULL", (season,)):
        rider_team_pts[(team_for_date(spells.get(slug, []), day), slug)] += pts
    for slug, day, pts in conn.execute(
            "SELECT rider_slug, date, penalty_points FROM rider_uci_penalties WHERE season = ?",
            (season,)):
        rider_team_pts[(team_for_date(spells.get(slug, []), day), slug)] -= pts
    per_team = defaultdict(list)
    for (team, slug), pts in rider_team_pts.items():
        per_team[team].append(pts)
    out = []
    for team in sorted(set(listing) | set(breakdown), key=lambda t: (listing[t][1] if t in listing else 999)):
        vals = sorted(per_team.get(team, []), reverse=True)
        lst = listing.get(team)
        b = breakdown.get(team)
        best20 = round(sum(vals[:20]), 2)
        bt = page_total.get(team, b["counted"] if b else None)
        out.append({
            "team_slug": team, "rank": lst[1] if lst else None,
            "team_name": lst[2] if lst else None, "class": lst[3] if lst else None,
            "listing_total": lst[4] if lst else None,
            "breakdown_total": round(bt, 2) if bt is not None else None,
            "breakdown_counted_sum": round(b["counted"], 2) if b else None,
            "breakdown_all_riders": round(b["all"], 2) if b else None,
            "breakdown_sanctions": b["sanction"] if b else None,
            "breakdown_riders": b["riders"] if b else 0,
            "ledger_all_riders": round(sum(vals), 2), "ledger_best20": best20,
            "ledger_riders": len(vals),
            "diff_best20_vs_breakdown": round(best20 - bt, 2) if bt is not None else None,
            "diff_breakdown_vs_listing": (round(bt - lst[4], 2)
                                          if lst and bt is not None and lst[4] is not None else None),
        })
    return out


# ---------------------------------------------------------------------------
# per rider
# ---------------------------------------------------------------------------

def fetch_rider_pages(season: int, ranking_date: str, slug: str, stats: dict,
                      failures: list, use_cache: bool = True) -> dict | None:
    """Fetch a rider's season results and UCI world-ranking breakdown (cached)."""
    from scraper import uci_points as up
    res = up.fetch_rider_season_results(slug, season, use_cache=use_cache)
    if res is None:
        failures.append(f"results {slug}")
        return None
    stats["result_pages"] += res["pages"]
    wr = up.fetch_rider_uci_ranking(res["rider_id"], ranking_date, use_cache=use_cache) \
        if res["rider_id"] else None
    if wr is None:
        failures.append(f"uci-world-ranking {slug}")
    else:
        stats["ranking_pages"] += 1
    return {"res": res, "wr": wr}


def _race_end_dates(conn, season: int) -> dict:
    """{'race/tour-of-guangxi/2026': '2026-10-19', ...} from the races table
    (empty when the DB has no races table, e.g. a UCI-points-only copy)."""
    try:
        return dict(conn.execute("SELECT pcs_slug, end_date FROM races WHERE year IN (?, ?) "
                                 "AND end_date IS NOT NULL", (season - 1, season)).fetchall())
    except sqlite3.OperationalError:
        return {}


def _stored(conn, table: str, season: int, slug: str, columns: str = "*") -> list[dict]:
    cur = conn.execute(f"SELECT {columns} FROM {table} WHERE season = ? AND rider_slug = ?", (season, slug))
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def _has_rows(conn, table: str, season: int, slug: str) -> bool:
    return conn.execute(f"SELECT 1 FROM {table} WHERE season = ? AND rider_slug = ? LIMIT 1",
                        (season, slug)).fetchone() is not None


def store_rider(conn, season: int, slug: str, info: dict, pages: dict, season_start: str,
                profiles: str, stats: dict, failures: list, race_end_dates: dict | None = None) -> dict:
    """Merge, store and reconcile one rider; returns diagnostics for the report.

    A table is only replaced from complete inputs.  When a page it depends on
    failed and the rider already has stored rows, those rows are kept (logged
    as ``kept_*`` in the diagnostics) instead of being overwritten with a
    degraded fallback:

    * results — the world-ranking page failed (no leader-jersey / previous-
      October / ranking-only items) or the results pages were partial;
    * penalties — neither the fines page nor the ranking page's sanctions are
      available for a rider the breakdown shows sanctions for;
    * spells — the profile was needed (several teams, or points that do not
      reconcile) but could not be fetched.
    """
    from scraper import uci_points as up
    from db.uci_points import replace_rider_results, replace_rider_penalties, replace_rider_spells

    diag: dict = {"rider_slug": slug}
    res, wr = pages["res"], pages["wr"]
    name = res["rider_name"] or info.get("name")
    jan1 = f"{season}-01-01"
    prof = None
    if race_end_dates is None:
        race_end_dates = _race_end_dates(conn, season)
    if wr is not None:
        rows, mdiag = up.merge_season_rows(res["rows"], wr, season, name, season_start, race_end_dates)
        diag.update({"ranking_total": wr["total"], "points_changed": mdiag["points_changed"],
                     "unlisted": mdiag["unlisted"], "outside_season": mdiag["outside_season"],
                     "leader_rows": sum(1 for r in rows if r["status"] == "leader"),
                     "previous_year": [e for e in mdiag["extra"] if e["date"] < jan1]})
    else:
        prof = up.fetch_rider_profile_season(slug, season)  # fallback for leader points
        stats["profiles"] += 1
        rows = [r for r in res["rows"] if r["date"] >= season_start] + (prof["leader_rows"] if prof else [])
    degraded = wr is None or not res.get("complete", True)
    if degraded and _has_rows(conn, "rider_uci_results", season, slug):
        log.warning("%s: %s — keeping the stored results", slug,
                    "ranking page unavailable" if wr is None else "results pages partial")
        diag["kept_results"] = True
        rows = _stored(conn, "rider_uci_results", season, slug)
    else:
        replace_rider_results(conn, season, slug, rows)
    if name:
        conn.execute("UPDATE uci_team_breakdown SET rider_name = ? WHERE season = ? AND rider_slug = ?",
                     (name, season, slug))

    # penalties: the fines page (race, article) cross-checked with the dated ranking sanctions
    penalties: list | None = []
    wr_sanctions = [x for x in (wr["sanctions"] if wr else []) if (x["date"] or season_start) >= season_start]
    if info["sanction"] > 0 or wr_sanctions:
        pens = up.fetch_rider_penalties(slug, season)
        stats["penalty_pages"] += 1
        if pens is not None:
            pens = [p for p in pens if p["date"] >= season_start]
        wr_total = sum(s["points"] for s in wr_sanctions)
        if pens is None:
            failures.append(f"penalties {slug}")
        if pens and (not wr or abs(sum(p["penalty_points"] for p in pens) - wr_total) <= TOLERANCE):
            penalties = pens
        elif wr_sanctions:
            by_date = {p["date"]: p for p in (pens or [])}
            races_by_date = {r["date"]: r["race_name"] for r in rows}
            penalties = [{"date": s["date"] or jan1,
                          "race_name": (by_date.get(s["date"]) or {}).get("race_name")
                          or races_by_date.get(s["date"]) or "Sanctions",
                          "penalty_points": s["points"],
                          "fine": (by_date.get(s["date"]) or {}).get("fine"),
                          "article": (by_date.get(s["date"]) or {}).get("article")}
                         for s in wr_sanctions]
            if pens is not None:
                log.warning("%s: fines page sanctions %s != ranking sanctions %s — using the ranking",
                            slug, [p["penalty_points"] for p in pens], wr_total)
        elif pens is None:
            penalties = None  # neither source available: unknown, not "no penalties"
    if penalties is None:
        log.warning("%s: sanctions unavailable — keeping the stored penalties", slug)
        diag["kept_penalties"] = True
        penalties = _stored(conn, "rider_uci_penalties", season, slug)
    else:
        replace_rider_penalties(conn, season, slug, penalties)

    # spells: profile team history when the rider moved, or his points do not reconcile
    ledger = sum(r["points_uci"] or 0 for r in rows) - sum(p["penalty_points"] for p in penalties)
    diag.update({"ledger": round(ledger, 2), "official": round(info["official"], 2)})
    multi_team = len(info["teams"]) > 1
    reconciles = abs(ledger - info["official"]) <= TOLERANCE
    needed = profiles == "all" or (profiles == "needed" and (multi_team or not reconciles))
    if needed and prof is None:
        prof = up.fetch_rider_profile_season(slug, season)
        stats["profiles"] += 1
        if prof is None:
            failures.append(f"profile {slug}")
    if needed and prof is None and _has_rows(conn, "rider_team_spells", season, slug):
        log.warning("%s: profile unavailable — keeping the stored team spells", slug)
        diag["kept_spells"] = True
        diag["spells"] = _stored(conn, "rider_team_spells", season, slug,
                                 "team_slug, team_name, start_date, end_date")
        return diag
    # A winter move only matters when the rider has points from the previous
    # October-December that his breakdown team does not already count: when a
    # one-team rider's whole ledger reconciles, PCS counts those points for the
    # franchise (Raisberg: Israel - Premier Tech 2025 -> NSN 2026).
    pre_start = season_start if any(r["date"] < jan1 for r in rows) and \
        (multi_team or not reconciles) else None
    spells = spells_from_history(prof["team_history"], season, info["teams"], pre_start) if prof else \
        [{"team_slug": t["team_slug"], "team_name": t["team_name"], "start_date": None,
          "end_date": None} for t in info["teams"]]
    replace_rider_spells(conn, season, slug, spells)
    diag["spells"] = spells
    return diag


# ---------------------------------------------------------------------------
# main flow
# ---------------------------------------------------------------------------

def _parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--season", type=int, default=2026)
    p.add_argument("--teams", choices=["top30", "all"], default="top30",
                   help="top 30 teams of the official ranking, or every listed team")
    p.add_argument("--max-riders", type=int, default=None,
                   help="only the N riders with the most breakdown points")
    p.add_argument("--db", default=None, help="SQLite path (default: config.DB_PATH / $PCS_DB_PATH)")
    p.add_argument("--profiles", choices=["needed", "all", "none"], default="needed",
                   help="when to fetch rider profiles (team history / transfer dates)")
    p.add_argument("--official-only", action="store_true",
                   help="only the listing and team breakdowns, no rider pages")
    p.add_argument("--refresh", action="store_true",
                   help="re-fetch the listing, breakdown and latest-points pages (rider pages stay cached; "
                        "the listing date drives the ranking-page URLs, so a refresh on a later day "
                        "fetches every rider's ranking page again)")
    p.add_argument("--refresh-riders", action="store_true",
                   help="also re-fetch every rider's results and ranking pages (~2 pages per rider)")
    p.add_argument("--season-start", default=None,
                   help="first day of the UCI season (default: calibrated from the ranking pages)")
    p.add_argument("--report", default=None, help="write the reconciliation report (JSON) here")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def run(argv=None) -> int:
    """CLI entry point; returns 0 (ok), 1 (listing unavailable) or 2 (some pages failed)."""
    args = _parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    db_path = os.path.abspath(args.db) if args.db else None
    os.chdir(ROOT)  # scraper cache path (cache/html) is relative to the repo root
    import config
    from scraper import uci_points as up
    from scraper.utils import close_browser
    from db.uci_points import ensure_uci_points_schema, upsert_team_totals, upsert_team_breakdown, \
        upsert_team_recent_points

    db_path = db_path or config.DB_PATH
    season = args.season
    t0 = time.time()
    stats: dict = defaultdict(int)
    failures: list[str] = []
    conn = sqlite3.connect(db_path)
    try:
        ensure_uci_points_schema(conn)

        # 1. official listing
        ranking, listing = up.fetch_team_ranking_listing(season, use_cache=not args.refresh)
        if not listing:
            log.error("could not fetch/parse %s", ranking)
            return 1
        ranking_date = listing[0]["ranking_date"] or time.strftime("%Y-%m-%d")
        upsert_team_totals(conn, ranking, ranking_date, listing)
        conn.commit()
        teams = sorted(listing, key=lambda r: r["rank"] or 999)
        if args.teams == "top30":
            teams = teams[:30]
        stats["teams"] = len(teams)
        log.info("%s (%s): %d teams listed, scraping %d", ranking, ranking_date, len(listing), len(teams))

        # 2. team breakdowns (+ last-30-days points)
        riders: dict[str, dict] = {}
        page_totals: list[dict] = []
        for t in teams:
            got = up.fetch_team_breakdown(t["team_slug"], season, use_cache=not args.refresh)
            if got is None:
                failures.append(f"breakdown {t['team_slug']}")
                continue
            meta, rows = got
            team_name = meta["team_name"] or t["team_name"]
            upsert_team_breakdown(conn, t["team_slug"], team_name, meta["team_class"] or t["team_class"], rows)
            stats["breakdown_rows"] += len(rows)
            if meta["season_totals"].get(season) is not None:
                page_totals.append({"team_slug": t["team_slug"], "team_name": team_name,
                                    "team_class": meta["team_class"] or t["team_class"],
                                    "points": meta["season_totals"][season]})
            log.info("  %-40s listing %8.1f  breakdown %9.2f  (%d riders)", t["team_slug"],
                     t["points"] or 0, meta["season_totals"].get(season) or 0, len(rows))
            recent = up.fetch_team_recent_points(t["team_slug"], use_cache=not args.refresh)
            if recent is None:
                failures.append(f"latest-uci-points {t['team_slug']}")
            else:
                upsert_team_recent_points(conn, season, t["team_slug"], recent)
            conn.commit()
            for r in rows:
                info = riders.setdefault(r["rider_slug"], {"name": r["rider_name"], "teams": [],
                                                           "official": 0.0, "sanction": 0.0})
                info["teams"].append({"team_slug": t["team_slug"], "team_name": team_name})
                info["official"] += (r["points_counted"] or 0) + (r["points_not_counted"] or 0)
                info["sanction"] += r["sanction_points"] or 0
        # the breakdown pages' own season totals, ranked, next to the listing
        page_totals.sort(key=lambda r: -r["points"])
        for i, r in enumerate(page_totals, 1):
            r["rank"] = i
        upsert_team_totals(conn, up.breakdown_totals_ranking(season), ranking_date, page_totals)
        conn.commit()
        if args.official_only:
            return 0 if not failures else 2

        # 3. riders, biggest scorers first
        todo = [(slug, info) for slug, info in riders.items()
                if info["official"] > 0 or info["sanction"] > 0]
        todo.sort(key=lambda x: -(x[1]["official"] + x[1]["sanction"]))
        if args.max_riders:
            todo = todo[:args.max_riders]
        log.info("%d riders with points in %d breakdown rows", len(todo), stats["breakdown_rows"])
        pages: dict = {}
        for i, (slug, info) in enumerate(todo, 1):
            try:
                got = fetch_rider_pages(season, ranking_date, slug, stats, failures,
                                        use_cache=not args.refresh_riders)
                if got is not None:
                    pages[slug] = got
            except Exception as exc:  # one bad rider never stops the run
                log.exception("rider %s failed: %s", slug, exc)
                failures.append(f"rider {slug}: {exc}")
            if i % 25 == 0:
                log.info("fetched %d/%d riders (%.0fs)", i, len(todo), time.time() - t0)

        # 4. where does the UCI season start? (late-October items of the previous year)
        season_start, scores = args.season_start, {}
        if not season_start:
            samples = [(riders[s]["official"], p["wr"]) for s, p in pages.items()
                       if p["wr"] is not None and len(riders[s]["teams"]) == 1]
            season_start, scores = up.calibrate_season_start(samples, season) if samples \
                else (f"{season}-01-01", {})
        log.info("season %d starts %s (riders matched per cutoff: %s)", season, season_start, scores)

        # 5. merge + store per rider (penalties, spells)
        diags = {}
        race_end_dates = _race_end_dates(conn, season)
        for i, (slug, info) in enumerate(todo, 1):
            if slug not in pages:
                continue
            try:
                diags[slug] = store_rider(conn, season, slug, info, pages[slug], season_start,
                                          args.profiles, stats, failures, race_end_dates)
                conn.commit()
            except Exception as exc:  # one bad rider never stops the run
                conn.rollback()
                log.exception("rider %s failed: %s", slug, exc)
                failures.append(f"rider {slug}: {exc}")
            if i % 100 == 0:
                log.info("stored %d/%d riders (%.0fs)", i, len(todo), time.time() - t0)

        # 6. reconciliation
        report = build_report(conn, season, ranking, ranking_date, diags, stats, failures,
                              time.time() - t0)
        report["season_start"] = season_start
        report["season_start_scores"] = scores
        log_report(report)
        if args.report:
            Path(args.report).write_text(json.dumps(report, indent=1, ensure_ascii=False))
            log.info("report written to %s", args.report)
        return 0 if not failures else 2
    finally:
        conn.close()
        close_browser()


def build_report(conn, season, ranking, ranking_date, diags, stats, failures, elapsed) -> dict:
    riders_rec = rider_reconciliation(conn, season)
    for r in riders_rec:
        d = diags.get(r["rider_slug"]) or {}
        r["ranking_total"] = d.get("ranking_total")
        r["points_changed"] = d.get("points_changed") or []
        r["unlisted"] = d.get("unlisted") or []
    teams_rec = team_reconciliation(conn, season, ranking)
    scraped = [r for r in riders_rec if r["scraped"]]
    q = lambda sql: conn.execute(sql, (season,)).fetchone()[0]  # noqa: E731
    stats = dict(stats)
    stats.update({
        "riders_scraped": len(scraped),
        "result_rows": q("SELECT COUNT(*) FROM rider_uci_results WHERE season=?"),
        "rows_with_uci_points": q("SELECT COUNT(*) FROM rider_uci_results WHERE season=? AND points_uci > 0"),
        "leader_rows": q("SELECT COUNT(*) FROM rider_uci_results WHERE season=? AND status='leader'"),
        "previous_year_rows": q(f"SELECT COUNT(*) FROM rider_uci_results WHERE season=? "
                                f"AND date < '{season}-01-01' AND points_uci > 0"),
        "penalties": q("SELECT COUNT(*) FROM rider_uci_penalties WHERE season=?"),
        "spells_with_dates": q("SELECT COUNT(*) FROM rider_team_spells WHERE season=? AND "
                               "(start_date IS NOT NULL OR end_date IS NOT NULL)"),
        "rider_mismatches": sum(1 for r in scraped if abs(r["diff"]) > TOLERANCE),
        "riders_exact": sum(1 for r in scraped if abs(r["diff"]) <= TOLERANCE),
        "rows_points_changed_by_ranking": sum(len(r["points_changed"]) for r in scraped),
        "rows_unlisted_in_ranking": sum(len(r["unlisted"]) for r in scraped),
    })
    return {"season": season, "ranking": ranking, "ranking_date": ranking_date,
            "elapsed_s": round(elapsed), "stats": stats, "failures": failures,
            "teams": teams_rec, "riders": riders_rec}


def log_report(report: dict) -> None:
    log.info("done in %ss: %s", report["elapsed_s"], report["stats"])
    mism = [r for r in report["riders"] if r["scraped"] and abs(r["diff"]) > TOLERANCE]
    for r in sorted(mism, key=lambda r: -abs(r["diff"]))[:30]:
        log.info("  mismatch %-28s official %9.2f ledger %9.2f diff %+8.2f ranking %s teams %s",
                 r["rider_slug"], r["official"], r["ledger"], r["diff"], r["ranking_total"],
                 ",".join(r["teams"]))
    for t in report["teams"]:
        log.info("  team %-38s listing %8s breakdown %9s best20 %9.2f all %9.2f",
                 t["team_slug"], t["listing_total"], t["breakdown_total"], t["ledger_best20"],
                 t["ledger_all_riders"])
    if report["failures"]:
        log.warning("%d failures: %s", len(report["failures"]), report["failures"][:20])


if __name__ == "__main__":
    sys.exit(run())
