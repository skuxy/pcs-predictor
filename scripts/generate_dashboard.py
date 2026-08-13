#!/usr/bin/env python3
"""
Generate a self-contained HTML dashboard: upcoming race predictions + model accuracy.

Run from anywhere:
    python scripts/generate_dashboard.py

Output: dashboard/index.html  (serve with any static file server)
"""
import os
import sys
import json
import datetime
import logging
from pathlib import Path

ROOT = Path(__file__).parent.parent
os.chdir(ROOT)          # keep all relative paths (DB, model files) working
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.WARNING)

from db.database import get_conn
from model.predict import predict_race
from model.train import model_paths
import pandas as pd
import numpy as np

TODAY      = datetime.date.today()
TODAY_STR  = TODAY.isoformat()
DAYS_AHEAD = 21
OUTPUT_DIR = ROOT / "dashboard"
OUTPUT_DIR.mkdir(exist_ok=True)

PCS_BASE = "https://www.procyclingstats.com"

PROFILE_LABELS = {
    "flat": "Flat", "hilly": "Hilly", "mountain": "Mountain",
    "itt": "ITT", "ttt": "TTT",
}

PROFILE_COLORS = {
    "mountain": "#c0392b",
    "hilly":    "#e67e22",
    "flat":     "#2980b9",
    "itt":      "#8e44ad",
    "ttt":      "#16a085",
}

# Inline SVG profile silhouettes (48×18 viewBox)
_SVGS = {
    "mountain": '<polygon points="0,18 18,4 24,10 30,2 48,18"/>',
    "hilly":    '<polygon points="0,18 10,10 20,15 30,6 40,12 48,18"/>',
    "flat":     '<polygon points="0,18 0,14 16,14 20,10 24,14 48,14 48,18"/>',
    "itt":      '<g fill="none" stroke-width="2.5" stroke-linecap="round"><line x1="21" y1="3" x2="27" y2="3"/><circle cx="24" cy="12" r="7"/><line x1="24" y1="5" x2="24" y2="12"/><line x1="24" y1="12" x2="29" y2="12"/></g>',
    "ttt":      '<g fill="none" stroke-width="2.5"><circle cx="12" cy="11" r="5"/><circle cx="24" cy="11" r="5"/><circle cx="36" cy="11" r="5"/></g>',
}


def _profile_svg(profile: str, active: bool = False) -> str:
    col = "rgba(255,255,255,0.9)" if active else PROFILE_COLORS.get(profile, "#95a5a6")
    body = _SVGS.get(profile, "")
    return f'<svg viewBox="0 0 48 18" class="psvg" style="color:{col}" fill="{col}" stroke="{col}">{body}</svg>'


def _country_flag(cc: str) -> str:
    if not cc or len(cc) != 2:
        return ""
    return (chr(0x1F1E6 + ord(cc[0].upper()) - ord("A")) +
            chr(0x1F1E6 + ord(cc[1].upper()) - ord("A")))


# ── Data loaders ───────────────────────────────────────────────────────────────

def _upcoming_races():
    end = (TODAY + datetime.timedelta(days=DAYS_AHEAD)).isoformat()
    with get_conn() as conn:
        return conn.execute(
            """SELECT id, pcs_slug, name, start_date, end_date,
                      is_stage_race, gender, country, class
               FROM races
               WHERE start_date >= ? AND start_date <= ?
               ORDER BY start_date""",
            (TODAY_STR, end),
        ).fetchall()


