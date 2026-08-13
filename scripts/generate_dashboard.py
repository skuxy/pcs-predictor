#!/usr/bin/env python3
"""
Generate a self-contained HTML dashboard: upcoming race predictions + model accuracy.

Run from anywhere:
    python scripts/generate_dashboard.py

Output: dashboard/index.html  (serve with any static file server)
"""
import html as _html
import os
import sys
import json
import datetime
import logging
from pathlib import Path

ROOT = Path(__file__).parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.WARNING)

from db.database import get_conn
from model.predict import predict_race
from model.train import model_paths
import pandas as pd
import numpy as np

TODAY     = datetime.date.today()
TODAY_STR = TODAY.isoformat()
GENERATED = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
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

_SVGS = {
    "mountain": '<polygon points="0,18 18,4 24,10 30,2 48,18"/>',
    "hilly":    '<polygon points="0,18 10,10 20,15 30,6 40,12 48,18"/>',
    "flat":     '<polygon points="0,18 0,14 16,14 20,10 24,14 48,14 48,18"/>',
    "itt":      '<g fill="none" stroke-width="2.5" stroke-linecap="round">'
                '<line x1="21" y1="3" x2="27" y2="3"/>'
                '<circle cx="24" cy="12" r="7"/>'
                '<line x1="24" y1="5" x2="24" y2="12"/>'
                '<line x1="24" y1="12" x2="29" y2="12"/></g>',
    "ttt":      '<g fill="none" stroke-width="2.5">'
                '<circle cx="12" cy="11" r="5"/>'
                '<circle cx="24" cy="11" r="5"/>'
                '<circle cx="36" cy="11" r="5"/></g>',
}


def _profile_svg(profile: str, active: bool = False) -> str:
    col  = "rgba(255,255,255,0.9)" if active else PROFILE_COLORS.get(profile, "#95a5a6")
    body = _SVGS.get(profile, "")
    return (
        f'<svg viewBox="0 0 48 18" class="psvg" '
        f'fill="{col}" stroke="{col}">{body}</svg>'
    )


def _country_flag(cc: str) -> str:
    if not cc or len(cc) != 2:
        return ""
    return (chr(0x1F1E6 + ord(cc[0].upper()) - ord("A")) +
            chr(0x1F1E6 + ord(cc[1].upper()) - ord("A")))


def _esc(s) -> str:
    return _html.escape(str(s)) if s else ""


# ── Data loaders ───────────────────────────────────────────────────────────────

def _upcoming_races():
    """Races that start within the next DAYS_AHEAD days OR are already in progress."""
    end = (TODAY + datetime.timedelta(days=DAYS_AHEAD)).isoformat()
    with get_conn() as conn:
        return conn.execute(
            """SELECT id, pcs_slug, name, start_date, end_date,
                      is_stage_race, gender, country, class
               FROM races
               WHERE end_date >= ? AND start_date <= ?
               ORDER BY start_date""",
            (TODAY_STR, end),
        ).fetchall()


