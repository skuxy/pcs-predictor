"""Find riders PCS re-slugged and merge the two ``riders`` rows their results ended up in.

Why riders get split
--------------------
ProCyclingStats renames rider slugs in mid-season — a first-name form
(``thomas-pidcock`` → ``tom-pidcock``), a second surname
(``david-lozano`` → ``david-lozano-riba``), or a namesake suffix dropped or
moved (``filippo-magli2`` → ``filippo-magli``).  Result pages always link
the slug PCS uses *at fetch time*, so stages scraped before the change sit
under the old ``riders`` row and every later stage creates a stub row for
the new slug: one rider, two ids, two half-careers.

Why the old slug's own page is never evidence
---------------------------------------------
PCS reuses slugs.  ``rider/thomas-pidcock/results`` answers "Page not
found", and ``rider/romain-gregoire1/results`` now shows a *different*
Romain Grégoire (PCS id 215185, a short career) — PCS swapped the two
namesakes' slugs, and our rider is ``rider/romain-gregoire`` (id 262232).
Name similarity is therefore not enough either.  Evidence accepted here:

* **same stage + bib** (offline): a cached stage page lists slug B with the
  bib our DB stores for slug A on that stage.  Bibs are unique per rider per
  race, so A and B were the same person in that race.
  (:func:`find_bib_drift_pairs`, or pairs another tool found the same way,
  :func:`load_pairs_file`.)
* **the new slug's own results pages** (online): A and B have compatible
  names, never raced on the same day (A's results precede B's within a short
  gap, or the two interleave), and
  ``rider/<B>/results`` (season-filtered through the page's form,
  ``rider.php?id=<n>&p=results&xseason=YYYY&offset=N``) lists a large share
  of A's stored results with the same date and position
  (:func:`find_name_candidates`, :func:`confirm_with_pcs`).

Every pair must also pass :func:`check_pair`: compatible names, no stage
on which both slugs hold a row of a *different* person (different bib or
position) and no day on which they rode different races.  Unconfirmed
candidates are reported, never merged.

Merging (:func:`merge_pair`) moves results / predictions / bets to the
canonical row, resolves same-stage duplicates, fills the canonical profile
from the old row, deletes the old row, re-keys the slug-keyed UCI tables
(only rows proven to be this rider's) and records the alias in
``rider_slug_aliases`` so scrapes of old cached pages map the old slug to the
canonical rider (:func:`resolve_rider_slug`, :func:`canonical_rows`).
"""
from __future__ import annotations

import datetime
import difflib
import json
import logging
import re
import sqlite3
import time
import unicodedata
from collections import defaultdict
from typing import Callable, Iterable, Optional

log = logging.getLogger(__name__)

RIDER_MERGE_DDL = """
CREATE TABLE IF NOT EXISTS rider_slug_aliases (
    old_slug    TEXT PRIMARY KEY,
    new_slug    TEXT NOT NULL,
    evidence    TEXT,
    merged_at   TEXT DEFAULT (datetime('now')),
    valid_until TEXT
)"""
# valid_until: last stage date our DB stored under old_slug.  A page fetched
# on or before that day certainly used old_slug for this rider; a page
# fetched later may already use old_slug for someone else (PCS reuses slugs),
# so resolve_rider_slug() leaves the old slug alone for such pages — unless
# the bib settles it (canonical_rows with a stage_id).  It is never NULL
# (an alias valid for every page) when written by merge_pair.


def ensure_rider_merge_schema(conn: sqlite3.Connection) -> None:
    """Create ``rider_slug_aliases`` if missing (idempotent; init_db may call it).

    Uses ``execute`` (not ``executescript``, which commits any open
    transaction) so it is safe inside :func:`merge_pair`'s savepoint.
    """
    conn.execute(RIDER_MERGE_DDL)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(rider_slug_aliases)")}
    if "valid_until" not in cols:  # table created by an earlier layout
        conn.execute("ALTER TABLE rider_slug_aliases ADD COLUMN valid_until TEXT")


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (name,)).fetchone() is not None


# ── alias lookup (used by every scrape path) ─────────────────────────────────

def alias_map(conn: sqlite3.Connection) -> dict[str, tuple[str, Optional[str]]]:
    """{old_slug: (new_slug, valid_until)}; empty when the table does not exist."""
    if not _has_table(conn, "rider_slug_aliases"):
        return {}
    cols = {r[1] for r in conn.execute("PRAGMA table_info(rider_slug_aliases)")}
    vu = "valid_until" if "valid_until" in cols else "NULL"
    return {r[0]: (r[1], r[2]) for r in conn.execute(
        f"SELECT old_slug, new_slug, {vu} FROM rider_slug_aliases")}


def _resolve(amap: dict, slug: str, page_slugs=None, fetched_on: Optional[str] = None) -> str:
    cur, seen = slug, {slug}
    while cur in amap:
        new, valid_until = amap[cur]
        if fetched_on and valid_until and fetched_on > valid_until:
            break  # page fetched after PCS stopped using this slug for the rider
        if new in seen:
            break  # defensive: never loop on a cyclic table
        cur = new
        seen.add(cur)
    if cur != slug and page_slugs is not None and cur in page_slugs:
        # the canonical slug is on the same page in its own right: the old
        # slug there is another rider (PCS handed it to a namesake)
        return slug
    return cur


def resolve_rider_slug(conn: sqlite3.Connection, slug: str, *, page_slugs=None,
                       fetched_on: Optional[str] = None) -> str:
    """Canonical slug of a scraped rider slug (alias chains followed).

    ``fetched_on`` (ISO date the page was fetched, e.g. the cache file's
    mtime): an alias only applies to pages fetched on or before its
    ``valid_until`` — later pages use PCS's current slugs, and an old slug
    there belongs to whoever PCS gave it to.  ``page_slugs``: the other slugs
    of the same page; if the canonical slug is among them the old slug is left
    alone.  Unknown slugs are returned unchanged.
    """
    return _resolve(alias_map(conn), slug, page_slugs, fetched_on)


def _race_bibs(conn: sqlite3.Connection, stage_id: int, slugs: Iterable[str]) -> dict[str, set[int]]:
    """{rider slug: bibs he wore in the race of ``stage_id``} for ``slugs``,
    from every stored stage of that race."""
    slugs = sorted(set(slugs))
    if not slugs:
        return {}
    out: dict[str, set[int]] = defaultdict(set)
    for slug, bib in conn.execute(
            f"""SELECT rd.pcs_slug, x.bib FROM results x JOIN riders rd ON rd.id = x.rider_id
                JOIN stages s ON s.id = x.stage_id
                WHERE s.race_id = (SELECT race_id FROM stages WHERE id = ?)
                  AND x.bib IS NOT NULL AND x.bib > 0
                  AND rd.pcs_slug IN ({','.join('?' * len(slugs))})""", [stage_id, *slugs]):
        out[slug].add(bib)
    return out


def canonical_rows(conn: sqlite3.Connection, rows: list[dict],
                   fetched_on: Optional[str] = None, stage_id: Optional[int] = None) -> list[dict]:
    """Parsed result rows with ``rider_slug`` mapped through the alias table.

    A page row under an aliased old slug X (alias X → C) is C when:

    * **bib** (``stage_id`` given, the row has a bib and C has stored rows in
      the same race): its bib is one C wore in that race — bibs are unique per
      rider per race, so this holds whatever the page's date, and a different
      bib means another rider (a namesake PCS handed X to), never mapped;
    * otherwise the **date window**: the page was fetched (cache file date) on
      or before the alias's ``valid_until`` (:func:`resolve_rider_slug`).
      ``valid_until`` is the old row's last stored race, the only date known
      to be before PCS's rename; a page fetched later may still show X for C
      (rename not done yet) or show X for a namesake (rename done), so such a
      row is left under X and marked ``alias_unresolved`` = C for the caller
      (refresh-stages then neither inserts it nor prunes C's stored row).

    C itself on the same page always means X is someone else.  A mapped row
    keeps the page's slug in ``page_slug`` (and ``alias_by`` = 'bib' / 'date').
    Rows are copied; the input list is not modified.
    """
    amap = alias_map(conn)
    if not amap:
        return list(rows)
    page = {r.get("rider_slug") for r in rows}
    targets = {s: _resolve(amap, s) for s in page if s in amap}
    bibs = _race_bibs(conn, stage_id, targets.values()) if stage_id is not None and targets else {}
    out = []
    for r in rows:
        slug = r.get("rider_slug")
        target = targets.get(slug)
        if not target or target == slug or target in page:
            out.append(r)   # not an alias, or the canonical rider is on the page himself
            continue
        worn = bibs.get(target)
        if worn and r.get("bib"):
            if r["bib"] in worn:
                out.append(dict(r, rider_slug=target, page_slug=slug, alias_by="bib"))
            else:
                out.append(dict(r, alias_other_bib=sorted(worn)))   # another rider under the old slug
            continue
        new = _resolve(amap, slug, page, fetched_on)
        if new != slug:
            out.append(dict(r, rider_slug=new, page_slug=slug, alias_by="date"))
        else:
            out.append(dict(r, alias_unresolved=target))
    return out