def _stage_info_for_race(race_id: int) -> dict:
    """Returns {date_str: {pcs_slug, departure, arrival, distance_km, elevation_m, profile_type, surface}}"""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT pcs_slug, date, departure, arrival,
                      distance_km, elevation_m, profile_type, surface
               FROM stages WHERE race_id = ? ORDER BY date""",
            (race_id,),
        ).fetchall()
    return {row["date"]: dict(row) for row in rows}


def _rider_id_slug_map() -> dict:
    """Returns {rider_id: pcs_slug} for all riders."""
    with get_conn() as conn:
        rows = conn.execute("SELECT id, pcs_slug FROM riders").fetchall()
    return {row["id"]: row["pcs_slug"] for row in rows}


def _recent_races(n: int = 10, gender: str = "men"):
    with get_conn() as conn:
        return conn.execute(
            """SELECT r.pcs_slug, r.name, r.end_date,
                      COUNT(DISTINCT s.id)  AS stages,
                      COUNT(res.id)         AS results
               FROM races r
               JOIN stages s ON s.race_id = r.id
               LEFT JOIN results res ON res.stage_id = s.id
               WHERE r.gender = ? AND r.end_date < ?
               GROUP BY r.id
               HAVING results > 0
               ORDER BY r.end_date DESC LIMIT ?""",
            (gender, TODAY_STR, n),
        ).fetchall()


def _model_metrics(gender: str = "men") -> dict:
    _, _, mpath = model_paths(gender)
    mpath = ROOT / mpath
    if mpath.exists():
        return json.loads(mpath.read_text())
    return {}


def _db_stats() -> dict:
    with get_conn() as conn:
        return {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("races", "stages", "results", "riders")
        }


# ── HTML helpers ───────────────────────────────────────────────────────────────

def _prob_colour(p: float) -> str:
    if p >= 0.40: return "#27ae60"
    if p >= 0.25: return "#2ecc71"
    if p >= 0.15: return "#f39c12"
    if p >= 0.08: return "#e67e22"
    return "#bdc3c7"


def _prob_bar(p: float) -> str:
    width = max(int(p * 140), 2)
    col   = _prob_colour(p)
    return (
        f'<div class="pbar">'
        f'<div class="pbar-fill" style="width:{width}px;background:{col}"></div>'
        f'<span class="pbar-val">{p:.3f}</span>'
        f'</div>'
    )


def _stage_table(rows: list[dict], top_n: int = 20, rider_slugs: dict | None = None,
                 stage_pcs_slug: str | None = None) -> str:
    rider_slugs = rider_slugs or {}
    trs = ""
    for i, r in enumerate(rows[:top_n], 1):
        extra = ""
        if "win_prob" in r and r["win_prob"] is not None:
            extra += f'<td class="aux">{r["win_prob"]:.3f}</td>'
        if "top3_prob" in r and r["top3_prob"] is not None:
            extra += f'<td class="aux">{r["top3_prob"]:.3f}</td>'

        medal = ("🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else str(i))
        cls   = "podium" if i <= 3 else ("hot" if i <= 10 else "")

        rid  = r.get("rider_id")
        slug = rider_slugs.get(rid) if rid and not (isinstance(rid, float) and np.isnan(rid)) else None
        if slug:
            name_html = (
                f'<a href="{PCS_BASE}/rider/{slug}" target="_blank" '
                f'class="rider-link" title="View on PCS">{r["rider_name"]}</a>'
            )
        else:
            name_html = str(r["rider_name"])

        trs += (
            f'<tr class="{cls}">'
            f'<td class="rank">{medal}</td>'
            f'<td class="name">{name_html}</td>'
            f'<td>{_prob_bar(r["top10_prob"])}</td>'
            f'{extra}'
            f'</tr>'
        )

    has_win  = any("win_prob"  in r and r["win_prob"]  is not None for r in rows[:1])
    has_top3 = any("top3_prob" in r and r["top3_prob"] is not None for r in rows[:1])
    extra_th = ""
    if has_win:  extra_th += '<th class="aux">P(win)</th>'
    if has_top3: extra_th += '<th class="aux">P(top3)</th>'

    stage_link = ""
    if stage_pcs_slug:
        stage_link = (
            f'<a href="{PCS_BASE}/{stage_pcs_slug}" target="_blank" '
            f'class="stage-ext-link">View stage on PCS ↗</a>'
        )

    return (
        f'<div class="panel-hd">{stage_link}</div>'
        f'<table>'
        f'<thead><tr><th class="rank">#</th><th>Rider</th>'
        f'<th>P(top10)</th>{extra_th}</tr></thead>'
        f'<tbody>{trs}</tbody>'
        f'</table>'
    )


def _stage_btn(rid: str, idx: int, date_str: str, stage_meta: dict | None, is_first: bool) -> str:
    profile  = (stage_meta or {}).get("profile_type", "")
    surface  = (stage_meta or {}).get("surface", "road")
    dist     = (stage_meta or {}).get("distance_km")
    elev     = (stage_meta or {}).get("elevation_m")
    dep      = (stage_meta or {}).get("departure") or ""
    arr      = (stage_meta or {}).get("arrival") or ""
    slug     = (stage_meta or {}).get("pcs_slug", "")

    label    = PROFILE_LABELS.get(profile, profile.title() if profile else "Stage")
    if surface in ("cobbled", "gravel"):
        label += f" ({surface})"

    col = PROFILE_COLORS.get(profile, "#95a5a6")
    active_cls = " active" if is_first else ""

    # Stats line
    stats_parts = []
    if dist:  stats_parts.append(f"{dist:.0f}km")
    if elev:  stats_parts.append(f"{elev:,}m↑")
    stats_str = " · ".join(stats_parts)

    # Route line (truncate city names)
    def _short(city: str, n: int = 14) -> str:
        return city[:n] + "…" if len(city) > n else city
    route_str = f"{_short(dep)} → {_short(arr)}" if dep and arr else (dep or arr or "")

    # Date formatted compactly
    try:
        dt = datetime.date.fromisoformat(date_str)
        date_fmt = dt.strftime("%b %-d")
    except ValueError:
        date_fmt = date_str[:10]

    psvg = _profile_svg(profile, active=is_first)

    btn_inner = (
        f'<span class="stbtn-date">{date_fmt}</span>'
        f'{psvg}'
        f'<span class="stbtn-label">{label}</span>'
    )
    if stats_str:
        btn_inner += f'<span class="stbtn-stats">{stats_str}</span>'
    if route_str:
        btn_inner += f'<span class="stbtn-route">{route_str}</span>'

    return (
        f'<button class="stbtn{active_cls}" onclick="sw(\'{rid}\',{idx})" '
        f'style="--pc:{col}">'
        f'{btn_inner}'
        f'</button>'
    )


def _race_info_strip(race) -> str:
    flag  = _country_flag(race["country"] or "")
    cc    = race["country"] or ""
    cls   = race["class"] or ""
    slug  = race["pcs_slug"] or ""
    pcs_url = f"{PCS_BASE}/{slug}"

    parts = []
    if cc:
        parts.append(f'<span class="meta-item">{flag} {cc}</span>')
    if cls:
        parts.append(f'<span class="meta-item badge class-badge">{cls}</span>')
    parts.append(
        f'<a href="{pcs_url}" target="_blank" class="meta-link">View on PCS ↗</a>'
    )

    return f'<div class="race-meta">{"".join(parts)}</div>'


def _race_card(race, preds: pd.DataFrame, idx: int,
               stage_info: dict, rider_slugs: dict) -> str:
    rid    = f"rc{idx}"
    gender = race["gender"] or "men"
    gbadge = f'<span class="badge g-{gender}">{gender}</span>'

    stages = []
    for sd, grp in preds.groupby("stage_date"):
        date_str = str(sd)[:10]
        meta     = stage_info.get(date_str) or {}
        profile  = grp["profile_type"].iloc[0] if "profile_type" in grp.columns else ""
        if not meta.get("profile_type") and profile:
            meta = dict(meta, profile_type=profile)
        rows = grp.sort_values("top10_prob", ascending=False).to_dict("records")
        stages.append((date_str, meta, rows))

    if not stages:
        return ""

    btns = "".join(
        _stage_btn(rid, i, d, meta, i == 0)
        for i, (d, meta, _) in enumerate(stages)
    )
    panels = "".join(
        f'<div class="stpanel{"  active" if i == 0 else ""}">'
        f'{_stage_table(rows, rider_slugs=rider_slugs, stage_pcs_slug=(meta or {}).get("pcs_slug"))}'
        f'</div>'
        for i, (_, meta, rows) in enumerate(stages)
    )

    race_type = "Stage race" if race["is_stage_race"] else "One-day"
    info_strip = _race_info_strip(race)

    return f"""