def _stage_info_for_race(race_id: int) -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT pcs_slug, date, departure, arrival,
                      distance_km, elevation_m, profile_type, surface
               FROM stages WHERE race_id = ? ORDER BY date""",
            (race_id,),
        ).fetchall()
    return {row["date"]: dict(row) for row in rows}


def _rider_id_data_map() -> dict:
    """Returns {rider_id: {"slug": pcs_slug, "team": team}} for all riders."""
    with get_conn() as conn:
        rows = conn.execute("SELECT id, pcs_slug, team FROM riders").fetchall()
    return {row["id"]: {"slug": row["pcs_slug"], "team": row["team"]} for row in rows}


def _race_cutoff(race) -> str:
    """
    For races already in progress, use start_date as cutoff so build_features()
    returns empty and predict_from_startlist() handles all stages.
    For future races, use today.
    """
    if race["start_date"] and race["start_date"] < TODAY_STR:
        return race["start_date"]
    return TODAY_STR


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
    pct   = f"{p:.1%}"
    return (
        f'<div class="pbar">'
        f'<div class="pbar-fill" style="width:{width}px;background:{col}"></div>'
        f'<span class="pbar-val">{pct}</span>'
        f'</div>'
    )


def _aux_cell(r: dict, key: str) -> str:
    v = r.get(key)
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return '<td class="aux">–</td>'
    return f'<td class="aux">{v:.1%}</td>'


def _stage_table(rows: list[dict], top_n: int = 20, rider_data: dict | None = None,
                 stage_pcs_slug: str | None = None) -> str:
    rider_data = rider_data or {}

    # Decide aux columns from all rows (not just first)
    has_win  = any(
        "win_prob"  in r and r["win_prob"]  is not None
        and not (isinstance(r["win_prob"],  float) and np.isnan(r["win_prob"]))
        for r in rows[:top_n]
    )
    has_top3 = any(
        "top3_prob" in r and r["top3_prob"] is not None
        and not (isinstance(r["top3_prob"], float) and np.isnan(r["top3_prob"]))
        for r in rows[:top_n]
    )

    trs = ""
    for i, r in enumerate(rows[:top_n], 1):
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else str(i)
        cls   = "podium" if i <= 3 else ("hot" if i <= 10 else "")

        rid   = r.get("rider_id")
        rdata = rider_data.get(rid) if rid and not (isinstance(rid, float) and np.isnan(rid)) else {}
        slug  = (rdata or {}).get("slug")
        team  = _esc((rdata or {}).get("team") or "")

        name_html = _esc(r["rider_name"])
        if slug:
            name_html = (
                f'<a href="{PCS_BASE}/rider/{_esc(slug)}" target="_blank" '
                f'class="rider-link" title="View {_esc(r["rider_name"])} on PCS">'
                f'{name_html}</a>'
            )
        if team:
            name_html += f'<br><span class="rider-team">{team}</span>'

        extra = (_aux_cell(r, "win_prob")  if has_win  else "") + \
                (_aux_cell(r, "top3_prob") if has_top3 else "")

        trs += (
            f'<tr class="{cls}">'
            f'<td class="rank">{medal}</td>'
            f'<td class="name">{name_html}</td>'
            f'<td>{_prob_bar(r["top10_prob"])}</td>'
            f'{extra}'
            f'</tr>'
        )

    extra_th = ""
    if has_win:  extra_th += '<th class="aux">P(win)</th>'
    if has_top3: extra_th += '<th class="aux">P(top3)</th>'

    stage_link = ""
    if stage_pcs_slug:
        stage_link = (
            f'<a href="{PCS_BASE}/{_esc(stage_pcs_slug)}" target="_blank" '
            f'class="stage-ext-link">View on PCS ↗</a>'
        )

    total   = len(rows)
    footer  = ""
    if total > top_n:
        footer = f'<div class="table-footer">Top {top_n} of {total} predicted starters</div>'

    return (
        f'<div class="panel-hd">{stage_link}</div>'
        f'<table>'
        f'<thead><tr><th class="rank">#</th><th>Rider</th>'
        f'<th>P(top10)</th>{extra_th}</tr></thead>'
        f'<tbody>{trs}</tbody>'
        f'</table>'
        f'{footer}'
    )


def _stage_btn(rid: str, idx: int, date_str: str, stage_meta: dict | None,
               is_active: bool) -> str:
    profile  = (stage_meta or {}).get("profile_type", "")
    surface  = (stage_meta or {}).get("surface", "road")
    dist     = (stage_meta or {}).get("distance_km") or 0
    elev     = (stage_meta or {}).get("elevation_m")
    dep      = (stage_meta or {}).get("departure") or ""
    arr      = (stage_meta or {}).get("arrival") or ""

    # B3: unscraped stages often get defaulted to 'mountain' with dist=0
    if not dist and profile == "mountain":
        profile = ""
    label = PROFILE_LABELS.get(profile, "Stage TBC" if not profile else profile.title())
    if surface in ("cobbled", "gravel"):
        label += f" · {surface}"

    col = PROFILE_COLORS.get(profile, "#95a5a6")
    active_cls = " active" if is_active else ""

    stats_parts = []
    if dist:  stats_parts.append(f"{dist:.0f}km")
    if elev:  stats_parts.append(f"{elev:,}m↑")
    stats_str = " · ".join(stats_parts)

    def _short(city: str, n: int = 14) -> str:
        return _esc(city[:n] + "…" if len(city) > n else city)
    route_str = f"{_short(dep)} → {_short(arr)}" if dep and arr else (_esc(dep) or _esc(arr) or "")

    # B7: avoid platform-specific %-d
    try:
        dt = datetime.date.fromisoformat(date_str)
        date_fmt = f'{dt.strftime("%b")} {dt.day}'
    except ValueError:
        date_fmt = date_str[:10]

    psvg = _profile_svg(profile, active=is_active)

    btn_inner = (
        f'<span class="stbtn-date">{date_fmt}</span>'
        f'{psvg}'
        f'<span class="stbtn-label">{_esc(label)}</span>'
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
    flag = _country_flag(race["country"] or "")
    cc   = _esc(race["country"] or "")
    cls  = _esc(race["class"] or "")
    slug = race["pcs_slug"] or ""

    parts = []
    if cc:
        parts.append(f'<span class="meta-item">{flag} {cc}</span>')
    if cls:
        parts.append(f'<span class="badge class-badge">{cls}</span>')
    parts.append(
        f'<a href="{PCS_BASE}/{_esc(slug)}" target="_blank" class="meta-link">View on PCS ↗</a>'
    )
    return f'<div class="race-meta">{"".join(parts)}</div>'


def _race_card(race, preds: pd.DataFrame, idx: int,
               stage_info: dict, rider_data: dict) -> str:
    rid    = f"rc{idx}"
    gender = race["gender"] or "men"
    gbadge = f'<span class="badge g-{_esc(gender)}">{_esc(gender)}</span>'

    # Only show upcoming/today stages for in-progress races
    preds = preds.copy()
    preds["_date_str"] = preds["stage_date"].apply(lambda d: str(d)[:10])
    if race["start_date"] and race["start_date"] < TODAY_STR:
        upcoming = preds[preds["_date_str"] >= TODAY_STR]
        if not upcoming.empty:
            preds = upcoming

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

    # U1: default-select the first stage with date >= today
    default_idx = 0
    for i, (d, _, _) in enumerate(stages):
        if d >= TODAY_STR:
            default_idx = i
            break

    btns = "".join(
        _stage_btn(rid, i, d, meta, i == default_idx)
        for i, (d, meta, _) in enumerate(stages)
    )
    panels = "".join(
        f'<div class="stpanel{"  active" if i == default_idx else ""}">'
        f'{_stage_table(rows, rider_data=rider_data, stage_pcs_slug=(meta or {}).get("pcs_slug"))}'
        f'</div>'
        for i, (_, meta, rows) in enumerate(stages)
    )

    race_type = "Stage race" if race["is_stage_race"] else "One-day"
    info_strip = _race_info_strip(race)

    return f"""