def page_fetched_on(stage_slug: str) -> Optional[str]:
    """ISO date of the newest cached copy of a stage page (``<slug>`` or
    ``<slug>/result``), None when neither is cached."""
    from scraper.utils import cache_path, pcs_url
    stamps = []
    for url in (pcs_url(stage_slug), pcs_url(stage_slug.rstrip("/") + "/result")):
        p = cache_path(url)
        try:
            stamps.append(p.stat().st_mtime)
        except OSError:
            continue
    if not stamps:
        return None
    return datetime.date.fromtimestamp(max(stamps)).isoformat()


# ── names ────────────────────────────────────────────────────────────────────

# name particles never count as a shared surname
_PARTICLES = {"van", "de", "der", "den", "del", "della", "dalla", "delle", "la", "le", "les",
              "di", "da", "das", "dos", "do", "du", "von", "ten", "ter", "te", "el", "al",
              "y", "i", "mac", "mc", "st", "saint", "sainte", "san", "santa", "vom", "zu", "op"}

# first-name forms PCS switches between (same person, different letters)
_FIRST_NAME_FORMS = [
    {"tom", "thomas", "tommy", "thom"}, {"alex", "alexander", "alexandre", "alessandro", "aleksandr",
                                        "alexandr", "aleksander", "alejandro", "sasha", "alexey", "aleksey"},
    {"matt", "matthew", "mathew", "matthias", "mathias", "mattias", "matteo", "mateo", "matej"},
    {"chris", "christopher", "christian", "kristian", "krists"}, {"ben", "benjamin", "benjamim"},
    {"sam", "samuel"}, {"dan", "daniel", "dani", "danny"}, {"nick", "nicholas", "nicolas", "nico",
                                                            "nicola", "niklas", "nikolas"},
    {"mike", "michael", "mikel", "michal", "michele"}, {"jim", "jimmy", "james", "jamie"},
    {"bob", "rob", "robbie", "robert", "roberto"}, {"bill", "will", "william", "willem", "wim"},
    {"joe", "joseph", "jose", "josep", "pepe", "chema"}, {"tony", "anthony", "antonio", "toni", "anton"},
    {"fred", "frederik", "frederic", "frederick", "fredrik", "federico", "fede"},
    {"max", "maximilian", "maxim", "maxime", "maximiliano"}, {"manu", "manuel", "emanuel", "emmanuel"},
    {"javi", "javier"}, {"edu", "eduardo", "eduard", "edward", "ted"},
    {"kiko", "paco", "fran", "francisco", "frank", "francesco", "franck"},
    {"nacho", "ignacio", "inaki"}, {"jack", "john", "jon", "johnny", "jonathan", "juan", "joao"},
    {"lizzie", "elizabeth", "elisabeth", "liz", "lizzy"}, {"kasia", "katarzyna"},
    {"pete", "peter", "pieter", "pedro", "piet"}, {"steve", "steven", "stephen", "stefan", "esteban"},
    {"andy", "andrew", "andreas", "andrea", "andres"}, {"greg", "gregory", "gregor"},
    {"zak", "zack", "zachary"}, {"olly", "oliver", "olivier", "ollie"}, {"harry", "henry", "henri"},
]
_FORM_GROUPS: dict[str, list[int]] = defaultdict(list)
for _i, _grp in enumerate(_FIRST_NAME_FORMS):
    for _n in _grp:
        _FORM_GROUPS[_n].append(_i)


def fold(text: Optional[str]) -> str:
    """Accent-free lower case ('Grégoire' -> 'gregoire')."""
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()


def slug_tokens(slug: str) -> list[str]:
    """'romain-gregoire1' -> ['romain', 'gregoire'] (namesake digits dropped)."""
    toks = [re.sub(r"\d+$", "", t) for t in fold(slug).split("-")]
    return [t for t in toks if t]


def _name_tokens(name: Optional[str]) -> list[str]:
    return [t for t in re.split(r"[\s\-'’.]+", fold(name)) if t]


def name_parts(slug: str, name: Optional[str]) -> tuple[str, set[str]]:
    """(first name, surname tokens) of a rider.

    PCS slugs are 'first-last', so the first slug token is the first name;
    surname tokens are the other slug and name tokens of >= 3 letters,
    particles excluded ('van', 'de', ...).
    """
    toks = slug_tokens(slug)
    first = toks[0] if toks else ""
    sur = {t for t in toks[1:] + _name_tokens(name)
           if len(t) >= 3 and t not in _PARTICLES and t != first}
    return first, sur


def first_name_relation(a: str, b: str) -> Optional[str]:
    """'same', 'variant' (tom/thomas, lucas/lukas, a prefix), 'initial' or None."""
    if not a or not b:
        return None
    if a == b:
        return "same"
    if set(_FORM_GROUPS.get(a, ())) & set(_FORM_GROUPS.get(b, ())):
        return "variant"
    if min(len(a), len(b)) >= 3 and (a.startswith(b) or b.startswith(a)):
        return "variant"
    if difflib.SequenceMatcher(None, a, b).ratio() >= 0.8:
        return "variant"
    if a[0] == b[0]:
        return "initial"
    return None


def names_compatible(a_slug: str, a_name: Optional[str], b_slug: str,
                     b_name: Optional[str]) -> tuple[bool, str]:
    """Could the two riders be one person whose PCS slug changed?

    Compatible when both carry the same name tokens in any order (slug or
    stored name), or share a surname token and have compatible first names.
    Returns (ok, strength): 'strong' for the same tokens, or the same / a
    known form of the first name with one surname set inside the other;
    'weak' otherwise; when not ok, the reason.  A marriage name
    ('megan-armitage' -> 'megan-monk') is not compatible: such a pair needs
    the new slug's PCS page as well (see :func:`build_plan`).
    """
    # the same name tokens in any order, from slug or stored name: PCS swapped
    # the order ('enkhtaivan-bolor-erdene' -> 'bolor-erdene-enkhtaivan'), or
    # the stored name already is the new slug's (row 'gabriel-muller' named
    # 'Mehmet Şampiyonbisiklet' -> 'mehmet-sampiyonbisiklet')
    sets_a = [frozenset(t) for t in (slug_tokens(a_slug), _name_tokens(a_name)) if len(t) >= 2]
    sets_b = [frozenset(t) for t in (slug_tokens(b_slug), _name_tokens(b_name)) if len(t) >= 2]
    if any(x == y for x in sets_a for y in sets_b):
        return True, "strong"
    fa, sa = name_parts(a_slug, a_name)
    fb, sb = name_parts(b_slug, b_name)
    shared = sa & sb
    if not shared:
        return False, "no shared surname"
    rel = first_name_relation(fa, fb)
    if rel is None:
        return False, f"first names differ ({fa} / {fb})"
    strong = rel in ("same", "variant") and (sa <= sb or sb <= sa)
    return True, "strong" if strong else "weak"


def display_name(old_name: Optional[str], new_name: Optional[str], new_slug: str) -> str:
    """Name to keep on a merged row: 'First Last' in the canonical slug's order.

    Stored names mix PCS formats ('Thomas  Pidcock' from a profile,
    'Pidcock Tom' / 'MAGLI Filippo' from result rows).  A name whose tokens
    are the canonical slug's tokens in slug order is kept (whitespace
    normalised); otherwise the canonical row's name is reordered to slug
    order, and an all-capitals surname is title-cased.
    """
    st = slug_tokens(new_slug)

    def pos(tok: str) -> int:
        """Slug position of a name token ('Bolor-erdene' -> position of
        'bolor'), -1 when any part of it is not in the slug."""
        parts = [x for x in re.split(r"[-'’]", fold(tok)) if x]
        return st.index(parts[0]) if parts and all(x in st for x in parts) else -1

    for nm in (new_name, old_name):
        toks = (nm or "").split()
        idx = [pos(t) for t in toks]
        if toks and -1 not in idx and idx == sorted(idx) and not any(
                t.isupper() and len(t) > 1 for t in toks):
            return " ".join(toks)
    toks = (new_name or old_name or "").split()
    if not toks:
        return new_slug
    if all(pos(t) >= 0 for t in toks):
        toks = sorted(toks, key=pos)

    def cap(t):
        return "-".join(p[:1].upper() + p[1:].lower() for p in t.split("-")) \
            if t.isupper() and len(t) > 1 else t
    return " ".join(cap(t) for t in toks)