<div class="card" id="{rid}">
  <div class="card-hd">
    <div>
      <span class="race-name">{race["name"]}</span>
      {gbadge}
      <span class="badge type-badge">{race_type}</span>
    </div>
    <span class="dates">{race["start_date"]} → {race["end_date"]}</span>
  </div>
  {info_strip}
  <div class="stbar">{btns}</div>
  <div class="panels">{panels}</div>
</div>"""


def _accuracy_section(metrics_m: dict, metrics_w: dict, recent_m, recent_w, stats: dict) -> str:

    def metric_box(val, lbl, sub=""):
        return (
            f'<div class="mbox">'
            f'<div class="mval">{val}</div>'
            f'<div class="mlbl">{lbl}</div>'
            f'{"<div class=msub>" + sub + "</div>" if sub else ""}'
            f'</div>'
        )

    boxes = ""
    if metrics_m.get("val_auc"):
        boxes += metric_box(f'{metrics_m["val_auc"]:.4f}', "Validation AUC", "men")
    if metrics_m.get("val_ap"):
        boxes += metric_box(f'{metrics_m["val_ap"]:.4f}', "Validation AP", "men")
    if metrics_w.get("val_auc"):
        boxes += metric_box(f'{metrics_w["val_auc"]:.4f}', "Validation AUC", "women")
    if metrics_m.get("train_rows"):
        boxes += metric_box(f'{metrics_m["train_rows"]:,}', "Training rows")
    if metrics_m.get("n_features"):
        boxes += metric_box(str(metrics_m["n_features"]), "Features")
    if metrics_m.get("train_cutoff"):
        boxes += metric_box(metrics_m["train_cutoff"], "Last trained")
    for tbl, cnt in stats.items():
        boxes += metric_box(f'{cnt:,}', tbl.capitalize())

    fi_html = ""
    fi = metrics_m.get("feature_importances", {})
    if fi:
        max_v   = max(fi.values())
        fi_rows = "".join(
            f'<div class="firow">'
            f'<span class="finame">{name}</span>'
            f'<div class="fibar" style="width:{int(v/max_v*220)}px"></div>'
            f'<span class="fival">{v:.5f}</span>'
            f'</div>'
            for name, v in list(fi.items())[:25]
        )
        fi_html = f'<h3 class="sec-title">Top Feature Importances (men)</h3><div class="fi-wrap">{fi_rows}</div>'

    def recent_table(rows, title):
        if not rows:
            return f'<p class="muted">No completed {title} races found.</p>'
        trs = "".join(
            f'<tr><td><a href="{PCS_BASE}/{r["pcs_slug"]}" target="_blank" class="rider-link">'
            f'{r["name"]}</a></td><td>{r["end_date"]}</td>'
            f'<td class="num">{r["stages"]}</td><td class="num">{r["results"]:,}</td></tr>'
            for r in rows
        )
        return (
            f'<h3 class="sec-title">{title} — Recent Completed Races</h3>'
            f'<div class="rtable-wrap"><table>'
            f'<thead><tr><th>Race</th><th>Ended</th><th class="num">Stages</th><th class="num">Results</th></tr></thead>'
            f'<tbody>{trs}</tbody>'
            f'</table></div>'
        )

    return f"""
