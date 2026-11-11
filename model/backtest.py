"""
Backtest: compare model predictions to actual results for a historic race.

Usage
─────
  python main.py backtest --race race/giro-d-italia/2026

A race covered by the last `python main.py evaluate` run is shown with its
saved walk-forward predictions (models that never saw it).  Any other race is
predicted by the model on disk, and the header says whether that model was
trained on the race.

Outputs
───────
  Per-stage ranked prediction table (showing top-10 predicted and whether
  they actually finished top 10).

  Summary metrics:
    - Top-10 precision/recall per stage
    - Overall AUC across all stages
    - GC leaderboard prediction (cumulative: who was predicted best most often)
"""
import argparse
import logging

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score, average_precision_score

from model.predict import backtest_predictions, model_info, SOURCE_SEEN, SOURCE_WALK_FORWARD

log = logging.getLogger(__name__)


def source_note(source: str, gender: str = "men") -> str:
    """One line on how far a backtest's numbers can be trusted."""
    if source == SOURCE_WALK_FORWARD:
        return "out of sample: walk-forward predictions, models trained only on earlier stages"
    info = model_info(gender)
    trained_to = info.get("last_training_stage", info.get("train_cutoff", "?"))
    if source == SOURCE_SEEN:
        return (f"IN SAMPLE: the current model was trained on this race (results up to {trained_to}) — "
                f"these numbers flatter it; run `python main.py evaluate` for honest ones")
    return f"out of sample: the race is after the last result the current model was trained on ({trained_to})"


def backtest(race_slug: str, cutoff_date: str | None = None, top_n: int = 10, gender: str = "men") -> None:
    df, source = backtest_predictions(race_slug, gender, cutoff_date)
    if df.empty:
        print("No data — check that the race is in the database.")
        return

    # Only rows where we know the actual outcome
    known = df.dropna(subset=["top10", "position"])

    print(f"\n{'='*70}")
    print(f"  BACKTEST: {race_slug}")
    print(f"  Stages: {known['stage_date'].nunique()}  |  {source_note(source, gender)}")
    print(f"{'='*70}\n")

    stage_metrics = []
    for stage_date, group in known.groupby("stage_date"):
        group = group.sort_values("top10_prob", ascending=False)
        predicted_top10 = group.head(top_n)

        actual_top10_riders = set(
            group[group["top10"] == 1]["rider_id"].tolist()
            if "rider_id" in group.columns else []
        )
        predicted_top10_riders = set(predicted_top10["rider_id"].tolist()
                                     if "rider_id" in predicted_top10.columns else [])

        hits = len(predicted_top10_riders & actual_top10_riders)
        precision = hits / top_n
        recall    = hits / max(len(actual_top10_riders), 1)

        stage_name = group["race_name"].iloc[0] if "race_name" in group.columns else "?"

        stage_metrics.append({
            "date":      stage_date,
            "profile":   group["profile_type"].iloc[0] if "profile_type" in group.columns else "?",
            "precision": precision,
            "recall":    recall,
            "hits":      hits,
        })

        # Per-stage table: top-10 predicted
        print(f"  Stage {str(stage_date)[:10]}  "
              f"precision={precision:.2f}  recall={recall:.2f}  ({hits}/{top_n} correct)\n")
        print(f"  {'Rank':>4}  {'Rider':<28}  {'P(top10)':>8}  {'Actual pos':>10}  {'✓':>3}")
        print(f"  {'-'*60}")
        for rank, (_, row) in enumerate(group.head(top_n).iterrows(), 1):
            actual_pos = int(row["position"]) if pd.notna(row["position"]) else "DNF"
            correct    = "✓" if row.get("top10") == 1 else " "
            name       = (row.get("rider_name") or "Unknown")[:27]
            print(f"  {rank:>4}  {name:<28}  {row['top10_prob']:>8.3f}  {str(actual_pos):>10}  {correct:>3}")
        print()

    # Overall metrics
    if known["top10"].nunique() > 1:
        auc = roc_auc_score(known["top10"], known["top10_prob"])
        ap  = average_precision_score(known["top10"], known["top10_prob"])
    else:
        auc = ap = float("nan")

    sm = pd.DataFrame(stage_metrics)
    print(f"{'='*70}")
    print(f"  SUMMARY")
    print(f"  Stages evaluated : {len(sm)}")
    print(f"  Avg precision@10 : {sm['precision'].mean():.3f}")
    print(f"  Avg recall@10    : {sm['recall'].mean():.3f}")
    print(f"  Overall AUC      : {auc:.4f}")
    print(f"  Overall AP       : {ap:.4f}")

    if "profile" in sm.columns and len(sm) > 1:
        print(f"\n  Precision@10 by profile type:")
        for profile, grp in sm.groupby("profile"):
            profile_known = known[known["profile_type"] == profile] if "profile_type" in known.columns else pd.DataFrame()
            p_auc = (
                roc_auc_score(profile_known["top10"], profile_known["top10_prob"])
                if len(profile_known) and profile_known["top10"].nunique() > 1
                else float("nan")
            )
            auc_str = f"  AUC={p_auc:.3f}" if not np.isnan(p_auc) else ""
            print(f"    {profile:<10}  precision={grp['precision'].mean():.3f}{auc_str}  ({len(grp)} stage{'s' if len(grp) != 1 else ''})")

    print(f"{'='*70}\n")

    # GC leaderboard: who appeared in predicted top-10 most often?
    print("  GC LEADERBOARD (predicted top-10 appearances across all stages)\n")
    gc = (
        df.sort_values("top10_prob", ascending=False)
        .groupby("stage_date")
        .head(top_n)
        .groupby("rider_name")
        .agg(
            appearances=("top10_prob", "count"),
            avg_prob=("top10_prob", "mean"),
            actual_top10=("top10", "sum"),
        )
        .sort_values("appearances", ascending=False)
        .head(20)
    )
    print(f"  {'Rider':<28}  {'Stages in pred top10':>20}  {'Avg P(top10)':>12}  {'Actual top10s':>13}")
    print(f"  {'-'*78}")
    for rider, row in gc.iterrows():
        print(f"  {str(rider):<28}  {int(row['appearances']):>20}  "
              f"{row['avg_prob']:>12.3f}  {int(row['actual_top10']):>13}")
    print()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--race",    default="race/giro-d-italia/2024")
    parser.add_argument("--cutoff",  default=None,
                        help="First stage date to predict (default: the race start)")
    parser.add_argument("--top-n",   type=int, default=10)
    parser.add_argument("--gender",  default="men", choices=["men", "women"])
    args = parser.parse_args()
    backtest(args.race, args.cutoff, args.top_n, args.gender)
