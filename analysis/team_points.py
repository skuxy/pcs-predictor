"""Weekly UCI team-points ledger, jump detection and the chart payload.

Reads the team-UCI-points tables written by ``scripts/scrape_uci_points.py``
(schema in ``db/uci_points.py``) and turns them into a per-week picture of how
the top men's teams collected their season's UCI points:

* :func:`load_points_ledger` — one row per points event (a result row with UCI
  points, or a UCI sanction as a negative row), attributed to the team the
  rider rode for on that date;
* :func:`weekly_team_series` — contiguous Monday weeks with gains, cumulative
  totals, the weekly rank and the UCI "best 20 riders" cumulative;
* :func:`detect_jumps` — the few weeks that moved a team (or a rider) most,
  explained by the riders/races behind them;
* :func:`build_chart_payload` — the JSON payload consumed by
  ``analysis.team_points_chart.render_html`` (the data contract's keys plus
  ``weeks_meta`` — :func:`weeks_meta` — and ``meta.axis_start`` —
  :func:`axis_start`);
* :func:`markdown_summary` — the text summary printed by
  ``scripts/team_points_report.py``.

Source tables (one season each):

``uci_team_totals``
    The official listing ``rankings/me/ranking-2026-2028`` (34 teams: 18 WT +
    16 PRT).  Defines the team universe and the top-N order.
``uci_team_breakdown``
    ``team/<slug>/results/ranking-2026-2028``: per rider ``points_counted``
    (the team's 20 best riders) / ``points_not_counted`` (the rest), already
    net of ``sanction_points``.
``rider_uci_results``
    ``rider/<slug>/results`` rows of the season merged with the GC
    leader-jersey points from the rider's UCI-ranking page (``status =
    'leader'``, ``result_label = 'Leader jersey after Stage 6'``).  The UCI
    season can open in late October of the previous calendar year (results
    dated before 1 January that UCI counts in this season).
``rider_uci_penalties``
    UCI sanction points (positive numbers; subtracted here).
``rider_team_spells``
    Mid-season transfers: which team a rider rode for when.

Attribution rule: a rider's points on date ``d`` count for the team whose
spell covers ``d``; with no spell data, the single team whose breakdown lists
him.  Only riders listed in some ``uci_team_breakdown`` and teams that have a
breakdown are part of the team ledger.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Iterable, Optional, Sequence

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

BEST_N = 20                      # UCI team ranking: a team's 20 best riders count
DEFAULT_RIDERS = ("tadej-pogacar",)
WEEK_CONVENTION = ("ISO weeks, Monday start; points dated to the result date "
                   "(GC/classification points on the race's final day)")
RECONCILE_TOL = 1.0              # points; PCS rounds fractional TTT shares to 2 decimals

LEDGER_COLUMNS = [
    "date", "week_start", "team_slug", "team_name", "rider_slug", "rider_name", "rider_short",
    "race_name", "race_slug", "result_label", "result_kind", "race_class", "position",
    "status", "points",
]

# Short chart labels for the 2026 teams (matched on normalised names).
TEAM_SHORT_NAMES = {
    "UAE Team Emirates - XRG": "UAE",
    "Red Bull - BORA - hansgrohe": "Red Bull-Bora",
    "Team Visma | Lease a Bike": "Visma",
    "Decathlon CMA CGM Team": "Decathlon",
    "Lidl - Trek": "Lidl-Trek",
    "XDS Astana Team": "Astana",
    "Bahrain - Victorious": "Bahrain",
    "Netcompany INEOS": "INEOS",
    "Uno-X Mobility": "Uno-X",
    "Soudal Quick-Step": "Soudal QS",
    "Alpecin - Premier Tech": "Alpecin",
    "Movistar Team": "Movistar",
    "Pinarello-Q36.5 Pro Cycling Team": "Pinarello-Q36.5",
    "Team Jayco AlUla": "Jayco",
    "EF Education - EasyPost": "EF",
    "Lotto Intermarché": "Lotto-Intermarché",
    "Tudor Pro Cycling Team": "Tudor",
    "Cofidis": "Cofidis",
    "NSN Cycling Team": "NSN",
    "Groupama - FDJ United": "FDJ",
    "TotalEnergies": "TotalEnergies",
    "Unibet Rose Rockets": "Unibet",
    "Caja Rural - Seguros RGA": "Caja Rural",
    "Burgos Burpellet BH": "Burgos",
    "Team Polti VisitMalta": "Polti",
    "Solution Tech NIPPO Rali": "Solution Tech",
    "Equipo Kern Pharma": "Kern Pharma",
    "Euskaltel - Euskadi": "Euskaltel",
    "MBH Bank CSB Telecom Fort": "MBH Bank",
    "Team Picnic PostNL": "Picnic",
}

# Race names that read better shortened (keys compared after clean_race_name).
RACE_NAME_OVERRIDES = {
    "La Vuelta Ciclista a España": "Vuelta a España",
    "La Vuelta a España": "Vuelta a España",
    "In Flanders Fields From Middelkerke to Wevelgem": "Gent-Wevelgem",
    "DSSK (Donostia San Sebastian Klasikoa)": "Clásica San Sebastián",
    "Dwars door Vlaanderen A travers la Flandre": "Dwars door Vlaanderen",
    "Ronde Van Brugge Tour of Bruges": "Ronde van Brugge",
    "Vuelta a Andalucia Ruta Ciclista Del Sol": "Vuelta a Andalucía",
    "Volta ao Algarve em Bicicleta": "Volta ao Algarve",
    "Volta a Portugal em Bicicleta": "Volta a Portugal",
    "Volta Ciclista a Catalunya": "Volta a Catalunya",
    "Presidential Cycling Tour of Türkiye": "Tour of Türkiye",
    "PostNord Tour of Denmark": "Tour of Denmark",
    "Skoda Tour de Luxembourg": "Tour de Luxembourg",
    "Ethias-Tour de Wallonie": "Tour de Wallonie",
    "Lloyds Tour of Britain Men": "Tour of Britain",
    "Boucles de la Mayenne Crédit Mutuel": "Boucles de la Mayenne",
    "De Brabantse Pijl La Flèche Brabançonne": "Brabantse Pijl",
    "GP de Denain Porte du Hainaut": "GP de Denain",
    "Utsunomiya Japan Cup Road Race": "Japan Cup",
    "Coppa Bernocchi GP Banco BPM": "Coppa Bernocchi",
    "Lotto GP de Wallonie": "GP de Wallonie",
    "O Gran Camiño The Historical Route": "O Gran Camiño",
    "Etoile de Bessèges Tour du Gard": "Étoile de Bessèges",
    "Giro della Toscana Memorial Alfredo Martini": "Giro della Toscana",
    "Coppa Agostoni Giro delle Brianze": "Coppa Agostoni",
    "Circuito de Getxo Memorial Hermanos Otxoa": "Circuito de Getxo",
    "Clásica de Ordizia Ordiziako Klasikoa": "Clásica de Ordizia",
    "Gran Premio città di Peccioli Coppa Sabatini": "Coppa Sabatini",
    "Clàssica Comunitat Valenciana Gran Premi València": "Clàssica Comunitat Valenciana",
    "Sparkassen Münsterland Giro": "Münsterland Giro",
    "La Route d'Occitanie CIC": "Route d'Occitanie",
    "Sun Hung Kai Properties Hong Kong Cyclothon": "Hong Kong Cyclothon",
    "Lotto Omloop van het Houtland": "Omloop van het Houtland",
    "Lotto Famenne Ardenne Classic": "Famenne Ardenne Classic",
    "Gran Premio Castellón Ruta de la Cerámica": "GP Castellón",
    "Vuelta Asturias Julio Alvarez Mendo": "Vuelta Asturias",
    "Tour du Finistère Pays de Quimper": "Tour du Finistère",
    "GP Marseille La Marseillaise": "GP La Marseillaise",
    "La Roue Tourangelle Centre Val de Loire Groupama P.V.L.": "La Roue Tourangelle",
    "Grande Prémio Internacional Beiras e Serra da Estrela": "GP Beiras e Serra da Estrela",
    "Tour of Rhodes Powered by Rodos Palace": "Tour of Rhodes",
    "Settimana Internazionale Coppi e Bartali": "Coppi e Bartali",
    "Giro della Provincia di Biella Torino-Biella": "Torino-Biella",
    "Gran Premio Primavera Ontur Diputación de Albacete": "GP Primavera Ontur",
    "Tour du Limousin-Périgord Nouvelle Aquitaine": "Tour du Limousin",
    "Mercan'Tour Classic Alpes-Maritimes": "Mercan'Tour Classic",
    "Grand Prix de la Ville de Lillers Souvenir Bruno Comini": "GP de Lillers",
    "GP de la Ville de Lillers Souvenir Bruno Comini": "GP de Lillers",
    "GP Internacional Torres Vedras Trofeu Joaquim Agostinho": "Troféu Joaquim Agostinho",
    "Vuelta a la Región de Murcia “Costa Cálida”": "Vuelta a Murcia",
    "GP d'Isbergues Pas de Calais": "GP d'Isbergues",
}


# ---------------------------------------------------------------------------
# names
# ---------------------------------------------------------------------------

def _fold(text: str) -> str:
    """Lowercase ASCII fold: 'Pogačar' -> 'pogacar', 'Großschartner' -> 'grossschartner'."""
    text = (text or "").replace("ß", "ss").replace("ø", "o").replace("Ø", "O") \
        .replace("æ", "ae").replace("Æ", "AE").replace("ł", "l").replace("Ł", "L") \
        .replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def _norm_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _fold(text))


# Surname particles written lowercase whenever they lead a surname
# ('VAN DER POEL' -> 'van der Poel', 'DEL TORO' -> 'del Toro').
_PARTICLES_LOWER = {"van", "der", "den", "ten", "ter", "te", "von", "vom", "zu", "zur",
                    "del", "dos", "das", "y", "'t"}
# Particles whose case depends on the name's origin ('De Lie', 'Le Gac', 'Di Rocco'
# but 'de la Cruz'): lowercase only inside a run of particles.
_PARTICLES_CONTEXT = {"de", "la", "le", "las", "los", "di", "da", "du", "des", "della",
                      "dalla", "dal", "do"}
_PARTICLES = _PARTICLES_LOWER | _PARTICLES_CONTEXT


def _is_upper_token(tok: str) -> bool:
    letters = [c for c in tok if c.isalpha()]
    return len(letters) >= 1 and all(not c.islower() or c == "ß" for c in letters)


def _cap_token(tok: str) -> str:
    """'POGAČAR' -> 'Pogačar', "O'CONNOR" -> "O'Connor", 'MCNULTY' -> 'McNulty'."""
    def cap(part: str) -> str:
        low = part.lower()
        if not low:
            return low
        if len(low) > 3 and low.startswith("mc"):
            return "Mc" + low[2].upper() + low[3:]
        return low[0].upper() + low[1:]
    out = []
    for piece in re.split(r"(['’\-])", tok):
        out.append(piece if piece in ("'", "’", "-") else cap(piece))
    return "".join(out)


def _case_surname(tokens: list[str]) -> list[str]:
    """Proper-case surname tokens given in capitals, minding particles."""
    out = []
    n = len(tokens)
    for i, tok in enumerate(tokens):
        low = tok.lower()
        last = i == n - 1
        if not last and low in _PARTICLES_LOWER:
            out.append(low)
        elif not last and low in _PARTICLES_CONTEXT and (
                tokens[i + 1].lower() in _PARTICLES or (i > 0 and tokens[i - 1].lower() in _PARTICLES)):
            out.append(low)
        elif not last and low in _PARTICLES_CONTEXT:
            out.append(_cap_token(tok))
        else:
            out.append(_cap_token(tok))
    return out


def split_rider_name(raw: str, slug: Optional[str] = None) -> tuple[str, str]:
    """PCS rider name -> (display name 'Firstname Lastname', surname).

    PCS lists riders as ``'POGAČAR Tadej'`` / ``'VAN DER POEL Mathieu'``
    (surname in capitals first); rider pages' ``<h1>`` already read
    ``'Tadej Pogačar'``.  Both forms are accepted::

        'POGAČAR Tadej'        -> ('Tadej Pogačar', 'Pogačar')
        'VAN DER POEL Mathieu' -> ('Mathieu van der Poel', 'van der Poel')
        'DEL TORO Isaac'       -> ('Isaac del Toro', 'del Toro')
        'DE LIE Arnaud'        -> ('Arnaud De Lie', 'De Lie')
        'Isaac del Toro'       -> ('Isaac del Toro', 'del Toro')

    Accents come from the page text.  When every token is in capitals the
    rider slug (``'tadej-pogacar'``: given names first) decides which tokens are
    the given names.
    """
    tokens = (raw or "").split()
    if not tokens:
        return "", ""
    upper = [_is_upper_token(t) for t in tokens]
    if all(upper) and len(tokens) > 1:
        # 'POGAČAR TADEJ': the slug ('tadej-pogacar', given names first) tells
        # how many trailing tokens are given names.
        n = len(tokens)
        given_n = 1
        if slug:
            want = [_norm_key(t) for t in slug.split("-")]
            for k in range(1, n):
                order = [_norm_key(t) for t in tokens[n - k:] + tokens[:n - k]]
                if "".join(order) == "".join(want):
                    given_n = k
                    break
        sur = _case_surname(tokens[:n - given_n])
        given = [_cap_token(t) for t in tokens[n - given_n:]]
        return " ".join(given + sur), " ".join(sur)
    if any(upper) and not all(upper):
        surname = [t for t, u in zip(tokens, upper) if u]
        given = [t for t, u in zip(tokens, upper) if not u]
        sur = _case_surname(surname)
        return " ".join(given + sur), " ".join(sur)
    # already 'Firstname Lastname' (or a single token)
    if len(tokens) == 1:
        return tokens[0], tokens[0]
    i = len(tokens) - 1
    while i > 1 and tokens[i - 1].lower() in _PARTICLES:
        i -= 1
    return " ".join(tokens), " ".join(tokens[i:])


def display_name(raw: str, slug: Optional[str] = None) -> str:
    """'VAN DER POEL Mathieu' -> 'Mathieu van der Poel' (see :func:`split_rider_name`)."""
    return split_rider_name(raw, slug)[0]


def surname_of(name: str, slug: Optional[str] = None) -> str:
    """Surname of a rider name in either PCS form ('del Toro', 'van der Poel', 'Pogačar')."""
    return split_rider_name(name, slug)[1]


def _sentence_case(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def clean_race_name(name: Optional[str]) -> str:
    """PCS race name -> chart label.

    Strips the elite category marker PCS appends to championship/one-day
    names (``'Ronde van Vlaanderen ME'``, ``'World Championships ME - Road
    Race'`` -> ``'World Championships Road Race'``) but keeps the age
    categories visible — U23 (``MU``/``WU``) and junior (``MJ``/``WJ``) races
    are different races from the elite ones (``'National Championships
    Hungary MU - Road Race'`` -> ``'Hungary U23 nationals'``, ``'World
    Championships MU - Road Race'`` -> ``'World Championships U23 Road
    Race'``); then a trailing class in parentheses
    (``'Tour de France (2.UWT)'``), a ``' | Stage 4'`` suffix, secondary titles
    after ``' / '`` and sponsor tails after ``' p/b '``; shortens
    'Grand Prix (Cycliste de)' to 'GP', 'Continental Championships' to
    'Championships' and ``'National Championships Norway Road Race'`` to
    ``'Norway nationals'`` (ITT: ``'Norway nationals ITT'``); then applies
    :data:`RACE_NAME_OVERRIDES`.
    """
    text = (name or "").split(" | ")[0].strip()
    if text in RACE_NAME_OVERRIDES:
        return RACE_NAME_OVERRIDES[text]
    text = re.sub(r"\s*\((?:[12]\.[A-Za-z0-9.]+|[A-Z]{1,3})\)\s*$", "", text)
    text = re.sub(r"\s+(?:ME|WE)(?=\s|$)", "", text)
    text = re.sub(r"\s+(?:MU|WU|U23)(?=\s|$)", " U23", text)
    text = re.sub(r"\s+(?:MJ|WJ)(?=\s|$)", " Junior", text)
    text = text.split(" / ")[0]
    text = re.sub(r"\s+Elite$", "", text)                     # 'Paris - Tours Elite'
    # 'Milano - Torino', 'Kuurne - Brussel - Kuurne', 'Veenendaal - Veenendaal':
    # a one-word place after the dash joins with a hyphen; a discipline
    # ('- ITT', '- Road Race') or a secondary title ('- Trofeo Arvedi') is
    # separated by a space instead.
    text = re.sub(r"\s+-\s+(?!(?:ITT|TTT|IRR)\b)(?=\S+(?:\s+-\s|$))", "-", text)
    text = re.sub(r"\s+-\s+", " ", text)
    text = re.sub(r"\s+-\s*$", "", text)
    text = re.split(r"\s+p/b\s+", text)[0]
    text = re.sub(r"^Grand Prix Cycliste (?:de |d')?", "GP ", text)
    text = re.sub(r"\bGrand Prix\b", "GP", text)
    text = text.replace("Continental Championships", "Championships")
    m = re.match(r"^National Championships (.+?) (Road Race|ITT)$", text)
    if m:
        country = re.sub(r" Junior$", " junior", m.group(1))
        text = f"{country} nationals" + ("" if m.group(2) == "Road Race" else " ITT")
    text = re.sub(r"\s+", " ", text).strip()
    return RACE_NAME_OVERRIDES.get(text, text)


_SHORT_BY_KEY = {_norm_key(k): v for k, v in TEAM_SHORT_NAMES.items()}


def team_short_name(name: Optional[str], slug: Optional[str] = None) -> str:
    """Short chart label for a team ('UAE Team Emirates - XRG' -> 'UAE').

    :data:`TEAM_SHORT_NAMES` first (matched on accent/punctuation-insensitive
    names, then on the slug without its year); otherwise a heuristic: drop
    'Team'/'Pro Cycling (Team)'/'Cycling Team', keep the part before ' - ' or
    ' | ', and at most two words.
    """
    key = _norm_key(name or "")
    if key in _SHORT_BY_KEY:
        return _SHORT_BY_KEY[key]
    if slug:
        skey = _norm_key(re.sub(r"-(?:19|20)\d{2}$", "", slug))
        if skey in _SHORT_BY_KEY:
            return _SHORT_BY_KEY[skey]
    text = (name or (slug or "")).strip()
    if not name and slug:
        text = re.sub(r"-(?:19|20)\d{2}$", "", slug).replace("-", " ").title()
    text = re.split(r"\s+[-|]\s+", text)[0]
    text = re.sub(r"\s+(?:Pro Cycling Team|Pro Cycling|Cycling Team|Team)$", "", text, flags=re.I)
    text = re.sub(r"^(?:Team|Equipo)\s+", "", text, flags=re.I)
    words = text.split()
    if len(" ".join(words)) > 16:
        words = words[:2]
    return " ".join(words) or (name or slug or "")


def ordinal(n: Optional[int]) -> str:
    """1 -> '1st', 2 -> '2nd', 11 -> '11th', 22 -> '22nd'; None -> ''."""
    if n is None:
        return ""
    n = int(n)
    if 10 <= n % 100 <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


# ---------------------------------------------------------------------------
# weeks
# ---------------------------------------------------------------------------

def week_start(day) -> str:
    """ISO date of the Monday starting ``day``'s ISO week."""
    d = day if isinstance(day, date) else date.fromisoformat(str(day)[:10])
    return (d - timedelta(days=d.weekday())).isoformat()


def week_axis(first_day, last_day) -> list[str]:
    """Contiguous Mondays from ``first_day``'s week through ``last_day``'s week."""
    start = date.fromisoformat(week_start(first_day))
    end = date.fromisoformat(week_start(last_day))
    out = []
    while start <= end:
        out.append(start.isoformat())
        start += timedelta(days=7)
    return out


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------

def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def ranking_name(conn: sqlite3.Connection, season: int) -> Optional[str]:
    """The official listing in ``uci_team_totals`` covering ``season``.

    Prefers ``rankings/me/ranking-A-B`` with A <= season <= B (the licence
    cycle), then any other ``rankings/...`` page, then whatever is stored.
    Pseudo-rankings the scraper keeps for reconciliation (e.g.
    ``team-breakdown/ranking-2026-2028``: the team pages' season totals) are
    never preferred over the listing.
    """
    if not _table_exists(conn, "uci_team_totals"):
        return None
    names = sorted(r[0] for r in conn.execute("SELECT DISTINCT ranking FROM uci_team_totals") if r[0])

    def covers(n: str) -> bool:
        m = re.search(r"ranking-(\d{4})-(\d{4})$", n)
        return bool(m) and int(m.group(1)) <= season <= int(m.group(2))
    for pred in (lambda n: n.startswith("rankings/me/") and covers(n),
                 lambda n: n.startswith("rankings/") and covers(n),
                 lambda n: n.startswith("rankings/"),
                 covers):
        hits = [n for n in names if pred(n)]
        if hits:
            return hits[0]
    return names[0] if names else None


def load_listing(conn: sqlite3.Connection, season: int) -> pd.DataFrame:
    """Latest official listing rows: rank, team_slug, team_name, team_class, points, ranking, ranking_date."""
    cols = ["rank", "team_slug", "team_name", "team_class", "points", "ranking", "ranking_date"]
    name = ranking_name(conn, season)
    if name is None:
        return pd.DataFrame(columns=cols)
    row = conn.execute("SELECT MAX(ranking_date) FROM uci_team_totals WHERE ranking = ?",
                       (name,)).fetchone()
    df = pd.read_sql_query(
        "SELECT rank, team_slug, team_name, team_class, points, ranking, ranking_date "
        "FROM uci_team_totals WHERE ranking = ? AND ranking_date = ? ORDER BY rank",
        conn, params=(name, row[0]))
    return df


def load_breakdown_page_totals(conn: sqlite3.Connection, season: int) -> dict[str, float]:
    """{team_slug: season total printed on its team/<slug>/results/ranking-<cycle> page}.

    The scraper stores them in ``uci_team_totals`` under the pseudo-ranking
    ``team-breakdown/ranking-<cycle>`` (latest ``ranking_date``).  They can
    differ from the sum of the riders' 'points counted': PCS prints a negative
    net as '-' (stored as NULL) but still subtracts it (Kern Pharma -10), and
    when two riders tie on 20th place the page total counts both (Euskaltel
    +0.71, MBH Bank +3) — the UCI best-20 sum counts one.  Empty when not
    scraped.
    """
    name = ranking_name(conn, season)
    if not name:
        return {}
    m = re.search(r"(ranking-\d{4}-\d{4})$", name)
    if not m:
        return {}
    pseudo = f"team-breakdown/{m.group(1)}"
    row = conn.execute("SELECT MAX(ranking_date) FROM uci_team_totals WHERE ranking = ?", (pseudo,)).fetchone()
    if not row or row[0] is None:
        return {}
    return {slug: float(pts) for slug, pts in conn.execute(
        "SELECT team_slug, points FROM uci_team_totals WHERE ranking = ? AND ranking_date = ?",
        (pseudo, row[0])) if pts is not None}


def load_breakdown(conn: sqlite3.Connection, season: int) -> pd.DataFrame:
    cols = ["team_slug", "team_name", "team_class", "rider_slug", "rider_name", "nth_best",
            "points_counted", "points_not_counted", "sanction_points"]
    if not _table_exists(conn, "uci_team_breakdown"):
        return pd.DataFrame(columns=cols)
    return pd.read_sql_query(
        f"SELECT {', '.join(cols)} FROM uci_team_breakdown WHERE season = ?",
        conn, params=(season,))


def _load_spells(conn: sqlite3.Connection, season: int) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    if not _table_exists(conn, "rider_team_spells"):
        return out
    for slug, team, tname, s, e in conn.execute(
            "SELECT rider_slug, team_slug, team_name, start_date, end_date FROM rider_team_spells "
            "WHERE season = ? ORDER BY rider_slug, COALESCE(start_date, '')", (season,)):
        out[slug].append({"team_slug": team, "team_name": tname, "start_date": s, "end_date": e})
    return out


def _spell_team(spells: list[dict], day: str) -> Optional[str]:
    """Team whose spell covers ``day`` (NULL bounds are open); else the nearest spell."""
    if not spells:
        return None
    for s in spells:
        if (s["start_date"] is None or s["start_date"] <= day) and \
           (s["end_date"] is None or day <= s["end_date"]):
            return s["team_slug"]

    def gap(s: dict) -> int:
        d = date.fromisoformat(day)
        if s["start_date"] and day < s["start_date"]:
            return (date.fromisoformat(s["start_date"]) - d).days
        if s["end_date"] and day > s["end_date"]:
            return (d - date.fromisoformat(s["end_date"])).days
        return 0
    return min(spells, key=gap)["team_slug"]


def _penalty_label(race_name: str) -> tuple[str, str]:
    """'Tour de France | Stage 4' -> ('Tour de France', 'Penalty (Stage 4)')."""
    name, _, label = (race_name or "").partition(" | ")
    return name.strip(), f"Penalty ({label.strip()})" if label.strip() else "Penalty"


def load_events(conn: sqlite3.Connection, season: int = 2026,
                rider_slugs: Optional[Iterable[str]] = None) -> pd.DataFrame:
    """Every points event of the season, before team attribution.

    Result rows of ``rider_uci_results`` with non-zero ``points_uci`` plus one
    negative row per ``rider_uci_penalties`` entry (``result_kind='penalty'``,
    ``points = -penalty_points``, race_slug borrowed from the rider's result
    row of the same race).  Columns: :data:`LEDGER_COLUMNS` minus the team
    columns, ``rider_name`` as stored (converted later).
    """
    cols = ["date", "rider_slug", "rider_name", "race_name", "race_slug", "result_label",
            "result_kind", "race_class", "position", "status", "points"]
    if not _table_exists(conn, "rider_uci_results"):
        return pd.DataFrame(columns=cols)
    where, params = "season = ? AND points_uci IS NOT NULL AND points_uci != 0", [season]
    slugs = list(rider_slugs) if rider_slugs is not None else None
    if slugs is not None:
        if not slugs:
            return pd.DataFrame(columns=cols)
        where += f" AND rider_slug IN ({', '.join('?' * len(slugs))})"
        params += slugs
    res = pd.read_sql_query(
        "SELECT date, rider_slug, rider_name, race_name, race_slug, result_label, result_kind, "
        f"race_class, position, status, points_uci AS points FROM rider_uci_results WHERE {where}",
        conn, params=params)
    pens = []
    if _table_exists(conn, "rider_uci_penalties"):
        pwhere, pparams = "season = ?", [season]
        if slugs is not None:
            pwhere += f" AND rider_slug IN ({', '.join('?' * len(slugs))})"
            pparams += slugs
        # race_slug / class for a penalty: the rider's own results row of that race
        races = defaultdict(list)
        for slug, d, rname, rslug, rcls in conn.execute(
                "SELECT rider_slug, date, race_name, race_slug, race_class FROM rider_uci_results "
                f"WHERE season = ?{' AND rider_slug IN (' + ', '.join('?' * len(slugs)) + ')' if slugs else ''}",
                [season] + (slugs or [])):
            races[slug].append((d, _norm_key(clean_race_name(rname)), rslug, rcls))
        for slug, d, rname, pts in conn.execute(
                f"SELECT rider_slug, date, race_name, penalty_points FROM rider_uci_penalties WHERE {pwhere}",
                pparams):
            if not pts:
                continue
            name, label = _penalty_label(rname)
            key = _norm_key(clean_race_name(name))
            cands = [r for r in races.get(slug, []) if r[1] == key]
            same_day = [r for r in cands if r[0] == d]
            best = (same_day or sorted(cands, key=lambda r: abs(
                (date.fromisoformat(r[0]) - date.fromisoformat(d)).days)))[:1]
            pens.append({"date": d, "rider_slug": slug, "rider_name": None, "race_name": name,
                         "race_slug": best[0][2] if best else None, "result_label": label,
                         "result_kind": "penalty", "race_class": best[0][3] if best else None,
                         "position": None, "status": "penalty", "points": -abs(float(pts))})
    if pens:
        res = pd.concat([res, pd.DataFrame(pens, columns=cols)], ignore_index=True)
    res["points"] = res["points"].astype(float)
    res["result_label"] = res["result_label"].fillna("")
    return res[cols]


def _rider_names(conn: sqlite3.Connection, season: int, breakdown: pd.DataFrame) -> dict[str, tuple[str, str]]:
    """rider_slug -> (display name, surname).

    The rider page's name (``rider_uci_results.rider_name``, the <h1>) wins
    over the breakdown's (approximate when PCS lists 'SURNAME Given').
    """
    names: dict[str, str] = {}
    for slug, nm in zip(breakdown["rider_slug"], breakdown["rider_name"]):
        if nm:
            names[slug] = nm
    if _table_exists(conn, "rider_uci_results"):
        for slug, nm in conn.execute(
                "SELECT rider_slug, MAX(rider_name) FROM rider_uci_results WHERE season = ? "
                "GROUP BY rider_slug", (season,)):
            if nm:
                names[slug] = nm
    out = {}
    for slug, nm in names.items():
        out[slug] = split_rider_name(nm, slug)
    return out


def _fallback_name(slug: str) -> tuple[str, str]:
    parts = [p.capitalize() for p in slug.split("-")]
    return " ".join(parts), parts[-1] if parts else slug


def _disambiguate_shorts(df: pd.DataFrame) -> pd.Series:
    """Surname labels, with an initial where two riders of one team share it ('R. Oliveira')."""
    short = df["rider_short"].copy()
    pairs = df[["team_slug", "rider_slug", "rider_short", "rider_name"]].drop_duplicates("rider_slug")
    clash = pairs.groupby(["team_slug", "rider_short"])["rider_slug"].transform("nunique") > 1
    fix = {}
    for _, r in pairs[clash].iterrows():
        initial = (r["rider_name"] or "?")[0]
        fix[r["rider_slug"]] = f"{initial}. {r['rider_short']}"
    if fix:
        short = df["rider_slug"].map(fix).fillna(short)
    return short


def load_points_ledger(conn: sqlite3.Connection, season: int = 2026,
                       return_excluded: bool = False):
    """One row per points event, attributed to a team.

    Columns (:data:`LEDGER_COLUMNS`): date, week_start (Monday), team_slug,
    team_name, rider_slug, rider_name ('Firstname Lastname'), rider_short
    (surname label), race_name (cleaned, see :func:`clean_race_name`),
    race_slug, result_label, result_kind ('oneday'|'stage'|'gc'|'points'|
    'kom'|'youth'|'other'|'penalty'), race_class, position, status, points
    (positive for results, negative for sanctions).

    Attribution: the spell (``rider_team_spells``) covering the date; with no
    spells, the rider's breakdown team (the one with most breakdown points if
    PCS lists him twice).  Riders absent from ``uci_team_breakdown`` and
    events attributed to a team without a breakdown are dropped; with
    ``return_excluded=True`` they come back as a second DataFrame.
    """
    breakdown = load_breakdown(conn, season)
    events = load_events(conn, season)
    if breakdown.empty or events.empty:
        empty = pd.DataFrame(columns=LEDGER_COLUMNS)
        return (empty, events.iloc[0:0]) if return_excluded else empty
    team_names = dict(zip(breakdown["team_slug"], breakdown["team_name"]))
    listing = load_listing(conn, season)
    team_names.update({t: n for t, n in zip(listing["team_slug"], listing["team_name"]) if n})
    bd = breakdown.assign(total=breakdown["points_counted"].fillna(0)
                          + breakdown["points_not_counted"].fillna(0))
    bd_teams = (bd.sort_values("total", ascending=False)
                .groupby("rider_slug")["team_slug"].apply(list).to_dict())
    spells = _load_spells(conn, season)
    known_teams = set(breakdown["team_slug"])

    teams = []
    for slug, day in zip(events["rider_slug"], events["date"]):
        if slug not in bd_teams:
            teams.append(None)
        elif spells.get(slug):
            teams.append(_spell_team(spells[slug], day))
        else:
            teams.append(bd_teams[slug][0])
    events = events.assign(team_slug=teams)
    keep = events["team_slug"].isin(known_teams)
    excluded = events[~keep].copy()
    df = events[keep].copy()
    multi = [s for s, t in bd_teams.items() if len(t) > 1 and not spells.get(s)]
    if multi:
        log.warning("%d riders in two breakdowns without spell data; attributed to their "
                    "bigger breakdown: %s", len(multi), ", ".join(sorted(multi)[:10]))

    names = _rider_names(conn, season, breakdown)
    nm = [names.get(s) or _fallback_name(s) for s in df["rider_slug"]]
    df["rider_name"] = [n[0] for n in nm]
    df["rider_short"] = [n[1] for n in nm]
    df["team_name"] = df["team_slug"].map(team_names)
    df["race_name"] = df["race_name"].map(clean_race_name)
    df["week_start"] = df["date"].map(week_start)
    df["rider_short"] = _disambiguate_shorts(df)
    df["position"] = df["position"].astype("Int64")
    df = df.sort_values(["date", "team_slug", "rider_slug", "result_label"]).reset_index(drop=True)
    df = df[LEDGER_COLUMNS]
    return (df, excluded) if return_excluded else df


def load_rider_ledger(conn: sqlite3.Connection, rider_slug: str, season: int = 2026) -> pd.DataFrame:
    """All of one rider's season points events, regardless of team (for 'rider alone').

    Same columns as :func:`load_points_ledger`; ``team_slug`` is the team he
    rode for on that date when known (spells / breakdown), else None.
    """
    events = load_events(conn, season, rider_slugs=[rider_slug])
    breakdown = load_breakdown(conn, season)
    mine = breakdown[breakdown["rider_slug"] == rider_slug]
    spells = _load_spells(conn, season).get(rider_slug, [])
    bd_team = None
    if not mine.empty:
        tot = mine["points_counted"].fillna(0) + mine["points_not_counted"].fillna(0)
        bd_team = mine.loc[tot.idxmax(), "team_slug"]
    team_names = dict(zip(breakdown["team_slug"], breakdown["team_name"]))
    listing = load_listing(conn, season)
    team_names.update({t: n for t, n in zip(listing["team_slug"], listing["team_name"]) if n})
    for s in spells:
        team_names.setdefault(s["team_slug"], s["team_name"])
    if events.empty:
        return pd.DataFrame(columns=LEDGER_COLUMNS)
    events = events.copy()
    events["team_slug"] = [(_spell_team(spells, d) if spells else bd_team) for d in events["date"]]
    names = _rider_names(conn, season, mine)
    name, short = names.get(rider_slug) or _fallback_name(rider_slug)
    events["rider_name"] = name
    events["rider_short"] = short
    events["team_name"] = events["team_slug"].map(team_names)
    events["race_name"] = events["race_name"].map(clean_race_name)
    events["week_start"] = events["date"].map(week_start)
    events["position"] = events["position"].astype("Int64")
    return events.sort_values(["date", "result_label"]).reset_index(drop=True)[LEDGER_COLUMNS]


def data_through(conn: sqlite3.Connection, season: int = 2026) -> Optional[str]:
    """Last result date in the data (any rider_uci_results row of the season, or a penalty)."""
    best = None
    for table in ("rider_uci_results", "rider_uci_penalties"):
        if _table_exists(conn, table):
            d = conn.execute(f"SELECT MAX(date) FROM {table} WHERE season = ?", (season,)).fetchone()[0]
            if d and (best is None or d > best):
                best = d
    return best


# ---------------------------------------------------------------------------
# weekly series
# ---------------------------------------------------------------------------

def _rank_desc(frame: pd.DataFrame) -> pd.DataFrame:
    """Per row (week): 1 + number of columns strictly ahead (ties share the better rank)."""
    return frame.rank(axis=1, method="min", ascending=False).astype(int)


def weekly_team_series(ledger: pd.DataFrame, teams: Optional[Sequence[str]] = None,
                       weeks: Optional[Sequence[str]] = None,
                       through: Optional[str] = None, best_n: int = BEST_N) -> dict:
    """Weekly gains / cumulative / rank / best-N cumulative per team.

    ``teams`` is the ranking universe (all teams of the official listing;
    teams without events get zeros); ``weeks`` defaults to the contiguous
    Mondays from the first scoring week to ``through`` (default: the last
    ledger date).  Returns {weeks, gains, cumulative, rank, best20} where the
    four tables are DataFrames indexed by week with one column per team.
    ``best20`` sums, each week, the season-to-date points of the team's
    ``best_n`` best riders counting only points attributed to that team.
    """
    teams = list(teams) if teams is not None else sorted(ledger["team_slug"].dropna().unique())
    if weeks is None:
        scoring = ledger[ledger["points"] != 0]
        if scoring.empty:
            weeks = []
        else:
            last = through or ledger["date"].max()
            weeks = week_axis(scoring["date"].min(), max(last, scoring["date"].max()))
    weeks = list(weeks)
    gains = (ledger.pivot_table(index="week_start", columns="team_slug", values="points",
                                aggfunc="sum", fill_value=0.0)
             .reindex(index=weeks, columns=teams, fill_value=0.0).fillna(0.0)
             if not ledger.empty else pd.DataFrame(0.0, index=weeks, columns=teams))
    gains = gains.astype(float)
    gains.index.name, gains.columns.name = "week_start", "team_slug"
    cumulative = gains.cumsum()
    rank = _rank_desc(cumulative) if teams and weeks else pd.DataFrame(index=weeks, columns=teams)
    best = pd.DataFrame(0.0, index=weeks, columns=teams)
    for team, g in ledger.groupby("team_slug"):
        if team not in best.columns:
            continue
        piv = (g.pivot_table(index="week_start", columns="rider_slug", values="points",
                             aggfunc="sum", fill_value=0.0)
               .reindex(index=weeks, fill_value=0.0).fillna(0.0).cumsum())
        seen = (g.pivot_table(index="week_start", columns="rider_slug", values="points",
                              aggfunc="size", fill_value=0)
                .reindex(index=weeks, columns=piv.columns, fill_value=0).fillna(0).cumsum())
        # A rider enters the team's list with his first points item (as on PCS's
        # breakdown, which lists only riders with points or sanctions): before
        # that he is not a "0" that would push a sanctioned team-mate's
        # negative net out of the best N.
        vals = np.where(seen.to_numpy() > 0, piv.to_numpy(), -np.inf)
        arr = -np.sort(-vals, axis=1)[:, :best_n]
        best[team] = np.where(np.isfinite(arr), arr, 0.0).sum(axis=1)
    best.index.name = "week_start"
    return {"weeks": weeks, "gains": gains, "cumulative": cumulative, "rank": rank, "best20": best}


def series_for(ledger: pd.DataFrame, weeks: Sequence[str]) -> tuple[pd.Series, pd.Series]:
    """(gains, cumulative) of one subject's ledger on a fixed week axis."""
    g = ledger.groupby("week_start")["points"].sum().reindex(list(weeks), fill_value=0.0).astype(float)
    return g, g.cumsum()


# ---------------------------------------------------------------------------
# jumps: contributors, details, labels
# ---------------------------------------------------------------------------

_CLASS_LABEL = {"points": "points", "kom": "KOM", "youth": "youth"}


def _stage_tag(label: str) -> str:
    """'Stage 6' -> '6', 'Stage 4 (ITT)' -> '4', 'Prologue' -> 'prologue'."""
    m = re.search(r"stage\s+(\d+[a-z]?)", label or "", re.I)
    if m:
        return m.group(1)
    if re.match(r"prologue", label or "", re.I):
        return "prologue"
    return (label or "").strip()


def _num_key(tag: str):
    m = re.match(r"(\d+)", tag)
    return (0, int(m.group(1)), tag) if m else (-1, 0, tag)


def _stage_list(tags: list[str]) -> str:
    """['4'] -> 'stage 4'; ['4', '15'] -> 'stages 4 & 15'; ['prologue'] -> 'prologue'."""
    tags = sorted(dict.fromkeys(tags), key=_num_key)
    if len(tags) == 1:
        return "prologue" if tags[0] == "prologue" else f"stage {tags[0]}"
    tags = ["P" if t == "prologue" else t for t in tags]
    return "stages " + (", ".join(tags[:-1]) + " & " + tags[-1])


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _is_ttt(label) -> bool:
    """'Stage 3 (TTT)' -> True: a team time trial, won by the team, not the rider."""
    return bool(re.search(r"\bTTT\b", str(label or "")))


def _summarise_group(rows: pd.DataFrame) -> dict:
    """Facts about one (rider, race) group of ledger rows (points-bearing rows).

    Team time trials (``'Stage 3 (TTT)'``) are kept apart in ``ttt`` as
    (position, stage tag): a TTT win is the team's, so it never counts as the
    rider's stage win (INEOS, Paris-Nice 2026 stage 3: seven riders share
    8.57 points each).
    """
    g = {"points": float(rows["points"].sum()), "oneday_pos": None, "gc_pos": None,
         "stage_pos": defaultdict(list), "class_pos": {}, "leader_days": 0, "penalty": 0.0,
         "other": [], "earlier_wins": [], "ttt": []}
    for r in rows.itertuples(index=False):
        pos = None if pd.isna(r.position) else int(r.position)
        kind = r.result_kind
        if kind == "oneday":
            g["oneday_pos"] = pos if g["oneday_pos"] is None else min(g["oneday_pos"], pos or 999)
        elif kind == "gc":
            g["gc_pos"] = pos
        elif kind == "stage" and _is_ttt(r.result_label):
            g["ttt"].append((pos, _stage_tag(r.result_label)))
        elif kind == "stage":
            g["stage_pos"][pos].append(_stage_tag(r.result_label))
        elif kind in _CLASS_LABEL:
            g["class_pos"][kind] = pos
        elif kind == "penalty":
            g["penalty"] += float(r.points)
        elif r.status == "leader" or str(r.result_label).startswith("Leader jersey"):
            g["leader_days"] += 1
        else:
            g["other"].append((r.result_label or "", pos))
    return g


def _detail(g: dict) -> str:
    """Compact description: 'GC 1st, stages 4 & 15 1st, KOM 2nd, leader 9 days, penalty -25'."""
    parts = []
    if g["oneday_pos"] is not None:
        parts.append(ordinal(g["oneday_pos"]))
    if g["gc_pos"] is not None:
        parts.append(f"GC {ordinal(g['gc_pos'])}")
    stage_pos = g["stage_pos"]
    for pos in (1, 2, 3):
        tags = stage_pos.get(pos) or []
        if not tags:
            continue
        if pos == 1 and len(set(tags)) >= 4:
            parts.append(f"{len(set(tags))} stage wins")
        else:
            parts.append(f"{_stage_list(tags)} {ordinal(pos)}")
    rest = sum(len(t) for p, t in stage_pos.items() if p is None or p > 3)
    if rest == 1:
        p, t = next((p, t) for p, t in stage_pos.items() if (p is None or p > 3) and t)
        parts.append(f"{_stage_list(t)} {ordinal(p)}".strip())
    elif rest:
        parts.append(_plural(rest, "other stage"))
    for pos, tag in sorted(g.get("ttt") or [], key=lambda t: (t[0] is None, t[0] or 0)):
        where = f"stage {tag} " if tag and tag[:1].isdigit() else ""
        parts.append(f"{where}TTT {ordinal(pos)}".strip())
    for kind in ("points", "kom", "youth"):
        if kind in g["class_pos"]:
            parts.append(f"{_CLASS_LABEL[kind]} {ordinal(g['class_pos'][kind])}".strip())
    if g["leader_days"]:
        parts.append(f"leader {_plural(g['leader_days'], 'day')}")
    for label, pos in g["other"]:
        parts.append(f"{label} {ordinal(pos)}".strip())
    if g["penalty"]:
        parts.append(f"penalty {g['penalty']:g}")
    text = ", ".join(parts)
    earlier = sorted(set(g.get("earlier_wins") or []) - set(stage_pos.get(1) or []), key=_num_key)
    if earlier:
        text += f"; also won {_stage_list(earlier)} earlier"
    return text


def _ttt_win(g: dict) -> bool:
    return any(p == 1 for p, _ in g.get("ttt") or [])


def _win_tags(g: dict) -> list[str]:
    """Stage wins of the group plus, for a race's final week, the earlier ones
    (team time trials excluded: see :func:`_summarise_group`)."""
    return sorted(set(g["stage_pos"].get(1) or []) | set(g.get("earlier_wins") or []), key=_num_key)


def _wins(g: dict) -> int:
    return len(_win_tags(g))


def _is_win(g: dict) -> bool:
    """A one-day win or a GC win with nothing else notable to add."""
    return g["oneday_pos"] == 1 or (g["gc_pos"] == 1 and not _wins(g)
                                    and not any(p == 1 for p in g["class_pos"].values()))


def _class_firsts(g: dict) -> list[str]:
    return [k for k in ("points", "kom", "youth") if g["class_pos"].get(k) == 1]


def _headline_gc(g: dict) -> Optional[int]:
    """The GC place worth a headline: outside the top 10 it is dropped when the
    rider also won a stage or a classification ('stage 21 win', not 'GC 26th')."""
    gc = g["gc_pos"]
    if gc is not None and gc > 10 and (_wins(g) or _class_firsts(g)):
        return None
    return gc


def _group_phrase(g: dict, race: str, who: Optional[str]) -> str:
    """Label for a week dominated by one (rider, race) group.

    ``who`` is the rider's surname for a team chart ('Pogačar wins Ronde van
    Vlaanderen', 'Vingegaard: Tour de France GC 2nd + 2 stage wins'), or None
    for a rider-alone series ('Wins Ronde van Vlaanderen', 'Tour de France:
    GC 1st + 4 stage wins').
    """
    def subj(verb_phrase: str) -> str:
        return f"{who} {verb_phrase}" if who else _sentence_case(verb_phrase)

    def colon(rest: str) -> str:
        return f"{who}: {rest}" if who else _sentence_case(rest)

    if g["oneday_pos"] is not None and g["gc_pos"] is None:
        if g["oneday_pos"] == 1:
            return subj(f"wins {race}")
        return subj(f"{ordinal(g['oneday_pos'])} at {race}")
    wins = _wins(g)
    gc = _headline_gc(g)
    parts = []
    if gc is not None:
        parts.append(f"GC {ordinal(gc)}")
    if wins:
        parts.append("1 stage win" if wins == 1 else f"{wins} stage wins")
    parts += [f"{_CLASS_LABEL[k]} 1st" for k in _class_firsts(g)]
    if gc is not None and len(parts) == 1:
        if gc == 1:
            return subj(f"wins {race}")
        return subj(f"{ordinal(gc)} at {race}")
    if gc is None and wins and len(parts) == 1:
        if wins == 1:
            tag = _stage_list(_win_tags(g))
            return colon(f"{tag} win at {race}")
        return colon(f"{wins} stage wins at {race}")
    if parts:
        return colon(f"{race} {' + '.join(parts)}") if who else f"{race}: {' + '.join(parts)}"
    if _ttt_win(g):
        return colon(f"{race} TTT win")
    # no win, no GC: best stage / classification placing, or leader-jersey days
    placed = sorted((p, t) for p, t in g["stage_pos"].items() if p is not None)
    if placed:
        p, tags = placed[0]
        return colon(f"{race} {_stage_list(tags)} {ordinal(p)}")
    if g["leader_days"] and not placed:
        return colon(f"{race} leader jersey")
    return f"{who} at {race}" if who else race


def _short_result(g: dict, who: str) -> str:
    """'Kuss GC 5th', 'McNulty 1st', 'Brennan 2 stage wins', 'Küng stage 18 win',
    'Van Aert points 1st', 'Meeus stage 17 2nd' — one rider's result in a race."""
    wins = _wins(g)
    if g["oneday_pos"] is not None and g["gc_pos"] is None:
        return f"{who} {ordinal(g['oneday_pos'])}"
    pieces = []
    gc = _headline_gc(g)
    if gc is not None:
        pieces.append(f"GC {ordinal(gc)}")
    if wins:
        pieces.append(f"{_stage_list(_win_tags(g))} win" if wins == 1 else f"{wins} stage wins")
    pieces += [f"{_CLASS_LABEL[k]} 1st" for k in _class_firsts(g)]
    if pieces:
        return f"{who} " + " & ".join(pieces[:2])
    if _ttt_win(g):
        return f"{who} TTT win"
    placed = sorted((p, t) for p, t in g["stage_pos"].items() if p is not None)
    if placed:
        p, tags = placed[0]
        return f"{who} {_stage_list(tags)} {ordinal(p)}"
    if g["leader_days"]:
        return f"{who} leader {_plural(g['leader_days'], 'day')}"
    return who


def _race_key(row) -> str:
    return row.race_slug if isinstance(row.race_slug, str) and row.race_slug else "name:" + row.race_name


_FINAL_KINDS = {"gc", "points", "kom", "youth"}


def _earlier_wins(history: Optional[pd.DataFrame]) -> dict[tuple[str, str], list[str]]:
    """{(rider_slug, race key): stage tags won} from earlier weeks' ledger rows."""
    if history is None or history.empty:
        return {}
    won = history[(history["result_kind"] == "stage") & (history["position"] == 1)
                  & ~history["result_label"].map(_is_ttt)]
    out: dict[tuple[str, str], list[str]] = defaultdict(list)
    for r in won.itertuples(index=False):
        out[(r.rider_slug, _race_key(r))].append(_stage_tag(r.result_label))
    return out


def contributors_for(rows: pd.DataFrame, gain: float, max_n: int = 4,
                     cover: float = 0.75,
                     history: Optional[pd.DataFrame] = None) -> tuple[list[dict], list[dict]]:
    """Group a week's rows by (rider, race); return (contributors, all groups).

    Contributors are sorted by points (desc) and taken until they cover
    ``cover`` of ``gain`` (at most ``max_n``).  Each: {rider, race, detail,
    points}.  The groups carry the facts used for the label.

    ``history`` (the same subject's ledger rows of EARLIER weeks) lets a stage
    race's final week speak for the whole race: a group holding a final
    classification (GC / points / KOM / youth) counts the stage wins of the
    earlier weeks too, so the label reads 'Tour de France GC 1st + 5 stage
    wins' rather than only the last week's one win; the detail lists them as
    'also won stages 3, 6 & 10 earlier'.  Points stay this week's.
    """
    groups = []
    earlier = _earlier_wins(history)
    rows = rows.assign(_race_key=[_race_key(r) for r in rows.itertuples(index=False)])
    for (slug, rkey), g in rows.groupby(["rider_slug", "_race_key"], sort=False):
        facts = _summarise_group(g)
        if earlier and set(g["result_kind"]) & _FINAL_KINDS:
            facts["earlier_wins"] = list(earlier.get((slug, rkey), []))
        groups.append({
            "rider_slug": slug, "rider": g["rider_name"].iloc[0], "short": g["rider_short"].iloc[0],
            "race_key": rkey, "race": g["race_name"].iloc[0], "facts": facts,
            "points": facts["points"], "detail": _detail(facts),
        })
    groups.sort(key=lambda x: (-x["points"], x["rider"]))
    out, acc = [], 0.0
    for grp in groups:
        if len(out) >= max_n or (gain > 0 and acc >= cover * gain):
            break
        if grp["points"] <= 0 and out:
            break
        out.append({"rider": grp["rider"], "race": grp["race"], "detail": grp["detail"],
                    "points": round(grp["points"], 2)})
        acc += grp["points"]
    return out, groups


def _same_rider_phrase(group: dict) -> str:
    """A second result of the rider already named: '2nd at Tre Valli Varesine',
    'wins Coppa Agostoni', 'stage 3 win at Tour of Guangxi', 'Giro d'Italia GC 4th'."""
    text = _group_phrase(group["facts"], group["race"], None)
    if re.match(r"(?:Wins|Stages?|Prologue) ", text) or re.match(r"\d+ stage wins? at ", text):
        text = text[0].lower() + text[1:]
    return text


def _week_win(g: dict) -> bool:
    """The group holds a win of this week: a one-day race, a GC, or a
    (non-TTT) stage — earlier weeks' stage wins do not count."""
    return g["oneday_pos"] == 1 or g["gc_pos"] == 1 or bool(g["stage_pos"].get(1))


def _more_suffix(unnamed: list[dict], named_races: set[str], by_rider: bool = False,
                 named_riders: frozenset[str] = frozenset()) -> str:
    """' + 3 more races' (results in races the label does not name) or
    ' + 2 more riders' (only further riders in the named races, or
    ``by_rider``: a label that opens with its race counts riders).

    Riders counted as 'more' are the ones the label does not name at all
    (``named_riders``): a named rider's further minor result is not another
    rider.  When only such results are left the tail reads ' + N more
    results'."""
    races = {g["race_key"] for g in unnamed} - named_races
    if races and not by_rider:
        return f" + {len(races)} more {'race' if len(races) == 1 else 'races'}"
    riders = {g["rider_slug"] for g in unnamed} - set(named_riders)
    if riders:
        return f" + {len(riders)} more {'rider' if len(riders) == 1 else 'riders'}"
    if unnamed:
        return f" + {len(unnamed)} more {'result' if len(unnamed) == 1 else 'results'}"
    return ""


def jump_label(groups: list[dict], gain: float, subject: str = "team",
               dominant: float = 0.6, leading: float = 0.4, second_share: float = 0.1,
               win_share: float = 0.1, max_extra: int = 1, max_len: int = 125) -> str:
    """Headline for a jump week: which riders and results caused it.

    ``groups`` are (rider, race) groups from :func:`contributors_for`, biggest
    first.  The base label, in order:

    * one group >= ``dominant`` of the gain -> its phrase ('Pogačar wins Ronde
      van Vlaanderen', 'Vingegaard: Tour de France GC 2nd + 2 stage wins');
    * one race >= ``dominant`` -> '<Race>: <rider result>[, <rider result>]'.
      A second rider of that race is named only when his share is at least
      ``second_share`` of the gain and no result outside the race is bigger;
      otherwise the biggest result outside the race follows after '; '
      ('Paris-Nice: Vingegaard GC 1st & 2 stage wins; Jorgenson 2nd at
      Tirreno-Adriatico', not '..., Armirail GC 19th');
    * one rider >= ``dominant`` across races -> '<Surname> wins A + B' /
      '<Surname> wins A & 2nd at B';
    * otherwise riders ranked by their week total, all races merged (a rider
      with two wins outranks one bigger single placing): the first rider's
      result(s), then the second's ('Barrenetxea wins Tour du Finistère +
      Boucles de l'Aulne; Milesi: Giro d'Italia stage 5 4th').

    Then every win of the week (one-day race, GC, non-TTT stage) worth at
    least ``win_share`` of the gain that the label does not name yet is
    appended (at most ``max_extra``, and only while the label stays within
    ``max_len`` characters): 'Philipsen: stage 17 win at Tour de
    France; van der Poel stage 21 win', 'T. Pidcock 2nd at Milano-Sanremo &
    wins Milano-Torino'.  Such a win chains with ' & ' only when its rider's
    phrase ends the label; after another rider's phrase it carries the
    surname again ('Hungary nationals: Takács 1st; Peák wins Hungary
    nationals ITT; Takács wins Hungary U23 nationals ITT', never '... Peák
    wins ... & wins Hungary U23 nationals ITT').  A rider's second result is
    named only when it is notable (a win or podium worth ``win_share`` of the
    gain, or any result worth twice that).  A '+ N more races' / '+ N more
    riders' tail counts what is left unnamed (not when one rider dominates
    the week; riders the label names do not count, see :func:`_more_suffix`).

    ``subject='rider'`` (a single rider's own series) drops the surname
    ('Wins Ronde van Vlaanderen', 'Tour de France: GC 1st + 4 stage wins') and,
    when its top race holds >= ``leading`` of the gain, reads '<phrase>;
    <race> (+ N more)', else '<race> + <race> (+ N more)'.
    """
    rider_mode = subject == "rider"
    pos = [g for g in groups if g["points"] > 0]
    if not pos or gain <= 0:
        return ""
    top = pos[0]
    who_top = None if rider_mode else _sentence_case(top["short"])

    if rider_mode:
        return _rider_alone_label(pos, gain, dominant, leading, win_share, max_extra, max_len)

    by_race: dict[str, dict] = {}
    for g in pos:
        r = by_race.setdefault(g["race_key"], {"key": g["race_key"], "race": g["race"], "points": 0.0})
        r["points"] += g["points"]
    races = sorted(by_race.values(), key=lambda r: -r["points"])
    by_rider: dict[str, list[dict]] = defaultdict(list)
    for g in pos:
        by_rider[g["rider_slug"]].append(g)
    riders = sorted(by_rider.values(), key=lambda gs: (-sum(g["points"] for g in gs), gs[0]["rider"]))

    named: list[dict] = []          # groups the label names, in order

    def notable(g: dict) -> bool:
        """Worth a second phrase for the same rider: a win or podium worth
        ``win_share`` of the gain, or any result worth twice that."""
        f = g["facts"]
        podium = _week_win(f) or (f["oneday_pos"] or 99) <= 3 or (f["gc_pos"] or 99) <= 3
        return g["points"] >= (win_share if podium else 2 * win_share) * gain

    def rider_phrase(gs: list[dict], second: bool = True, first: bool = True) -> str:
        """One rider's week: two wins, a result & a second notable one
        (``second``), or one result.  ``first``: it opens the label (capital)."""
        gs = sorted(gs, key=lambda g: (not _is_win(g["facts"]), -g["points"]))
        who = _sentence_case(gs[0]["short"]) if first else gs[0]["short"]
        if len(gs) > 1 and all(_is_win(g["facts"]) for g in gs[:2]):
            named.extend(gs[:2])
            return f"{who} wins " + " + ".join(g["race"] for g in gs[:2])
        if second and len(gs) > 1 and notable(gs[1]):
            named.extend(gs[:2])
            return f"{_group_phrase(gs[0]['facts'], gs[0]['race'], who)} & {_same_rider_phrase(gs[1])}"
        best = max(gs, key=lambda g: g["points"])
        named.append(best)
        return _group_phrase(best["facts"], best["race"], who)

    single = top["points"] >= dominant * gain
    prefix_race = None              # race named as '<Race>: ...' at the start
    # ``last``: the rider whose phrase ends the label.  Only his further wins
    # may follow as ' & wins X' — after another rider's phrase that clause
    # would read as that rider's ('...; Peák wins A & wins B' when B was
    # Takács's), so a named rider whose phrase is not last gets his surname.
    if single:
        named.append(top)
        label = _group_phrase(top["facts"], top["race"], who_top)
        last = top["rider_slug"]
    elif races[0]["points"] >= dominant * gain:
        in_race = [g for g in pos if g["race_key"] == races[0]["key"]]   # points order
        outside = [g for g in pos if g["race_key"] != races[0]["key"]]
        prefix_race = races[0]["key"]
        named.append(in_race[0])
        last = in_race[0]["rider_slug"]
        parts = [_short_result(in_race[0]["facts"], in_race[0]["short"])]
        second = in_race[1] if len(in_race) > 1 else None
        best_out = outside[0] if outside else None
        if second is not None and second["points"] >= second_share * gain and \
                (best_out is None or second["points"] >= best_out["points"]):
            named.append(second)
            last = second["rider_slug"]
            parts.append(_short_result(second["facts"], second["short"]))
        label = f"{races[0]['race']}: {', '.join(parts)}"
        if best_out is not None and best_out["points"] >= second_share * gain and \
                (second is None or not any(second is n for n in named)):
            if best_out["rider_slug"] == last:
                label += " & " + _same_rider_phrase(best_out)
            else:
                label += "; " + _group_phrase(best_out["facts"], best_out["race"], best_out["short"])
            named.append(best_out)
            last = best_out["rider_slug"]
    elif sum(g["points"] for g in riders[0]) >= dominant * gain and len(riders[0]) > 1:
        single = True
        label = rider_phrase(riders[0])
        last = riders[0][0]["rider_slug"]
    else:
        label = rider_phrase(riders[0])
        last = riders[0][0]["rider_slug"]
        if len(riders) > 1:
            label += "; " + rider_phrase(riders[1], second=False, first=False)
            last = riders[1][0]["rider_slug"]

    # wins of the week the label has not named yet
    extra = 0
    for g in pos:
        if extra >= max_extra:
            break
        if any(g is n for n in named) or not _week_win(g["facts"]) or g["points"] < win_share * gain:
            continue
        if g["rider_slug"] == last:
            add = f" & {_same_rider_phrase(g)}"
        elif g["race_key"] in (prefix_race, top["race_key"] if single else None):
            # the race is already in the label: 'Paris-Nice: ...; Godon stage 7 win'
            add = f"; {_short_result(g['facts'], g['short'])}"
        else:
            add = "; " + _group_phrase(g["facts"], g["race"], g["short"])
        if len(label + add) > max_len:
            continue
        label += add
        named.append(g)
        last = g["rider_slug"]
        extra += 1
    if single:  # the week is one rider's result(s): no '+ N more' tail
        return label
    unnamed = [g for g in pos if not any(g is n for n in named)]
    return label + _more_suffix(unnamed, {n["race_key"] for n in named}, by_rider=prefix_race is not None,
                                named_riders=frozenset(n["rider_slug"] for n in named))


def _rider_alone_label(pos: list[dict], gain: float, dominant: float, leading: float,
                       win_share: float, max_extra: int, max_len: int) -> str:
    """:func:`jump_label` for one rider's own series (no surname)."""
    top = pos[0]
    if top["points"] >= dominant * gain:
        label, named = _group_phrase(top["facts"], top["race"], None), [top]
    else:
        ordered = sorted(pos, key=lambda g: -g["points"])
        if len(ordered) >= 2 and all(_is_win(g["facts"]) for g in ordered[:2]):
            label, named = "Wins " + " + ".join(g["race"] for g in ordered[:2]), ordered[:2]
        elif top["points"] >= leading * gain and len(ordered) > 1:
            label, named = f"{_group_phrase(top['facts'], top['race'], None)}; {ordered[1]['race']}", ordered[:2]
        else:
            label, named = " + ".join(g["race"] for g in ordered[:2]), ordered[:2]
    extra = 0
    for g in pos:
        if extra >= max_extra:
            break
        if any(g is n for n in named) or not _week_win(g["facts"]) or g["points"] < win_share * gain:
            continue
        if len(label) + len(_same_rider_phrase(g)) + 3 > max_len:
            continue
        label += f" & {_same_rider_phrase(g)}"
        named.append(g)
        extra += 1
    races = {g["race_key"] for g in pos} - {n["race_key"] for n in named}
    return label + (f" + {len(races)} more" if races else "")


# Detection thresholds, tuned on the 2026 data (see detect_jumps): teams get
# 3-6 gain-based annotations (mostly 3-5) plus the weeks of their headline
# wins, up to ``max_with_wins``; a single rider's season is spikier, so the
# "typical week" multiplier is lower and the share of the season higher.
TEAM_JUMP_PARAMS = {"min_abs": 200.0, "rel": 0.05, "typical_mult": 2.0, "max_jumps": 6,
                    "min_jumps": 3, "fallback_rel": 0.04, "max_with_wins": 10}
RIDER_JUMP_PARAMS = {"min_abs": 200.0, "rel": 0.06, "typical_mult": 1.0, "max_jumps": 6,
                     "min_jumps": 2, "fallback_rel": 0.04, "max_with_wins": 10}

# Headline wins (see headline_tier), compared on _norm_key(clean race name).
MONUMENTS = {"milanosanremo", "rondevanvlaanderen", "parisroubaix", "liegebastogneliege", "illombardia"}
GRAND_TOURS = {"tourdefrance", "giroditalia", "vueltaaespana"}


def headline_tier(race_name: str, race_class: Optional[str], kind: str, position,
                  label: str = "", status: str = "") -> int:
    """1 for a major win (a Monument, an elite World title, a Grand Tour GC),
    2 for a WorldTour win (a 1.UWT one-day race, a 2.UWT GC), else 0.

    Week annotations are chosen by gain, so a team's biggest wins could go
    unmentioned when bigger weeks without a win crowd them out (Seixas,
    Il Lombardia 2026: Decathlon's 1,179-point week lost to a 1,182-point
    week of placings); :func:`detect_jumps` always keeps tier-1 weeks and
    adds tier-2 weeks up to its ``max_with_wins`` cap.
    """
    if position is None or pd.isna(position) or int(position) != 1 or status == "leader" \
            or _is_ttt(label):
        return 0
    key = _norm_key(race_name or "")
    cls = (race_class or "").upper()
    if kind == "oneday":
        if key in MONUMENTS:
            return 1
        if key.startswith("worldchampionships") and not re.search(r"u23|junior|mixedrelay|team", key):
            return 1
        return 2 if cls == "1.UWT" else 0
    if kind == "gc":
        if key in GRAND_TOURS:
            return 1
        return 2 if cls == "2.UWT" else 0
    return 0


def _headline_weeks(ledger: pd.DataFrame) -> dict[str, int]:
    """{week_start: best headline tier of the subject's wins that week}."""
    out: dict[str, int] = {}
    if ledger is None or ledger.empty:
        return out
    for r in ledger.itertuples(index=False):
        if not r.points or r.points <= 0:
            continue
        t = headline_tier(r.race_name, r.race_class, r.result_kind, r.position, r.result_label, r.status)
        if t:
            out[r.week_start] = min(out.get(r.week_start, 9), t)
    return out


def detect_jumps(gains: pd.Series, ledger: pd.DataFrame, final_total: float,
                 subject: str = "team", min_abs: Optional[float] = None,
                 rel: Optional[float] = None, typical_mult: Optional[float] = None,
                 max_jumps: Optional[int] = None, min_jumps: Optional[int] = None,
                 fallback_rel: Optional[float] = None,
                 max_with_wins: Optional[int] = None) -> list[dict]:
    """The weeks that moved a team (or a rider) most, and its headline wins.

    A week is a jump when its gain >= max(``min_abs``, ``rel`` x the final
    total, ``typical_mult`` x the median non-zero weekly gain); the largest
    ``max_jumps`` are kept.  Every week holding a tier-1 headline win (a
    Monument, an elite World title, a Grand Tour GC: :func:`headline_tier`)
    is added, then tier-2 weeks (a WorldTour one-day win or stage-race GC) by
    gain while fewer than ``max_with_wins`` are chosen.  When fewer than
    ``min_jumps`` qualify, the next largest weeks with gain >= max(``min_abs``,
    ``fallback_rel`` x the final total) fill up to ``min_jumps``.  Unset
    parameters come from :data:`TEAM_JUMP_PARAMS` / :data:`RIDER_JUMP_PARAMS`
    by ``subject``.  Returned chronologically, each: {week, week_index, gain,
    share_of_season, label, contributors}.
    """
    params = dict(RIDER_JUMP_PARAMS if subject == "rider" else TEAM_JUMP_PARAMS)
    for k, v in (("min_abs", min_abs), ("rel", rel), ("typical_mult", typical_mult),
                 ("max_jumps", max_jumps), ("min_jumps", min_jumps), ("fallback_rel", fallback_rel),
                 ("max_with_wins", max_with_wins)):
        if v is not None:
            params[k] = v
    min_abs, rel, typical_mult = params["min_abs"], params["rel"], params["typical_mult"]
    max_jumps, min_jumps, fallback_rel = (int(params["max_jumps"]), int(params["min_jumps"]),
                                          params["fallback_rel"])
    gains = gains.astype(float)
    weeks = list(gains.index)
    positive = gains[gains > 0]
    if positive.empty:
        return []
    typical = float(positive.median())
    total = max(float(final_total or 0.0), 0.0)
    thr = max(min_abs, rel * total, typical_mult * typical)
    ranked = positive.sort_values(ascending=False, kind="mergesort")
    chosen = list(ranked[ranked >= thr].index[:max_jumps])
    tiers = _headline_weeks(ledger)
    cap = max(int(params.get("max_with_wins") or max_jumps), max_jumps)
    for tier in (1, 2):
        for wk in ranked.index:
            if tiers.get(wk) == tier and wk not in chosen and (tier == 1 or len(chosen) < cap):
                chosen.append(wk)
    if len(chosen) < min_jumps:
        floor = max(min_abs, fallback_rel * total)
        for wk in ranked.index:
            if len(chosen) >= min_jumps:
                break
            if wk not in chosen and ranked[wk] >= floor:
                chosen.append(wk)
    out = []
    for wk in sorted(chosen):
        gain = float(gains[wk])
        rows = ledger[ledger["week_start"] == wk]
        contributors, groups = contributors_for(rows, gain, history=ledger[ledger["week_start"] < wk])
        out.append({
            "week": wk, "week_index": weeks.index(wk), "gain": round(gain, 2),
            "share_of_season": round(gain / total, 4) if total else 0.0,
            "label": jump_label(groups, gain, subject=subject),
            "contributors": contributors,
        })
    return out


# ---------------------------------------------------------------------------
# reconciliation
# ---------------------------------------------------------------------------

def _best_n(totals: pd.DataFrame, best_n: int = BEST_N) -> dict[str, float]:
    """{team: sum of its ``best_n`` best rider totals} from rows (team_slug, rider_slug, points)."""
    if totals.empty:
        return {}
    top = (totals.sort_values(["team_slug", "points"], ascending=[True, False])
           .groupby("team_slug").head(best_n))
    return top.groupby("team_slug")["points"].sum().to_dict()


def _best_n_asof(ledger: pd.DataFrame, day: str, best_n: int = BEST_N) -> dict[str, float]:
    sub = ledger[ledger["date"] <= day]
    return _best_n(sub.groupby(["team_slug", "rider_slug"], as_index=False)["points"].sum(), best_n)


def listing_snapshot(ledger: pd.DataFrame, listing: pd.DataFrame, through: Optional[str],
                     days: int = 21, tol: float = RECONCILE_TOL, max_races: int = 8) -> dict:
    """Reproduce the official listing from the ledger to explain how far it lags.

    PCS recomputes the listing (``rankings/me/ranking-2026-2028``) in
    batches, so on a race day it can include some of that day's races but not
    others, while the per-team breakdown pages already include everything.
    For each of the last ``days`` days ``d`` we check how many teams' listing
    totals fall between our best-20 ledger before ``d`` and through ``d``;
    for the best such day every subset of its races (the ``max_races``
    biggest) is tried.  Returns {date, included, excluded, teams_matched,
    teams, matched_full_day}: the listing equals results before ``date`` plus
    ``included`` races of ``date`` for ``teams_matched`` teams (within ``tol``).
    """
    out = {"date": None, "included": [], "excluded": [], "teams_matched": 0,
           "teams": int(len(listing)), "matched_full_day": 0}
    if ledger.empty or listing.empty or not through:
        return out
    official = {t: float(p or 0.0) for t, p in zip(listing["team_slug"], listing["points"])}

    def score(best: dict) -> int:
        return sum(abs(best.get(t, 0.0) - p) <= tol for t, p in official.items())

    per_day = ledger.groupby(["date", "team_slug", "rider_slug"], as_index=False)["points"].sum()
    end = date.fromisoformat(through)
    best_day, best_bracket, cache = None, -1, {}
    for k in range(days + 1):
        d = (end - timedelta(days=k)).isoformat()
        before = per_day[per_day["date"] < d].groupby(["team_slug", "rider_slug"], as_index=False)["points"].sum()
        upto = per_day[per_day["date"] <= d].groupby(["team_slug", "rider_slug"], as_index=False)["points"].sum()
        b0, b1 = _best_n(before), _best_n(upto)
        cache[d] = (before, b0, b1)
        bracket = sum(min(b0.get(t, 0.0), b1.get(t, 0.0)) - tol <= p <= max(b0.get(t, 0.0), b1.get(t, 0.0)) + tol
                      for t, p in official.items())
        if bracket > best_bracket:
            best_day, best_bracket = d, bracket
    before, b0, b1 = cache[best_day]
    day_rows = ledger[ledger["date"] == best_day]
    races = (day_rows.groupby("race_name")["points"].apply(lambda s: s.abs().sum())
             .sort_values(ascending=False).index.tolist())
    cand, fixed = races[:max_races], races[max_races:]
    best_sub, best_score = [], score(b0)
    per_race = {r: g.groupby(["team_slug", "rider_slug"], as_index=False)["points"].sum()
                for r, g in day_rows.groupby("race_name")}
    for mask in range(1, 1 << len(cand)):
        sub = [r for i, r in enumerate(cand) if mask >> i & 1]
        tot = pd.concat([before] + [per_race[r] for r in sub], ignore_index=True)
        tot = tot.groupby(["team_slug", "rider_slug"], as_index=False)["points"].sum()
        sc = score(_best_n(tot))
        if sc > best_score or (sc == best_score and len(sub) > len(best_sub)):
            best_sub, best_score = sub, sc
    out.update({"date": best_day, "included": best_sub,
                "excluded": [r for r in races if r not in best_sub],
                "teams_matched": int(best_score), "matched_full_day": int(score(b1)),
                "matched_day_before": int(score(b0))})
    if fixed:
        log.info("listing_snapshot: %d small races of %s not searched: %s", len(fixed), best_day, fixed)
    return out


# ---------------------------------------------------------------------------
# payload
# ---------------------------------------------------------------------------

def _r(x) -> float:
    v = float(x)
    if not np.isfinite(v):
        return 0.0
    v = round(v, 2)
    return 0.0 if v == 0 else v


def _rank_among(value: float, others: Iterable[float]) -> int:
    """1 + number of ``others`` strictly greater (ties -> the better rank)."""
    return 1 + sum(1 for o in others if o > value + 1e-9)


def weeks_meta(ledger: pd.DataFrame, weeks: Sequence[str], n_races: int = 3) -> list[dict]:
    """Per week: the races that brought the most points to the charted teams.

    ``ledger`` is the charted teams' ledger (every rider, not only the ones
    behind annotated jumps); races are summed by cleaned name, sanctions
    excluded.  Each entry: {week, top_race, top_race_points, races: [{race,
    points}] (the ``n_races`` biggest)}; ``top_race`` is None in an empty week.
    """
    pos = ledger[(ledger["result_kind"] != "penalty") & (ledger["points"] > 0)]
    per = pos.groupby(["week_start", "race_name"])["points"].sum()
    out = []
    for wk in weeks:
        races = per.loc[wk].sort_values(ascending=False, kind="mergesort") if wk in per.index.get_level_values(0) \
            else pd.Series(dtype=float)
        top = [{"race": r, "points": _r(p)} for r, p in races.head(n_races).items()]
        out.append({"week": wk, "top_race": top[0]["race"] if top else None,
                    "top_race_points": top[0]["points"] if top else 0.0, "races": top})
    return out


def axis_start(weeks: Sequence[str], gains: pd.DataFrame, finals: dict, season: int,
               share: float = 0.02) -> Optional[str]:
    """First week the chart should show: the week before the first week of
    ``season`` in which some charted team gains ``share`` of its final total.

    The UCI season opens in late October of the previous year, but those
    weeks hold a few hundred points (a quarter of the axis, flat); the chart
    starts here instead and each line opens at its value of that week, which
    carries the earlier points.  None when the axis needs no trimming.
    """
    if not weeks:
        return None
    jan = week_start(f"{season}-01-01")
    for i, wk in enumerate(weeks):
        if wk < jan:
            continue
        row = gains.loc[wk]
        if any(finals.get(t, 0) > 0 and row.get(t, 0.0) >= share * finals[t] for t in gains.columns):
            return weeks[max(i - 1, 0)] if i > 0 and weeks[0] < jan else None
    return None


def _top_riders(ledger: pd.DataFrame, n: int = 5) -> list[dict]:
    if ledger.empty:
        return []
    per = ledger.groupby(["rider_slug", "rider_name"])["points"].sum().sort_values(ascending=False)
    return [{"rider": name, "points": _r(p)} for (slug, name), p in per.head(n).items()]


def build_chart_payload(conn: sqlite3.Connection, season: int = 2026, top_n: int = 30,
                        generated_at: Optional[str] = None,
                        riders: Sequence[str] = DEFAULT_RIDERS) -> dict:
    """The chart payload (see the module docstring and the data contract).

    Top ``top_n`` teams by the official listing (latest ``ranking_date`` of
    ``rankings/me/ranking-<cycle>``).  Weekly ranks are among all listing
    teams.  ``riders`` adds single-rider series (the user's "Pogačar alone"
    comparison) to ``payload['riders']``: all of the rider's season points
    minus sanctions, whichever team he rode for.
    """
    listing = load_listing(conn, season)
    if listing.empty:
        raise ValueError(f"no official team listing for {season} in uci_team_totals")
    ledger, excluded = load_points_ledger(conn, season, return_excluded=True)
    breakdown = load_breakdown(conn, season)
    through = data_through(conn, season)
    all_teams = list(listing["team_slug"])
    rider_ledgers = {slug: load_rider_ledger(conn, slug, season) for slug in riders}
    dates = [d for d in [ledger["date"].min() if not ledger.empty else None]
             + [rl["date"].min() for rl in rider_ledgers.values() if not rl.empty] if d]
    last = max([d for d in [through, ledger["date"].max() if not ledger.empty else None] if d],
               default=None)
    weeks = week_axis(min(dates), last) if dates and last else []
    series = weekly_team_series(ledger, teams=all_teams, weeks=weeks, through=last)
    cum, gains, rank, best = series["cumulative"], series["gains"], series["rank"], series["best20"]
    finals = {t: float(cum[t].iloc[-1]) if weeks else 0.0 for t in all_teams}

    # PCS's own team page total when scraped (it counts negative nets that the
    # page prints as '-'), else the sum of the riders' 'points counted'
    bd_counted = breakdown.groupby("team_slug")["points_counted"].sum(min_count=1).to_dict()
    bd_page = load_breakdown_page_totals(conn, season)
    bd_tot = {**bd_counted, **bd_page}
    bd_all = (breakdown.assign(t=breakdown["points_counted"].fillna(0)
                               + breakdown["points_not_counted"].fillna(0))
              .groupby("team_slug")["t"].sum().to_dict())
    top = listing.sort_values("rank").head(top_n)
    team_entries = []
    diffs = []
    for row in top.itertuples(index=False):
        t = row.team_slug
        tl = ledger[ledger["team_slug"] == t]
        final = finals.get(t, 0.0)
        bt = bd_tot.get(t)
        bt = None if bt is None or pd.isna(bt) else float(bt)
        final_b20 = float(best[t].iloc[-1]) if weeks else 0.0
        if bt is not None:
            diffs.append((t, final_b20 - bt))
        team_entries.append({
            "key": t, "name": row.team_name, "short": team_short_name(row.team_name, t),
            "class": row.team_class, "official_rank": int(row.rank),
            "official_total": _r(row.points or 0.0),
            "breakdown_total": _r(bt) if bt is not None else None,
            "final_total": _r(final),
            "final_rank": _rank_among(final, [finals[o] for o in all_teams if o != t]),
            "cumulative": [_r(v) for v in cum[t]] if weeks else [],
            "gains": [_r(v) for v in gains[t]] if weeks else [],
            "rank_by_week": [int(v) for v in rank[t]] if weeks else [],
            "cumulative_best20": [_r(v) for v in best[t]] if weeks else [],
            "final_total_best20": _r(final_b20),
            "jumps": detect_jumps(gains[t], tl, final, subject="team") if weeks else [],
            "top_riders": _top_riders(tl),
        })

    rider_entries = []
    for slug, rl in rider_ledgers.items():
        if rl.empty:
            log.warning("rider %s has no %s points in rider_uci_results — skipped", slug, season)
            continue
        g, c = series_for(rl, weeks)
        mine = breakdown[breakdown["rider_slug"] == slug]
        official = (float((mine["points_counted"].fillna(0) + mine["points_not_counted"].fillna(0)).sum())
                    if not mine.empty else None)
        team_slug = rl["team_slug"].dropna().iloc[-1] if rl["team_slug"].notna().any() else None
        team_name = rl["team_name"].dropna().iloc[-1] if rl["team_name"].notna().any() else None
        final = float(c.iloc[-1]) if len(c) else 0.0
        rbw = [_rank_among(float(c.iloc[i]), cum.iloc[i].tolist()) for i in range(len(weeks))]
        rider_entries.append({
            "key": f"rider:{slug}", "kind": "rider", "name": rl["rider_name"].iloc[0],
            "short": _sentence_case(rl["rider_short"].iloc[0]),
            "team_slug": team_slug, "team_short": team_short_name(team_name, team_slug) if team_slug else None,
            "official_total": _r(official) if official is not None else None,
            "final_total": _r(final),
            "would_rank": _rank_among(final, finals.values()),
            "would_rank_best20": _rank_among(final, [float(best[t].iloc[-1]) for t in all_teams]) if weeks else None,
            "would_rank_official": _rank_among(final, [float(p or 0) for p in listing["points"]]),
            "cumulative": [_r(v) for v in c], "gains": [_r(v) for v in g],
            "rank_by_week": rbw,
            "cumulative_best20": [_r(v) for v in c],
            "jumps": detect_jumps(g, rl, final, subject="rider"),
            "top_riders": None,
        })

    # coverage + reconciliation
    tops = set(top["team_slug"])
    led_top = ledger[ledger["team_slug"].isin(tops)]
    plot_start = axis_start(weeks, gains[[t for t in all_teams if t in tops]], finals, season) \
        if weeks else None
    n_exact = sum(abs(d) <= RECONCILE_TOL for _, d in diffs)
    max_diff = max((abs(d) for _, d in diffs), default=0.0)
    all_off = [(e["key"], e["short"], e["final_total"] - bd_all[e["key"]])
               for e in team_entries if e["key"] in bd_all
               and abs(e["final_total"] - bd_all[e["key"]]) > RECONCILE_TOL]
    all_ok = sum(1 for e in team_entries if e["key"] in bd_all) - len(all_off)
    off_ok = sum(abs(e["final_total_best20"] - e["official_total"]) <= RECONCILE_TOL for e in team_entries)
    page_vs_counted = [(e["key"], e["short"], bd_page[e["key"]] - float(bd_counted[e["key"]]))
                       for e in team_entries
                       if e["key"] in bd_page and bd_counted.get(e["key"]) is not None
                       and not pd.isna(bd_counted[e["key"]])
                       and abs(bd_page[e["key"]] - float(bd_counted[e["key"]])) > 0.005]
    snap = listing_snapshot(ledger, listing, last) if last else {}
    notes = _coverage_notes(conn, season, ledger, led_top, excluded, listing, breakdown,
                            team_entries, diffs, last, snap, plot_start)
    if page_vs_counted:
        notes.insert(len(notes) - 1, _page_total_note(page_vs_counted, breakdown))
    if weeks:
        recon_note = _recon_note(diffs, n_exact, all_ok, all_off, off_ok, team_entries, breakdown,
                                 bool(bd_page), snap)
    else:
        recon_note = "No ledger data."
    ranking_date = str(listing["ranking_date"].iloc[0])
    payload = {
        "meta": {
            "title": f"{season} UCI points — top {len(team_entries)} men's teams, week by week",
            "season": season, "gender": "men",
            "generated_at": generated_at or datetime.now().strftime("%Y-%m-%d %H:%M"),
            "data_through": last,
            "week_convention": WEEK_CONVENTION,
            "source": ("ProCyclingStats: official UCI team ranking "
                       f"({listing['ranking'].iloc[0]}, {ranking_date}), each team's per-rider "
                       "breakdown, and every contributing rider's dated season results, GC "
                       "leader-jersey points and UCI sanctions"),
            "metric_default": "all_riders",
            "axis_start": plot_start,
            "coverage": {
                "teams": len(team_entries),
                "riders": int(led_top["rider_slug"].nunique()),
                "results_with_points": int((led_top["result_kind"] != "penalty").sum()),
                "penalties": int((led_top["result_kind"] == "penalty").sum()),
                "reconciliation": {"max_abs_team_diff": _r(max_diff), "teams_exact": int(n_exact),
                                   "note": recon_note},
                "notes": notes,
            },
        },
        "weeks": weeks,
        "weeks_meta": weeks_meta(led_top, weeks),
        "teams": team_entries,
        "riders": rider_entries,
        "official": {"ranking": str(listing["ranking"].iloc[0]), "date": ranking_date,
                     "label": f"Official UCI team ranking {season} (PCS)"},
    }
    return payload


def _fmt_pts(x: float) -> str:
    return f"{x:+,.2f}".rstrip("0").rstrip(".").replace("-", "−")


def _recon_note(diffs, n_exact, all_ok, all_off, off_ok, team_entries, breakdown, page_totals: bool,
                snap: Optional[dict]) -> str:
    """The reconciliation in plain words: what was compared with what, how
    many teams match, and why the exceptions differ."""
    short = {e["key"]: e["short"] for e in team_entries}
    why = []
    for t, d in diffs:
        if abs(d) <= RECONCILE_TOL:
            continue
        tie = _tie_on_20th(breakdown, t)
        reason = ", a tie on 20th place that PCS's page counts twice" if tie is not None and \
            abs(tie + d) < 0.01 else ""
        why.append(f"{short.get(t, t)} ({_fmt_pts(d)} pts{reason})")
    page = "points page" if page_totals else "per-rider breakdown"
    txt = (f"Our rebuilt best-20 totals (each team's 20 best riders, as the UCI counts) match the "
           f"team's PCS {page} for {n_exact} of {len(diffs)} teams")
    txt += f"; the {'exception is' if len(why) == 1 else 'exceptions are'} {_join_and(why)}." if why else "."
    txt += (f" Counting every rider, they match the sum of the riders' PCS entries for {all_ok} of "
            f"{all_ok + len(all_off)} teams")
    if all_off:
        txt += " (" + ", ".join(f"{s} {_fmt_pts(d)} pts" for _, s, d in all_off) + \
            ": a rider whose sanctions exceed his points, which PCS lists as '-')"
    txt += "."
    if snap and snap.get("date"):
        day = date.fromisoformat(snap["date"]).strftime("%-d %b")
        missing = snap.get("excluded") or []
        txt += (f" The official ranking listing matches our best-20 totals for {off_ok} of "
                f"{len(team_entries)} teams only: it was captured on {day}"
                + (f" before that day's {_join_and(missing)} results were added" if missing else "")
                + " (see notes).")
    else:
        txt += f" The official ranking listing matches our best-20 totals for {off_ok} of {len(team_entries)} teams."
    return txt


def _tie_on_20th(breakdown: pd.DataFrame, team: str) -> Optional[float]:
    """Points of the rider tied on 20th place (the 21st with the 20th's
    points, not counted), or None."""
    b = breakdown[breakdown["team_slug"] == team]
    p20 = b.loc[b["nth_best"] == BEST_N, "points_counted"]
    p21 = b.loc[b["nth_best"] == BEST_N + 1, "points_not_counted"]
    if p20.empty or p21.empty or pd.isna(p20.iloc[0]) or pd.isna(p21.iloc[0]):
        return None
    return float(p21.iloc[0]) if abs(float(p20.iloc[0]) - float(p21.iloc[0])) < 0.005 else None


def _page_total_note(page_vs_counted: list[tuple[str, str, float]], breakdown: pd.DataFrame) -> str:
    """Why a team page's total differs from its riders' 'points counted'.

    Two causes, read from the breakdown itself: a rider tied on 20th place
    (the page total counts both tied riders; Euskaltel 0.71, MBH Bank 3) and
    a negative net (sanctions above points) that the page prints as '-' but
    subtracts (Kern Pharma -10).
    """
    ties, negs, other = [], [], []
    for team, short, d in page_vs_counted:
        tie = _tie_on_20th(breakdown, team)
        txt = f"{short} {d:+,.2f}".rstrip("0").rstrip(".")
        if tie is not None and abs(tie - d) < 0.01:
            ties.append(txt)
        elif d < 0:
            negs.append(txt)
        else:
            other.append(txt)
    parts = []
    if ties:
        parts.append(f"{_join_and(ties)}: two riders tie on 20th place and the page total counts both "
                     f"(the UCI best-20 sum, and ours, counts one)")
    if negs:
        parts.append(f"{_join_and(negs)}: a rider with more sanction points than points shows '-' but "
                     f"is subtracted")
    if other:
        parts.append(f"{_join_and(other)}: unexplained")
    return (f"For {len(page_vs_counted)} teams PCS's team page total differs from the sum of its "
            f"riders' 'points counted' — " + "; ".join(parts) + ". The page total is shown as the "
            "breakdown total.")


def _join_and(items: Sequence[str]) -> str:
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _coverage_notes(conn, season, ledger, led_top, excluded, listing, breakdown, team_entries,
                    diffs, through, snap: Optional[dict] = None,
                    plot_start: Optional[str] = None) -> list[str]:
    """Plain-language notes on how the ledger relates to the official numbers."""
    notes: list[str] = []
    n_top = len(team_entries)
    off_vs_bd = [(e["short"], e["official_total"], e["breakdown_total"])
                 for e in team_entries if e["breakdown_total"] is not None]
    lag = [(s, b - o) for s, o, b in off_vs_bd if abs(b - o) > RECONCILE_TOL]
    if off_vs_bd:
        txt = (f"Official listing vs per-team breakdown: {len(off_vs_bd) - len(lag)} of "
               f"{len(off_vs_bd)} teams agree within {RECONCILE_TOL:g} pt")
        if lag:
            big = sorted(lag, key=lambda x: -abs(x[1]))[:3]
            txt += (f"; {len(lag)} differ (breakdown {sum(d for _, d in lag):+,.0f} pts in total, e.g. "
                    + ", ".join(f"{s} {d:+,.1f}" for s, d in big) + ")")
            if snap is None:
                snap = listing_snapshot(ledger, listing, through) if through else {}
            if snap.get("date") and snap["teams_matched"] > snap.get("matched_full_day", 0):
                day = snap["date"]
                inc = _join_and(snap["included"]) or "none of its races"
                exc = _join_and(snap["excluded"])
                txt += (f". The listing is a snapshot taken during {day}: our best-20 ledger "
                        f"reproduces it for {snap['teams_matched']}/{snap['teams']} teams with "
                        f"results up to {(date.fromisoformat(day) - timedelta(days=1)).isoformat()} "
                        f"plus that day's {inc}" + (f", but not yet {exc}" if exc else "")
                        + f" (all of {day}: {snap['matched_full_day']}/{snap['teams']}); the per-team "
                          "breakdown pages were fetched later and include every result")
            elif snap.get("date"):
                txt += (f". Best reproduction of the listing from our ledger: results up to "
                        f"{snap['date']} ({snap['teams_matched']}/{snap['teams']} teams within "
                        f"{RECONCILE_TOL:g} pt)")
        notes.append(txt + ".")
    notes.append("'All riders' totals add every rider's points; the official ranking counts only "
                 "each team's 20 best riders of the season (the 'best 20' view), so all-rider "
                 "totals are usually higher.")
    if not led_top.empty:
        pens = led_top[led_top["result_kind"] == "penalty"]
        if not pens.empty:
            notes.append(f"UCI sanctions are subtracted on the day they were imposed ({len(pens)} "
                         f"sanctions, {-pens['points'].sum():,.0f} pts across the top {n_top}); a "
                         f"week with a sanction can show a negative gain.")
        per_rider = led_top.groupby(["team_slug", "rider_slug"])["points"].sum()
        neg = per_rider[per_rider < 0]
        if not neg.empty:
            notes.append(f"{len(neg)} riders have more sanction points than points ({neg.sum():,.0f} "
                         f"pts in total): they lower the all-rider total but almost never count among "
                         f"a team's best 20, so a few small teams' best-20 totals exceed their "
                         f"all-rider totals.")
        pre = led_top[led_top["date"] < f"{season}-01-01"]
        if not pre.empty:
            txt = (f"The UCI {season} season opened in late {season - 1}: {len(pre)} results "
                   f"({pre['points'].sum():,.0f} pts) dated {pre['date'].min()} to "
                   f"{pre['date'].max()} count for {season}")
            if plot_start:
                txt += (f"; the chart starts at the week of {plot_start}, each line opening at its "
                        f"total so far (those points included), while the data keeps every week "
                        f"from {ledger['week_start'].min()}")
            else:
                txt += f", so the weekly axis starts in {season - 1}"
            notes.append(txt + ".")
        lead = led_top[led_top["status"] == "leader"]
        if not lead.empty:
            notes.append(f"GC leader-jersey points ({len(lead)} awards, {lead['points'].sum():,.0f} pts) "
                         f"are not listed on PCS rider result pages; they come from each rider's "
                         f"UCI-ranking page and are dated to the stage after which the jersey was held.")
    spells = _load_spells(conn, season)
    moved = sorted(s for s, sp in spells.items() if len({x["team_slug"] for x in sp}) > 1)
    if moved:
        notes.append(f"{len(moved)} riders changed team during the season; their points are split by "
                     f"date between the teams.")
    if excluded is not None and not excluded.empty:
        outside = excluded[excluded["team_slug"].notna()]
        unknown = excluded[excluded["team_slug"].isna()]
        if not outside.empty:
            notes.append(f"{outside['points'].sum():,.0f} pts that riders scored for teams outside the "
                         f"ranking (development/continental teams) before a transfer are not in any "
                         f"team total, as on PCS.")
        if not unknown.empty:
            notes.append(f"{unknown['rider_slug'].nunique()} riders with results but no team "
                         f"breakdown entry are excluded ({unknown['points'].sum():,.0f} pts).")
    # rider-level gaps behind the team gaps
    bd = breakdown.assign(off=breakdown["points_counted"].fillna(0) + breakdown["points_not_counted"].fillna(0))
    bd = bd[bd["team_slug"].isin({e["key"] for e in team_entries})]
    led = led_top.groupby(["team_slug", "rider_slug"])["points"].sum()
    gaps = []
    names = dict(zip(bd["rider_slug"], bd["rider_name"]))
    shorts = {e["key"]: e["short"] for e in team_entries}
    for r in bd.itertuples(index=False):
        d = float(led.get((r.team_slug, r.rider_slug), 0.0)) - float(r.off)
        if abs(d) > RECONCILE_TOL and not (r.off == 0 and d < 0):
            gaps.append((r.team_slug, r.rider_slug, d))
    if gaps:
        gaps.sort(key=lambda x: -abs(x[2]))
        notes.append(f"{len(gaps)} riders' ledger totals differ from their breakdown entry by more than "
                     f"{RECONCILE_TOL:g} pt (" + ", ".join(
                         f"{display_name(names.get(s) or s, s)} ({shorts.get(t, t)}) {d:+,.1f}"
                         for t, s, d in gaps[:4])
                     + "): results or transfer dates PCS counts differently from the scraped rider pages; "
                       "they account for the remaining ledger-vs-breakdown team gaps.")
    notes.append("Annotations: each team's biggest weeks (normally 3 to 6 per team) plus the weeks of its "
                 "headline wins — every Monument, elite World title and Grand Tour GC, and WorldTour "
                 "one-day or stage-race wins up to 10 annotations per team. Labels name the riders and "
                 "results behind the week ('wins' only for a 1st place; a team time trial is the team's "
                 "win, not a rider's stage win); in a stage race's final week the label counts the stage "
                 "wins of the whole race, while the points are that week's.")
    return notes


# ---------------------------------------------------------------------------
# text summary
# ---------------------------------------------------------------------------

def _fmt(x) -> str:
    if x is None:
        return "–"
    return f"{x:,.0f}" if abs(x - round(x)) < 0.05 else f"{x:,.1f}"


def _md_cell(text) -> str:
    return str(text).replace("|", "\\|")


def markdown_summary(payload: dict) -> str:
    """Markdown summary: the top-N table plus a line per single-rider series."""
    meta = payload["meta"]
    lines = [f"## {meta['title']}", "",
             f"Data through {meta['data_through']} · official listing {payload['official']['date']} · "
             f"{len(payload['weeks'])} weeks", "",
             "| # | Team | Official | Ledger (all riders) | Best-20 | Biggest jump |",
             "|---:|---|---:|---:|---:|---|"]
    for t in payload["teams"]:
        big = max(t["jumps"], key=lambda j: j["gain"]) if t["jumps"] else None
        jump = (f"{big['label']} ({big['week']}, +{_fmt(big['gain'])})" if big else "–")
        lines.append(f"| {t['official_rank']} | {_md_cell(t['name'])} | {_fmt(t['official_total'])} | "
                     f"{_fmt(t['final_total'])} | {_fmt(t['final_total_best20'])} | {_md_cell(jump)} |")
    for r in payload.get("riders") or []:
        lines.append("")
        lines.append(f"{r['short']} alone: {_fmt(r['final_total'])} pts — would rank "
                     f"{ordinal(r['would_rank'])} among teams (all-rider totals; "
                     f"{ordinal(r.get('would_rank_official'))} against the official listing)")
    rec = meta["coverage"]["reconciliation"]
    lines += ["", f"Reconciliation: {rec['note']}"]
    for n in meta["coverage"]["notes"]:
        lines.append(f"- {n}")
    return "\n".join(lines) + "\n"