<h2 class="pg-title">Model &amp; Data</h2>
<div class="mboxes">{boxes}</div>
{fi_html}
{recent_table(recent_m, "Men")}
{recent_table(recent_w, "Women")}
"""


# ── CSS + JS ───────────────────────────────────────────────────────────────────

CSS = """
:root{--bg:#f0f2f5;--card:#fff;--hd:#1a252f;--accent:#e84118;--text:#2c3e50;--muted:#95a5a6;--border:#e8ecef;--green:#27ae60;--amber:#f39c12}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);font-size:14px;line-height:1.5}

header{background:linear-gradient(135deg,#1a252f 0%,#c0392b 100%);color:#fff;padding:20px 32px 18px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
header h1{font-size:1.5rem;font-weight:700;letter-spacing:-.02em}
header p{font-size:.8rem;opacity:.75}
.updated{font-size:.75rem;opacity:.6;margin-top:2px}

nav{background:var(--card);border-bottom:2px solid var(--border);padding:0 32px;display:flex;gap:0}
.navbtn{background:none;border:none;cursor:pointer;padding:12px 22px;font-size:.9rem;color:var(--muted);border-bottom:3px solid transparent;margin-bottom:-2px;font-weight:500;transition:color .15s}
.navbtn.active{color:var(--accent);border-bottom-color:var(--accent)}
.navbtn:hover:not(.active){color:var(--text)}

main{padding:24px 32px;max-width:1100px;margin:0 auto}
section{display:none}
section.active{display:block}

.card{background:var(--card);border-radius:10px;margin-bottom:20px;box-shadow:0 1px 6px rgba(0,0,0,.07);overflow:hidden}
.card-hd{background:var(--hd);color:#fff;padding:12px 18px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px}
.race-name{font-weight:700;font-size:1rem;margin-right:8px}
.dates{font-size:.78rem;opacity:.65}
.badge{display:inline-block;padding:2px 7px;border-radius:10px;font-size:.7rem;font-weight:700;margin-right:4px}
.g-men{background:#3498db33;color:#5dade2}
.g-women{background:#e91e6333;color:#f48fb1}
.type-badge{background:#ffffff22;color:#ffffff99}

/* Race meta strip */
.race-meta{display:flex;align-items:center;gap:10px;padding:7px 18px;background:#f7f9fc;border-bottom:1px solid var(--border);font-size:.8rem;flex-wrap:wrap}
.meta-item{color:var(--text);opacity:.8}
.meta-sep{color:var(--muted)}
.meta-link{color:var(--accent);text-decoration:none;font-weight:500;margin-left:auto}
.meta-link:hover{text-decoration:underline}
.class-badge{background:#e8f0fe;color:#1967d2;padding:2px 6px;border-radius:8px;font-size:.72rem}

/* Stage tab bar */
.stbar{display:flex;flex-wrap:nowrap;gap:4px;padding:10px 14px;border-bottom:1px solid var(--border);background:#fafbfc;overflow-x:auto;scrollbar-width:thin}
.stbtn{background:#fff;border:1px solid var(--border);border-radius:6px;cursor:pointer;padding:6px 10px;font-size:.76rem;color:var(--muted);text-align:center;line-height:1.3;transition:all .15s;min-width:90px;max-width:130px;display:flex;flex-direction:column;align-items:center;gap:2px;border-top:3px solid var(--pc,#bdc3c7);flex-shrink:0}
.stbtn.active{background:var(--pc,var(--accent));color:#fff;border-color:var(--pc,var(--accent));font-weight:600;border-top-color:transparent}
.stbtn:hover:not(.active){border-color:var(--pc,var(--accent));color:var(--pc,var(--accent))}
.stbtn-date{font-weight:600;font-size:.8rem}
.stbtn-label{font-size:.7rem;opacity:.85}
.stbtn-stats{font-size:.68rem;opacity:.75}
.stbtn-route{font-size:.66rem;opacity:.7;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:120px}
.psvg{display:block;width:48px;height:18px;margin:2px auto}
.stbtn.active .psvg{filter:brightness(10)}

.panels{padding:0}
.stpanel{display:none;padding:14px 18px}
.stpanel.active{display:block}

/* Stage panel header */
.panel-hd{display:flex;justify-content:flex-end;margin-bottom:8px;min-height:18px}
.stage-ext-link{font-size:.76rem;color:var(--accent);text-decoration:none;font-weight:500}
.stage-ext-link:hover{text-decoration:underline}

table{width:100%;border-collapse:collapse;font-size:.84rem}
th{text-align:left;padding:7px 10px;color:var(--muted);font-size:.72rem;text-transform:uppercase;letter-spacing:.06em;border-bottom:2px solid var(--border)}
th.rank,th.num{text-align:right}
td{padding:5px 10px;border-bottom:1px solid #f5f5f5}
tr:last-child td{border-bottom:none}
tr:hover td{background:#fafafa}
.rank{color:var(--muted);text-align:right;width:32px;font-size:.8rem}
.name{font-weight:500}
.rider-link{color:inherit;text-decoration:none}
.rider-link:hover{color:var(--accent);text-decoration:underline}
.aux{color:var(--muted);font-size:.8rem;text-align:right;width:70px}
.podium td{background:#fffdf0}
.podium .name{font-weight:700}
.hot td{background:#f9fffa}
.num{text-align:right}

.pbar{display:flex;align-items:center;gap:8px}
.pbar-fill{height:8px;border-radius:4px;transition:width .3s}
.pbar-val{font-size:.8rem;color:var(--muted);min-width:36px}

.mboxes{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:14px;margin-bottom:28px}
.mbox{background:var(--card);border-radius:8px;padding:16px 18px;box-shadow:0 1px 4px rgba(0,0,0,.07)}
.mval{font-size:1.6rem;font-weight:700;color:var(--accent)}
.mlbl{font-size:.72rem;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-top:3px}
.msub{font-size:.7rem;color:var(--muted);margin-top:1px}

.pg-title{font-size:1.3rem;font-weight:700;margin-bottom:18px}
.sec-title{font-size:1rem;font-weight:600;margin:24px 0 12px;color:var(--text)}
.fi-wrap{margin-bottom:24px}
.firow{display:flex;align-items:center;gap:8px;padding:3px 0}
.finame{width:210px;font-size:.78rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fibar{height:11px;border-radius:3px;background:var(--accent);opacity:.7}
.fival{font-size:.74rem;color:var(--muted);width:58px}
.rtable-wrap{background:var(--card);border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,.07);overflow:hidden;margin-bottom:24px}
.muted{color:var(--muted);font-size:.85rem;padding:12px 0}

footer{text-align:center;color:var(--muted);padding:20px;font-size:.78rem;border-top:1px solid var(--border);margin-top:8px}

@media(max-width:640px){
  header,main,nav{padding-left:14px;padding-right:14px}
  .mboxes{grid-template-columns:repeat(2,1fr)}
  .stbtn{min-width:80px;padding:5px 7px}
}
"""

JS = """
function sw(rid, idx) {
  var c = document.getElementById(rid);
  c.querySelectorAll('.stbtn').forEach(function(b,i){ b.classList.toggle('active', i===idx); });
  c.querySelectorAll('.stpanel').forEach(function(p,i){ p.classList.toggle('active', i===idx); });
}
function nav(id) {
  document.querySelectorAll('.navbtn').forEach(function(b){ b.classList.remove('active'); });
  document.querySelectorAll('main section').forEach(function(s){ s.classList.remove('active'); });
  document.getElementById('nb-'+id).classList.add('active');
  document.getElementById('s-'+id).classList.add('active');
}
document.addEventListener('DOMContentLoaded', function(){ nav('races'); });
"""


# ── Main ───────────────────────────────────────────────────────────────────────

def generate():
    print(f"[{TODAY_STR}] Generating dashboard …")
    upcoming   = _upcoming_races()
    rider_slugs = _rider_id_slug_map()
    metrics_m  = _model_metrics("men")
    metrics_w  = _model_metrics("women")
    recent_m   = _recent_races(10, "men")
    recent_w   = _recent_races(10, "women")
    stats      = _db_stats()

    print(f"  {len(upcoming)} upcoming race(s) within {DAYS_AHEAD} days")
    print(f"  {len(rider_slugs)} riders in slug map")

    cards_html = ""
    for i, race in enumerate(upcoming):
        slug       = race["pcs_slug"]
        gender     = race["gender"] or "men"
        stage_info = _stage_info_for_race(race["id"])
        print(f"  Predicting {race['name']} ({gender}) …", end="", flush=True)
        try:
            preds = predict_race(slug, TODAY_STR, gender=gender)
            if preds.empty:
                print(" no predictions")
                continue
            card = _race_card(race, preds, i, stage_info, rider_slugs)
            if card:
                cards_html += card
                n_stages = preds["stage_date"].nunique()
                n_riders = len(preds) // max(n_stages, 1)
                print(f" {n_stages} stage(s), ~{n_riders} riders")
            else:
                print(" skipped")
        except Exception as exc:
            print(f" ERROR: {exc}")

    if not cards_html:
        cards_html = (
            '<div class="card" style="padding:24px;color:var(--muted)">'
            'No upcoming races with predictions found within the next '
            f'{DAYS_AHEAD} days. Run <code>python main.py scrape --years {TODAY.year}</code> '
            'to update the database.'
            '</div>'
        )

    acc_html = _accuracy_section(metrics_m, metrics_w, recent_m, recent_w, stats)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>🚴 PCS Race Predictor</title>
  <style>{CSS}</style>
</head>
<body>
<header>
  <div>
    <h1>🚴 PCS Race Predictor</h1>
    <p>Top-10 finish probabilities · Gradient-boosted ML model</p>
  </div>
  <p class="updated">Updated {TODAY_STR}</p>
</header>
<nav>
  <button id="nb-races"    class="navbtn" onclick="nav('races')">🔮 Upcoming Races</button>
  <button id="nb-accuracy" class="navbtn" onclick="nav('accuracy')">📊 Model &amp; Data</button>
</nav>
<main>
  <section id="s-races">{cards_html}</section>
  <section id="s-accuracy">{acc_html}</section>
</main>
<footer>PCS Predictor · Data from <a href="{PCS_BASE}" target="_blank" style="color:inherit">ProCyclingStats</a> · {TODAY_STR}</footer>
<script>{JS}</script>
</body>
</html>"""

    out = OUTPUT_DIR / "index.html"
    out.write_text(html, encoding="utf-8")
    size_kb = out.stat().st_size / 1024
    print(f"\n✓ Dashboard written to {out}  ({size_kb:.1f} KB)")


if __name__ == "__main__":
    generate()