<div class="card" id="{rid}" data-gender="{_esc(gender)}">
  <div class="card-hd">
    <div>
      <span class="race-name">{_esc(race["name"])}</span>
      {gbadge}
      <span class="badge type-badge">{_esc(race_type)}</span>
    </div>
    <span class="dates">{_esc(race["start_date"])} → {_esc(race["end_date"])}</span>
  </div>
  {info_strip}
  <div class="stbar" id="{rid}-bar">{btns}</div>
  <div class="panels">{panels}</div>
</div>"""


def _accuracy_section(metrics_m: dict, metrics_w: dict, recent_m, recent_w, stats: dict) -> str:

    def metric_box(val, lbl, sub=""):
        return (
            f'<div class="mbox">'
            f'<div class="mval">{_esc(val)}</div>'
            f'<div class="mlbl">{_esc(lbl)}</div>'
            f'{"<div class=msub>" + _esc(sub) + "</div>" if sub else ""}'
            f'</div>'
        )

    def _add_boxes(metrics: dict, suffix: str, boxes_acc: list) -> None:
        if metrics.get("val_auc"):
            boxes_acc.append(metric_box(f'{metrics["val_auc"]:.4f}', "Val AUC", suffix))
        if metrics.get("val_ap"):
            boxes_acc.append(metric_box(f'{metrics["val_ap"]:.4f}', "Val AP", suffix))
        if metrics.get("train_rows"):
            boxes_acc.append(metric_box(f'{metrics["train_rows"]:,}', "Train rows", suffix))
        if metrics.get("n_features"):
            boxes_acc.append(metric_box(str(metrics["n_features"]), "Features", suffix))
        if metrics.get("train_cutoff"):
            boxes_acc.append(metric_box(metrics["train_cutoff"], "Last trained", suffix))

    boxes_list: list = []
    _add_boxes(metrics_m, "men",   boxes_list)
    _add_boxes(metrics_w, "women", boxes_list)

    # DB stats
    for tbl, cnt in stats.items():
        boxes_list.append(metric_box(f'{cnt:,}', tbl.capitalize()))

    # Placeholders if no metrics at all
    if not any(metrics_m.get(k) for k in ("val_auc", "val_ap")):
        boxes_list.insert(0, metric_box("n/a", "Val AUC", "run train --val-race"))

    boxes = "".join(boxes_list)

    fi_html = ""
    fi = metrics_m.get("feature_importances", {})
    if fi:
        max_v   = max(fi.values())
        fi_rows = "".join(
            f'<div class="firow">'
            f'<span class="finame">{_esc(name)}</span>'
            f'<div class="fibar" style="width:{int(v/max_v*220)}px"></div>'
            f'<span class="fival">{v:.5f}</span>'
            f'</div>'
            for name, v in list(fi.items())[:25]
        )
        fi_html = f'<h3 class="sec-title">Top Feature Importances (men)</h3><div class="fi-wrap">{fi_rows}</div>'

    def recent_table(rows, title):
        if not rows:
            return f'<p class="muted">No completed {_esc(title)} races found.</p>'
        trs = "".join(
            f'<tr><td><a href="{PCS_BASE}/{_esc(r["pcs_slug"])}" target="_blank" '
            f'class="rider-link">{_esc(r["name"])}</a></td>'
            f'<td>{_esc(r["end_date"])}</td>'
            f'<td class="num">{r["stages"]}</td>'
            f'<td class="num">{r["results"]:,}</td></tr>'
            for r in rows
        )
        return (
            f'<h3 class="sec-title">{_esc(title)} — Recent Completed Races</h3>'
            f'<div class="rtable-wrap"><table>'
            f'<thead><tr><th>Race</th><th>Ended</th>'
            f'<th class="num">Stages</th><th class="num">Results</th></tr></thead>'
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

nav{background:var(--card);border-bottom:2px solid var(--border);padding:0 32px;display:flex;gap:0;position:sticky;top:0;z-index:100;box-shadow:0 2px 6px rgba(0,0,0,.06)}
.navbtn{background:none;border:none;cursor:pointer;padding:12px 22px;font-size:.9rem;color:var(--muted);border-bottom:3px solid transparent;margin-bottom:-2px;font-weight:500;transition:color .15s}
.navbtn.active{color:var(--accent);border-bottom-color:var(--accent)}
.navbtn:hover:not(.active){color:var(--text)}

main{padding:24px 32px;max-width:1100px;margin:0 auto}
section{display:none}
section.active{display:block}

/* Quick-jump chips */
.jumps{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:18px;align-items:center}
.jump-label{font-size:.74rem;color:var(--muted);margin-right:4px}
.jump-chip{padding:3px 11px;border-radius:14px;font-size:.74rem;text-decoration:none;background:var(--card);border:1px solid var(--border);color:var(--text);transition:all .12s}
.jump-chip:hover{border-color:var(--accent);color:var(--accent)}
.jump-chip.g-women{border-color:#f48fb144;color:#c2185b}
.jump-chip.g-women:hover{border-color:#c2185b;background:#fce4ec22}

/* Gender filter */
.filter-bar{display:flex;gap:6px;margin-bottom:16px;align-items:center}
.filter-label{font-size:.78rem;color:var(--muted);margin-right:2px}
.filter-btn{padding:4px 14px;border-radius:14px;font-size:.78rem;border:1px solid var(--border);background:var(--card);cursor:pointer;color:var(--text);transition:all .12s}
.filter-btn.active{background:var(--accent);color:#fff;border-color:var(--accent)}
.filter-btn:hover:not(.active){border-color:var(--accent);color:var(--accent)}

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
.meta-link{color:var(--accent);text-decoration:none;font-weight:500;margin-left:auto}
.meta-link:hover{text-decoration:underline}
.class-badge{background:#e8f0fe;color:#1967d2;padding:2px 7px;border-radius:8px;font-size:.72rem;font-weight:700}

/* Stage tab bar */
.stbar{display:flex;flex-wrap:nowrap;gap:4px;padding:10px 14px;border-bottom:1px solid var(--border);background:#fafbfc;overflow-x:auto;scrollbar-width:thin;scroll-behavior:smooth}
.stbtn{background:#fff;border:1px solid var(--border);border-radius:6px;cursor:pointer;padding:6px 10px;font-size:.76rem;color:var(--muted);text-align:center;transition:all .15s;min-width:90px;max-width:130px;display:flex;flex-direction:column;align-items:center;gap:2px;border-top:3px solid var(--pc,#bdc3c7);flex-shrink:0}
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
td{padding:5px 10px;border-bottom:1px solid #f5f5f5;vertical-align:top}
tr:last-child td{border-bottom:none}
tr:hover td{background:#fafafa}
.rank{color:var(--muted);text-align:right;width:32px;font-size:.8rem;vertical-align:middle}
.name{font-weight:500;line-height:1.3}
.rider-link{color:inherit;text-decoration:none}
.rider-link:hover{color:var(--accent);text-decoration:underline}
.rider-team{font-size:.7rem;color:var(--muted);font-weight:400;display:block}
.aux{color:var(--muted);font-size:.8rem;text-align:right;width:70px;vertical-align:middle}
.podium td{background:#fffdf0}
.podium .name{font-weight:700}
.hot td{background:#f9fffa}
.num{text-align:right}
.table-footer{font-size:.74rem;color:var(--muted);text-align:right;padding:6px 10px 2px;border-top:1px solid var(--border)}

.pbar{display:flex;align-items:center;gap:8px}
.pbar-fill{height:8px;border-radius:4px;transition:width .3s}
.pbar-val{font-size:.8rem;color:var(--muted);min-width:42px}

.mboxes{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:14px;margin-bottom:28px}
.mbox{background:var(--card);border-radius:8px;padding:16px 18px;box-shadow:0 1px 4px rgba(0,0,0,.07)}
.mval{font-size:1.5rem;font-weight:700;color:var(--accent)}
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
footer a{color:inherit}

@media(max-width:640px){
  header,main,nav{padding-left:14px;padding-right:14px}
  nav{padding-left:0;padding-right:0}
  .mboxes{grid-template-columns:repeat(2,1fr)}
  .stbtn{min-width:80px;padding:5px 7px}
  td.aux,th.aux{display:none}
  .pbar-fill{max-width:80px}
  .jumps{display:none}
}
"""

