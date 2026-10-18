"""Interactive HTML chart for the weekly team-UCI-points payload.

:func:`render_html` turns the payload built by
:func:`analysis.team_points.build_chart_payload` into one self-contained HTML
page (inline CSS + vanilla JS + inline SVG, no external requests):

* a short header (title, dek, the rider sentence naming the basis of his
  rank, coverage figures) so the season chart comes in the first screen;
* the season overview: cumulative season-to-date UCI points per ISO week for
  every team (muted grey lines), up to three highlighted teams in colour, the
  ``payload['riders']`` series (Pogačar alone) as a dashed yellow line that is
  always shown, direct end labels, a crosshair tooltip and jump markers;
* small multiples, one panel per team in official-rank order (each rider panel
  inserted where the rider would rank), cumulative line + weekly-gain strip,
  numbered jump markers and the jump list; shared or per-panel y scale;
* a standings table (the chart's accessible twin), the rider row interleaved
  at his ``would_rank``.

A metric toggle switches every view between all riders (``cumulative``) and the
UCI rule of a team's best 20 riders (``cumulative_best20``); in best-20 mode a
jump's gain is shown in that metric with the all-rider gain beside it.

Optional payload keys: ``meta.axis_start`` (first plotted week: the
late-October weeks of the previous year stay in the data but are not drawn;
each line opens at its value of that week) and ``weeks_meta`` (per week the
races that brought the charted teams the most points: the tooltip's "biggest
race of the week" and the race rules above the season chart; without it both
fall back to the annotated jumps' contributors).  Below the standings, a
"Sources and reconciliation" section carries the source line, the
reconciliation note and the coverage notes.

The payload contract is documented in ``analysis.team_points``; this module
only reads it and tolerates missing optional keys (empty ``weeks``, no
``riders``, teams without ``jumps``).  Strings from the payload reach the DOM
through ``textContent`` only, and the JSON is embedded in a
``<script type="application/json">`` block with ``<``, ``>`` and ``&``
escaped as ``\\u003c``/``\\u003e``/``\\u0026`` so a label such as
``</script>`` cannot end the block.

Colour (validated with the dataviz skill's ``validate_palette.js``, all-pairs,
both modes): highlighted teams take blue / magenta / green, the rider series
takes yellow.  No larger set that includes yellow passes the all-pairs floors,
which is why at most three teams are coloured at once.  Dark mode uses the
palette's dark steps; its yellow-vs-green pair sits in the 6-8 CVD warning band
and is backed by the rider line's dash pattern and direct label.

CLI (writes a page from a payload JSON, or the synthetic demo)::

    python -m analysis.team_points_chart --demo --out /tmp/demo.html
    python -m analysis.team_points_chart --payload team_points.json --out chart.html
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import math
import random
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

PAGE_TITLE = "Team UCI Points"

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# ---------------------------------------------------------------------------
# small formatting helpers (mirrored by the page's JS so static and live text agree)
# ---------------------------------------------------------------------------

def _num(value: Any) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return v if math.isfinite(v) else 0.0


def _fmt(value: Any) -> str:
    """Thousands separators; one decimal only for small fractional values (e.g. TTT shares)."""
    v = _num(value)
    a = abs(v)
    text = f"{a:,.1f}" if a < 100 and round(a, 1) % 1 else f"{a:,.0f}"
    if text.endswith(".0"):
        text = text[:-2]
    return ("\u2212" if v < 0 and text != "0" else "") + text


def _ordinal(n: Any) -> str:
    k = int(round(_num(n)))
    if 11 <= k % 100 <= 13:
        return f"{k}th"
    return f"{k}{({1: 'st', 2: 'nd', 3: 'rd'}).get(k % 10, 'th')}"


def _day(text: Any) -> str:
    """'2026-10-18' -> 'Oct 18, 2026' (anything unparsable is returned as-is)."""
    try:
        d = date.fromisoformat(str(text)[:10])
    except ValueError:
        return str(text or "")
    return f"{_MONTHS[d.month - 1]} {d.day}, {d.year}"


def _esc(text: Any) -> str:
    return html.escape("" if text is None else str(text), quote=True)


def _sanitize(obj: Any) -> Any:
    """Make the payload strict-JSON safe: non-finite floats -> None, tuples -> lists."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    if hasattr(obj, "item") and not isinstance(obj, (str, bytes)):  # numpy scalars
        try:
            return _sanitize(obj.item())
        except (TypeError, ValueError):
            return str(obj)
    return obj