# ── rider activity ───────────────────────────────────────────────────────────

def rider_activity(conn: sqlite3.Connection) -> dict[int, dict]:
    """{rider_id: {id, slug, name, first, last, n, women}} for riders with results."""
    out = {}
    for rid, slug, name, d0, d1, n, w in conn.execute(
            """SELECT rd.id, rd.pcs_slug, rd.name, MIN(s.date), MAX(s.date), COUNT(*),
                      SUM(ra.gender = 'women')
               FROM riders rd JOIN results x ON x.rider_id = rd.id
               JOIN stages s ON s.id = x.stage_id JOIN races ra ON ra.id = s.race_id
               GROUP BY rd.id"""):
        out[rid] = {"id": rid, "slug": slug, "name": name, "first": d0, "last": d1, "n": n,
                    "women": (w or 0) * 2 > n}
    return out


def _rider(conn, slug: str) -> Optional[dict]:
    row = conn.execute(
        """SELECT rd.id, rd.pcs_slug, rd.name,
                  (SELECT MIN(s.date) FROM results x JOIN stages s ON s.id = x.stage_id
                    WHERE x.rider_id = rd.id),
                  (SELECT MAX(s.date) FROM results x JOIN stages s ON s.id = x.stage_id
                    WHERE x.rider_id = rd.id),
                  (SELECT COUNT(*) FROM results x WHERE x.rider_id = rd.id)
           FROM riders rd WHERE rd.pcs_slug = ?""", (slug,)).fetchone()
    if row is None:
        return None
    return {"id": row[0], "slug": row[1], "name": row[2], "first": row[3], "last": row[4], "n": row[5]}


def shared_stages(conn, id_a: int, id_b: int) -> list[dict]:
    """Stages on which both riders hold a row, classified.

    ``same_person`` when the two rows can only be one rider stored twice
    (same bib, or no bibs and the same position/status); otherwise two
    different riders rode that stage.
    """
    out = []
    for slug, xa, ba, pa, sa_, xb, bb, pb, sb_ in conn.execute(
            """SELECT s.pcs_slug, a.id, a.bib, a.position, a.status, b.id, b.bib, b.position, b.status
               FROM results a JOIN results b ON b.stage_id = a.stage_id
               JOIN stages s ON s.id = a.stage_id
               WHERE a.rider_id = ? AND b.rider_id = ?""", (id_a, id_b)):
        if ba is not None and bb is not None:
            same = ba == bb
        else:
            same = (pa, sa_) == (pb, sb_) and pa is not None
        out.append({"stage": slug, "old_row": xa, "new_row": xb, "same_person": same})
    return out


def shared_race_days(conn, id_a: int, id_b: int) -> list[str]:
    """Days on which the two riders have results in different races — one
    rider rides one race a day, so such a pair is two people."""
    return [d for (d,) in conn.execute(
        """SELECT DISTINCT sa.date FROM results a JOIN stages sa ON sa.id = a.stage_id
           JOIN results b ON b.rider_id = ? JOIN stages sb ON sb.id = b.stage_id
           WHERE a.rider_id = ? AND sa.date IS NOT NULL AND sb.date = sa.date
             AND sb.race_id != sa.race_id ORDER BY 1""", (id_b, id_a))]


def team_continuity(conn, id_a: int, id_b: int, n: int = 3) -> dict:
    """Team of A's last results vs B's first ones.

    ``team_a`` / ``team_b``: team slug without the season of A's last and B's
    first result row with a team; ``shared_teammates``: riders who wore A's
    bib block (bib 11-18 → block 1) in A's last ``n`` stages and B's block in
    B's first ``n`` stages.
    """
    def side(rid, order):
        rows = conn.execute(
            f"""SELECT x.stage_id, x.bib, x.team_slug FROM results x JOIN stages s ON s.id = x.stage_id
                WHERE x.rider_id = ? ORDER BY s.date {order}, s.id {order} LIMIT ?""",
            (rid, n)).fetchall()
        team = next((re.sub(r"-\d{4}$", "", t) for _, _, t in rows if t), None)
        mates = set()
        for stage_id, bib, _ in rows:
            if not bib:
                continue
            for other, obib in conn.execute(
                    "SELECT rider_id, bib FROM results WHERE stage_id = ? AND rider_id != ? AND bib > 0",
                    (stage_id, rid)):
                if (obib - 1) // 10 == (bib - 1) // 10:
                    mates.add(other)
        return team, mates

    ta, ma = side(id_a, "DESC")
    tb, mb = side(id_b, "ASC")
    return {"team_a": ta, "team_b": tb, "same_team": bool(ta and ta == tb),
            "shared_teammates": len(ma & mb)}


# ── pair sanity ──────────────────────────────────────────────────────────────

def check_pair(conn, old_slug: str, new_slug: str, page_name: Optional[str] = None) -> dict:
    """Sanity checks every pair must pass, whatever its evidence.

    Returns {ok, reasons, old, new, shared, names_ok, only_names}: ``old``
    must exist; names must be compatible (``page_name`` stands in for a new
    slug not in the DB); no stage may hold rows of two different riders under
    the two slugs.  ``only_names``: the names are the sole objection.
    """
    reasons = []
    old = _rider(conn, old_slug)
    new = _rider(conn, new_slug)
    if old is None:
        reasons.append(f"{old_slug} is not in riders")
    if old_slug == new_slug:
        reasons.append("same slug")
    ok_names, why = names_compatible(old_slug, old["name"] if old else None, new_slug,
                                     new["name"] if new else page_name)
    if not ok_names:
        reasons.append(f"names incompatible: {why}")
    shared = shared_stages(conn, old["id"], new["id"]) if old and new else []
    diff = [s["stage"] for s in shared if not s["same_person"]]
    if diff:
        reasons.append(f"both slugs on {len(diff)} stage(s) as different riders: {', '.join(diff[:5])}")
    days = shared_race_days(conn, old["id"], new["id"]) if old and new else []
    if days:
        reasons.append(f"both slugs raced on {len(days)} day(s) in different races: {', '.join(days[:5])}")
    return {"ok": not reasons, "reasons": reasons, "old": old, "new": new, "shared": shared,
            "names_ok": ok_names, "name_strength": why if ok_names else None,
            "only_names": not ok_names and len(reasons) == 1}


# ── evidence (a): same stage + bib, from cached pages (no network) ──────────

def _cached_html(url: str) -> Optional[str]:
    from scraper.utils import cache_path
    p = cache_path(url)
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def parse_cached_stage(stage_slug: str) -> Optional[tuple[list[dict], str]]:
    """Parse a stage / one-day result page from the HTML cache only.

    Mirrors scraper.results.fetch_stage_results' page choice (the page, then
    ``<slug>/result`` for one-day races) without ever fetching: skeleton,
    empty and cancelled pages give None, as does a top-N ``table.basic``
    (no bibs).  Returns (rows, url) or None.
    """
    from scraper.results import (_is_missing_page, _is_skeleton, _looks_cancelled,
                                 _parse_results_table, _parse_ttt_results, select_stage_result)
    from scraper.utils import pcs_url, soup
    urls = [pcs_url(stage_slug)]
    if not re.search(r"/(stage-\w+|prologue)$", stage_slug.rstrip("/")):
        urls.append(pcs_url(stage_slug.rstrip("/") + "/result"))
    for url in urls:
        html = _cached_html(url)
        if not html or _is_missing_page(html):
            continue
        target = select_stage_result(soup(html))
        if target is None or _is_skeleton(target) or "basic" in (target.get("class") or []):
            continue
        rows = _parse_ttt_results(target) if target.name == "ul" else _parse_results_table(target)
        if not rows or _looks_cancelled(rows):
            return None
        return rows, url
    return None