JS = """
function sw(rid, idx) {
  var c = document.getElementById(rid);
  c.querySelectorAll('.stbtn').forEach(function(b,i){ b.classList.toggle('active', i===idx); });
  c.querySelectorAll('.stpanel').forEach(function(p,i){ p.classList.toggle('active', i===idx); });
  var bar = document.getElementById(rid+'-bar');
  var btn = bar.querySelectorAll('.stbtn')[idx];
  if(btn) btn.scrollIntoView({inline:'nearest', block:'nearest', behavior:'smooth'});
}
function nav(id) {
  document.querySelectorAll('.navbtn').forEach(function(b){ b.classList.remove('active'); });
  document.querySelectorAll('main section').forEach(function(s){ s.classList.remove('active'); });
  document.getElementById('nb-'+id).classList.add('active');
  document.getElementById('s-'+id).classList.add('active');
  try{ localStorage.setItem('pcs_tab', id); }catch(e){}
}
function filterGender(g) {
  document.querySelectorAll('.card').forEach(function(c){
    c.style.display = (g === 'all' || c.dataset.gender === g) ? '' : 'none';
  });
  document.querySelectorAll('.filter-btn').forEach(function(b){
    b.classList.toggle('active', b.dataset.filter === g);
  });
}
document.addEventListener('DOMContentLoaded', function(){
  var tab = 'races';
  try{ tab = localStorage.getItem('pcs_tab') || 'races'; }catch(e){}
  nav(tab);
});
"""


