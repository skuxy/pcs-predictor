#!/usr/bin/env bash
# Weekly update pipeline:
#   1. Scrape missing results for the current year
#   2. Regenerate the static HTML dashboard
#
# Recommended cron (runs every Sunday at 03:00):
#   0 3 * * 0  /path/to/pcs-predictor/scripts/update_weekly.sh >> /path/to/pcs-predictor/logs/weekly.log 2>&1

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$SCRIPT_DIR/.."
cd "$PROJECT"

YEAR=$(date +%Y)
LOGDIR="$PROJECT/logs"
mkdir -p "$LOGDIR"

echo "===== $(date '+%Y-%m-%d %H:%M:%S') ====="
echo "Project: $PROJECT"
echo "Year:    $YEAR"

source .venv/bin/activate

# ── 1. Scrape missing race results ────────────────────────────────────────────
echo ""
echo ">> Scraping missing results for $YEAR ..."
python main.py scrape-missing --year "$YEAR"

# ── 2. Optionally retrain the model ──────────────────────────────────────────
# Uncomment to auto-retrain weekly (slow — ~5 min):
#
# CUTOFF=$(date +%Y-%m-%d)
# echo ""
# echo ">> Retraining men's model (cutoff $CUTOFF) ..."
# python main.py train --cutoff "$CUTOFF"
#
# echo ">> Retraining women's model ..."
# python main.py train --gender women --cutoff "$CUTOFF"

# ── 3. Generate static dashboard ─────────────────────────────────────────────
echo ""
echo ">> Generating dashboard ..."
python scripts/generate_dashboard.py

echo ""
echo "===== Done $(date '+%Y-%m-%d %H:%M:%S') ====="