def find_bib_drift_pairs(conn, years: Optional[Iterable[int]] = None,
                         stage_parser: Callable = parse_cached_stage) -> dict:
    """Pairs (stored slug → page slug) seen on the same stage with the same bib.

    For every stored stage with a cached page, a page row and a stored row
    with the same bib but different slugs pair up when neither slug is on the
    other side of that stage, and both are finishers or their position /
    status agree.  Bibs that occur twice on the page or in the DB (team time
    trials carry none; some races reuse numbers) are ignored.  A stored slug
    paired with two different page slugs is contradicting and dropped.

    Returns {"pairs": {(old, new): {"stages": [...], "page_name": str}},
             "contradicted": [...], "stages_parsed": int, "stages_cached": int}.
    """
    years = list(years or [])
    sql = """SELECT s.id, s.pcs_slug FROM stages s JOIN races ra ON ra.id = s.race_id
             WHERE EXISTS (SELECT 1 FROM results x WHERE x.stage_id = s.id)"""
    params: list = []
    if years:
        sql += f" AND ra.year IN ({','.join('?' * len(years))})"
        params += years
    stages = conn.execute(sql + " ORDER BY s.date, s.id", params).fetchall()
    amap = alias_map(conn)
    found: dict[tuple, dict] = {}
    on_pages: dict[str, set[int]] = defaultdict(set)   # page slug -> stage ids
    parsed = cached = 0
    for stage_id, stage_slug in stages:
        got = stage_parser(stage_slug)
        if got is None:
            continue
        cached += 1
        rows, _url = got
        if not rows:
            continue
        parsed += 1
        page_slugs = {r["rider_slug"] for r in rows}
        db = conn.execute(
            """SELECT rd.pcs_slug, x.bib, x.position, x.status FROM results x
               JOIN riders rd ON rd.id = x.rider_id WHERE x.stage_id = ?""", (stage_id,)).fetchall()
        db_slugs = {r[0] for r in db}
        for slug in page_slugs:
            on_pages[slug].add(stage_id)
        page_by_bib: dict[int, list] = defaultdict(list)
        for r in rows:
            if r.get("bib"):
                page_by_bib[r["bib"]].append(r)
        db_by_bib: dict[int, list] = defaultdict(list)
        for slug, bib, pos, status in db:
            if bib:
                db_by_bib[bib].append((slug, pos, status))
        for bib, prow in page_by_bib.items():
            drow = db_by_bib.get(bib)
            if len(prow) != 1 or not drow or len(drow) != 1:
                continue
            p, (d, dpos, dstatus) = prow[0], drow[0]
            ps = p["rider_slug"]
            if ps == d or ps in db_slugs or d in page_slugs:
                continue
            if _resolve(amap, ps) == d:
                continue  # an old page showing a merged rider's old slug: not a new pair
            # (a stored slug that is itself an alias of the page slug is NOT
            # skipped: the merged rider was split again by a later scrape)
            agree = (p.get("status") == "finished" and dstatus == "finished") or \
                    (p.get("position"), p.get("status")) == (dpos, dstatus)
            if not agree:
                continue
            ent = found.setdefault((d, ps), {"stages": [], "page_name": p.get("rider_name")})
            ent["stages"].append(stage_slug)
    contradicted = []
    by_old: dict[str, list] = defaultdict(list)
    for (d, ps) in found:
        by_old[d].append(ps)
    pairs = {}
    for (d, ps), ent in found.items():
        both = on_pages.get(d, set()) & on_pages.get(ps, set())
        if both:
            contradicted.append({"old_slug": d, "new_slug": ps,
                                 "reason": f"both slugs on {len(both)} cached page(s)"})
        elif len(by_old[d]) > 1:
            contradicted.append({"old_slug": d, "new_slug": ps,
                                 "reason": f"{d} paired with {len(by_old[d])} page slugs: "
                                           f"{', '.join(sorted(by_old[d]))}"})
        else:
            pairs[(d, ps)] = ent
    return {"pairs": pairs, "contradicted": contradicted, "stages_parsed": parsed,
            "stages_cached": cached}


# ── evidence (b): names + activity, confirmed on the new slug's PCS page ────

def _race_days(conn) -> dict[int, dict[str, set[int]]]:
    """{rider_id: {date: race ids he has a row in that day}}."""
    out: dict[int, dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))
    for rid, day, race in conn.execute(
            """SELECT x.rider_id, s.date, s.race_id FROM results x JOIN stages s ON s.id = x.stage_id
               WHERE s.date IS NOT NULL"""):
        out[rid][day].add(race)
    return out


def find_name_candidates(conn, years: Optional[Iterable[int]] = None, max_gap_days: int = 200,
                         activity: Optional[dict] = None) -> list[dict]:
    """Rider pairs (A → B) that look like one rider under two slugs.

    A and B share a surname token and have compatible first names, and no day
    holds a result of both (a rider rides one race a day).  Two shapes:

    * consecutive — A's results all precede B's, B starting at most
      ``max_gap_days`` after A stops (PCS re-slugged him once);
    * interleaved — the two date ranges overlap: the old slug's pages were
      scraped in between pages already showing the new one (a cached page
      older than its neighbours; andrea-ramirez, Itzulia Women 2023, inside
      andrea-ramirez-fregoso's 2023-2026).

    B (the canonical side) is the slug with the later last result.  Gender is
    advisory (``gender_differs``): the calendar scrape stored some women's
    races as men's (Tour El Salvador, Setmana Valenciana Fèmines 2023), so a
    rider's majority gender is no gate.  ``years`` keeps pairs whose switch
    (A's last or B's first result) falls in one of them.  Every candidate
    still needs the new slug's PCS page (:func:`confirm_with_pcs`).  Sorted
    strongest first (name strength, same gender, consecutive, same team,
    shared teammates, gap).
    """
    act = activity if activity is not None else rider_activity(conn)
    years = {str(y) for y in (years or [])}
    days = _race_days(conn)
    by_tok: dict[str, set[int]] = defaultdict(set)
    parts = {}
    for rid, r in act.items():
        parts[rid] = name_parts(r["slug"], r["name"])
        for t in parts[rid][1]:
            by_tok[t].add(rid)
    seen = set()
    out = []
    for ids in by_tok.values():
        if len(ids) < 2:
            continue
        for a in ids:
            A = act[a]
            for b in ids:
                if a == b or (a, b) in seen:
                    continue
                B = act[b]
                if not (A["last"] and B["first"] and A["last"] < B["last"]):
                    continue
                if years and A["last"][:4] not in years and B["first"][:4] not in years:
                    continue
                gap = (datetime.date.fromisoformat(B["first"]) - datetime.date.fromisoformat(A["last"])).days
                interleaved = gap <= 0
                if gap > max_gap_days:
                    continue
                if interleaved and days[a].keys() & days[b].keys():
                    continue   # both raced on one day: two riders
                ok, strength = names_compatible(A["slug"], A["name"], B["slug"], B["name"])
                if not ok:
                    continue
                seen.add((a, b))
                out.append({"old_slug": A["slug"], "new_slug": B["slug"], "old_id": a, "new_id": b,
                            "old_name": A["name"], "new_name": B["name"], "gap_days": gap,
                            "interleaved": interleaved, "gender_differs": A["women"] != B["women"],
                            "name_strength": strength})
    for c in out:
        c["team"] = team_continuity(conn, c["old_id"], c["new_id"])
    out.sort(key=lambda c: (c["name_strength"] != "strong", c["gender_differs"], c["interleaved"],
                            not c["team"]["same_team"], -c["team"]["shared_teammates"], abs(c["gap_days"]),
                            c["old_slug"]))
    return out


class CountingFetch:
    """scraper.utils.fetch with a count of network fetches (cache misses) and their time."""

    def __init__(self, fetch: Optional[Callable] = None):
        self._fetch = fetch
        self.network = 0
        self.cached = 0
        self.seconds = 0.0

    def __call__(self, url: str) -> Optional[str]:
        from scraper import utils
        fetch = self._fetch or utils.fetch
        if utils.cache_path(url).exists():
            self.cached += 1
            return fetch(url)
        t0 = time.time()
        try:
            return fetch(url)
        finally:
            self.network += 1
            self.seconds += time.time() - t0


def fetch_rider_rows(slug: str, years: Iterable[int], fetcher: Callable) -> Optional[dict]:
    """Result rows of ``rider/<slug>/results`` for the calendar ``years``.

    The pretty URL lists the newest 100 results; a year it does not reach
    past is read from the season-filtered form endpoint
    (``rider.php?id=<n>&p=results&xseason=Y&offset=N``, paginated by 100).
    Returns {rider_id, rider_name, rows, pages, complete, covered_years} or
    None when the first page cannot be fetched.
    """
    from scraper.uci_points import (MAX_RESULT_PAGES, RESULTS_PAGE_SIZE, parse_rider_results,
                                    rider_results_form_path)
    from scraper.utils import pcs_url
    years = sorted({int(y) for y in years}, reverse=True)
    html = fetcher(pcs_url(f"rider/{slug}/results"))
    if not html:
        return None
    out = {"rider_id": None, "rider_name": None, "rows": [], "pages": 1, "complete": True,
           "covered_years": []}
    seen = set()

    def add(rows, year):
        for r in rows:
            if r["date"][:4] != str(year):
                continue
            key = (r["date"], r["result_url"], r["result_label"])
            if key not in seen:
                seen.add(key)
                out["rows"].append(r)

    todo = []
    for y in years:
        p = parse_rider_results(html, y)
        out["rider_id"] = out["rider_id"] or p["rider_id"]
        out["rider_name"] = out["rider_name"] or p["rider_name"]
        add(p["rows"], y)
        if p["has_older"] or p["n_rows"] < RESULTS_PAGE_SIZE:
            out["covered_years"].append(y)
        else:
            todo.append(y)
    for y in todo:
        if not out["rider_id"]:
            out["complete"] = False
            break
        for page in range(MAX_RESULT_PAGES):
            html = fetcher(pcs_url(rider_results_form_path(out["rider_id"], y, page * RESULTS_PAGE_SIZE)))
            out["pages"] += 1
            if not html:
                out["complete"] = False
                break
            p = parse_rider_results(html, y)
            add(p["rows"], y)
            if p["n_rows"] < RESULTS_PAGE_SIZE or not p["rows"]:
                break
        out["covered_years"].append(y)
    return out