def _json_for_script(payload: Any) -> str:
    """JSON text safe inside ``<script type="application/json">``.

    ``<``, ``>`` and ``&`` only ever occur inside JSON strings, where the
    ``\\u00XX`` escapes are equivalent, so ``</script>`` or ``<!--`` in a label
    can neither close the element nor switch the HTML tokenizer's state.
    """
    text = json.dumps(_sanitize(payload), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return (text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


# ---------------------------------------------------------------------------
# page
# ---------------------------------------------------------------------------

_CSS = r"""
/* Layout: one reading column (max 1240px): masthead, control row (sticky on wide screens),
   season overview, small-multiple grid, standings table. Quiet chart chrome; the rider's
   yellow-jersey dashed line is the page's one loud colour. */
:root {
  --plane: #f4f4f1;
  --surface: #fcfcfb;
  --ink: #0b0b0b;
  --ink-2: #52514e;
  --muted: #6c6b66;
  --tick: #6c6b66;   /* 5.2:1 on --surface, 4.85:1 on --plane (WCAG AA small text) */
  --grid: #e5e4de;
  --axis: #c3c2b7;
  --line: #d2d0c7;
  --line-lab: #a4a298;
  --bar: #cbc9c0;
  --wash: rgba(11, 11, 11, 0.045);
  --border: rgba(11, 11, 11, 0.11);
  --hl-1: #2a78d6;
  --hl-2: #e87ba4;
  --hl-3: #008300;
  --rider: #eda100;
  --rider-wash: rgba(237, 161, 0, 0.09);
  --neg: #d03b3b;
  --focus: #2a78d6;
  --shadow: 0 10px 30px rgba(24, 22, 10, 0.14), 0 1px 3px rgba(24, 22, 10, 0.08);
  --font-ui: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --plane: #0f0f0e;
    --surface: #1a1a19;
    --ink: #f0efec;
    --ink-2: #c3c2b7;
    --muted: #a09f97;
    --tick: #8f8e87;
    --grid: #2a2a28;
    --axis: #3d3d3a;
    --line: #3b3b38;
    --line-lab: #64635d;
    --bar: #4a4945;
    --wash: rgba(255, 255, 255, 0.06);
    --border: rgba(255, 255, 255, 0.11);
    --hl-1: #3987e5;
    --hl-2: #d55181;
    --hl-3: #008300;
    --rider: #c98500;
    --rider-wash: rgba(201, 133, 0, 0.13);
    --focus: #6da7ec;
    --shadow: 0 10px 30px rgba(0, 0, 0, 0.55), 0 1px 3px rgba(0, 0, 0, 0.4);
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --plane: #0f0f0e;
  --surface: #1a1a19;
  --ink: #f0efec;
  --ink-2: #c3c2b7;
  --muted: #a09f97;
  --tick: #8f8e87;
  --grid: #2a2a28;
  --axis: #3d3d3a;
  --line: #3b3b38;
  --line-lab: #64635d;
  --bar: #4a4945;
  --wash: rgba(255, 255, 255, 0.06);
  --border: rgba(255, 255, 255, 0.11);
  --hl-1: #3987e5;
  --hl-2: #d55181;
  --hl-3: #008300;
  --rider: #c98500;
  --rider-wash: rgba(201, 133, 0, 0.13);
  --focus: #6da7ec;
  --shadow: 0 10px 30px rgba(0, 0, 0, 0.55), 0 1px 3px rgba(0, 0, 0, 0.4);
  color-scheme: dark;
}
[hidden] { display: none !important; }
*, *::before, *::after { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  background: var(--plane);
  color: var(--ink);
  font: 15px/1.5 var(--font-ui);
  -webkit-font-smoothing: antialiased;
}
body::before {
  content: ""; position: fixed; inset: 0 0 auto; z-index: 30;
  height: env(safe-area-inset-top, 0px); background: var(--plane);
}
.page {
  max-width: 1240px; margin: 0 auto;
  padding-left: max(16px, env(safe-area-inset-left, 0px));
  padding-right: max(16px, env(safe-area-inset-right, 0px));
  padding-block: 28px 56px;
  display: grid; gap: 30px;
}
.page > * { min-width: 0; }
button, select { font: inherit; }
:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
.sr {
  position: absolute !important; width: 1px; height: 1px; margin: -1px; padding: 0;
  overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0;
}

/* masthead */
.masthead { display: grid; gap: 14px; }
.eyebrow { margin: 0; font-size: 12px; font-weight: 650; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); }
h1 {
  margin: 0; max-width: 24ch;
  font-size: clamp(27px, 1.6vw + 20px, 42px); line-height: 1.07; font-weight: 760; letter-spacing: -0.022em;
  text-wrap: balance;
}
.dek { margin: 0; max-width: 86ch; font-size: 16px; color: var(--ink-2); text-wrap: pretty; }
.dek-phone { display: none; }
.dek-rider {
  margin: 0; display: flex; align-items: baseline; gap: 10px; max-width: 100ch;
  font-size: 15px; font-weight: 600; color: var(--ink);
}
.dek-rider .key { transform: translateY(-4px); }
.facts { margin: 0; display: flex; flex-wrap: wrap; gap: 6px 24px; font-size: 13px; }
.facts div { display: flex; gap: 6px; align-items: baseline; }
.facts dt { color: var(--muted); }
.facts dd { margin: 0; font-weight: 650; font-variant-numeric: tabular-nums; }
.facts a { color: var(--ink-2); font-weight: 500; text-underline-offset: 3px; text-decoration-color: var(--axis); }
.prov {
  margin: 0; display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 6px 16px;
  font-size: 13px; color: var(--ink-2); max-width: 104ch;
  padding-top: 12px; border-top: 1px solid var(--border);
}
.prov dt {
  color: var(--muted); font-size: 11px; font-weight: 650; text-transform: uppercase; letter-spacing: 0.07em;
  padding-top: 2px;
}
.prov dd { margin: 0; min-width: 0; overflow-wrap: anywhere; }
.prov details summary { cursor: pointer; color: var(--ink); font-weight: 600; width: max-content; max-width: 100%; }
#sources .prov { border-top: 0; padding-top: 0; }
.prov ul { margin: 8px 0 0; padding-left: 18px; display: grid; gap: 6px; }

/* controls */
.controls { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 22px; padding-block: 10px; }
@media (min-width: 760px) {
  .controls {
    position: sticky; top: env(safe-area-inset-top, 0px); z-index: 20;
    background: var(--plane); border-bottom: 1px solid var(--border);
  }
}
.ctl { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; min-width: 0; }
.ctl-label { font-size: 11px; font-weight: 650; text-transform: uppercase; letter-spacing: 0.07em; color: var(--muted); }
.seg {
  display: inline-flex; flex-wrap: wrap; padding: 2px; gap: 2px;
  border: 1px solid var(--border); border-radius: 999px; background: var(--surface);
}
.seg button {
  border: 0; background: transparent; color: var(--ink-2); cursor: pointer;
  font-size: 13px; padding: 5px 12px; border-radius: 999px; line-height: 1.3;
}
.seg button:hover { background: var(--wash); color: var(--ink); }
.seg button[aria-pressed="true"] { background: var(--ink); color: var(--surface); }
select {
  font-size: 13px; color: var(--ink); background: var(--surface); max-width: 100%;
  border: 1px solid var(--border); border-radius: 8px; padding: 6px 10px; min-height: 32px;
  width: 290px;
}
.chips { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 8px; min-width: 0; }
.chip {
  display: inline-flex; align-items: center; gap: 7px; cursor: pointer; max-width: 100%;
  font-size: 12.5px; color: var(--ink); background: var(--surface);
  border: 1px solid var(--border); border-radius: 999px; padding: 4px 9px 4px 10px; line-height: 1.3;
}
.chip:hover { background: var(--wash); }
.chip .x { color: var(--muted); font-size: 15px; line-height: 1; margin-left: 1px; }
.chip-rider[aria-pressed="false"] { color: var(--muted); background: transparent; }
.chip-rider .state { color: var(--muted); font-size: 11.5px; }
.hint { font-size: 12px; color: var(--muted); }
.key {
  display: inline-block; flex: none; width: 18px; height: 0; border-radius: 2px;
  border-top: 2.5px solid var(--k, var(--ink-2));
}
.key-rider { border-top-style: dashed; border-top-color: var(--rider); }
.key-dot {
  display: inline-block; flex: none; width: 10px; height: 10px; border-radius: 50%;
  background: var(--ink-2); box-shadow: 0 0 0 2px var(--surface);
}

/* sections */
.sec-head {
  display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: space-between;
  gap: 10px 24px; margin-bottom: 14px;
}
.sec-head > div:first-child { min-width: 0; flex: 1 1 320px; }
.sec-head h2 { margin: 0; font-size: 21px; line-height: 1.2; letter-spacing: -0.012em; text-wrap: balance; }
.sec-head p { margin: 4px 0 0; color: var(--ink-2); font-size: 14px; max-width: 72ch; }
.figure {
  margin: 0; background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
  padding: 14px 12px 12px 8px; display: grid; gap: 10px;
}
.ov-host { min-height: 330px; min-width: 0; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 18px; padding-left: 8px; font-size: 12.5px; color: var(--ink-2); }
.legend span { display: inline-flex; align-items: center; gap: 7px; }
.legend .key { --k: var(--line-lab); }
.empty { margin: 24px 8px; color: var(--muted); }

/* svg chrome */
.ov-svg, .mini { display: block; touch-action: pan-y; -webkit-tap-highlight-color: transparent; }
.ov-svg { cursor: crosshair; }
.ov-svg:focus:not(:focus-visible), .mini:focus:not(:focus-visible), .jump-li:focus:not(:focus-visible) { outline: none; }
.ov-svg:focus-visible, .mini:focus-visible { outline: 2px solid var(--focus); outline-offset: 3px; border-radius: 4px; }
.gridline { stroke: var(--grid); stroke-width: 1; shape-rendering: crispEdges; }
.axis { stroke: var(--axis); stroke-width: 1; shape-rendering: crispEdges; }
.tick { fill: var(--tick); font-size: 11px; font-variant-numeric: tabular-nums; }
.race-rule { stroke: var(--grid); stroke-width: 1; }
.race-label { fill: var(--muted); font-size: 10.5px; font-weight: 550; }
.ln { fill: none; stroke-linejoin: round; stroke-linecap: round; }
.ln-muted { stroke: var(--line); stroke-width: 1.25; }
.ln-muted.ln-lab { stroke: var(--line-lab); }
.ln-hl { stroke-width: 2; }
.ln-rider { stroke: var(--rider); stroke-width: 2; stroke-dasharray: 7 4; stroke-linecap: butt; }
.ln-rider.on { stroke-width: 2.5; }
.ln-hover { stroke: var(--ink); stroke-width: 2.25; }
.end-dot, .cur-dot { stroke: var(--surface); stroke-width: 2; }
.leader { stroke: var(--axis); stroke-width: 1; fill: none; }
.lab-key { stroke-width: 2.5; stroke-linecap: round; }
.lab-key.dashed { stroke-dasharray: 4 2.5; stroke-linecap: butt; }
.lab { fill: var(--ink-2); font-size: 11px; font-weight: 500; font-variant-numeric: tabular-nums; }
.lab.is-hl { fill: var(--ink); font-weight: 650; }
.jm { stroke: var(--surface); stroke-width: 2; cursor: pointer; }
.jm:focus { outline: none; stroke: var(--ink); }
.crosshair { stroke: var(--ink-2); stroke-opacity: 0.55; stroke-width: 1; }
.cursor, .cursor * { pointer-events: none; }

/* small multiples */
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 272px), 1fr)); gap: 14px; }
.panel {
  --c: var(--ink-2);
  min-width: 0; display: flex; flex-direction: column; gap: 8px;
  background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 12px 12px 12px;
}
.panel.is-hl { border-color: color-mix(in srgb, var(--c) 60%, transparent); }
.panel-rider {
  background: linear-gradient(var(--rider-wash), var(--rider-wash)), var(--surface);
  border: 1.5px dashed var(--rider);
}
.p-head { display: flex; align-items: flex-start; gap: 10px; }
.bib {
  position: relative; flex: none; display: grid; justify-items: center; min-width: 46px;
  padding: 3px 7px 4px; background: var(--surface); color: var(--ink);
  border: 1px solid var(--border); border-radius: 3px;
  background-image:
    radial-gradient(circle at 5px 5px, var(--axis) 1.6px, transparent 2px),
    radial-gradient(circle at calc(100% - 5px) 5px, var(--axis) 1.6px, transparent 2px),
    radial-gradient(circle at 5px calc(100% - 5px), var(--axis) 1.6px, transparent 2px),
    radial-gradient(circle at calc(100% - 5px) calc(100% - 5px), var(--axis) 1.6px, transparent 2px);
}
.bib-cap { font-size: 8.5px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); line-height: 1.4; }
.bib-num { font-size: 21px; font-weight: 800; line-height: 1; font-variant-numeric: tabular-nums; letter-spacing: -0.02em; }
.bib-rider { border: 1.5px dashed var(--rider); }
.p-id { flex: 1; min-width: 0; }
.p-name {
  margin: 0; display: flex; flex-wrap: wrap; align-items: center; gap: 4px 7px;
  font-size: 15.5px; line-height: 1.25; font-weight: 720;
}
.p-name .nm { overflow-wrap: anywhere; }
.badge {
  font-size: 10.5px; font-weight: 650; letter-spacing: 0.05em; text-transform: uppercase; color: var(--ink-2);
  border: 1px solid var(--border); border-radius: 4px; padding: 0 5px; line-height: 17px; white-space: nowrap;
}
.badge-rider { border: 1px dashed var(--rider); color: var(--ink); text-transform: none; letter-spacing: 0; }
.p-full { margin: 2px 0 0; font-size: 12px; color: var(--muted); overflow-wrap: anywhere; }
.hl-btn {
  flex: none; position: relative; width: 30px; height: 30px; display: grid; place-items: center; cursor: pointer;
  color: var(--muted); background: transparent; border: 1px solid var(--border); border-radius: 50%; padding: 0;
}
.hl-btn svg { width: 16px; height: 16px; display: block; }
.hl-btn svg path { fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
.hl-btn:hover { background: var(--wash); color: var(--ink); }
.hl-btn[aria-pressed="true"] { color: var(--c); border-color: var(--c); background: color-mix(in srgb, var(--c) 12%, transparent); }
.panel-rider .hl-btn[aria-pressed="true"] { color: var(--rider); border-color: var(--rider); background: color-mix(in srgb, var(--rider) 14%, transparent); }
.p-stats { margin: 0; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0 10px; }
.p-stats dt { font-size: 10px; font-weight: 650; letter-spacing: 0.06em; text-transform: uppercase; color: var(--muted); }
.p-stats dd { margin: 0; font-size: 14px; font-weight: 600; color: var(--ink-2); font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.p-stats .is-metric dd { color: var(--ink); font-weight: 750; }
.p-chart { min-width: 0; }
.mini-line { fill: none; stroke: var(--c); stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
.mini-line.dashed { stroke: var(--rider); stroke-dasharray: 5 3.5; stroke-linecap: butt; }
.mini-area { fill: var(--c); fill-opacity: 0.08; }
.panel-rider .mini-area { fill: var(--rider); fill-opacity: 0.12; }
.bar { fill: var(--bar); }
.bar-jump { fill: var(--c); }
.panel-rider .bar-jump { fill: var(--rider); }
.bar-neg { fill: var(--neg); }
.pt-dot { fill: var(--c); stroke: var(--surface); stroke-width: 1.5; }
.panel-rider .pt-dot { fill: var(--rider); }
.jm-mini circle { fill: var(--surface); stroke: var(--c); stroke-width: 1.5; }
.panel-rider .jm-mini circle { stroke: var(--rider); }
.jm-mini text { fill: var(--ink); font-size: 9.5px; font-weight: 750; }
.jm-mini line { stroke: var(--axis); stroke-width: 1; }
.jumps { list-style: none; margin: 0; padding: 0; display: grid; gap: 2px; font-size: 12.5px; line-height: 1.38; }
.jump-li {
  display: grid; grid-template-columns: 18px minmax(0, 1fr); gap: 7px; align-items: start;
  padding: 3px 5px; margin: 0 -5px; border-radius: 6px; cursor: default;
}
.jump-li:hover, .jump-li:focus-visible { background: var(--wash); }
.jump-li:focus-visible { outline-offset: 0; }
.jn {
  width: 17px; height: 17px; border-radius: 50%; margin-top: 1px; display: grid; place-items: center;
  border: 1.5px solid var(--c); font-size: 9.5px; font-weight: 750; font-variant-numeric: tabular-nums;
}
.panel-rider .jn { border-color: var(--rider); }
.jt { min-width: 0; overflow-wrap: anywhere; color: var(--ink-2); }
.jt time { color: var(--muted); font-variant-numeric: tabular-nums; }
.jt strong { color: var(--ink); font-variant-numeric: tabular-nums; }
.p-none { margin: 0; font-size: 12.5px; color: var(--muted); }

/* table */
.table-wrap {
  overflow-x: auto; background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  -webkit-overflow-scrolling: touch;
}
table { width: 100%; min-width: 820px; border-collapse: collapse; font-size: 13.5px; }
th {
  text-align: left; white-space: nowrap; padding: 10px 12px;
  font-size: 11px; font-weight: 650; letter-spacing: 0.06em; text-transform: uppercase; color: var(--muted);
  border-bottom: 1px solid var(--axis);
}
td { padding: 9px 12px; border-bottom: 1px solid var(--grid); vertical-align: top; }
tbody tr:last-child td { border-bottom: 0; }
.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
th.is-metric { color: var(--ink); box-shadow: inset 0 -2px 0 var(--ink); }
td.is-metric { color: var(--ink); font-weight: 680; }
td.num:not(.is-metric) { color: var(--ink-2); }
.rk { font-weight: 700; font-variant-numeric: tabular-nums; white-space: nowrap; }
.t-btn {
  appearance: none; border: 0; background: none; padding: 0; margin: 0; color: var(--ink); cursor: pointer;
  display: inline-flex; align-items: center; gap: 8px; font-weight: 680; text-align: left; border-radius: 3px;
}
.t-btn .key { width: 14px; --k: var(--axis); }
.is-hl .t-btn .key { --k: var(--c); }
.t-btn:hover .t-nm { text-decoration: underline; text-underline-offset: 3px; text-decoration-color: var(--axis); }
.t-full { display: block; font-size: 12px; color: var(--muted); margin-top: 1px; }
.row-rider td { background: var(--rider-wash); }
.row-rider td:first-child { box-shadow: inset 3px 0 0 var(--rider); }
.row-rider .t-btn .key { border-top-style: dashed; --k: var(--rider); }
.pill {
  display: inline-block; margin-left: 6px; font-size: 10.5px; font-weight: 650; padding: 0 6px; line-height: 17px;
  border: 1px dashed var(--rider); border-radius: 999px; color: var(--ink); vertical-align: 1px;
}
.cls { color: var(--ink-2); font-size: 12px; font-weight: 600; letter-spacing: 0.04em; }
.jump-cell { min-width: 240px; max-width: 380px; color: var(--ink-2); font-size: 12.5px; line-height: 1.4; }
.jump-cell strong { color: var(--ink); font-variant-numeric: tabular-nums; }
.jump-cell time { color: var(--muted); }

/* tooltip */
.tip {
  position: fixed; left: 0; top: 0; z-index: 60; pointer-events: none;
  width: max-content; max-width: min(370px, calc(100vw - 16px));
  background: var(--surface); color: var(--ink); border: 1px solid var(--border); border-radius: 10px;
  box-shadow: var(--shadow); padding: 10px 12px 10px; font-size: 12.5px; line-height: 1.35;
}
.tt-head { display: grid; gap: 1px; margin-bottom: 7px; }
.tt-head strong { font-size: 13px; }
.tt-sub { color: var(--muted); font-size: 11.5px; }
.tt-rows {
  display: grid; grid-template-columns: 18px auto minmax(0, 1fr) auto auto;
  column-gap: 8px; row-gap: 3px; align-items: center;
}
.tt-row, .tt-colhead { display: contents; }
.tt-colhead span { font-size: 10px; font-weight: 650; letter-spacing: 0.06em; text-transform: uppercase; color: var(--muted); }
.tt-colhead .r { text-align: right; }
.tt-val { text-align: right; font-variant-numeric: tabular-nums; font-weight: 700; }
.tt-name { color: var(--ink-2); overflow-wrap: anywhere; }
.tt-row.is-hover .tt-name { color: var(--ink); font-weight: 650; }
.tt-gain, .tt-rank { color: var(--muted); text-align: right; font-variant-numeric: tabular-nums; font-size: 11.5px; }
.tt-gain.neg { color: var(--neg); }
.tt-jump { margin-top: 9px; padding-top: 8px; border-top: 1px solid var(--grid); }
.tt-jt { display: flex; align-items: baseline; gap: 7px; color: var(--ink-2); }
.tt-jt strong { color: var(--ink); font-variant-numeric: tabular-nums; }
.tt-jt .key { transform: translateY(-3px); }
.tt-jl { font-weight: 650; margin: 2px 0 5px; }
.tt-contrib { list-style: none; margin: 0; padding: 0; display: grid; gap: 4px; }
.tt-contrib li { display: grid; grid-template-columns: 48px minmax(0, 1fr); column-gap: 8px; }
.c-pts { grid-row: 1 / span 2; text-align: right; font-weight: 650; font-variant-numeric: tabular-nums; }
.c-who { overflow-wrap: anywhere; }
.c-det { grid-column: 2; color: var(--muted); font-size: 11.5px; overflow-wrap: anywhere; }
.c-more { color: var(--muted); }
.tt-foot { margin-top: 8px; color: var(--muted); font-size: 11.5px; }
.foot { font-size: 12px; color: var(--muted); display: grid; gap: 4px; }
.foot p { margin: 0; overflow-wrap: anywhere; }
@media (prefers-reduced-motion: no-preference) {
  .chip, .seg button, .hl-btn, .jump-li { transition: background-color 120ms ease, color 120ms ease; }
}
@media (max-width: 600px) {
  /* phone: Rank, Team, Official, All riders, Best 20 fit without sideways
     scrolling; the class column, full names and the jump text are in the panels */
  table { min-width: 0; font-size: 13px; }
  th, td { padding: 8px 6px; }
  th { letter-spacing: 0.02em; }
  .col-cls, .col-jump, .t-full { display: none; }
  /* a shorter masthead so the season chart starts in the first screen
     (390x844: the dek's short form, the rider sentence without the team
     comparison, two fact rows, the Highlight label beside its select).
     '.facts .fact-minor' outranks '.facts div { display: flex }'. */
  .page { gap: 24px; }
  .masthead { gap: 10px; }
  .dek { font-size: 15px; }
  .dek-full, .dek-more, .fact-long, .controls .hint { display: none; }
  .dek-phone { display: inline; }
  .facts .fact-minor { display: none; }
  .facts dt { white-space: nowrap; }
  .controls { padding-block: 4px; gap: 8px 16px; }
  .ctl-pick { flex-wrap: nowrap; width: 100%; }
  .ctl-pick select { width: auto; flex: 1 1 auto; min-width: 0; }
  .t-btn .key { display: none; }
  .rk, th.col-rank { position: sticky; left: 0; z-index: 1; background: var(--surface); }
  .row-rider .rk { background: linear-gradient(var(--rider-wash), var(--rider-wash)), var(--surface); }
}
@media (max-width: 520px) {
  .prov { grid-template-columns: minmax(0, 1fr); gap: 2px 0; }
  .prov dd { margin-bottom: 8px; }
  .figure { padding: 10px 6px 10px 2px; }
}
"""

_JS = r"""
(() => {
  'use strict';
  const $ = (q, r = document) => r.querySelector(q);
  const $$ = (q, r = document) => Array.from(r.querySelectorAll(q));

  let P = {};
  try { P = JSON.parse($('#tp-data').textContent) || {}; } catch (err) { P = {}; }
  if (!P || typeof P !== 'object') P = {};
  const META = (P.meta && typeof P.meta === 'object') ? P.meta : {};
  const WEEKS = Array.isArray(P.weeks) ? P.weeks.map(String) : [];
  const NW = WEEKS.length;
  // First plotted week (meta.axis_start): the late-October weeks of the
  // previous year are kept in the data but not drawn; each line opens at its
  // value of that week, which carries the earlier points.
  const I0 = (() => { const i = WEEKS.indexOf(String(META.axis_start || '')); return i > 0 && i < NW - 1 ? i : 0; })();
  const SPAN = Math.max(0, NW - 1 - I0);
  const WMETA = Array.isArray(P.weeks_meta) && P.weeks_meta.length === NW ? P.weeks_meta : null;
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const HL = ['var(--hl-1)', 'var(--hl-2)', 'var(--hl-3)'];
  const MAX_HL = HL.length;
  const FONT = getComputedStyle(document.body).fontFamily || 'system-ui, sans-serif';
  const SVGNS = 'http://www.w3.org/2000/svg';

  /* ---------- helpers ---------- */
  const num = v => { v = Number(v); return Number.isFinite(v) ? v : 0; };
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const seriesOf = a => { const o = new Array(NW); for (let i = 0; i < NW; i++) o[i] = num(Array.isArray(a) ? a[i] : 0); return o; };
  const diff = c => c.map((v, i) => (i ? v - c[i - 1] : v));
  function parseDay(s) {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(s == null ? '' : s));
    return m ? new Date(Date.UTC(+m[1], +m[2] - 1, +m[3])) : null;
  }
  function dayLabel(s) { const d = parseDay(s); return d ? MONTHS[d.getUTCMonth()] + ' ' + d.getUTCDate() : String(s == null ? '' : s); }
  function fullDay(s) { const d = parseDay(s); return d ? dayLabel(s) + ', ' + d.getUTCFullYear() : String(s == null ? '' : s); }
  function isoWeek(d) {
    const t = new Date(d.getTime()); const wd = t.getUTCDay() || 7;
    t.setUTCDate(t.getUTCDate() + 4 - wd);
    return Math.ceil(((t - Date.UTC(t.getUTCFullYear(), 0, 1)) / 864e5 + 1) / 7);
  }
  const NF0 = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });
  const NF1 = new Intl.NumberFormat('en-US', { maximumFractionDigits: 1 });
  function fmt(v) {
    v = num(v); const a = Math.abs(v);
    const s = (a < 100 && Math.round(a * 10) % 10 !== 0 ? NF1 : NF0).format(a);
    return (v < 0 && s !== '0' ? '\u2212' : '') + s;
  }
  function signed(v) { v = num(v); const s = fmt(v); return v > 0 && s !== '0' ? '+' + s : s; }
  function compact(v) {
    v = num(v); const a = Math.abs(v); let s;
    if (a >= 1000) s = (a / 1000).toFixed(a >= 1e5 ? 0 : 1).replace(/\.0$/, '') + 'K';
    else s = (a < 10 && Math.round(a * 10) % 10 !== 0 ? NF1 : NF0).format(a);
    return (v < 0 && s !== '0' ? '\u2212' : '') + s;
  }
  function ordinal(n) {
    n = Math.round(num(n)); const v = Math.abs(n) % 100;
    return n + ((v >= 11 && v <= 13) ? 'th' : (['th', 'st', 'nd', 'rd'][v % 10] || 'th'));
  }
  function niceStep(range, count) {
    const raw = range / Math.max(1, count); const p = Math.pow(10, Math.floor(Math.log10(raw))); const f = raw / p;
    return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
  }
  // Nice y ticks. A small negative minimum (a sanction before any points) gets a padded floor
  // instead of a whole extra tick step; among counts minCount..maxCount the least headroom wins.
  function ticks1(lo, hi, count) {
    if (!(hi > lo)) hi = lo + 10;
    const st = niceStep(hi - Math.min(lo, 0), count);
    let a = Math.floor(lo / st + 1e-9) * st;
    const b = Math.max(Math.ceil(hi / st - 1e-9) * st, a + st);
    if (lo < 0 && -lo < st * 0.35) a = lo - Math.max(-lo * 0.25, (b - lo) * 0.015);
    const out = [];
    for (let k = Math.ceil(a / st - 1e-9); k * st <= b + st * 1e-6 && out.length < 60; k++) out.push(+(k * st).toFixed(6));
    return { lo: a, hi: b, ticks: out };
  }
  function ticks(lo, hi, minCount, maxCount) {
    let best = null;
    for (let c = minCount; c <= (maxCount || minCount); c++) {
      const t = ticks1(lo, hi, c);
      if (t.ticks.length > (maxCount || minCount) + 2) continue;
      if (!best || t.hi - hi <= best.hi - hi + 1e-9) best = t;
    }
    return best || ticks1(lo, hi, minCount);
  }
  function setAttrs(n, attrs) {
    if (!attrs) return;
    for (const k of Object.keys(attrs)) {
      const v = attrs[k];
      if (v == null || v === false) continue;
      if (k === 'text') n.textContent = String(v);
      else n.setAttribute(k, v === true ? '' : String(v));
    }
  }
  function S(tag, attrs, parent) { const n = document.createElementNS(SVGNS, tag); setAttrs(n, attrs); if (parent) parent.appendChild(n); return n; }
  function E(tag, attrs, parent) { const n = document.createElement(tag); setAttrs(n, attrs); if (parent) parent.appendChild(n); return n; }
  const canvas = document.createElement('canvas');
  const mctx = canvas.getContext ? canvas.getContext('2d') : null;
  function textW(t, font) { if (!mctx) return String(t).length * 6.2; mctx.font = font; return mctx.measureText(String(t)).width; }
  function fit(t, maxw, font) {
    t = String(t); if (textW(t, font) <= maxw) return t;
    let s = t; while (s.length > 1 && textW(s + '\u2026', font) > maxw) s = s.slice(0, -1);
    return s.replace(/\s+$/, '') + '\u2026';
  }
  function pathD(c, X, Y) { let d = ''; for (let i = I0; i < c.length; i++) d += (i > I0 ? 'L' : 'M') + X(i).toFixed(1) + ' ' + Y(c[i]).toFixed(1); return d; }
  function areaD(c, X, Y, y0) { if (c.length <= I0) return ''; return pathD(c, X, Y) + 'L' + X(c.length - 1).toFixed(1) + ' ' + y0.toFixed(1) + 'L' + X(I0).toFixed(1) + ' ' + y0.toFixed(1) + 'Z'; }

  /* ---------- model ---------- */
  const usedKeys = new Set();
  function uniqKey(k, i, prefix) {
    let key = (k == null || k === '') ? prefix + i : String(k);
    while (usedKeys.has(key)) key += '~';
    usedKeys.add(key); return key;
  }
  function jumpsOf(raw) {
    const out = [];
    (Array.isArray(raw.jumps) ? raw.jumps : []).forEach(j => {
      if (!j || typeof j !== 'object') return;
      let wi = WEEKS.indexOf(String(j.week));
      if (wi < 0 && Number.isInteger(j.week_index) && j.week_index >= 0 && j.week_index < NW) wi = j.week_index;
      if (wi < 0 && NW) return;
      out.push({
        wi, gain: num(j.gain), share: num(j.share_of_season), label: String(j.label == null ? '' : j.label),
        week: String(j.week || (wi >= 0 ? WEEKS[wi] : '')),
        contributors: Array.isArray(j.contributors) ? j.contributors.filter(c => c && typeof c === 'object') : [],
      });
    });
    out.sort((a, b) => a.wi - b.wi);
    out.forEach((j, n) => { j.n = n + 1; });
    return out;
  }
  const teams = (Array.isArray(P.teams) ? P.teams : []).filter(t => t && typeof t === 'object').map((t, i) => {
    const cumAll = seriesOf(t.cumulative);
    const cumB20 = Array.isArray(t.cumulative_best20) && t.cumulative_best20.length ? seriesOf(t.cumulative_best20) : cumAll.slice();
    const rk = Number(t.official_rank);
    const name = String(t.name || t.short || t.key || 'Team ' + (i + 1));
    return {
      kind: 'team', key: uniqKey(t.key, i, 'team-'), raw: t, name,
      short: String(t.short || name), cls: String(t.class || ''),
      rank: (t.official_rank != null && Number.isFinite(rk)) ? rk : i + 1,
      cumAll, cumB20, gainsAll: Array.isArray(t.gains) && t.gains.length ? seriesOf(t.gains) : diff(cumAll),
      gainsB20: diff(cumB20), jumps: jumpsOf(t),
    };
  }).sort((a, b) => a.rank - b.rank);
  const riders = (Array.isArray(P.riders) ? P.riders : []).filter(r => r && typeof r === 'object').map((r, i) => {
    const c = seriesOf(r.cumulative);
    const cb = Array.isArray(r.cumulative_best20) && r.cumulative_best20.length ? seriesOf(r.cumulative_best20) : c.slice();
    const name = String(r.name || r.short || 'Rider ' + (i + 1));
    return {
      kind: 'rider', key: uniqKey(r.key, i, 'rider:'), raw: r, name, short: String(r.short || name),
      teamShort: r.team_short ? String(r.team_short) : '', cls: 'Rider',
      cumAll: c, cumB20: cb, gainsAll: Array.isArray(r.gains) && r.gains.length ? seriesOf(r.gains) : diff(c),
      gainsB20: diff(cb), jumps: jumpsOf(r),
    };
  });
  const ALL = teams.concat(riders);
  const BYKEY = new Map(ALL.map(s => [s.key, s]));
  const labelOf = s => (s.kind === 'rider' ? s.short + ' (1 rider)' : s.short);

  // the races behind the biggest team jumps, per week (for annotations + tooltip footers)
  const raceWeek = new Map();
  teams.forEach(t => t.jumps.forEach(j => {
    if (j.wi < 0) return;
    let m = raceWeek.get(j.wi); if (!m) raceWeek.set(j.wi, (m = new Map()));
    j.contributors.forEach(c => {
      const race = String(c.race == null ? '' : c.race).trim(); if (!race) return;
      m.set(race, (m.get(race) || 0) + Math.max(0, num(c.points)));
    });
  }));
  // weeks_meta (every charted rider's points per race) is the authority; the
  // jump contributors are only a fallback for payloads without it
  function topRaceAt(wi) {
    if (WMETA) {
      const m = WMETA[wi];
      return m && m.top_race ? { race: String(m.top_race), pts: num(m.top_race_points) } : null;
    }
    const m = raceWeek.get(wi); if (!m) return null;
    let best = null, bp = 0; m.forEach((p, r) => { if (p > bp) { bp = p; best = r; } });
    return best ? { race: best, pts: bp, partial: true } : null;
  }
  const bigRaces = (() => {
    const byRace = new Map();
    const add = (race, wi, p) => { const cur = byRace.get(race); if (!cur || p > cur.pts) byRace.set(race, { race, wi, pts: p }); };
    if (WMETA) WMETA.forEach((m, wi) => (Array.isArray(m && m.races) ? m.races : []).forEach(r => { if (r && r.race) add(String(r.race), wi, num(r.points)); }));
    else raceWeek.forEach((m, wi) => m.forEach((p, race) => add(race, wi, p)));
    return Array.from(byRace.values()).filter(r => r.wi >= I0).sort((a, b) => b.pts - a.pts);
  })();

  /* ---------- state ---------- */
  const state = {
    metric: /best/i.test(String(META.metric_default || '')) ? 'best20' : 'all',
    scale: 'shared', selected: [], slot: new Map(), riderOn: new Set(riders.map(r => r.key)),
    hoverKey: null, cursor: null,
  };
  let lastInput = 'pointer', lastPointer = 'mouse', depth = 0;
  const busy = () => depth > 0;
  const cum = s => (state.metric === 'best20' ? s.cumB20 : s.cumAll);
  const gainsOf = s => (state.metric === 'best20' ? s.gainsB20 : s.gainsAll);
  function finalOf(s) {
    if (NW) return cum(s)[NW - 1];
    const r = s.raw;
    return num(state.metric === 'best20' && s.kind === 'team' ? r.final_total_best20 : r.final_total);
  }
  function rankAt(s, i) {
    const rb = s.raw.rank_by_week;
    if (state.metric === 'all' && Array.isArray(rb) && rb[i] != null && Number.isFinite(Number(rb[i]))) return Number(rb[i]);
    const v = cum(s)[i]; let r = 1;
    teams.forEach(t => { if (t !== s && cum(t)[i] > v + 1e-9) r++; });
    return r;
  }
  function wouldRank(r) {
    const raw = r.raw;
    const v = state.metric === 'best20' ? (raw.would_rank_best20 != null ? raw.would_rank_best20 : raw.would_rank) : raw.would_rank;
    if (v != null && Number.isFinite(Number(v))) return Number(v);
    const f = finalOf(r); return 1 + teams.filter(t => finalOf(t) > f + 1e-9).length;
  }
  // Panels and table follow the official listing, so the rider is placed by
  // his total against the listing's totals (would_rank_official).
  function officialPlace(r) {
    const v = Number(r.raw.would_rank_official);
    if (r.raw.would_rank_official != null && Number.isFinite(v)) return v;
    const f = num(r.raw.official_total != null ? r.raw.official_total : finalOf(r));
    return 1 + teams.filter(t => num(t.raw.official_total) > f + 1e-9).length;
  }
  const metricWords = () => (state.metric === 'best20' ? 'best-20 totals' : 'all-rider points');
  function rankBasis(r) {
    return 'Would rank ' + ordinal(officialPlace(r)) + ' against the official listing\'s team totals (the order of ' +
      'these panels and the table); ' + ordinal(wouldRank(r)) + ' among the teams on ' + metricWords();
  }
  const colorOf = s => (s.kind === 'rider' ? 'var(--rider)' : (state.slot.has(s.key) ? HL[state.slot.get(s.key)] : null));
  const isHL = s => (s.kind === 'rider' ? state.riderOn.has(s.key) : state.slot.has(s.key));
  function selectTeam(key) {
    if (state.slot.has(key)) return null;
    let dropped = null;
    if (state.selected.length >= MAX_HL) { dropped = state.selected.shift(); state.slot.delete(dropped); }
    const used = new Set(state.slot.values()); let sl = 0; while (used.has(sl)) sl++;
    state.slot.set(key, sl); state.selected.push(key);
    return dropped;
  }
  teams.slice(0, Math.min(MAX_HL, teams.length)).forEach(t => selectTeam(t.key));
  function toggleSeries(key) {
    const s = BYKEY.get(key); if (!s) return;
    let msg;
    if (s.kind === 'rider') {
      if (state.riderOn.has(key)) { state.riderOn.delete(key); msg = labelOf(s) + ': jump markers hidden.'; }
      else { state.riderOn.add(key); msg = labelOf(s) + ': jump markers shown.'; }
    } else if (state.slot.has(key)) {
      state.selected = state.selected.filter(k => k !== key); state.slot.delete(key); msg = s.short + ' no longer highlighted.';
    } else {
      const dropped = selectTeam(key);
      msg = s.short + ' highlighted.' + (dropped ? ' ' + BYKEY.get(dropped).short + ' was removed: up to ' + MAX_HL + ' teams at once.' : '');
    }
    renderAll(); announce(msg);
  }

  /* ---------- tooltip ---------- */
  const tip = $('#tp-tip');
  function showTip(build, cx, cy) {
    tip.textContent = ''; build(tip); tip.hidden = false;
    const vw = document.documentElement.clientWidth, vh = window.innerHeight;
    const r = tip.getBoundingClientRect();
    let left = cx + 16; if (left + r.width > vw - 8) left = cx - 16 - r.width;
    left = clamp(left, 8, Math.max(8, vw - r.width - 8));
    let top = cy - r.height - 14; if (top < 8) top = cy + 18;
    top = clamp(top, 8, Math.max(8, vh - r.height - 8));
    tip.style.left = Math.round(left) + 'px'; tip.style.top = Math.round(top) + 'px';
  }
  // Beside the panel (right, else left) at the list item's height, so the
  // panel's own mini chart and its linked cursor stay visible.  With no room
  // at either side (phones) the tip never moves over the item or the mini
  // chart.  It takes the first gap that holds it: below the item, above the
  // mini chart, or between the two (over the panel's other list items); when
  // none does, the tip is shortened (all contributors -> 2 -> none -> no
  // season row either) and the gaps are tried again; as a last resort it
  // opens below the item and runs off the bottom of the screen.
  // ``build(n, compact)`` fills the tip with at most n contributors (and
  // without the season row when compact); ``mini`` is the panel's svg.mini.
  const TIP_STEPS = [[8, false], [2, false], [0, false], [0, true]];
  function showTipBeside(build, panel, item, mini) {
    const vw = document.documentElement.clientWidth, vh = window.innerHeight;
    const fill = st => { tip.textContent = ''; build(st[0], st[1])(tip); tip.hidden = false; return tip.getBoundingClientRect(); };
    const put = (x, y) => { tip.style.left = Math.round(x) + 'px'; tip.style.top = Math.round(y) + 'px'; };
    let r = fill(TIP_STEPS[0]);
    const p = panel.getBoundingClientRect(), it = item.getBoundingClientRect();
    let left = p.right + 10;
    if (left + r.width > vw - 8) left = p.left - 10 - r.width;
    if (left >= 8) { put(left, clamp(it.top - 8, 8, Math.max(8, vh - r.height - 8))); return; }
    const m = mini ? mini.getBoundingClientRect() : null;
    const gaps = [[it.bottom + 8, vh - 8, 'top']];                  // [start, end, which edge to align]
    if (m) gaps.push([8, m.top - 8, 'bottom'], [Math.max(8, m.bottom + 8), Math.min(vh - 8, it.top - 8), 'top']);
    for (let k = 0; k < TIP_STEPS.length; k++) {
      if (k) r = fill(TIP_STEPS[k]);
      left = clamp(it.left, 8, Math.max(8, vw - r.width - 8));
      const g = gaps.find(([a, b]) => b - a >= r.height);
      if (g) { put(left, g[2] === 'top' ? g[0] : g[1] - r.height); return; }
    }
    put(left, it.bottom + 8);
  }
  function hideTip() { tip.hidden = true; }
  function announce(msg) { const el = $('#tp-live'); el.textContent = ''; setTimeout(() => { el.textContent = msg; }, 40); }
  function keyEl(parent, s, cls) {
    const k = E('span', { class: 'key' + (s && s.kind === 'rider' ? ' key-rider' : '') + (cls ? ' ' + cls : '') }, parent);
    if (s && s.kind !== 'rider') k.style.setProperty('--k', colorOf(s) || (s.key === state.hoverKey ? 'var(--ink)' : 'var(--line-lab)'));
    return k;
  }
  function tipHead(parent, wi) {
    const w = WEEKS[wi], d = parseDay(w);
    const h = E('div', { class: 'tt-head' }, parent);
    E('strong', { text: 'Week of ' + fullDay(w) }, h);
    let sub = d ? 'ISO week ' + isoWeek(d) : '';
    const dt = parseDay(META.data_through);
    if (wi === NW - 1 && dt && d && (dt - d) / 864e5 < 6) sub += (sub ? ' \u00b7 ' : '') + 'partial week, data through ' + dayLabel(META.data_through);
    if (sub) E('span', { class: 'tt-sub', text: sub }, h);
  }
  function tipRows(parent, list, wi, focusKey) {
    const rows = E('div', { class: 'tt-rows' }, parent);
    const ch = E('div', { class: 'tt-colhead', 'aria-hidden': 'true' }, rows);
    E('span', {}, ch); E('span', { class: 'r', text: 'Season' }, ch); E('span', {}, ch);
    E('span', { class: 'r', text: 'Week' }, ch); E('span', { class: 'r', text: 'Rank' }, ch);
    list.forEach(s => {
      const row = E('div', { class: 'tt-row' + (s.key === focusKey || s.key === state.hoverKey ? ' is-hover' : '') }, rows);
      keyEl(row, s);
      E('span', { class: 'tt-val', text: fmt(cum(s)[wi]) }, row);
      E('span', { class: 'tt-name', text: labelOf(s) }, row);
      const g = gainsOf(s)[wi];
      E('span', { class: 'tt-gain' + (g < 0 ? ' neg' : ''), text: g ? signed(g) : '\u2013' }, row);
      E('span', { class: 'tt-rank', text: ordinal(rankAt(s, wi)) }, row);
    });
  }
  // A jump week is chosen on all-rider points; in best-20 mode its gain is
  // shown in that metric, with the all-rider gain next to it when they differ.
  function jumpGain(s, j) {
    if (state.metric !== 'best20' || j.wi < 0) return { main: signed(j.gain), alt: '', share: j.share };
    const g = gainsOf(s)[j.wi], f = finalOf(s);
    return { main: signed(g), alt: Math.abs(g - j.gain) > 0.5 ? 'all riders ' + signed(j.gain) : '', share: f > 0 ? g / f : 0 };
  }
  function tipJump(parent, s, j, maxC) {
    const b = E('div', { class: 'tt-jump' }, parent);
    const t = E('div', { class: 'tt-jt' }, b);
    keyEl(t, s);
    const jg = jumpGain(s, j);
    E('strong', { text: jg.main }, t);
    E('span', { text: labelOf(s) + (jg.alt ? ' \u00b7 ' + jg.alt : '') + (jg.share ? ' \u00b7 ' + Math.round(jg.share * 100) + '% of the season' : '') }, t);
    if (j.label) E('div', { class: 'tt-jl', text: j.label }, b);
    if (j.contributors.length) {
      const ul = E('ul', { class: 'tt-contrib' }, b);
      j.contributors.slice(0, maxC).forEach(c => {
        const li = E('li', null, ul);
        E('span', { class: 'c-pts', text: signed(c.points) }, li);
        E('span', { class: 'c-who', text: [c.rider, c.race].filter(x => x != null && x !== '').map(String).join(' \u00b7 ') }, li);
        if (c.detail != null && c.detail !== '') E('span', { class: 'c-det', text: String(c.detail) }, li);
      });
      if (j.contributors.length > maxC) E('li', { class: 'c-more', text: '+' + (j.contributors.length - maxC) + ' more' }, ul);
    }
  }
  function overviewList(wi, focusKey) {
    const keys = new Set(state.selected);
    riders.forEach(r => keys.add(r.key));
    if (state.hoverKey) keys.add(state.hoverKey);
    if (focusKey) keys.add(focusKey);
    teams.slice().sort((a, b) => cum(b)[wi] - cum(a)[wi]).slice(0, 3).forEach(t => keys.add(t.key));
    return Array.from(keys).map(k => BYKEY.get(k)).filter(Boolean).sort((a, b) => cum(b)[wi] - cum(a)[wi]);
  }
  function overviewTip(wi, focusKey) {
    return parent => {
      tipHead(parent, wi);
      const list = overviewList(wi, focusKey);
      tipRows(parent, list, wi, focusKey);
      const js = list.filter(s => isHL(s) || s.key === state.hoverKey || s.key === focusKey)
        .map(s => [s, s.jumps.find(j => j.wi === wi)]).filter(x => x[1])
        .sort((a, b) => (b[0].key === focusKey) - (a[0].key === focusKey) || (b[0].key === state.hoverKey) - (a[0].key === state.hoverKey) || b[1].gain - a[1].gain);
      js.slice(0, 2).forEach(([s, j]) => tipJump(parent, s, j, 4));
      if (js.length > 2) E('div', { class: 'tt-foot', text: (js.length - 2) + ' more jump' + (js.length > 3 ? 's' : '') + ' this week: ' + js.slice(2).map(x => labelOf(x[0])).join(', ') }, parent);
      const race = topRaceAt(wi);
      if (race) E('div', { class: 'tt-foot', text: race.partial ? 'Biggest race among this week\'s annotated jumps: ' + race.race
        : 'Biggest race of the week: ' + race.race + ' (' + fmt(race.pts) + ' pts for the ' + teams.length + ' teams)' }, parent);
    };
  }
  // ``compact``: no season/week/rank row (the last size step of showTipBeside)
  function panelTip(s, wi, maxC, compact) {
    return parent => {
      tipHead(parent, wi);
      if (!compact) tipRows(parent, [s], wi, s.key);
      const j = s.jumps.find(x => x.wi === wi);
      if (j) tipJump(parent, s, j, maxC == null ? 6 : maxC);
      else if (gainsOf(s)[wi] < 0) E('div', { class: 'tt-foot', text: 'Negative week: UCI sanction points outweighed the points scored.' }, parent);
    };
  }

  /* ---------- overview ---------- */
  let ov = null;
  function spread(items, gap, lo, hi) {
    items.sort((a, b) => a.y0 - b.y0);
    items.forEach(it => { it.y = clamp(it.y0, lo, hi); });
    for (let pass = 0; pass < 80; pass++) {
      let moved = false;
      for (let i = 1; i < items.length; i++) {
        const d = items[i].y - items[i - 1].y;
        if (d < gap - 0.01) { const sh = (gap - d) / 2; items[i - 1].y -= sh; items[i].y += sh; moved = true; }
      }
      items.forEach(it => { it.y = clamp(it.y, lo, hi); });
      if (!moved) break;
    }
    for (let i = 1; i < items.length; i++) items[i].y = Math.max(items[i].y, items[i - 1].y + gap);
    const n = items.length;
    if (n && items[n - 1].y > hi) { items[n - 1].y = hi; for (let i = n - 2; i >= 0; i--) items[i].y = Math.min(items[i].y, items[i + 1].y - gap); }
  }
  function idxAt(x, l, pw) { return SPAN > 0 ? clamp(I0 + Math.round((x - l) / (pw / SPAN)), I0, NW - 1) : I0; }

  function renderOverview() {
    const host = $('#tp-ov');
    const hadFocus = !!(ov && document.activeElement === ov.svg);
    depth++;
    try {
      host.textContent = ''; ov = null;
      if (!NW || !ALL.length) {
        E('p', { class: 'empty', text: NW ? 'No team series in this payload.' : 'No weekly points in this payload yet.' }, host);
        return;
      }
      const W = Math.max(260, Math.floor(host.clientWidth));
      const narrow = W < 600;
      const H = narrow ? 340 : 460;
      const labFont = '650 11px ' + FONT;
      const ranked = teams.slice().sort((a, b) => finalOf(b) - finalOf(a));
      const lab = new Set(ranked.slice(0, narrow ? 5 : 8).map(t => t.key));
      state.selected.forEach(k => lab.add(k));
      riders.forEach(r => lab.add(r.key));
      if (state.hoverKey) lab.add(state.hoverKey);
      const labelText = s => labelOf(s) + (narrow ? '' : '  ' + compact(finalOf(s)));
      let maxw = 0;
      lab.forEach(k => { const s = BYKEY.get(k); if (s) maxw = Math.max(maxw, textW(labelText(s), labFont)); });
      const right = Math.round(Math.min(maxw + 34, W * (narrow ? 0.42 : 0.3)));
      const M = { t: 28, r: right, b: 26, l: 40 };
      const pw = W - M.l - M.r, ph = H - M.t - M.b;
      let lo = 0, hi = 0;
      ALL.forEach(s => cum(s).forEach((v, i) => { if (i < I0) return; if (v > hi) hi = v; if (v < lo) lo = v; }));
      const T = ticks(lo, hi, narrow ? 3 : 4, narrow ? 5 : 7);
      const X = i => M.l + (SPAN > 0 ? (i - I0) * pw / SPAN : pw / 2);
      const Y = v => M.t + ph - (v - T.lo) / (T.hi - T.lo) * ph;
      const svg = S('svg', {
        viewBox: '0 0 ' + W + ' ' + H, width: W, height: H, class: 'ov-svg', tabindex: 0, role: 'group',
        'data-fid': 'ov', 'aria-roledescription': 'chart',
        'aria-label': 'Season-to-date UCI points by week for ' + teams.length + ' teams' + (riders.length ? ' and ' + riders.map(labelOf).join(', ') : '') +
          '. Left and right arrow keys move through the weeks, up and down pick a line, Enter highlights it.',
      }, host);
      S('rect', { x: 0, y: 0, width: W, height: H, fill: 'transparent', class: 'hit' }, svg);

      const g = S('g', { class: 'axes' }, svg);
      T.ticks.forEach(v => {
        const y = Y(v);
        S('line', { x1: M.l, x2: M.l + pw, y1: y, y2: y, class: Math.abs(v) < 1e-9 ? 'axis' : 'gridline' }, g);
        S('text', { x: M.l - 8, y: y + 3.5, 'text-anchor': 'end', class: 'tick', text: compact(v) }, g);
      });
      const mf = '11px ' + FONT; let lastEnd = -1e9, lastYear = null;
      const years = new Set(WEEKS.slice(I0).map(w => (parseDay(w) || { getUTCFullYear: () => 0 }).getUTCFullYear()));
      WEEKS.forEach((w, i) => {
        if (i < I0) return;
        const d = parseDay(w); if (!d) return;
        const p = i > I0 ? parseDay(WEEKS[i - 1]) : null;
        if (p && p.getUTCMonth() === d.getUTCMonth()) return;
        const x = X(i);
        S('line', { x1: x, x2: x, y1: M.t + ph, y2: M.t + ph + 4, class: 'axis' }, g);
        const yr = d.getUTCFullYear();
        const label = MONTHS[d.getUTCMonth()] + (years.size > 1 && yr !== lastYear ? ' ' + yr : '');
        const tw = textW(label, mf);
        const lx = clamp(x - tw / 2, 0, W - tw);
        if (lx < lastEnd + 8) return;
        lastEnd = lx + tw; lastYear = yr;
        S('text', { x: lx, y: H - 6, class: 'tick', text: label }, g);
      });

      const rg = S('g', { class: 'races' }, svg);
      const placed = []; const rf = '550 10.5px ' + FONT;
      bigRaces.slice(0, narrow ? 3 : 6).forEach(r => {
        const x = X(r.wi), w = textW(r.race, rf);
        if (w > pw) return;
        const lx = clamp(x - w / 2, M.l, M.l + pw - w);
        if (placed.some(p => lx < p[1] + 10 && lx + w > p[0] - 10)) return;
        placed.push([lx, lx + w]);
        S('line', { x1: x, x2: x, y1: M.t - 4, y2: M.t + ph, class: 'race-rule' }, rg);
        S('text', { x: lx, y: M.t - 9, class: 'race-label', text: r.race }, rg);
      });

      const lines = S('g', { class: 'lines' }, svg);
      const hov = state.hoverKey ? BYKEY.get(state.hoverKey) : null;
      const d = s => pathD(cum(s), X, Y);
      teams.filter(t => !state.slot.has(t.key) && t !== hov)
        .sort((a, b) => (lab.has(a.key) ? 1 : 0) - (lab.has(b.key) ? 1 : 0))
        .forEach(t => S('path', { d: d(t), class: 'ln ln-muted' + (lab.has(t.key) ? ' ln-lab' : '') }, lines));
      state.selected.forEach(k => { const t = BYKEY.get(k); if (!t || t === hov) return; S('path', { d: d(t), class: 'ln ln-hl' }, lines).style.stroke = colorOf(t); });
      riders.forEach(r => { if (r !== hov) S('path', { d: d(r), class: 'ln ln-rider' + (state.riderOn.has(r.key) ? ' on' : '') }, lines); });
      if (hov) {
        const p = S('path', { d: d(hov), class: 'ln ' + (hov.kind === 'rider' ? 'ln-rider on' : 'ln-hover') }, lines);
        if (hov.kind !== 'rider' && colorOf(hov)) { p.style.stroke = colorOf(hov); p.style.strokeWidth = '3'; }
        if (hov.kind === 'rider') p.style.strokeWidth = '3';
      }
      if (NW === 1) ALL.forEach(s => { S('circle', { cx: X(0), cy: Y(cum(s)[0]), r: 3, class: 'end-dot' }, lines).style.fill = colorOf(s) || 'var(--line-lab)'; });

      const xe = M.l + pw;
      const items = Array.from(lab).map(k => BYKEY.get(k)).filter(Boolean).map(s => ({ s, y0: Y(cum(s)[NW - 1]) }));
      spread(items, 13, M.t + 4, M.t + ph);
      const lg = S('g', { class: 'labels' }, svg);
      items.forEach(it => {
        const s = it.s, col = colorOf(s);
        if (col) S('circle', { cx: xe, cy: it.y0, r: 3.5, class: 'end-dot' }, lg).style.fill = col;
        if (Math.abs(it.y - it.y0) > 1.5) S('path', { d: 'M' + (xe + 4) + ' ' + it.y0.toFixed(1) + 'L' + (xe + 9) + ' ' + it.y.toFixed(1), class: 'leader' }, lg);
        const k = S('line', { x1: xe + 10, x2: xe + 20, y1: it.y, y2: it.y, class: 'lab-key' + (s.kind === 'rider' ? ' dashed' : '') }, lg);
        k.style.stroke = col || (s.key === state.hoverKey ? 'var(--ink)' : 'var(--line-lab)');
        S('text', { x: xe + 24, y: it.y + 3.8, class: 'lab' + (col || s.key === state.hoverKey ? ' is-hl' : ''), text: fit(labelText(s), M.r - 26, labFont) }, lg);
      });

      const mk = S('g', { class: 'markers' }, svg);
      state.selected.map(k => BYKEY.get(k)).filter(Boolean).concat(riders.filter(r => state.riderOn.has(r.key))).forEach(s => s.jumps.forEach(j => {
        if (j.wi < I0) return;
        const c = S('circle', {
          cx: X(j.wi), cy: Y(cum(s)[j.wi]), r: 5, class: 'jm', tabindex: 0, role: 'img', 'data-fid': 'jm:' + s.key + ':' + j.n,
          'aria-label': labelOf(s) + ', week of ' + fullDay(WEEKS[j.wi]) + ': ' + signed(j.gain) + ' points. ' + j.label,
        }, mk);
        c.style.fill = colorOf(s);
        c.addEventListener('focus', () => {
          if (busy()) return;
          state.cursor = j.wi; drawCursor();
          const r = c.getBoundingClientRect(); showTip(overviewTip(j.wi, s.key), r.left + r.width / 2, r.top);
        });
        c.addEventListener('blur', e => {
          if (busy() || lastInput !== 'key') return;
          if (e.relatedTarget && svg.contains(e.relatedTarget)) return;   // next marker: its focus handler takes over
          if (state.hoverKey != null) clearOverview();                    // focus has left the chart: safe to re-render
          else { state.cursor = null; drawCursor(); hideTip(); }
        });
      }));
      const cur = S('g', { class: 'cursor' }, svg);
      ov = { svg, X, Y, M, pw, ph, W, H, cur };
      svg.style.cursor = state.hoverKey ? 'pointer' : 'crosshair';
      drawCursor();

      svg.addEventListener('pointermove', onOvPointer);
      svg.addEventListener('pointerdown', onOvPointer);
      svg.addEventListener('pointerleave', e => { if (ov && ov.svg === svg && e.pointerType === 'mouse') clearOverview(); });
      svg.addEventListener('click', () => { if (ov && ov.svg === svg && lastPointer === 'mouse' && state.hoverKey) toggleSeries(state.hoverKey); });
      svg.addEventListener('keydown', onOvKey);
      svg.addEventListener('focus', e => {
        if (busy() || e.target !== svg || !svg.matches(':focus-visible')) return;
        if (state.cursor == null || state.cursor < I0) state.cursor = NW - 1;
        drawCursor(); keyTip();
      });
      svg.addEventListener('blur', e => {
        if (busy() || e.target !== svg || lastInput !== 'key') return;
        // Tab from the chart into one of its own jump markers: re-rendering now
        // would destroy the marker about to take focus (focus fell to the page body)
        if (e.relatedTarget && svg.contains(e.relatedTarget)) return;
        clearOverview();
      });
      if (hadFocus) svg.focus({ preventScroll: true });
    } finally { depth--; }
  }
  function drawCursor() {
    if (!ov) return;
    ov.cur.textContent = '';
    const i = state.cursor;
    if (i == null || i < 0 || i >= NW) return;
    const x = ov.X(i);
    S('line', { x1: x, x2: x, y1: ov.M.t - 2, y2: ov.M.t + ov.ph, class: 'crosshair' }, ov.cur);
    const keys = new Set(state.selected); riders.forEach(r => keys.add(r.key)); if (state.hoverKey) keys.add(state.hoverKey);
    keys.forEach(k => {
      const s = BYKEY.get(k); if (!s) return;
      S('circle', { cx: x, cy: ov.Y(cum(s)[i]), r: 4, class: 'cur-dot' }, ov.cur).style.fill = colorOf(s) || 'var(--ink)';
    });
  }
  function clearOverview() {
    const had = state.hoverKey != null;
    state.cursor = null; state.hoverKey = null; hideTip();
    if (had) renderOverview(); else drawCursor();
  }
  function onOvPointer(e) {
    if (!ov || e.currentTarget !== ov.svg) return;
    lastInput = 'pointer'; lastPointer = e.pointerType || 'mouse';
    const r = ov.svg.getBoundingClientRect();
    const x = (e.clientX - r.left) * ov.W / r.width, y = (e.clientY - r.top) * ov.H / r.height;
    if (x < ov.M.l - 12 || x > ov.M.l + ov.pw + 8) { if (lastPointer === 'mouse') clearOverview(); return; }
    const i = idxAt(x, ov.M.l, ov.pw);
    let best = null, bd = 12;
    ALL.forEach(s => { const dd = Math.abs(ov.Y(cum(s)[i]) - y); if (dd < bd - (isHL(s) ? 0.5 : 0)) { bd = dd; best = s.key; } });
    state.cursor = i;
    if (best !== state.hoverKey) { state.hoverKey = best; renderOverview(); } else drawCursor();
    showTip(overviewTip(i), e.clientX, e.clientY);
  }
  function keyTip() {
    if (!ov || state.cursor == null) return;
    const i = state.cursor;
    const list = overviewList(i);
    const s = (state.hoverKey && BYKEY.get(state.hoverKey)) || list[0];
    const r = ov.svg.getBoundingClientRect();
    const cx = r.left + ov.X(i) * r.width / ov.W, cy = r.top + (s ? ov.Y(cum(s)[i]) : ov.M.t) * r.height / ov.H;
    showTip(overviewTip(i), cx, cy);
    announce('Week of ' + fullDay(WEEKS[i]) + ': ' + list.slice(0, 4).map(x => labelOf(x) + ' ' + fmt(cum(x)[i])).join(', ') + '.');
  }
  function onOvKey(e) {
    if (!NW || !ov || e.target !== ov.svg) return;
    lastInput = 'key';
    let i = state.cursor == null ? NW - 1 : state.cursor;
    const k = e.key;
    if (k === 'ArrowRight') i = Math.min(NW - 1, i + (e.shiftKey ? 4 : 1));
    else if (k === 'ArrowLeft') i = Math.max(I0, i - (e.shiftKey ? 4 : 1));
    else if (k === 'Home') i = I0;
    else if (k === 'End') i = NW - 1;
    else if (k === 'ArrowUp' || k === 'ArrowDown') {
      e.preventDefault();
      const order = ALL.slice().sort((a, b) => cum(b)[i] - cum(a)[i]);
      let p = order.findIndex(s => s.key === state.hoverKey);
      p = k === 'ArrowDown' ? p + 1 : (p < 0 ? order.length - 1 : p - 1);
      state.hoverKey = order[clamp(p, 0, order.length - 1)].key; state.cursor = i;
      renderOverview(); keyTip(); return;
    } else if (k === 'Enter' || k === ' ') { e.preventDefault(); if (state.hoverKey) { toggleSeries(state.hoverKey); keyTip(); } return; }
    else if (k === 'Escape') { clearOverview(); return; }
    else return;
    e.preventDefault();
    state.cursor = i; drawCursor(); keyTip();
  }

  /* ---------- small multiples ---------- */
  const minis = new WeakMap();
  let miniActive = null;
  function withRiders(list) {
    riders.slice().sort((a, b) => officialPlace(a) - officialPlace(b)).forEach(r => list.splice(clamp(officialPlace(r) - 1, 0, list.length), 0, r));
    return list;
  }
  function stat(dl, label, value, isMetric, title) {
    const d = E('div', { class: isMetric ? 'is-metric' : null, title: title || null }, dl);
    E('dt', { text: label }, d); E('dd', { text: value }, d);
  }
  function buildCard(s, grid) {
    const isR = s.kind === 'rider', on = isHL(s);
    const card = E('article', { class: 'panel' + (isR ? ' panel-rider' : '') + (on ? ' is-hl' : ''), 'aria-label': labelOf(s) }, grid);
    if (colorOf(s) && !isR) card.style.setProperty('--c', colorOf(s));
    const head = E('header', { class: 'p-head' }, card);
    const bib = E('div', {
      class: 'bib' + (isR ? ' bib-rider' : ''), role: 'img',
      'aria-label': isR ? rankBasis(s) : 'Official rank ' + s.rank, title: isR ? rankBasis(s) : null,
    }, head);
    E('span', { class: 'bib-cap', text: isR ? 'would rank' : 'rank', 'aria-hidden': 'true' }, bib);
    E('span', { class: 'bib-num', text: isR ? ordinal(officialPlace(s)) : String(s.rank), 'aria-hidden': 'true' }, bib);
    const id = E('div', { class: 'p-id' }, head);
    const h = E('h3', { class: 'p-name' }, id);
    E('span', { class: 'nm', text: s.short }, h);
    if (isR) E('span', { class: 'badge badge-rider', text: 'Rider, not a team' }, h);
    else if (s.cls) E('span', { class: 'badge', text: s.cls }, h);
    const full = isR ? s.name + (s.teamShort ? ' \u00b7 rides for ' + s.teamShort : '') : (s.name !== s.short ? s.name : '');
    if (full) E('p', { class: 'p-full', text: full }, id);
    const tipText = isR ? 'Show his jump markers in the season chart' : 'Highlight ' + s.short + ' in the season chart';
    const btn = E('button', { type: 'button', class: 'hl-btn', 'aria-pressed': String(on), 'data-fid': 'phl:' + s.key, 'aria-label': tipText, title: tipText }, head);
    const ic = S('svg', { viewBox: '0 0 16 16', 'aria-hidden': 'true' }, btn);
    S('path', { d: 'M1.5 12.5 5.5 8l3 2.5 6-7' }, ic);
    btn.addEventListener('click', () => toggleSeries(s.key));
    const dl = E('dl', { class: 'p-stats' }, card);
    const r = s.raw;
    if (isR) {
      stat(dl, 'Season', fmt(finalOf(s)), true);
      stat(dl, 'PCS total', r.official_total != null ? fmt(r.official_total) : '\u2013', false, 'His entry on his team\'s PCS points page (counted + not counted)');
      stat(dl, 'Team', s.teamShort || '\u2013', false);
    } else {
      stat(dl, 'Official', r.official_total != null ? fmt(r.official_total) : '\u2013', false, 'Official ranking listing' + (r.breakdown_total != null ? '; per-team breakdown ' + fmt(r.breakdown_total) : ''));
      stat(dl, 'All riders', fmt(NW ? s.cumAll[NW - 1] : r.final_total), state.metric === 'all');
      stat(dl, 'Best 20', fmt(NW ? s.cumB20[NW - 1] : r.final_total_best20), state.metric === 'best20');
    }
    const chartHost = E('div', { class: 'p-chart' }, card);
    if (s.jumps.length) {
      const ol = E('ol', { class: 'jumps', 'aria-label': 'Biggest weeks' }, card);
      s.jumps.forEach(j => {
        const li = E('li', { class: 'jump-li', tabindex: 0, 'data-fid': 'jl:' + s.key + ':' + j.n, 'data-key': s.key, 'data-n': j.n }, ol);
        E('span', { class: 'jn', text: String(j.n), 'aria-hidden': 'true' }, li);
        const t = E('span', { class: 'jt' }, li);
        E('time', { text: dayLabel(j.week), datetime: j.week }, t);
        t.appendChild(document.createTextNode(' \u00b7 '));
        const jg = jumpGain(s, j);
        E('strong', { text: jg.main }, t);
        if (jg.alt) t.appendChild(document.createTextNode(' (' + jg.alt + ')'));
        if (j.label) t.appendChild(document.createTextNode(' \u2014 ' + j.label));
      });
    } else E('p', { class: 'p-none', text: 'No single week stood out.' }, card);
    return { s, chartHost };
  }
  function extent(arr) { let lo = 0, hi = 0; arr.forEach(v => { if (v > hi) hi = v; if (v < lo) lo = v; }); return [lo, hi]; }
  function buildMini(s, host, W, shared) {
    host.textContent = '';
    if (!NW) { E('p', { class: 'p-none', text: 'No weekly data.' }, host); return; }
    const isR = s.kind === 'rider';
    const c = cum(s), gs = gainsOf(s);
    const H = 138, top = 12, lineB = 86, base = 114, posH = 20, negH = 7;
    const M = { l: 9, r: 9 };
    const [cl, ch] = state.scale === 'shared' ? [shared.yLo, shared.yHi] : extent(c);
    const T = ticks(cl, ch, 6, 9);
    const [gl, gh] = state.scale === 'shared' ? [shared.gLo, shared.gHi] : extent(gs);
    const X = i => M.l + (SPAN > 0 ? (i - I0) * (W - M.l - M.r) / SPAN : (W - M.l - M.r) / 2);
    const Y = v => top + (T.hi - v) / (T.hi - T.lo) * (lineB - top);
    const svg = S('svg', {
      viewBox: '0 0 ' + W + ' ' + H, width: W, height: H, class: 'mini', tabindex: 0, role: 'group', 'data-fid': 'mini:' + s.key,
      'aria-roledescription': 'chart',
      'aria-label': labelOf(s) + ': season-to-date points and points per week. Arrow keys step through the weeks.',
    }, host);
    S('rect', { x: 0, y: 0, width: W, height: H, fill: 'transparent' }, svg);
    S('line', { x1: M.l, x2: W - M.r, y1: Y(T.hi), y2: Y(T.hi), class: 'gridline' }, svg);
    S('text', { x: M.l, y: Y(T.hi) - 3, class: 'tick', text: compact(T.hi) }, svg);
    S('line', { x1: M.l, x2: W - M.r, y1: Y(0), y2: Y(0), class: 'axis' }, svg);
    S('path', { d: areaD(c, X, Y, Y(0)), class: 'mini-area' }, svg);
    S('path', { d: pathD(c, X, Y), class: 'mini-line' + (isR ? ' dashed' : '') }, svg);
    const bw = Math.max(1, Math.min(8, (W - M.l - M.r) / Math.max(1, SPAN + 1) - 1.2));
    const jw = new Set(s.jumps.map(j => j.wi));
    gs.forEach((v, i) => {
      if (!v || i < I0) return;
      if (v > 0) {
        const hgt = Math.max(1, gh > 0 ? v / gh * posH : 1);
        S('rect', { x: X(i) - bw / 2, y: base - hgt, width: bw, height: hgt, rx: Math.min(1.5, bw / 2), class: jw.has(i) ? 'bar bar-jump' : 'bar' }, svg);
      } else {
        const hgt = Math.max(3, gl < 0 ? v / gl * negH : 3);
        S('rect', { x: X(i) - bw / 2, y: base + 1, width: bw, height: hgt, rx: Math.min(1.5, bw / 2), class: 'bar-neg' }, svg);
      }
    });
    S('line', { x1: M.l, x2: W - M.r, y1: base + 0.5, y2: base + 0.5, class: 'axis' }, svg);
    const twoYears = NW > 1 && WEEKS[I0].slice(0, 4) !== WEEKS[NW - 1].slice(0, 4);
    const axisDay = w => (twoYears ? fullDay(w) : dayLabel(w));
    S('text', { x: M.l, y: H - 3, class: 'tick', text: axisDay(WEEKS[I0]) }, svg);
    if (NW > 1) S('text', { x: W - M.r, y: H - 3, 'text-anchor': 'end', class: 'tick', text: axisDay(WEEKS[NW - 1]) }, svg);
    const mk = S('g', { class: 'jm-layer' }, svg);
    const used = [];
    s.jumps.forEach(j => {
      if (j.wi < 0) return;
      const wi = Math.max(j.wi, I0);   // a jump before the first plotted week sits in the opening value
      const px = X(wi), py = Y(c[wi]);
      let level = 0;
      while (used.some(u => Math.abs(u.x - px) < 17 && u.level === level)) level++;
      used.push({ x: px, level });
      let my = py - 12 - level * 17;
      if (my < 8) my = py + 12 + level * 17;
      const mx = clamp(px, M.l + 1, W - M.r - 1);
      const g = S('g', { class: 'jm-mini' }, mk);
      S('line', { x1: px, x2: mx, y1: py, y2: my, class: '' }, g);
      S('circle', { cx: px, cy: py, r: 2.5, class: 'pt-dot' }, g);
      S('circle', { cx: mx, cy: my, r: 7.5 }, g);
      S('text', { x: mx, y: my + 3.3, 'text-anchor': 'middle', text: String(j.n) }, g);
    });
    const cur = S('g', { class: 'cursor' }, svg);
    minis.set(svg, { s, X, Y, W, H, M, cur, base, top });
  }
  function setMiniCursor(svg, i) {
    const m = minis.get(svg); if (!m) return;
    i = Math.max(i, I0);
    if (miniActive && miniActive !== svg) { const o = minis.get(miniActive); if (o) o.cur.textContent = ''; }
    miniActive = svg; m.i = i; m.cur.textContent = '';
    const x = m.X(i);
    S('line', { x1: x, x2: x, y1: m.top, y2: m.base + 8, class: 'crosshair' }, m.cur);
    S('circle', { cx: x, cy: m.Y(cum(m.s)[i]), r: 4, class: 'cur-dot' }, m.cur).style.fill = m.s.kind === 'rider' ? 'var(--rider)' : 'var(--c)';
  }
  function clearMinis() { if (miniActive) { const m = minis.get(miniActive); if (m) m.cur.textContent = ''; miniActive = null; } }
  function miniPoint(svg, i) {
    const m = minis.get(svg); const r = svg.getBoundingClientRect();
    return [r.left + m.X(i) * r.width / m.W, r.top + m.Y(cum(m.s)[i]) * r.height / m.H];
  }
  function renderPanels() {
    const grid = $('#tp-grid');
    depth++;
    try {
      grid.textContent = ''; miniActive = null;
      let yLo = 0, yHi = 0, gLo = 0, gHi = 0;
      ALL.forEach(s => { cum(s).forEach(v => { if (v > yHi) yHi = v; if (v < yLo) yLo = v; }); gainsOf(s).forEach(v => { if (v > gHi) gHi = v; if (v < gLo) gLo = v; }); });
      const cards = withRiders(teams.slice()).map(s => buildCard(s, grid));
      const first = grid.querySelector('.p-chart');
      const cw = Math.max(200, Math.floor(first ? first.clientWidth : 280));
      cards.forEach(({ s, chartHost }) => buildMini(s, chartHost, cw, { yLo, yHi, gLo, gHi }));
      if (!cards.length) E('p', { class: 'empty', text: 'No teams in this payload.' }, grid);
    } finally { depth--; }
  }
  function showJumpLi(li) {
    const s = BYKEY.get(li.getAttribute('data-key')); if (!s) return;
    const j = s.jumps.find(x => x.n === Number(li.getAttribute('data-n'))); if (!j) return;
    const svg = li.closest('.panel') && li.closest('.panel').querySelector('svg.mini');
    if (svg && j.wi >= 0) setMiniCursor(svg, j.wi);
    if (j.wi >= 0) showTipBeside((n, compact) => panelTip(s, j.wi, n, compact), li.closest('.panel') || li, li, svg);
  }
  (() => {
    const grid = $('#tp-grid');
    const onMove = e => {
      const svg = e.target.closest && e.target.closest('svg.mini'); if (!svg || !NW) return;
      lastInput = 'pointer';
      const m = minis.get(svg); if (!m) return;
      const r = svg.getBoundingClientRect();
      const i = idxAt((e.clientX - r.left) * m.W / r.width, m.M.l, m.W - m.M.l - m.M.r);
      setMiniCursor(svg, i); showTip(panelTip(m.s, i), e.clientX, e.clientY);
    };
    grid.addEventListener('pointermove', onMove);
    grid.addEventListener('pointerdown', onMove);
    grid.addEventListener('pointerout', e => {
      const svg = e.target.closest && e.target.closest('svg.mini');
      if (!svg || (e.relatedTarget && svg.contains(e.relatedTarget))) return;
      if (e.pointerType === 'mouse') { clearMinis(); hideTip(); }
    });
    grid.addEventListener('mouseover', e => { const li = e.target.closest && e.target.closest('.jump-li'); if (li && !(e.relatedTarget && li.contains(e.relatedTarget))) showJumpLi(li); });
    grid.addEventListener('mouseout', e => {
      const li = e.target.closest && e.target.closest('.jump-li');
      if (li && !(e.relatedTarget && li.contains(e.relatedTarget)) && document.activeElement !== li) { clearMinis(); hideTip(); }
    });
    grid.addEventListener('focusin', e => {
      if (busy()) return;
      const t = e.target;
      if (t.classList && t.classList.contains('jump-li')) { showJumpLi(t); return; }
      if (t.matches && t.matches('svg.mini') && t.matches(':focus-visible') && NW) {
        const m = minis.get(t); const i = m.i != null ? m.i : NW - 1;
        setMiniCursor(t, i); const [x, y] = miniPoint(t, i); showTip(panelTip(m.s, i), x, y);
      }
    });
    grid.addEventListener('focusout', e => {
      if (busy()) return;
      const t = e.target;
      if (t.classList && (t.classList.contains('jump-li') || t.classList.contains('mini'))) { clearMinis(); hideTip(); }
    });
    grid.addEventListener('keydown', e => {
      const t = e.target;
      if (!(t.matches && t.matches('svg.mini')) || !NW) return;
      lastInput = 'key';
      const m = minis.get(t); if (!m) return;
      let i = m.i != null ? m.i : NW - 1;
      if (e.key === 'ArrowRight') i = Math.min(NW - 1, i + (e.shiftKey ? 4 : 1));
      else if (e.key === 'ArrowLeft') i = Math.max(I0, i - (e.shiftKey ? 4 : 1));
      else if (e.key === 'Home') i = I0;
      else if (e.key === 'End') i = NW - 1;
      else if (e.key === 'Escape') { clearMinis(); hideTip(); return; }
      else return;
      e.preventDefault();
      setMiniCursor(t, i); const [x, y] = miniPoint(t, i); showTip(panelTip(m.s, i), x, y);
      announce(labelOf(m.s) + ', week of ' + fullDay(WEEKS[i]) + ': ' + fmt(cum(m.s)[i]) + ' points, ' + signed(gainsOf(m.s)[i]) + ' that week.');
    });
  })();

  /* ---------- table ---------- */
  function renderTable() {
    const tbody = $('#tp-table tbody');
    depth++;
    try {
      tbody.textContent = '';
      withRiders(teams.slice()).forEach(s => {
        const isR = s.kind === 'rider', r = s.raw;
        const tr = E('tr', { class: (isR ? 'row-rider' : '') + (isHL(s) ? ' is-hl' : '') }, tbody);
        if (colorOf(s) && !isR) tr.style.setProperty('--c', colorOf(s));
        const rk = E('td', { class: 'rk num' }, tr);
        if (isR) { rk.textContent = '\u2248' + officialPlace(s); rk.title = rankBasis(s); }
        else rk.textContent = String(s.rank);
        const tc = E('td', null, tr);
        const b = E('button', {
          type: 'button', class: 't-btn', 'aria-pressed': String(isHL(s)), 'data-fid': 'tb:' + s.key,
          title: isR ? 'Show or hide his jump markers in the season chart' : 'Show or hide this team in colour in the season chart',
        }, tc);
        E('span', { class: 'key' }, b);
        E('span', { class: 't-nm', text: labelOf(s) }, b);
        b.addEventListener('click', () => toggleSeries(s.key));
        if (isR) E('span', { class: 'pill', text: 'Rider' }, tc);
        const sub = isR ? s.name + (s.teamShort ? ' \u00b7 ' + s.teamShort : '') : (s.name !== s.short ? s.name : '');
        if (sub) E('span', { class: 't-full', text: sub }, tc);
        E('td', { class: 'cls col-cls', text: isR ? 'Rider' : (s.cls || '\u2013') }, tr);
        E('td', { class: 'num', text: r.official_total != null ? fmt(r.official_total) : '\u2013', title: (!isR && r.breakdown_total != null) ? 'Per-team breakdown: ' + fmt(r.breakdown_total) : null }, tr);
        const allV = NW ? s.cumAll[NW - 1] : r.final_total;
        const b20V = NW ? s.cumB20[NW - 1] : (isR ? r.final_total : r.final_total_best20);
        E('td', { class: 'num' + (state.metric === 'all' ? ' is-metric' : ''), text: fmt(allV) }, tr);
        E('td', { class: 'num' + (state.metric === 'best20' ? ' is-metric' : ''), text: fmt(b20V) }, tr);
        const jc = E('td', { class: 'jump-cell col-jump' }, tr);
        const big = s.jumps.slice().sort((a, x) => x.gain - a.gain)[0];
        if (big) {
          const jg = jumpGain(s, big);
          E('strong', { text: jg.main }, jc);
          if (jg.alt) jc.appendChild(document.createTextNode(' (' + jg.alt + ')'));
          jc.appendChild(document.createTextNode(' \u00b7 '));
          E('time', { text: dayLabel(big.week), datetime: big.week }, jc);
          if (big.label) { E('br', null, jc); jc.appendChild(document.createTextNode(big.label)); }
        } else jc.textContent = '\u2013';
      });
      $$('#tp-table thead th[data-col]').forEach(th => th.classList.toggle('is-metric', th.getAttribute('data-col') === state.metric));
    } finally { depth--; }
  }

  /* ---------- controls + text ---------- */
  function renderPicker() {
    const sel = $('#tp-pick');
    sel.textContent = '';
    E('option', { value: '', text: 'Add or remove a team\u2026' }, sel);
    const og = E('optgroup', { label: 'Teams, official rank' }, sel);
    teams.forEach(t => E('option', { value: t.key, text: (state.slot.has(t.key) ? '\u2713 ' : '') + t.rank + '. ' + t.short + (t.name !== t.short ? ' (' + t.name + ')' : '') }, og));
    if (riders.length) {
      const rg = E('optgroup', { label: 'Riders' }, sel);
      riders.forEach(r => E('option', { value: r.key, text: (state.riderOn.has(r.key) ? '\u2713 ' : '') + labelOf(r) + ': jump markers' }, rg));
    }
    sel.value = '';
  }
  function renderChips() {
    const box = $('#tp-chips');
    box.textContent = '';
    state.selected.forEach(k => {
      const s = BYKEY.get(k); if (!s) return;
      const b = E('button', { type: 'button', class: 'chip', 'data-fid': 'chip:' + k, 'aria-label': 'Remove highlight: ' + s.name }, box);
      keyEl(b, s); E('span', { text: s.short }, b); E('span', { class: 'x', 'aria-hidden': 'true', text: '\u00d7' }, b);
      b.addEventListener('click', () => toggleSeries(k));
    });
    riders.forEach(r => {
      const on = state.riderOn.has(r.key);
      const b = E('button', { type: 'button', class: 'chip chip-rider', 'aria-pressed': String(on), 'data-fid': 'chip:' + r.key, title: on ? 'Hide his jump markers' : 'Show his jump markers' }, box);
      keyEl(b, r); E('span', { text: labelOf(r) }, b); E('span', { class: 'state', text: on ? 'markers on' : 'markers off' }, b);
      b.addEventListener('click', () => toggleSeries(r.key));
    });
  }
  function renderText() {
    const metricLabel = state.metric === 'best20' ? 'Best 20 riders per team (UCI rule)' : 'All riders';
    const sub = $('#tp-sub');
    if (sub) sub.textContent = metricLabel + ' \u00b7 season-to-date UCI points per ISO week (Monday start)' + (META.data_through ? ' \u00b7 data through ' + fullDay(META.data_through) : '');
    const dek = $('#tp-dek-rider');
    if (dek && riders.length) {
      const r = riders[0];
      const lead = teams.slice().sort((a, b) => finalOf(b) - finalOf(a))[0];
      dek.textContent = '';
      E('span', { class: 'key key-rider' }, dek);
      let txt = r.name + ' alone has ' + fmt(finalOf(r)) + ' points. As a team he would rank ' + ordinal(wouldRank(r)) +
        ' on ' + metricWords() + ' (' + ordinal(officialPlace(r)) + ' against the official listing).';
      let more = '';    // the team comparison: hidden on phones (.dek-more)
      if (lead) {
        const other = state.metric === 'best20' ? lead.cumAll : lead.cumB20;
        more = ' ' + lead.short + ' lead the teams with ' + fmt(finalOf(lead)) + (state.metric === 'best20'
          ? ' under the UCI best-20 rule (' + fmt(NW ? other[NW - 1] : lead.raw.final_total) + ' from all riders).'
          : ' from all riders; ' + fmt(NW ? other[NW - 1] : lead.raw.final_total_best20) + ' under the UCI best-20 rule.');
      }
      const span = E('span', { text: txt }, dek);
      if (more) E('span', { class: 'dek-more', text: more }, span);
    }
    $$('[data-metric-btn]').forEach(b => b.setAttribute('aria-pressed', String(b.getAttribute('data-metric-btn') === state.metric)));
    $$('[data-scale-btn]').forEach(b => b.setAttribute('aria-pressed', String(b.getAttribute('data-scale-btn') === state.scale)));
  }
  function activeFid() { const a = document.activeElement; return a && a.getAttribute ? a.getAttribute('data-fid') : null; }
  function restoreFocus(fid) {
    if (!fid) return;
    const n = document.querySelector('[data-fid="' + (window.CSS && CSS.escape ? CSS.escape(fid) : fid) + '"]');
    if (n && document.activeElement !== n) n.focus({ preventScroll: true });
  }
  function renderAll() {
    const fid = activeFid();
    depth++;
    try { renderPicker(); renderChips(); renderOverview(); renderPanels(); renderTable(); renderText(); restoreFocus(fid); }
    finally { depth--; }
  }

  $$('[data-metric-btn]').forEach(b => b.addEventListener('click', () => {
    state.metric = b.getAttribute('data-metric-btn') === 'best20' ? 'best20' : 'all';
    state.cursor = null; hideTip(); renderAll();
    announce('Showing ' + (state.metric === 'best20' ? 'best 20 riders per team.' : 'all riders.'));
  }));
  $$('[data-scale-btn]').forEach(b => b.addEventListener('click', () => {
    state.scale = b.getAttribute('data-scale-btn') === 'own' ? 'own' : 'shared';
    const fid = activeFid(); hideTip(); renderPanels(); renderText(); restoreFocus(fid);
  }));
  $('#tp-pick').addEventListener('change', e => { const v = e.target.value; if (v) toggleSeries(v); e.target.value = ''; });
  document.addEventListener('keydown', () => { lastInput = 'key'; }, true);
  document.addEventListener('pointerdown', e => {
    lastInput = 'pointer';
    const t = e.target;
    if (t && t.closest && (t.closest('.ov-svg') || t.closest('svg.mini') || t.closest('.jump-li'))) return;
    if (!tip.hidden) { hideTip(); clearMinis(); if (state.cursor != null || state.hoverKey) clearOverview(); }
  }, true);
  window.addEventListener('scroll', () => {
    if (tip.hidden || lastInput === 'key') return;
    hideTip(); clearMinis(); if (state.cursor != null || state.hoverKey) clearOverview();
  }, { passive: true });

  renderAll();
  let lastW = $('#tp-ov').clientWidth;
  if (window.ResizeObserver) {
    let raf = 0;
    new ResizeObserver(() => {
      const w = $('#tp-ov').clientWidth;
      if (Math.abs(w - lastW) < 1) return;
      lastW = w; cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => { const fid = activeFid(); hideTip(); renderOverview(); renderPanels(); restoreFocus(fid); });
    }).observe($('.page'));
  }
})();
"""


def _facts(meta: dict, n_teams: int) -> str:
    cov = meta.get("coverage") if isinstance(meta.get("coverage"), dict) else {}
    rec = cov.get("reconciliation") if isinstance(cov.get("reconciliation"), dict) else {}
    rows = [("Teams", _fmt(cov.get("teams", n_teams)))]
    # hidden on phones: the team count is in the title and dek, the rest is
    # repeated in the sources section
    minor = {"Teams"}
    if cov.get("riders") is not None:
        rows.append(("Riders", _fmt(cov["riders"])))
        minor.add("Riders")
    if cov.get("results_with_points") is not None:
        rows.append(("Scoring results", _fmt(cov["results_with_points"])))
        minor.add("Scoring results")
    if cov.get("penalties") is not None:
        rows.append(("UCI sanctions", _fmt(cov["penalties"])))
        minor.add("UCI sanctions")
    if rec.get("teams_exact") is not None:
        txt = f"{_fmt(rec['teams_exact'])}/{_fmt(cov.get('teams', n_teams))} teams"
        if rec.get("max_abs_team_diff"):
            txt += f" (largest gap {_fmt(rec['max_abs_team_diff'])} pts)"
        rows.append(("Matches PCS team pages", txt))
    if meta.get("data_through"):
        rows.append(("Data through", _day(meta["data_through"])))
    minor_attr = ' class="fact-minor"'

    def dt(k: str) -> str:
        # phones: 'Matches PCS 29/30 teams (...)' fits one row
        if k == "Matches PCS team pages":
            return 'Matches PCS<span class="fact-long"> team pages</span>'
        return _esc(k)
    return "".join(f"<div{minor_attr if k in minor else ''}><dt>{dt(k)}</dt><dd>{_esc(v)}</dd></div>"
                   for k, v in rows)


def _provenance(meta: dict) -> str:
    """Source, reconciliation, week convention and coverage notes as a <dl>
    (rendered in the "Sources and reconciliation" section below the table, so
    the masthead stays short and the chart is in the first screen)."""
    cov = meta.get("coverage") if isinstance(meta.get("coverage"), dict) else {}
    rec = cov.get("reconciliation") if isinstance(cov.get("reconciliation"), dict) else {}
    parts = []
    if meta.get("source"):
        parts.append(("Source", _esc(meta["source"])))
    if rec.get("note"):
        parts.append(("Reconciliation", _esc(rec["note"])))
    if meta.get("week_convention"):
        parts.append(("Weeks", _esc(meta["week_convention"])))
    notes = [n for n in (cov.get("notes") or []) if n not in (None, "")]
    if notes:
        items = "".join(f"<li>{_esc(n)}</li>" for n in notes)
        parts.append(("Notes", f'<details id="tp-notes" open><summary>{len(notes)} coverage note'
                               f'{"s" if len(notes) != 1 else ""}</summary><ul>{items}</ul></details>'))
    if not parts:
        return ""
    body = "".join(f"<dt>{_esc(k)}</dt><dd>{v}</dd>" for k, v in parts)
    return f'<dl class="prov">{body}</dl>'


def _dek_rider(payload: dict, metric_best20: bool) -> str:
    """Static version of the rider sentence (the page's JS rewrites it per metric)."""
    riders = [r for r in (payload.get("riders") or []) if isinstance(r, dict)]
    teams = [t for t in (payload.get("teams") or []) if isinstance(t, dict)]
    if not riders:
        return ""
    r = riders[0]
    rank = r.get("would_rank_best20") if metric_best20 and r.get("would_rank_best20") is not None else r.get("would_rank")
    text = f"{r.get('name') or r.get('short') or 'The rider'} alone has {_fmt(r.get('final_total'))} points."
    more = ""                         # the team comparison, hidden on phones
    if rank is not None:
        basis = "best-20 totals" if metric_best20 else "all-rider points"
        text += f" As a team he would rank {_ordinal(rank)} on {basis}"
        if r.get("would_rank_official") is not None:
            text += f" ({_ordinal(r['would_rank_official'])} against the official listing)"
        text += "."
        key = "final_total_best20" if metric_best20 else "final_total"
        lead = max(teams, key=lambda t: _num(t.get(key)), default=None)
        if lead:
            name = lead.get('short') or lead.get('name')
            if metric_best20:
                more = (f" {name} lead the teams with {_fmt(lead.get(key))} under the UCI best-20 rule "
                        f"({_fmt(lead.get('final_total'))} from all riders).")
            else:
                more = (f" {name} lead the teams with {_fmt(lead.get(key))} from all riders; "
                        f"{_fmt(lead.get('final_total_best20'))} under the UCI best-20 rule.")
    tail = f'<span class="dek-more">{_esc(more)}</span>' if more else ""
    return f'<span class="key key-rider"></span><span>{_esc(text)}{tail}</span>'


def render_html(payload: dict, *, standalone: bool = True) -> str:
    """Render the chart payload as an HTML page.

    ``standalone=True`` (default) returns a complete document
    (``<!doctype html>`` ... ``</html>``).  ``standalone=False`` returns the
    body fragment (``<title>``, ``<style>``, content, scripts) for hosts that
    wrap pages in their own document skeleton, such as a claude.ai Artifact.
    """
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dict (see analysis.team_points.build_chart_payload)")
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    teams = [t for t in (payload.get("teams") or []) if isinstance(t, dict)]
    official = payload.get("official") if isinstance(payload.get("official"), dict) else {}
    season = meta.get("season") or ""
    best20 = "best" in str(meta.get("metric_default") or "").lower()
    title = meta.get("title") or f"{season} UCI points by team, week by week".strip()
    page_title = f"{PAGE_TITLE} {season}".strip()
    gender = {"men": "Men's road", "women": "Women's road"}.get(str(meta.get("gender") or "").lower(), "Road")
    eyebrow = [x for x in ("UCI team points", gender, f"Season {season}" if season else "") if x]
    off_label = official.get("label") or "official UCI team ranking"
    off_date = f" of {_day(official['date'])}" if official.get("date") else ""
    n = len(teams)
    dek = (f"Season-to-date UCI points per team, week by week, for the top {n} teams of the "
           f"{off_label}{off_date}.")
    dek_phone = f"The top {n} of the {off_label}{off_date}."      # phones: the title says the rest
    dek_more = ("The numbered weeks are each team's biggest jumps and headline wins, with the riders "
                "and results behind them.")
    metric_label = "Best 20 riders per team (UCI rule)" if best20 else "All riders"
    sub = f"{metric_label} \u00b7 season-to-date UCI points per ISO week (Monday start)"
    if meta.get("data_through"):
        sub += f" \u00b7 data through {_day(meta['data_through'])}"
    foot = []
    if meta.get("generated_at"):
        foot.append(f"Generated {meta['generated_at']}.")
    if official.get("ranking"):
        foot.append(f"Official ranking page: {official['ranking']}" + (f" ({official['date']})." if official.get("date") else "."))
    rider_dek = _dek_rider(payload, best20)
    pressed = (lambda on: "true" if on else "false")

    head = (f"<title>{_esc(page_title)}</title>\n"
            '<meta name="claude:viewport-fit-sides" content="cover">\n'
            f"<style>{_CSS}</style>")
    body = f"""<div class="page">
<header class="masthead">
  <p class="eyebrow">{_esc(' \u00b7 '.join(eyebrow))}</p>
  <h1>{_esc(title)}</h1>
  <p class="dek"><span class="dek-full">{_esc(dek)} </span><span class="dek-phone">{_esc(dek_phone)}</span><span class="dek-more">{_esc(dek_more)}</span></p>
  <p class="dek-rider" id="tp-dek-rider"{'' if rider_dek else ' hidden'}>{rider_dek}</p>
  <dl class="facts">{_facts(meta, n)}<div><dd><a href="#sources">Sources<span class="fact-long"> and reconciliation</span> \u2193</a></dd></div></dl>
</header>
<div class="controls" role="group" aria-label="Chart controls">
  <div class="ctl"><span class="ctl-label" id="tp-lbl-metric">Points</span>
    <div class="seg" role="group" aria-labelledby="tp-lbl-metric">
      <button type="button" id="tp-m-all" data-metric-btn="all" aria-pressed="{pressed(not best20)}">All riders</button>
      <button type="button" id="tp-m-b20" data-metric-btn="best20" aria-pressed="{pressed(best20)}">Best 20 riders (UCI rule)</button>
    </div>
  </div>
  <div class="ctl ctl-pick"><label class="ctl-label" for="tp-pick">Highlight</label><select id="tp-pick"></select>
    <span class="hint">Up to 3 teams in colour</span></div>
  <div class="chips" id="tp-chips" role="group" aria-label="Highlighted series (legend)"></div>
</div>
<section id="season" aria-labelledby="tp-h-season">
  <div class="sec-head"><div><h2 id="tp-h-season">The season, week by week</h2><p id="tp-sub">{_esc(sub)}</p></div></div>
  <figure class="figure">
    <div class="ov-host" id="tp-ov"></div>
    <figcaption class="legend">
      <span><span class="key"></span>Other teams</span>
      <span><span class="key key-rider"></span>One rider's own points, plotted like a team</span>
      <span><span class="key-dot"></span>Biggest weeks of the coloured series</span>
      <span class="hint">Hover or tap for the week; click a line to colour it</span>
    </figcaption>
  </figure>
</section>
<section id="teams" aria-labelledby="tp-h-teams">
  <div class="sec-head">
    <div><h2 id="tp-h-teams">Team by team</h2>
      <p>Line: season-to-date points. Bars: points per week, red below the line for weeks lost to UCI sanctions. Numbered circles mark the biggest weeks and headline wins, listed under each panel. The rider's panel sits where his points rank against the official team totals.</p></div>
    <div class="ctl"><span class="ctl-label" id="tp-lbl-scale">Y scale</span>
      <div class="seg" role="group" aria-labelledby="tp-lbl-scale">
        <button type="button" id="tp-s-shared" data-scale-btn="shared" aria-pressed="true">Shared</button>
        <button type="button" id="tp-s-own" data-scale-btn="own" aria-pressed="false">Per panel</button>
      </div>
    </div>
  </div>
  <div class="grid" id="tp-grid"></div>
</section>
<section id="standings" aria-labelledby="tp-h-standings">
  <div class="sec-head"><div><h2 id="tp-h-standings">Standings</h2>
    <p>Official rank and total from the ranking listing; all-rider and best-20 totals from the weekly ledger. The rider row sits where his points rank against the official team totals.</p></div></div>
  <div class="table-wrap" role="region" aria-labelledby="tp-h-standings" tabindex="0">
    <table id="tp-table">
      <thead><tr>
        <th scope="col" class="num col-rank">Rank</th><th scope="col">Team</th><th scope="col" class="col-cls">Class</th>
        <th scope="col" class="num" data-col="official">Official</th>
        <th scope="col" class="num{' is-metric' if not best20 else ''}" data-col="all">All riders</th>
        <th scope="col" class="num{' is-metric' if best20 else ''}" data-col="best20">Best 20</th>
        <th scope="col" class="col-jump">Biggest jump</th>
      </tr></thead>
      <tbody></tbody>
    </table>
  </div>
</section>
<section id="sources" aria-labelledby="tp-h-sources">
  <div class="sec-head"><div><h2 id="tp-h-sources">Sources and reconciliation</h2></div></div>
  {_provenance(meta) or '<p class="hint">No source information in this payload.</p>'}
</section>
<footer class="foot">{''.join(f'<p>{_esc(x)}</p>' for x in foot)}</footer>
</div>
<div class="tip" id="tp-tip" role="tooltip" hidden></div>
<div class="sr" id="tp-live" aria-live="polite"></div>
<script type="application/json" id="tp-data">{_json_for_script(payload)}</script>
<script>{_JS}</script>"""
    if not standalone:
        return head + "\n" + body + "\n"
    return ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">\n"
            f"{head}\n</head>\n<body>\n{body}\n</body>\n</html>\n")


# ---------------------------------------------------------------------------
# synthetic demo payload (development + tests; NOT real results)
# ---------------------------------------------------------------------------

# (slug, name, short, class, all-rider target total, three riders)
_DEMO_TEAMS = [
    ("uae-team-emirates-xrg-2026", "UAE Team Emirates - XRG", "UAE", "WT", 32340,
     ("Tadej Pogačar", "Isaac del Toro", "João Almeida")),
    ("red-bull-bora-hansgrohe-2026", "Red Bull - BORA - hansgrohe", "Red Bull-Bora", "WT", 26090,
     ("Remco Evenepoel", "Primož Roglič", "Florian Lipowitz")),
    ("team-visma-lease-a-bike-2026", "Team Visma | Lease a Bike", "Visma", "WT", 23430,
     ("Jonas Vingegaard", "Wout van Aert", "Matteo Jorgenson")),
    ("decathlon-cma-cgm-team-2026", "Decathlon CMA CGM Team", "Decathlon", "WT", 21420,
     ("Paul Seixas", "Felix Gall", "Olav Kooij")),
    ("lidl-trek-2026", "Lidl - Trek", "Lidl-Trek", "WT", 19810,
     ("Mads Pedersen", "Juan Ayuso", "Jonathan Milan")),
    ("xds-astana-team-2026", "XDS Astana Team", "Astana", "WT", 17420,
     ("Christian Scaroni", "Harold Tejada", "Alberto Bettiol")),
    ("bahrain-victorious-2026", "Bahrain - Victorious", "Bahrain", "WT", 14270,
     ("Santiago Buitrago", "Antonio Tiberi", "Matej Mohorič")),
    ("netcompany-ineos-2026", "Netcompany INEOS", "INEOS", "WT", 13380,
     ("Carlos Rodríguez", "Egan Bernal", "Filippo Ganna")),
    ("uno-x-mobility-2026", "Uno-X Mobility", "Uno-X", "WT", 13160,
     ("Tobias Halland Johannessen", "Magnus Cort", "Søren Wærenskjold")),
    ("soudal-quick-step-2026", "Soudal Quick-Step", "Soudal QS", "WT", 12830,
     ("Tim Merlier", "Paul Magnier", "Ilan Van Wilder")),
    ("alpecin-premier-tech-2026", "Alpecin - Premier Tech", "Alpecin", "WT", 11710,
     ("Mathieu van der Poel", "Jasper Philipsen", "Kaden Groves")),
    ("movistar-team-2026", "Movistar Team", "Movistar", "WT", 10870,
     ("Enric Mas", "Javier Romo", "Iván Romeo")),
    ("pinarello-q36-5-pro-cycling-team-2026", "Pinarello Q36.5 Pro Cycling Team", "Pinarello-Q36.5", "PRT", 11220,
     ("Tom Pidcock", "Fabio Christen", "Matteo Moschetti")),
    ("team-jayco-alula-2026", "Team Jayco AlUla", "Jayco", "WT", 10820,
     ("Ben O'Connor", "Michael Matthews", "Luke Plapp")),
    ("ef-education-easypost-2026", "EF Education - EasyPost", "EF", "WT", 10930,
     ("Richard Carapaz", "Ben Healy", "Neilson Powless")),
    ("lotto-intermarche-2026", "Lotto Intermarché", "Lotto-Intermarché", "WT", 10710,
     ("Arnaud De Lie", "Lennert Van Eetvelt", "Biniam Girmay")),
    ("tudor-pro-cycling-team-2026", "Tudor Pro Cycling Team", "Tudor", "PRT", 10730,
     ("Julian Alaphilippe", "Marc Hirschi", "Michael Storer")),
    ("cofidis-2026", "Cofidis", "Cofidis", "PRT", 9880,
     ("Benjamin Thomas", "Emanuel Buchmann", "Alex Aranburu")),
    ("nsn-cycling-team-2026", "NSN Cycling Team", "NSN", "WT", 9850,
     ("Ethan Vernon", "Derek Gee", "Nick Schultz")),
    ("groupama-fdj-united-2026", "Groupama - FDJ United", "FDJ", "WT", 9680,
     ("David Gaudu", "Romain Grégoire", "Guillaume Martin")),
    ("totalenergies-2026", "TotalEnergies", "TotalEnergies", "PRT", 7440,
     ("Anthony Turgis", "Mattéo Vercher", "Emilien Jeannière")),
    ("unibet-rose-rockets-2026", "Unibet Rose Rockets", "Unibet", "PRT", 7430,
     ("Dylan Groenewegen", "Victor Lafay", "Clément Venturini")),
    ("caja-rural-seguros-rga-2026", "Caja Rural - Seguros RGA", "Caja Rural", "PRT", 4920,
     ("Jefferson Cepeda", "Abel Balderstone", "Fernando Barceló")),
    ("burgos-burpellet-bh-2026", "Burgos Burpellet BH", "Burgos", "PRT", 4220,
     ("Eric Fagúndez", "Pelayo Sánchez", "Jesús Ezquerra")),
    ("team-polti-visitmalta-2026", "Team Polti VisitMalta", "Polti", "PRT", 3980,
     ("Davide Piganzoli", "Giovanni Lonardi", "Andrea Pietrobon")),
    ("solution-tech-nippo-rali-2026", "Solution Tech NIPPO Rali", "Solution Tech", "PRT", 3420,
     ("Sergio Meris", "Manuele Tarozzi", "Lorenzo Quartucci")),
    ("equipo-kern-pharma-2026", "Equipo Kern Pharma", "Kern Pharma", "PRT", 3080,
     ("Pablo Castrillo", "Urko Berrade", "José Félix Parra")),
    ("euskaltel-euskadi-2026", "Euskaltel - Euskadi", "Euskaltel", "PRT", 2910,
     ("Mikel Bizkarra", "Xabier Isasa", "Gotzon Martín")),
    ("mbh-bank-csb-telecom-fort-2026", "MBH Bank CSB Telecom Fort", "MBH Bank", "PRT", 2880,
     ("Attila Valter", "Márton Dina", "Erik Fetter")),
    ("team-picnic-postnl-2026", "Team Picnic PostNL", "Picnic", "WT", 2800,
     ("Max Poole", "Oscar Onley", "Pavel Bittner")),
]

# (week Monday, race, kind, winner points); GC/one-day points land in the week of the final day
_DEMO_RACES = [
    ("2026-01-19", "Tour Down Under", "stage", 500),
    ("2026-02-16", "UAE Tour", "stage", 500),
    ("2026-03-02", "Strade Bianche", "oneday", 500),
    ("2026-03-09", "Paris-Nice", "stage", 500),
    ("2026-03-16", "Milano-Sanremo", "oneday", 800),
    ("2026-03-23", "Volta a Catalunya", "stage", 500),
    ("2026-03-30", "Ronde van Vlaanderen", "oneday", 800),
    ("2026-04-06", "Paris-Roubaix", "oneday", 800),
    ("2026-04-13", "Amstel Gold Race", "oneday", 500),
    ("2026-04-20", "Liège-Bastogne-Liège", "oneday", 800),
    ("2026-04-27", "Tour de Romandie", "stage", 500),
    ("2026-05-25", "Giro d'Italia", "gt", 1150),
    ("2026-06-08", "Critérium du Dauphiné", "stage", 500),
    ("2026-06-15", "Tour de Suisse", "stage", 500),
    ("2026-07-20", "Tour de France", "gt", 1300),
    ("2026-08-03", "Clásica San Sebastián", "oneday", 500),
    ("2026-09-07", "Vuelta a España", "gt", 1100),
    ("2026-09-21", "World Championships Road Race", "oneday", 600),
    ("2026-10-05", "Il Lombardia", "oneday", 800),
]

# Pogačar's demo season: week -> (race, detail, points, won?)
_DEMO_POGACAR = {
    "2026-03-02": ("Strade Bianche", "1st", 500.0, True),
    "2026-03-16": ("Milano-Sanremo", "1st", 800.0, True),
    "2026-03-30": ("Ronde van Vlaanderen", "1st, penalty -25", 775.0, True),
    "2026-04-06": ("Paris-Roubaix", "2nd", 640.0, False),
    "2026-04-20": ("Liège-Bastogne-Liège", "1st", 800.0, True),
    "2026-04-27": ("Tour de Romandie", "GC 1st, 4 stage wins", 820.0, True),
    "2026-06-15": ("Tour de Suisse", "GC 1st, stages 1, 4 & 5 1st", 728.0, True),
    "2026-07-06": ("Tour de France", "stage 4 1st", 210.0, False),
    "2026-07-13": ("Tour de France", "stage 15 1st, stage 10 2nd", 330.0, False),
    "2026-07-20": ("Tour de France", "GC 1st, stage 19 1st, KOM 2nd", 2040.0, True),
    "2026-09-14": ("GP de Montréal", "3rd", 250.0, False),
    "2026-09-21": ("World Championships Road Race", "2nd", 480.0, False),
    "2026-10-05": ("Il Lombardia", "1st", 800.0, True),
    "2026-10-12": ("Tre Valli Varesine", "1st", 200.0, False),
}


# background points: the smaller races of each month (1.1 / 2.1 / 1.Pro / 2.Pro)
_DEMO_MINOR = {
    1: "Challenge Mallorca", 2: "Volta ao Algarve", 3: "Nokere Koerse", 4: "Tour of the Alps",
    5: "Boucles de la Mayenne", 6: "Tour of Slovenia", 7: "Tour de Wallonie", 8: "Arctic Race of Norway",
    9: "Tour of Britain", 10: "Tour of Guangxi",
}


def _surname(full: str) -> str:
    parts = full.split()
    for i, p in enumerate(parts[1:], start=1):
        if p[:1].islower():  # 'del Toro', 'van der Poel', 'Van Wilder' stays 'Wilder'
            return " ".join(parts[i:])
    return parts[-1] if parts else full


def _demo_label(events: list[dict]) -> str:
    top = max(events, key=lambda e: e["points"])
    who, race, det = _surname(top["rider"]), top["race"], top["detail"]
    who = who[:1].upper() + who[1:]
    if det.startswith("GC 1st"):
        rest = det[len("GC 1st"):].lstrip(", ")
        return f"{who}: {race} GC 1st" + (f" + {rest}" if rest else "")
    if det == "1st" or det.startswith("1st,"):
        return f"{who} wins {race}"
    if len(events) > 2:
        return f"{race} ({len(events)} riders)"
    return f"{who}: {race} {det.split(',')[0]}"


def _demo_payload(seed: int = 2026) -> dict:
    """A realistic synthetic payload (30 teams incl. long names, 40 weeks, jumps, a penalty week).

    Shaped exactly like ``analysis.team_points.build_chart_payload``'s output,
    including the ``riders`` entry for Pogačar.  Numbers are invented (seeded,
    deterministic); ``meta.source`` says so.
    """
    rng = random.Random(seed)
    weeks = [date(2026, 1, 12) + timedelta(weeks=i) for i in range(40)]
    wk = [w.isoformat() for w in weeks]
    widx = {w: i for i, w in enumerate(wk)}
    nw, nt = len(wk), len(_DEMO_TEAMS)
    events: list[list[list[dict]]] = [[[] for _ in range(nw)] for _ in range(nt)]

    def add(team: int, week: str, rider: str, race: str, detail: str, pts: float) -> None:
        events[team][widx[week]].append({"rider": rider, "race": race, "detail": detail, "points": float(pts)})

    weights = [(nt + 1 - i) ** 2.2 for i in range(nt)]

    def pick(exclude: set[int]) -> int:
        while True:
            t = rng.choices(range(nt), weights=weights)[0]
            if t not in exclude:
                return t

    pog = _DEMO_TEAMS[0][5][0]
    for week, (race, detail, pts, _won) in _DEMO_POGACAR.items():
        add(0, week, pog, race, detail, pts)
    for week, race, kind, win in _DEMO_RACES:
        p = _DEMO_POGACAR.get(week)
        taken: set[int] = set()
        if p and p[0] == race and p[3]:
            taken.add(0)  # Pogačar won it
            podium = [0.8, 0.68]
        else:
            podium = [1.0, 0.8, 0.68]
            if race == "Paris-Roubaix":
                add(10, week, _DEMO_TEAMS[10][5][0], race, "1st", win)
                taken.update({0, 10})
                podium = [0.68]
        for share in podium:
            t = pick(taken)
            taken.add(t)
            place = {1.0: "1st", 0.8: "2nd", 0.68: "3rd"}[share]
            rider = _DEMO_TEAMS[t][5][rng.randrange(2) if t else 1]
            if kind in ("stage", "gt"):
                stages = rng.randint(0, 2 if kind == "gt" else 1)
                detail = f"GC {place}" + (f", {stages} stage win{'s' if stages > 1 else ''}" if stages else "")
                pts = win * share + stages * (210 if kind == "gt" else 60)
            else:
                detail, pts = place, win * share
            add(t, week, rider, race, detail, round(pts))
        for _ in range(7 if kind == "gt" else 4):  # minor placings 4th-10th
            t = pick(set())
            add(t, week, _DEMO_TEAMS[t][5][2], race, f"{rng.randint(4, 10)}th", round(win * rng.uniform(0.12, 0.4)))
        if kind == "gt":  # stage wins in the two earlier weeks of a grand tour
            for back in (1, 2):
                prev = (date.fromisoformat(week) - timedelta(weeks=back)).isoformat()
                if prev in widx and not (p and prev in _DEMO_POGACAR):
                    for _ in range(3):
                        t = pick(set())
                        add(t, prev, _DEMO_TEAMS[t][5][rng.randrange(3)], race, f"stage {rng.randint(2, 18)} 1st", 210)

    gains = [[0.0] * nw for _ in range(nt)]
    for t, (_slug, _name, _short, _cls, target, riders) in enumerate(_DEMO_TEAMS):
        fixed = sum(e["points"] for wkev in events[t] for e in wkev)
        bg_w = [rng.random() ** 1.6 * (0.3 if i < 2 else 1.0) for i in range(nw)]
        scale = max(0.0, target - fixed) / (sum(bg_w) or 1.0)
        for i in range(nw):
            bg = round(bg_w[i] * scale)
            if bg:
                race = _DEMO_MINOR[weeks[i].month]
                events[t][i].append({"rider": riders[1 + i % 2], "race": race, "detail": "GC 2nd, 1 stage win" if bg > 300 else "several top-10s",
                                     "points": float(bg), "minor": True})
            gains[t][i] = sum(e["points"] for e in events[t][i])
    # TTT shares, so a few totals carry fractions, as on PCS
    gains[1][widx["2026-03-09"]] += 0.25
    gains[4][widx["2026-03-09"]] += 0.75
    # penalty weeks: sanctions with no points that week -> negative gains
    for t, week, pts in ((10, "2026-07-27", 37.0), (27, "2026-05-04", 17.0)):
        i = widx[week]
        events[t][i] = []
        gains[t][i] = -pts

    cums = []
    for t in range(nt):
        run, out = 0.0, []
        for g in gains[t]:
            run = round(run + g, 2)
            out.append(run)
        cums.append(out)

    def rank_at(value: float, i: int, skip: Optional[int] = None) -> int:
        return 1 + sum(1 for o in range(nt) if o != skip and cums[o][i] > value + 1e-9)

    teams = []
    for t, (slug, name, short, cls, _target, riders) in enumerate(_DEMO_TEAMS):
        total = cums[t][-1]
        f = 0.93 + 0.07 * (t / (nt - 1)) - 0.01 * rng.random()
        best = [round(v * f, 2) for v in cums[t]]
        order = sorted(range(nw), key=lambda i: gains[t][i], reverse=True)
        jump_weeks = sorted(i for i in order[:5] if gains[t][i] >= 0.05 * total)
        if short == "Picnic":
            jump_weeks = []  # a team without a standout week
        jumps = []
        for i in jump_weeks:
            evs = sorted(events[t][i], key=lambda e: -e["points"])
            jumps.append({
                "week": wk[i], "week_index": i, "gain": round(gains[t][i], 2),
                "share_of_season": round(gains[t][i] / total, 4) if total else 0.0,
                "label": _demo_label(evs),
                "contributors": [{"rider": e["rider"], "race": e["race"], "detail": e["detail"], "points": e["points"]}
                                 for e in evs[:4]],
            })
        b20 = best[-1]
        lag = 0 if t % 4 == 0 else rng.uniform(0.0, 0.015)
        teams.append({
            "key": slug, "name": name, "short": short, "class": cls,
            "official_rank": t + 1, "official_total": float(round(b20 * (1 - lag))),
            "breakdown_total": b20, "final_total": total, "final_rank": rank_at(total, nw - 1, skip=t),
            "cumulative": cums[t], "gains": [round(g, 2) for g in gains[t]],
            "rank_by_week": [rank_at(cums[t][i], i, skip=t) for i in range(nw)],
            "cumulative_best20": best, "final_total_best20": b20,
            "jumps": jumps,
            "top_riders": [{"rider": r, "points": round(total * s, 2)} for r, s in zip(riders, (0.29, 0.17, 0.09))],
        })

    # Pogačar alone
    g = [0.0] * nw
    for week, (_race, _detail, pts, _won) in _DEMO_POGACAR.items():
        g[widx[week]] += pts
    c, run = [], 0.0
    for v in g:
        run = round(run + v, 2)
        c.append(run)
    final = c[-1]
    big = sorted(range(nw), key=lambda i: g[i], reverse=True)[:6]
    rjumps = []
    for i in sorted(big):
        race, detail, pts, _won = _DEMO_POGACAR[wk[i]]
        ev = [{"rider": pog, "race": race, "detail": detail, "points": pts}]
        rjumps.append({"week": wk[i], "week_index": i, "gain": pts, "share_of_season": round(pts / final, 4),
                       "label": _demo_label(ev).replace("Pogačar wins", "Wins").replace("Pogačar: ", ""),
                       "contributors": ev})
    finals = [tm["final_total"] for tm in teams]
    riders_out = [{
        "key": "rider:tadej-pogacar", "kind": "rider", "name": pog, "short": "Pogačar",
        "team_slug": _DEMO_TEAMS[0][0], "team_short": "UAE",
        "official_total": final, "final_total": final,
        "would_rank": 1 + sum(1 for v in finals if v > final + 1e-9),
        "would_rank_best20": 1 + sum(1 for tm in teams if tm["final_total_best20"] > final + 1e-9),
        "would_rank_official": 1 + sum(1 for tm in teams if tm["official_total"] > final + 1e-9),
        "cumulative": c, "gains": g,
        "rank_by_week": [1 + sum(1 for t in range(nt) if cums[t][i] > c[i] + 1e-9) for i in range(nw)],
        "cumulative_best20": list(c), "jumps": rjumps, "top_riders": None,
    }]
    n_pen = 2
    return {
        "meta": {
            "title": "2026 UCI points \u2014 top 30 men's teams, week by week",
            "season": 2026, "gender": "men", "generated_at": "2026-10-18 21:00",
            "data_through": "2026-10-18",
            "week_convention": ("ISO weeks, Monday start; points dated to the result date "
                                "(GC/classification points on the race's final day)"),
            "source": ("Synthetic demo data from analysis.team_points_chart._demo_payload, shaped like "
                       "the ProCyclingStats official UCI team ranking (rankings/me/ranking-2026-2028) "
                       "and riders' dated results; not real results"),
            "metric_default": "all_riders",
            "coverage": {
                "teams": nt, "riders": 612, "results_with_points": 8020, "penalties": n_pen,
                "reconciliation": {
                    "max_abs_team_diff": 42.5, "teams_exact": 27,
                    "note": ("Best-20 ledger vs per-team PCS breakdown ('points counted'): 27/30 teams within "
                             "1 pt, largest gap 42.5 pts. Best-20 ledger vs the official listing: 8/30; the "
                             "listing lags the breakdown pages."),
                },
                "notes": [
                    "Synthetic demo data: team names are real, every number and result is invented.",
                    "'All riders' adds every rider's points; the official ranking counts each team's 20 best "
                    "riders of the season, so all-rider totals are usually higher.",
                    "UCI sanctions are subtracted on the day they were imposed; a week with a sanction and no "
                    "points shows a negative gain (Alpecin, week of Jul 27; Euskaltel, week of May 4).",
                    "Fractional points come from team time trial shares, which PCS rounds to two decimals.",
                ],
            },
        },
        "weeks": wk,
        "teams": teams,
        "riders": riders_out,
        "official": {"ranking": "rankings/me/ranking-2026-2028", "date": "2026-10-18",
                     "label": "Official UCI team ranking 2026 (PCS)"},
    }


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Render the team-UCI-points chart page.")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--payload", help="chart payload JSON (analysis.team_points.build_chart_payload)")
    src.add_argument("--demo", action="store_true", help="render the synthetic demo payload")
    p.add_argument("--out", required=True, help="HTML file to write")
    p.add_argument("--fragment", action="store_true", help="write the body fragment, not a full document")
    args = p.parse_args(argv)
    payload = _demo_payload() if args.demo else json.loads(Path(args.payload).read_text(encoding="utf-8"))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(payload, standalone=not args.fragment), encoding="utf-8")
    log.info("wrote %s", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
