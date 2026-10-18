#!/usr/bin/env python3
"""Weekly UCI team-points report: chart payload (JSON), optional HTML chart, markdown summary.

    python scripts/team_points_report.py --season 2026 --top 30 \\
        --db /path/to/copy.db --out-json /tmp/team_points.json --out-html /tmp/team_points.html

Reads the team-UCI-points tables written by scripts/scrape_uci_points.py
(``uci_team_totals``, ``uci_team_breakdown``, ``rider_uci_results``,
``rider_uci_penalties``, ``rider_team_spells``) from ``--db`` (default
``config.DB_PATH``), opened READ-ONLY, and builds the payload with
``analysis.team_points.build_chart_payload``.  ``--rider SLUG`` (repeatable,
default ``tadej-pogacar``) adds a single rider's own series to compare with
the teams.  ``--out-html`` renders the payload with
``analysis.team_points_chart.render_html`` (imported only when asked for).
The markdown summary (top-N table + one line per rider) goes to stdout.

Exit codes: 0 ok, 1 database missing / no UCI-points tables / no listing / the HTML
renderer is unavailable (the JSON and the summary are still written).
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

log = logging.getLogger("team_points_report")


def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--season", type=int, default=2026)
    p.add_argument("--top", type=int, default=30, help="number of teams (official listing order)")
    p.add_argument("--db", default=None, help="SQLite DB with the UCI-points tables "
                                             "(default: config.DB_PATH); opened read-only")
    p.add_argument("--out-json", default=None, help="write the chart payload (JSON) here")
    p.add_argument("--out-html", default=None, help="render the chart (HTML) here")
    p.add_argument("--generated-at", default=None, help="timestamp stored in meta.generated_at")
    p.add_argument("--rider", action="append", default=None, metavar="SLUG",
                   help="add a single rider's series (repeatable; default tadej-pogacar)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def _resolve_db(path: str | None) -> Path:
    """--db as given (relative to the cwd), else config.DB_PATH relative to the cwd or the repo root."""
    if path:
        return Path(path).expanduser().resolve()
    import config
    p = Path(config.DB_PATH).expanduser()
    if p.is_absolute() or p.exists():
        return p.resolve()
    return (ROOT / p).resolve()


def open_readonly(path: Path) -> sqlite3.Connection:
    """Open ``path`` read-only (sqlite URI ``mode=ro``) — never creates or writes the DB."""
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def _write_text(path: str, text: str) -> None:
    out = Path(path).expanduser()
    if out.parent and not out.parent.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")


def run(argv=None) -> int:
    """CLI entry point; returns the exit code."""
    args = _parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    from analysis.team_points import DEFAULT_RIDERS, build_chart_payload, markdown_summary

    db = _resolve_db(args.db)
    if not db.exists():
        print(f"error: database not found: {db}", file=sys.stderr)
        return 1
    conn = open_readonly(db)
    try:
        has = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='uci_team_totals'").fetchone()
        if not has:
            print(f"error: {db} has no uci_team_totals table — run scripts/scrape_uci_points.py first",
                  file=sys.stderr)
            return 1
        riders = tuple(args.rider) if args.rider else DEFAULT_RIDERS
        try:
            payload = build_chart_payload(conn, season=args.season, top_n=args.top,
                                          generated_at=args.generated_at, riders=riders)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    finally:
        conn.close()

    if args.out_json:
        _write_text(args.out_json, json.dumps(payload, ensure_ascii=False, indent=1, allow_nan=False))
        log.info("wrote %s", args.out_json)
    rc = 0
    if args.out_html:
        try:
            from analysis.team_points_chart import render_html  # imported only when asked for
        except ImportError as exc:
            print(f"error: cannot render HTML ({exc})", file=sys.stderr)
            rc = 1
        else:
            html = render_html(payload)
            if not isinstance(html, str):
                print("error: render_html did not return the page as a string", file=sys.stderr)
                rc = 1
            else:
                _write_text(args.out_html, html)
                log.info("wrote %s", args.out_html)
    sys.stdout.write(markdown_summary(payload))
    return rc


if __name__ == "__main__":
    sys.exit(run())