def _db_result_rows(conn, rider_id: int) -> list[dict]:
    """A rider's stored finishes with a position (team time trials excluded:
    rider pages give the team's place)."""
    rows = []
    for date, pos, race, stage, prof, ttt in conn.execute(
            """SELECT s.date, x.position, ra.pcs_slug, s.pcs_slug, s.profile_type,
                      EXISTS (SELECT 1 FROM results y WHERE y.stage_id = s.id
                              AND y.points_uci IS NOT NULL AND y.points_uci != CAST(y.points_uci AS INTEGER))
               FROM results x JOIN stages s ON s.id = x.stage_id JOIN races ra ON ra.id = s.race_id
               WHERE x.rider_id = ? AND x.position IS NOT NULL AND x.status = 'finished'
                 AND s.date IS NOT NULL ORDER BY s.date""", (rider_id,)):
        if prof == "ttt" or ttt:
            continue
        rows.append({"date": date, "position": pos, "race_slug": race, "stage": stage})
    return rows


def _match(db_rows: list[dict], pcs_rows: list[dict]) -> dict:
    index: dict[tuple, set] = defaultdict(set)
    for r in pcs_rows:
        if r.get("result_kind") in ("stage", "oneday") and r.get("position") is not None \
                and "(TTT)" not in (r.get("result_label") or ""):
            index[(r["date"], r["position"])].add(r.get("race_slug"))
    strict = loose = 0
    for r in db_rows:
        races = index.get((r["date"], r["position"]))
        if races:
            loose += 1
            if r["race_slug"] in races:
                strict += 1
    return {"n": len(db_rows), "strict": strict, "loose": loose}


def confirm_with_pcs(conn, old_id: int, new_id: Optional[int], new_slug: str, fetcher: Callable,
                     min_match: float = 0.8, min_rows: int = 5) -> dict:
    """Do ``rider/<new_slug>/results`` pages list the old row's results?

    Every stored finish of the old row with a position (team time trials
    excluded) is looked up on the new slug's PCS results by date + position;
    with fewer than ``min_rows`` rows the race must match too.  Confirmed
    when the share is >= ``min_match`` and at least ``min(min_rows, n)``
    rows match.  As a guard, the new row's own stored finishes of its latest
    year must be on that page too (>= 50% when it has ``min_rows``), else the
    new slug in our DB is not the person PCS calls ``new_slug`` today.  Years
    are checked newest first; after 10 rows matching under 30% the check
    stops early.  The old slug's own page is never fetched (PCS reuses old
    slugs for namesakes).
    """
    a_rows = _db_result_rows(conn, old_id)
    res = {"confirmed": False, "n": len(a_rows), "matched": 0, "rate": 0.0,
           "pcs_id": None, "pcs_name": None, "years": [], "reason": None}
    if not a_rows:
        res["reason"] = "old slug has no stored finish with a position"
        return res
    by_year: dict[int, list] = {}

    def rows_of(y: int) -> Optional[list]:
        if y not in by_year:
            got = fetch_rider_rows(new_slug, [y], fetcher)
            if got is None:
                return None
            res["pcs_id"] = res["pcs_id"] or got["rider_id"]
            res["pcs_name"] = res["pcs_name"] or got["rider_name"]
            if not got["complete"]:
                res.setdefault("incomplete_years", []).append(y)
            by_year[y] = got["rows"]
        return by_year[y]

    # guard first: it usually needs only the (cached) first page
    if new_id is not None:
        b_all = _db_result_rows(conn, new_id)
        if b_all:
            yb = int(b_all[-1]["date"][:4])
            got = rows_of(yb)
            if got is None:
                res["reason"] = f"rider/{new_slug}/results could not be fetched"
                return res
            if res["pcs_id"] is None and not got:
                res["reason"] = f"rider/{new_slug}/results has no results table (page not found?)"
                return res
            mb = _match([r for r in b_all if r["date"][:4] == str(yb)], got)
            res["new_rows_checked"] = mb["n"]
            res["new_rows_rate"] = round(mb["loose"] / mb["n"], 3) if mb["n"] else None
            if mb["n"] >= min_rows and mb["loose"] / mb["n"] < 0.5:
                res["reason"] = (f"rider/{new_slug} lists only {mb['loose']}/{mb['n']} of the new row's "
                                 f"own {yb} results: the slug's DB rows are another rider")
                return res
    years = sorted({int(r["date"][:4]) for r in a_rows}, reverse=True)
    checked: list[dict] = []
    pcs_rows: list[dict] = []
    for y in years:
        got = rows_of(y)
        if got is None:
            res["reason"] = f"rider/{new_slug}/results could not be fetched"
            return res
        if res["pcs_id"] is None and not got and not res["years"]:
            res["reason"] = f"rider/{new_slug}/results has no results table (page not found?)"
            return res
        res["years"].append(y)
        pcs_rows += got
        checked += [r for r in a_rows if r["date"][:4] == str(y)]
        m = _match(checked, pcs_rows)
        if m["n"] >= 10 and m["loose"] / m["n"] < 0.3 and y != years[-1]:
            res["reason"] = f"stopped after {y}: only {m['loose']}/{m['n']} matched"
            break
    m = _match(a_rows, pcs_rows)
    matched = m["loose"] if m["n"] >= min_rows else m["strict"]
    res.update(matched=matched, rate=round(matched / m["n"], 3), strict=m["strict"], loose=m["loose"])
    if res["reason"] is None and res["rate"] >= min_match and matched >= min(min_rows, m["n"]):
        res["confirmed"] = True
    elif res["reason"] is None:
        res["reason"] = f"only {matched}/{m['n']} of the old row's results on rider/{new_slug}"
    return res


# ── evidence (c): externally found pairs ────────────────────────────────────