# ── Main ───────────────────────────────────────────────────────────────────────

def generate():
    print(f"[{GENERATED}] Generating dashboard …")
    upcoming    = _upcoming_races()
    rider_data  = _rider_id_data_map()
    metrics_m   = _model_metrics("men")
    metrics_w   = _model_metrics("women")
    recent_m    = _recent_races(10, "men")
    recent_w    = _recent_races(10, "women")
    stats       = _db_stats()

    print(f"  {len(upcoming)} race(s) in window  |  {len(rider_data)} riders in map")

    # Build quick-jump chips + gender filter (needs all card ids first)
    cards_html  = ""
    jump_chips  = ""
    card_data   = []  # (idx, race, card_html)

    for i, race in enumerate(upcoming):
        slug       = race["pcs_slug"]
        gender     = race["gender"] or "men"
        stage_info = _stage_info_for_race(race["id"])
        cutoff     = _race_cutoff(race)
        in_progress = race["start_date"] and race["start_date"] < TODAY_STR
        print(f"  {'↺' if in_progress else '→'} {race['name']} ({gender}, cutoff={cutoff}) …",
              end="", flush=True)
        try:
            preds = predict_race(slug, cutoff, gender=gender)
            if preds.empty:
                # U7: stub card for races with no startlist yet
                card_data.append((i, race, None))
                print(" no predictions")
                continue
            card = _race_card(race, preds, i, stage_info, rider_data)
            if card:
                card_data.append((i, race, card))
                n_stages = preds["stage_date"].nunique()
                n_riders = len(preds) // max(n_stages, 1)
                print(f" {n_stages} stage(s), ~{n_riders} riders")
            else:
                card_data.append((i, race, None))
                print(" skipped")
        except Exception as exc:
            card_data.append((i, race, None))
            print(f" ERROR: {exc}")

    # Assemble cards + jump chips
    for i, race, card in card_data:
        rid    = f"rc{i}"
        gender = race["gender"] or "men"
        chip_cls = f"jump-chip g-{gender}"
        try:
            dt = datetime.date.fromisoformat(race["start_date"])
            chip_date = f'{dt.strftime("%b")} {dt.day}'
        except Exception:
            chip_date = (race["start_date"] or "")[:10]
        jump_chips += (
            f'<a href="#{rid}" class="{chip_cls}">'
            f'{chip_date} · {_esc(race["name"])}'
            f'</a>'
        )
        if card:
            cards_html += card
        else:
            # U7: stub card
            cards_html += (
                f'<div class="card" id="{rid}" data-gender="{_esc(gender)}">'
                f'<div class="card-hd">'
                f'<span class="race-name">{_esc(race["name"])}</span>'
                f'<span class="badge g-{_esc(gender)}">{_esc(gender)}</span>'
                f'<span class="dates">{_esc(race["start_date"])} → {_esc(race["end_date"])}</span>'
                f'</div>'
                f'{_race_info_strip(race)}'
                f'<div style="padding:14px 18px;color:var(--muted);font-size:.85rem">'
                f'No startlist available yet — check back closer to race day.</div>'
                f'</div>'
            )

    if not any(card for _, _, card in card_data):
        cards_html = (
            '<div class="card" style="padding:24px;color:var(--muted)">'
            'No upcoming races with predictions found. '
            f'Run <code>python main.py scrape --years {TODAY.year}</code> to update.'
            '</div>'
        )

    filter_bar = (
        '<div class="filter-bar">'
        '<span class="filter-label">Filter:</span>'
        '<button class="filter-btn active" data-filter="all" onclick="filterGender(\'all\')">All</button>'
        '<button class="filter-btn" data-filter="men" onclick="filterGender(\'men\')">Men</button>'
        '<button class="filter-btn" data-filter="women" onclick="filterGender(\'women\')">Women</button>'
        '</div>'
    )

    races_section = (
        f'<div class="jumps"><span class="jump-label">Jump to:</span>{jump_chips}</div>'
        f'{filter_bar}'
        f'{cards_html}'
    )

    acc_html = _accuracy_section(metrics_m, metrics_w, recent_m, recent_w, stats)

    favicon = (
        '<link rel="icon" href="data:image/svg+xml,'
        '<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22>'
        '<text y=%22.9em%22 font-size=%2290%22>🚴</text></svg>">'
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>🚴 PCS Race Predictor</title>
  {favicon}
  <style>{CSS}</style>
</head>
<body>
<header>
  <div>
    <h1>🚴 PCS Race Predictor</h1>
    <p>Top-10 finish probabilities · Gradient-boosted ML model</p>
  </div>
  <p class="updated">Updated {GENERATED}</p>
</header>
<nav>
  <button id="nb-races"    class="navbtn active" onclick="nav('races')">🔮 Upcoming Races</button>
  <button id="nb-accuracy" class="navbtn"        onclick="nav('accuracy')">📊 Model &amp; Data</button>
</nav>
<main>
  <section id="s-races" class="active">{races_section}</section>
  <section id="s-accuracy">{acc_html}</section>
</main>
<footer>PCS Predictor · Data from <a href="{PCS_BASE}" target="_blank">ProCyclingStats</a> · {GENERATED}</footer>
<script>{JS}</script>
</body>
</html>"""

    out = OUTPUT_DIR / "index.html"
    out.write_text(html, encoding="utf-8")
    size_kb = out.stat().st_size / 1024
    print(f"\n✓ Dashboard written to {out}  ({size_kb:.1f} KB)")


if __name__ == "__main__":
    generate()