def load_pairs_file(path: str) -> list[dict]:
    """[{"old_slug", "new_slug", "evidence"}] from a JSON file (a list, or
    {"pairs": [...]}).  Entries without both slugs are skipped."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        data = data.get("pairs", [])
    out = []
    for e in data or []:
        if isinstance(e, dict) and e.get("old_slug") and e.get("new_slug"):
            out.append({"old_slug": e["old_slug"].strip(), "new_slug": e["new_slug"].strip(),
                        "evidence": e.get("evidence") or "external pairs file"})
    return out


# ── plan ─────────────────────────────────────────────────────────────────────

def build_plan(conn, *, years: Optional[Iterable[int]] = None, offline_only: bool = False,
               pairs: Optional[list[dict]] = None, min_match: float = 0.8,
               max_gap_days: int = 200, fetcher: Optional[Callable] = None,
               stage_parser: Callable = parse_cached_stage, max_confirm: int = 200) -> dict:
    """Detect re-slugged riders and return a merge plan (nothing is written).

    {"pairs": confirmed [{old_slug, new_slug, old_id, new_id, ..., evidence,
     evidence_type, confidence, rows_to_move, conflicts}],
     "unconfirmed": [...], "rejected": [...], "stats": {...}}

    Order: (a) bib drift on cached pages, (c) ``pairs`` given by the caller —
    both confirmed when :func:`check_pair` passes; when the names are its only
    objection (a marriage name) the pair is confirmed on the new slug's PCS
    page instead (evidence 'bib+pcs' / 'pairs-file+pcs') — then (b) name +
    activity candidates not already covered, confirmed on PCS unless
    ``offline_only``.  Chains (a → b, b → c) collapse to the final slug.
    """
    years = list(years or [])
    stats = {"bib_stages_cached": 0, "bib_stages_parsed": 0, "name_candidates": 0,
             "pcs_confirmations": 0, "pcs_fetches": 0, "pcs_cached_reads": 0, "pcs_seconds": 0.0}
    confirmed: dict[str, dict] = {}
    unconfirmed: list[dict] = []
    rejected: list[dict] = []

    def add_confirmed(old, new, ev_type, evidence, extra=None):
        ent = confirmed.get(old)
        if ent is not None:
            if ent["new_slug"] != new:
                rejected.append({"old_slug": old, "new_slug": new, "evidence_type": ev_type,
                                 "reason": f"conflicts with {ent['evidence_type'][0]} pair "
                                           f"{old} -> {ent['new_slug']}"})
                return
            if ev_type not in ent["evidence_type"]:
                ent["evidence_type"].append(ev_type)
                ent["evidence"].append(evidence)
            ent.update(extra or {})
            return
        confirmed[old] = {"old_slug": old, "new_slug": new, "evidence_type": [ev_type],
                          "evidence": [evidence], **(extra or {})}

    counting = fetcher if isinstance(fetcher, CountingFetch) else CountingFetch(fetcher)

    def needs_pcs(old, new, ev_type, evidence, chk, extra=None):
        """Bib-evidenced pair whose names differ (marriage name ...): merge only
        if the new slug's PCS page lists the old row's results too."""
        why = "; ".join(chk["reasons"])
        if offline_only:
            unconfirmed.append({"old_slug": old, "new_slug": new, "evidence_type": ev_type,
                                "evidence": evidence, "reason": f"{why}; needs PCS confirmation "
                                                                f"(--offline-only)"})
            return
        stats["pcs_confirmations"] += 1
        log.info("confirming %s -> %s on PCS (%s evidence, names differ)", old, new, ev_type)
        res = confirm_with_pcs(conn, chk["old"]["id"], chk["new"]["id"] if chk["new"] else None,
                               new, counting, min_match)
        if res["confirmed"]:
            add_confirmed(old, new, f"{ev_type}+pcs",
                          f"{evidence}; rider/{new}/results (PCS id {res['pcs_id']}, "
                          f"{res['pcs_name']}) lists {res['matched']}/{res['n']} of {old}'s results",
                          {"pcs_match": res, **(extra or {}),
                           **({"new_name_hint": res["pcs_name"]} if res.get("pcs_name") else {})})
        else:
            rejected.append({"old_slug": old, "new_slug": new, "evidence_type": ev_type,
                             "evidence": evidence, "pcs_match": res,
                             "reason": f"{why}; PCS: {res['reason']}"})

    # (a) bib drift
    bib = find_bib_drift_pairs(conn, years, stage_parser=stage_parser)
    stats["bib_stages_cached"] = bib["stages_cached"]
    stats["bib_stages_parsed"] = bib["stages_parsed"]
    for c in bib["contradicted"]:
        rejected.append({**c, "evidence_type": "bib"})
    for (old, new), ent in sorted(bib["pairs"].items()):
        chk = check_pair(conn, old, new, page_name=ent["page_name"])
        stages = ent["stages"]
        ev = f"same stage+bib on {len(stages)} cached page(s): {', '.join(stages[:6])}" + \
             (" ..." if len(stages) > 6 else "")
        hint = {"bib_stages": stages, "new_name_hint": ent["page_name"]}
        if chk["ok"]:
            add_confirmed(old, new, "bib", ev, hint)
        elif chk["only_names"]:
            needs_pcs(old, new, "bib", ev, chk, hint)
        else:
            rejected.append({"old_slug": old, "new_slug": new, "evidence_type": "bib", "evidence": ev,
                             "reason": "; ".join(chk["reasons"])})

    # (c) external pairs
    for p in pairs or []:
        chk = check_pair(conn, p["old_slug"], p["new_slug"])
        if chk["ok"]:
            add_confirmed(p["old_slug"], p["new_slug"], "pairs-file", p["evidence"])
        elif chk["only_names"]:
            needs_pcs(p["old_slug"], p["new_slug"], "pairs-file", p["evidence"], chk)
        else:
            rejected.append({**p, "evidence_type": "pairs-file", "reason": "; ".join(chk["reasons"])})

    # (b) names + activity
    activity = rider_activity(conn)
    cands = [c for c in find_name_candidates(conn, years, max_gap_days, activity)
             if c["old_slug"] not in confirmed]
    stats["name_candidates"] = len(cands)
    for i, c in enumerate(cands):
        info = {k: c[k] for k in ("old_slug", "new_slug", "old_name", "new_name", "gap_days",
                                  "interleaved", "gender_differs", "name_strength", "team")}
        if offline_only:
            unconfirmed.append({**info, "evidence_type": "name+activity",
                                "reason": "not confirmed (--offline-only)"})
            continue
        if stats["pcs_confirmations"] >= max_confirm:
            unconfirmed.append({**info, "evidence_type": "name+activity",
                                "reason": f"not checked (max {max_confirm} confirmations)"})
            continue
        stats["pcs_confirmations"] += 1
        log.info("confirming %s -> %s on PCS [%d/%d]", c["old_slug"], c["new_slug"], i + 1, len(cands))
        res = confirm_with_pcs(conn, c["old_id"], c["new_id"], c["new_slug"], counting, min_match)
        if res["confirmed"]:
            add_confirmed(c["old_slug"], c["new_slug"], "name+pcs",
                          f"rider/{c['new_slug']}/results (PCS id {res['pcs_id']}) lists "
                          f"{res['matched']}/{res['n']} of {c['old_slug']}'s results "
                          f"(date + position)", {"pcs_match": res, **info})
        else:
            unconfirmed.append({**info, "evidence_type": "name+activity", "pcs_match": res,
                                "reason": res["reason"]})
    stats["pcs_fetches"] = counting.network
    stats["pcs_cached_reads"] = counting.cached
    stats["pcs_seconds"] = round(counting.seconds, 1)

    # chains: follow confirmed pairs and stored aliases to the final slug
    amap = alias_map(conn)
    for ent in confirmed.values():
        path, cur = [ent["old_slug"]], ent["new_slug"]
        while True:
            path.append(cur)
            nxt = confirmed[cur]["new_slug"] if cur in confirmed else None
            if nxt is None and cur in amap:
                nxt = amap[cur][0]
            if nxt is None or nxt in path:
                break
            cur = nxt
        ent["new_slug"] = cur
        if len(path) > 2:
            ent["chain"] = path

    plan_pairs = []
    for ent in confirmed.values():
        if ent["old_slug"] == ent["new_slug"]:
            continue
        sim = merge_pair(conn, ent["old_slug"], ent["new_slug"], "; ".join(ent["evidence"]),
                         dry_run=True, new_name=ent.get("new_name_hint"))
        old = _rider(conn, ent["old_slug"])
        new = _rider(conn, ent["new_slug"])
        plan_pairs.append({
            "old_slug": ent["old_slug"], "new_slug": ent["new_slug"],
            "old_id": old["id"] if old else None, "new_id": new["id"] if new else None,
            "old_name": old["name"] if old else None, "new_name": new["name"] if new else None,
            "old_dates": [old["first"], old["last"]] if old else None,
            "new_dates": [new["first"], new["last"]] if new else None,
            "evidence_type": ent["evidence_type"], "evidence": ent["evidence"],
            "confidence": "confirmed", "action": sim["action"],
            "rows_to_move": sim["results_moved"], "conflicts": sim["conflicts"],
            "predictions_to_move": sim["predictions_moved"], "bets_to_move": sim["bets_moved"],
            "slug_tables": sim["slug_tables"], "kept_name": sim.get("name"),
            "new_name_hint": ent.get("new_name_hint"),
            **({"chain": ent["chain"]} if "chain" in ent else {}),
            **({"pcs_match": ent["pcs_match"]} if "pcs_match" in ent else {}),
            **({"team": ent["team"]} if "team" in ent else {}),
        })
    plan_pairs.sort(key=lambda p: p["old_slug"])
    return {"pairs": plan_pairs, "unconfirmed": unconfirmed, "rejected": rejected, "stats": stats}


# ── merge ────────────────────────────────────────────────────────────────────

_PROFILE_FIELDS = ("nationality", "dob", "team", "pcs_rank", "speciality", "weight_kg", "height_cm")
_RESULT_DATA = ("position", "time_seconds", "points_pcs", "points_uci", "bib", "team_slug",
                "team_name", "uci_penalty")

# slug-keyed tables (scrape_uci_points): (table, its UNIQUE key columns)
_SLUG_TABLES = {
    "rider_uci_results": ("season", "rider_slug", "date", "result_url", "result_label"),
    "rider_uci_penalties": ("season", "rider_slug", "date", "race_name"),
    "rider_uci_scrapes": ("season", "rider_slug"),   # what the rider pages showed (db/uci_points.py)
    "rider_team_spells": ("season", "rider_slug", "team_slug"),
    "uci_team_breakdown": ("season", "team_slug", "rider_slug"),
}


def _completeness(row: sqlite3.Row | tuple, cols: list[str]) -> int:
    return sum(1 for c in _RESULT_DATA if row[cols.index(c)] is not None)


def _slug_rows_belong(conn, old_slug: str, rider_ids: list[int]) -> tuple[bool, str]:
    """Are the slug-keyed rows stored under ``old_slug`` this rider's?

    Those tables are keyed by the slug PCS used when they were scraped; PCS
    may have handed an old slug to a namesake since, so rows are only moved
    when they are shown to be the merged rider's: his rider_uci_results rows
    under the old slug match his stored finishes (date + position, >= 80%),
    or — without such rows — the old slug's team breakdown team is a team he
    rode for that season.
    """
    marks = ",".join("?" * len(rider_ids))
    have = {(d, p) for d, p in conn.execute(
        f"""SELECT s.date, x.position FROM results x JOIN stages s ON s.id = x.stage_id
            WHERE x.rider_id IN ({marks}) AND x.position IS NOT NULL""", rider_ids)}
    if _has_table(conn, "rider_uci_results"):
        rows = conn.execute(
            """SELECT date, position FROM rider_uci_results WHERE rider_slug = ?
               AND result_kind IN ('stage', 'oneday') AND position IS NOT NULL""", (old_slug,)).fetchall()
        if rows:
            hit = sum(1 for d, p in rows if (d, p) in have)
            if hit / len(rows) >= 0.8 and hit >= min(5, len(rows)):
                return True, f"{hit}/{len(rows)} rider_uci_results rows match his results"
            return False, (f"only {hit}/{len(rows)} rider_uci_results rows of {old_slug} match his "
                           f"results — PCS uses {old_slug} for another rider now")
    if _has_table(conn, "uci_team_breakdown"):
        teams = conn.execute("SELECT season, team_slug FROM uci_team_breakdown WHERE rider_slug = ?",
                             (old_slug,)).fetchall()
        if teams:
            mine = {t for (t,) in conn.execute(
                f"SELECT DISTINCT team_slug FROM results WHERE rider_id IN ({marks}) AND team_slug IS NOT NULL",
                rider_ids)}
            if all(t in mine for _, t in teams):
                return True, "team breakdown team matches his result rows"
            return False, f"team breakdown of {old_slug} names a team he did not ride for"
    return False, "no rider_uci_results / breakdown rows to verify them"


# Tables scrape_uci_points stores per (season, rider) as one replace-set: a
# scrape deletes the rider's season rows and writes the new ones.
_RIDER_SEASON_TABLES = ("rider_uci_results", "rider_uci_penalties", "rider_uci_scrapes",
                        "rider_team_spells")


def _move_slug_rows(conn, old_slug: str, new_slug: str, rider_ids: list[int]) -> dict:
    """Re-key the old slug's UCI-table rows to ``new_slug``, season by season.

    The rider tables (:data:`_RIDER_SEASON_TABLES`) hold one scrape per rider
    and season, replaced as a whole; two scrapes are never combined.  For a
    season the new slug has nothing in, the old slug's rows move (all of
    them); for a season the new slug was scraped in too, the old slug's rows
    are deleted in every rider table (``superseded``): the new slug is the one
    PCS uses now, so its scrape came after the rename, and a result PCS
    relabelled in between ('Stage 1' → 'Stage 1 (ITT)') would otherwise be
    counted twice.  ``uci_team_breakdown`` is one set per team and season:
    an old-slug row moves unless the new slug has a row on that team's page
    of the same season (then the new one is kept).
    """
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE rider_slug = ?", (old_slug,)).fetchone()[0]
              for t in _SLUG_TABLES if _has_table(conn, t)}
    counts = {t: n for t, n in counts.items() if n}
    if not counts:
        return {"moved": {}, "superseded": {}, "left": {}}
    ok, why = _slug_rows_belong(conn, old_slug, rider_ids)
    if not ok:
        return {"moved": {}, "superseded": {}, "left": counts, "reason": why}
    moved: dict[str, int] = defaultdict(int)
    superseded: dict[str, int] = defaultdict(int)
    rider_tables = [t for t in _RIDER_SEASON_TABLES if _has_table(conn, t)]
    seasons = sorted({s for t in rider_tables for (s,) in conn.execute(
        f"SELECT DISTINCT season FROM {t} WHERE rider_slug = ?", (old_slug,))})
    for season in seasons:
        new_has = any(conn.execute(f"SELECT 1 FROM {t} WHERE rider_slug = ? AND season = ? LIMIT 1",
                                   (new_slug, season)).fetchone() for t in rider_tables)
        for t in rider_tables:
            if new_has:
                n = conn.execute(f"DELETE FROM {t} WHERE rider_slug = ? AND season = ?",
                                 (old_slug, season)).rowcount
                if n:
                    superseded[t] += n
            else:
                n = conn.execute(f"UPDATE {t} SET rider_slug = ? WHERE rider_slug = ? AND season = ?",
                                 (new_slug, old_slug, season)).rowcount
                if n:
                    moved[t] += n
    if "uci_team_breakdown" in counts:
        n = conn.execute("UPDATE OR IGNORE uci_team_breakdown SET rider_slug = ? WHERE rider_slug = ?",
                         (new_slug, old_slug)).rowcount
        if n:
            moved["uci_team_breakdown"] += n
        # left behind: the new slug is on the same team page of the same season
        n = conn.execute("DELETE FROM uci_team_breakdown WHERE rider_slug = ?", (old_slug,)).rowcount
        if n:
            superseded["uci_team_breakdown"] += n
    return {"moved": dict(moved), "superseded": dict(superseded), "left": {}, "reason": why}


def record_alias(conn, old_slug: str, new_slug: str, evidence: Optional[str],
                 valid_until: Optional[str]) -> None:
    """Store old → new; aliases pointing at ``old_slug`` now point at
    ``new_slug`` (chains collapse), and ``new_slug`` — canonical now — stops
    being an alias of anything."""
    ensure_rider_merge_schema(conn)
    conn.execute("UPDATE rider_slug_aliases SET new_slug = ? WHERE new_slug = ?", (new_slug, old_slug))
    conn.execute("DELETE FROM rider_slug_aliases WHERE old_slug = ?", (new_slug,))
    conn.execute(
        """INSERT INTO rider_slug_aliases (old_slug, new_slug, evidence, valid_until)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(old_slug) DO UPDATE SET
               new_slug = excluded.new_slug,
               evidence = COALESCE(excluded.evidence, rider_slug_aliases.evidence),
               valid_until = MAX(COALESCE(excluded.valid_until, ''),
                                 COALESCE(rider_slug_aliases.valid_until, ''))""",
        (old_slug, new_slug, evidence, valid_until))
    conn.execute("DELETE FROM rider_slug_aliases WHERE old_slug = new_slug")
    conn.execute("UPDATE rider_slug_aliases SET valid_until = NULL WHERE valid_until = ''")


def merge_pair(conn, old_slug: str, new_slug: str, evidence: Optional[str] = None,
               dry_run: bool = False, new_name: Optional[str] = None) -> dict:
    """Merge rider ``old_slug`` into ``new_slug`` (atomic; a savepoint).

    * new slug not in ``riders``: the old row is renamed (keeps its id) and
      takes ``new_name`` (PCS's current name for the new slug, when known —
      a marriage name changes it) in 'First Last' form;
    * otherwise results move to the new row — a stage holding both (one
      rider stored twice) keeps the more complete row (ties: the newer one,
      higher id), its NULL bib / team filled from the other, and the other is
      deleted; predictions (same stage + model: the newer kept) and bets move;
      the new row takes the old row's profile where its own is NULL and a
      'First Last' name (:func:`display_name`); the old row is deleted;
    * slug-keyed UCI tables: old-slug rows proven to be this rider's are
      re-keyed season by season (:func:`_slug_rows_belong`,
      :func:`_move_slug_rows`), others are left and reported;
    * the alias old → new is recorded (``valid_until`` = old row's last
      date; no alias when the old row has no result to date it).

    Re-running is a no-op ("already merged", only when the alias table maps
    the pair; an old slug that is gone without one is reported "old slug not
    in riders" and nothing is written).  ``dry_run`` computes the
    report and rolls everything back.  ``new_slug`` is first resolved through
    stored aliases (chains collapse).
    """
    if _has_table(conn, "rider_slug_aliases"):
        new_slug = resolve_rider_slug(conn, new_slug)
    rep = {"old_slug": old_slug, "new_slug": new_slug, "action": None, "results_moved": 0,
           "conflicts": [], "predictions_moved": 0, "predictions_dropped": 0, "bets_moved": 0,
           "slug_tables": {}, "dry_run": dry_run}
    conn.execute("SAVEPOINT merge_pair")
    try:
        old = conn.execute("SELECT * FROM riders WHERE pcs_slug = ?", (old_slug,)).fetchone()
        new = conn.execute("SELECT * FROM riders WHERE pcs_slug = ?", (new_slug,)).fetchone()
        rcols = [d[1] for d in conn.execute("PRAGMA table_info(riders)")]
        if old_slug == new_slug:
            rep["action"] = "nothing (same slug)"
        elif old is None:
            # Nothing to move.  "Already merged" only when the alias table says
            # so; otherwise nothing is recorded: an alias written here would
            # carry no valid_until (no stored result dates it) and so apply to
            # every page — PCS reuses old slugs (romain-gregoire1 is another
            # rider today) — or would redirect an existing alias to a rider
            # it was never shown to be.
            known = old_slug in alias_map(conn) and resolve_rider_slug(conn, old_slug) == new_slug
            rep["action"] = "already merged" if known else "old slug not in riders"
        else:
            old_id = old[rcols.index("id")]
            last = conn.execute("""SELECT MAX(s.date) FROM results x JOIN stages s ON s.id = x.stage_id
                                   WHERE x.rider_id = ?""", (old_id,)).fetchone()[0]
            if new is None:
                rep["action"] = "rename"
                rep["results_moved"] = conn.execute("SELECT COUNT(*) FROM results WHERE rider_id = ?",
                                                    (old_id,)).fetchone()[0]
                rep["name"] = display_name(old[rcols.index("name")], new_name, new_slug)
                conn.execute("UPDATE riders SET pcs_slug = ?, name = ?, updated_at = datetime('now') "
                             "WHERE id = ?", (new_slug, rep["name"], old_id))
                ids = [old_id]
            else:
                new_id = new[rcols.index("id")]
                rep["action"] = "merge"
                _merge_rows(conn, old_id, new_id, rep)
                prof = {f: (new[rcols.index(f)] if new[rcols.index(f)] is not None else old[rcols.index(f)])
                        for f in _PROFILE_FIELDS if f in rcols}
                rep["name"] = display_name(old[rcols.index("name")], new[rcols.index("name")], new_slug)
                rep["profile_filled"] = sorted(f for f in prof if new[rcols.index(f)] is None
                                               and prof[f] is not None)
                conn.execute(f"UPDATE riders SET {', '.join(f'{f} = ?' for f in prof)}, name = ?, "
                             "updated_at = datetime('now') WHERE id = ?",
                             [*prof.values(), rep["name"], new_id])
                conn.execute("DELETE FROM riders WHERE id = ?", (old_id,))
                ids = [new_id]
            rep["slug_tables"] = _move_slug_rows(conn, old_slug, new_slug, ids)
            if last is not None:
                record_alias(conn, old_slug, new_slug, evidence, last)
            # else: no result was ever stored under the old slug, so no cached
            # page needs mapping — and a NULL valid_until would map every page
            rep["valid_until"] = last
    except Exception:
        conn.execute("ROLLBACK TO merge_pair")
        conn.execute("RELEASE merge_pair")
        raise
    if dry_run:
        conn.execute("ROLLBACK TO merge_pair")
    conn.execute("RELEASE merge_pair")
    return rep


def _merge_rows(conn, old_id: int, new_id: int, rep: dict) -> None:
    """Move results / predictions / bets of ``old_id`` to ``new_id``."""
    cols = [d[1] for d in conn.execute("PRAGMA table_info(results)")]
    for o in conn.execute(
            """SELECT a.*, s.pcs_slug AS stage_slug FROM results a JOIN stages s ON s.id = a.stage_id
               WHERE a.rider_id = ? AND EXISTS (SELECT 1 FROM results b WHERE b.stage_id = a.stage_id
                                                AND b.rider_id = ?)""", (old_id, new_id)).fetchall():
        n = conn.execute("SELECT * FROM results WHERE stage_id = ? AND rider_id = ?",
                         (o[cols.index("stage_id")], new_id)).fetchone()
        oid, nid = o[cols.index("id")], n[cols.index("id")]
        co, cn = _completeness(o, cols), _completeness(n, cols)
        keep_old = co > cn or (co == cn and oid > nid)
        keep, drop = (o, n) if keep_old else (n, o)
        fill = {c: drop[cols.index(c)] for c in ("bib", "team_slug", "team_name")
                if keep[cols.index(c)] is None and drop[cols.index(c)] is not None}
        conn.execute("DELETE FROM results WHERE id = ?", (drop[cols.index("id")],))
        if fill:
            conn.execute(f"UPDATE results SET {', '.join(f'{c} = ?' for c in fill)} WHERE id = ?",
                         [*fill.values(), keep[cols.index("id")]])
        rep["conflicts"].append({
            "stage": o[len(cols)], "kept": "old" if keep_old else "new",
            "kept_row": keep[cols.index("id")], "deleted_row": drop[cols.index("id")],
            "reason": f"{'more complete' if co != cn else 'newer'} ({max(co, cn)} vs {min(co, cn)} "
                      f"filled columns)", "filled": sorted(fill)})
    rep["results_moved"] = conn.execute("UPDATE results SET rider_id = ? WHERE rider_id = ?",
                                        (new_id, old_id)).rowcount
    if _has_table(conn, "predictions"):
        for pid, opid in conn.execute(
                """SELECT b.id, a.id FROM predictions a JOIN predictions b
                   ON b.stage_id IS a.stage_id AND b.model_version IS a.model_version
                   WHERE a.rider_id = ? AND b.rider_id = ?""", (old_id, new_id)).fetchall():
            older = conn.execute(
                """SELECT id FROM predictions WHERE id IN (?, ?)
                   ORDER BY COALESCE(created_at, ''), id LIMIT 1""", (pid, opid)).fetchone()[0]
            conn.execute("DELETE FROM predictions WHERE id = ?", (older,))
            rep["predictions_dropped"] += 1
        rep["predictions_moved"] = conn.execute("UPDATE predictions SET rider_id = ? WHERE rider_id = ?",
                                                (new_id, old_id)).rowcount
    if _has_table(conn, "bets"):
        rep["bets_moved"] = conn.execute("UPDATE bets SET rider_id = ? WHERE rider_id = ?",
                                         (new_id, old_id)).rowcount


def revalidate_plan(conn, plan: dict) -> tuple[list[dict], list[dict]]:
    """Re-check a saved plan's pairs against the DB before applying it.

    A pair is kept when :func:`check_pair` passes now, or when the names are
    its only objection and its evidence included the new slug's PCS page
    ('name+pcs', 'bib+pcs', 'pairs-file+pcs').  A pair whose old slug is gone
    is kept only when the alias table already maps it to the same rider
    (merge_pair then reports 'already merged' and writes nothing); otherwise
    it is dropped — a plan built on another DB copy, or a stale or edited
    one, must not record an alias nothing in this DB backs.  Returns
    (pairs to apply, dropped pairs with the reason).
    """
    keep, dropped = [], []
    amap = alias_map(conn)
    for p in plan.get("pairs", []):
        if p.get("confidence") != "confirmed":
            continue
        chk = check_pair(conn, p["old_slug"], p["new_slug"], page_name=p.get("new_name_hint"))
        pcs = any(t.endswith("pcs") for t in p.get("evidence_type", []))
        if chk["old"] is None:
            if p["old_slug"] in amap and _resolve(amap, p["old_slug"]) == _resolve(amap, p["new_slug"]):
                keep.append(p)
            else:
                dropped.append({**p, "reason": f"{p['old_slug']} is not in riders and no alias maps it "
                                               f"to {p['new_slug']}"})
        elif chk["ok"] or (chk["only_names"] and pcs):
            keep.append(p)
        else:
            dropped.append({**p, "reason": "; ".join(chk["reasons"])})
    return keep, dropped


def apply_plan(conn_factory: Callable, plan: dict) -> list[dict]:
    """Merge every confirmed pair of ``plan``, one transaction per pair.

    ``conn_factory()`` yields a connection context (db.database.get_conn)
    that commits on exit.
    """
    reports = []
    for p in plan["pairs"]:
        if p.get("confidence") != "confirmed":
            continue
        with conn_factory() as conn:
            ensure_rider_merge_schema(conn)
            rep = merge_pair(conn, p["old_slug"], p["new_slug"], "; ".join(p["evidence"]),
                             new_name=p.get("new_name_hint"))
        reports.append(rep)
    return reports
