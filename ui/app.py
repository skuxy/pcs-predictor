"""
Streamlit UI for the cycling race predictor.

Run from the project root:
    streamlit run ui/app.py
"""
import sys
import json
import logging
from datetime import date, timedelta
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st
from sklearn.metrics import roc_auc_score

# ── project root on path ──────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from db.database import get_conn
from model.evaluate import evaluation_path, stage_scores
from model.predict import (
    backtest_predictions, model_info, predict_race, ROUTE_UNKNOWN_NOTE, SOURCE_SEEN, SOURCE_WALK_FORWARD,
)
from model.train import model_paths

logging.basicConfig(level=logging.WARNING)

st.set_page_config(
    page_title="Cycling Predictor",
    page_icon="🚴",
    layout="wide",
)
st.title("🚴 Cycling Race Predictor")

PROFILE_LABELS = {"flat": "Flat", "hilly": "Hilly", "mountain": "Mountain", "itt": "Time trial",
                  "ttt": "Team time trial"}

# ── helpers ───────────────────────────────────────────────────────────────────

@st.cache_data(ttl=300)
def load_races(gender: str = "men") -> pd.DataFrame:
    """Every race of a gender with how many of its stages have a result."""
    with get_conn() as conn:
        return pd.read_sql(
            """SELECT r.pcs_slug, r.name, r.year, r.start_date, r.end_date, r.is_stage_race, r.class,
                      COUNT(s.id) AS stages,
                      SUM(CASE WHEN EXISTS (SELECT 1 FROM results x WHERE x.stage_id = s.id)
                               THEN 1 ELSE 0 END) AS stages_with_results,
                      MIN(CASE WHEN NOT EXISTS (SELECT 1 FROM results x WHERE x.stage_id = s.id)
                               THEN s.date END) AS first_open_stage
               FROM races r LEFT JOIN stages s ON s.race_id = r.id
               WHERE r.gender = ?
               GROUP BY r.id
               ORDER BY r.start_date DESC""",
            conn, params=(gender,),
        )


@st.cache_data(show_spinner="Running predictions…")
def run_predict(race_slug: str, cutoff: str, gender: str) -> pd.DataFrame:
    return predict_race(race_slug, cutoff, gender=gender)


@st.cache_data(show_spinner="Running backtest…")
def run_backtest(race_slug: str, gender: str) -> tuple[pd.DataFrame, str]:
    return backtest_predictions(race_slug, gender)


def load_evaluation(gender: str) -> dict:
    path = ROOT / evaluation_path(gender)
    return json.loads(path.read_text()) if path.exists() else {}


def stage_label(group: pd.DataFrame, stage_date) -> str:
    profile = group["profile_type"].iloc[0] if "profile_type" in group.columns else None
    label = PROFILE_LABELS.get(profile, "Profile unknown")
    if "is_cobbled" in group.columns and group["is_cobbled"].iloc[0]:
        label += " · cobbled"
    elif "is_gravel" in group.columns and group["is_gravel"].iloc[0]:
        label += " · gravel"
    return f"{str(stage_date)[:10]} — {label}"


PROB_LABELS = {"top10_prob": "P(top 10)", "top3_prob": "P(podium)", "win_prob": "P(win)"}


def probability_table(group: pd.DataFrame, top_n: int, extra: dict | None = None) -> pd.DataFrame:
    """Top-N riders of one stage with the probability columns the models gave."""
    cols = {"rider_name": "Rider"}
    cols.update({c: label for c, label in PROB_LABELS.items() if c in group.columns})
    cols.update(extra or {})
    out = group.sort_values("top10_prob", ascending=False).head(top_n)[list(cols)].rename(columns=cols)
    out.index = range(1, len(out) + 1)
    return out


def probability_columns(df: pd.DataFrame) -> dict:
    return {
        label: st.column_config.ProgressColumn(label, format="%.2f", min_value=0.0, max_value=1.0)
        for label in PROB_LABELS.values() if label in df.columns
    }


def metric_table(rows: list[dict], first_col: str) -> pd.DataFrame:
    """A breakdown table of the walk-forward report, ready for st.dataframe."""
    df = pd.DataFrame(rows).rename(columns={
        "group": first_col, "stages": "Stages", "p_at_10": "Top-10 hits / 10", "hits3": "Podium hits / 3",
        "win_top1": "Winner called", "win_top3": "Winner in top 3", "win_top10": "Winner in top 10",
        "winner_rank_median": "Winner's median rank", "auc": "AUC",
    })
    df["Top-10 hits / 10"] = df["Top-10 hits / 10"] * 10
    for col in ("Winner called", "Winner in top 3", "Winner in top 10"):
        df[col] = df[col] * 100
    keep = [first_col, "Stages", "Top-10 hits / 10", "Podium hits / 3", "Winner called", "Winner in top 3",
            "Winner in top 10", "Winner's median rank", "AUC"]
    return df[[c for c in keep if c in df.columns]]


METRIC_FORMAT = {
    "Stages": st.column_config.NumberColumn(format="%d"),
    "Top-10 hits / 10": st.column_config.NumberColumn(format="%.2f"),
    "Podium hits / 3": st.column_config.NumberColumn(format="%.2f"),
    "Winner called": st.column_config.NumberColumn(format="%.0f%%"),
    "Winner in top 3": st.column_config.NumberColumn(format="%.0f%%"),
    "Winner in top 10": st.column_config.NumberColumn(format="%.0f%%"),
    "Winner's median rank": st.column_config.NumberColumn(format="%d"),
    "AUC": st.column_config.NumberColumn(format="%.3f"),
}


# ── sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Settings")
    gender = st.selectbox("Gender", ["men", "women"], index=0)
    top_n  = st.slider("Top N riders shown", 5, 30, 15)
    info = model_info(gender)
    if info:
        st.caption(f"Model trained on results up to **{info.get('last_training_stage', info.get('train_cutoff', '?'))}**")
    else:
        st.caption("No trained model for this gender — run `python main.py train`.")

# ── tabs ──────────────────────────────────────────────────────────────────────
tab_pred, tab_hist, tab_model, tab_clv = st.tabs(
    ["🔮 Predictions", "📊 Historical Backtest", "🧠 Model", "💰 Bet Tracker"]
)

today = date.today()
races = load_races(gender)
races["start_date"] = pd.to_datetime(races["start_date"], errors="coerce")
races["end_date"] = pd.to_datetime(races["end_date"], errors="coerce")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — PREDICTIONS
# ══════════════════════════════════════════════════════════════════════════════
with tab_pred:
    st.subheader("Upcoming Race Predictions")

    # A race can be predicted while it still has a stage without a result
    # that has not long passed (under way, or yet to start).
    open_races = races[
        races["first_open_stage"].notna()
        & (races["end_date"] >= pd.Timestamp(today - timedelta(days=1)))
    ].sort_values("start_date")

    if open_races.empty:
        done = races[races["stages_with_results"] > 0]
        last = done["end_date"].max() if not done.empty else None
        st.info(
            "No race in the database has a stage still to be ridden"
            + (f" — the last stored result is from {last.date()}." if last is not None and pd.notna(last) else ".")
            + "  \nOnce the next calendar is on PCS, add it with "
              f"`python main.py scrape --years {today.year + (1 if today.month >= 11 else 0)} --skip-riders` "
              "and the races will show up here. Past races are under **Historical Backtest**."
        )
    else:
        col1, col2 = st.columns([3, 1])
        with col1:
            race_options = {
                f"{row['name']} ({str(row['start_date'])[:10]})": row["pcs_slug"]
                for _, row in open_races.iterrows()
            }
            selected_label = st.selectbox("Select race", list(race_options.keys()))
            selected_slug  = race_options[selected_label]

        with col2:
            race_row = open_races[open_races["pcs_slug"] == selected_slug].iloc[0]
            first_open = pd.to_datetime(race_row["first_open_stage"]).date()
            cutoff = st.date_input("Predict stages from", value=first_open,
                                   help="Stages on or after this date are predicted, each from the results "
                                        "stored before it.")

        run_btn = st.button("▶ Run predictions", type="primary")

        if run_btn:
            run_predict.clear()
            st.session_state["pred_df"] = run_predict(selected_slug, str(cutoff), gender)
            st.session_state["pred_slug"] = selected_slug

        if st.session_state.get("pred_slug") == selected_slug and "pred_df" in st.session_state:
            df = st.session_state["pred_df"]
            if df.empty:
                st.warning("No predictions generated. Check that the race has a start list on PCS.")
            else:
                for stage_date, group in df.groupby("stage_date"):
                    st.markdown(f"**{stage_label(group, stage_date)}**")
                    if "route_known" in group.columns and not group["route_known"].all():
                        st.caption(f"⚠️ {ROUTE_UNKNOWN_NOTE}")
                    group = group.copy()
                    # Fair decimal odds for a top-10 finish
                    group["fair_odds"] = (1.0 / group["top10_prob"].where(group["top10_prob"] > 0)).round(2)
                    display = probability_table(group, top_n, {"fair_odds": "Fair odds (top 10)"})
                    st.dataframe(
                        display, use_container_width=True, height=38 + 35 * len(display),
                        column_config={**probability_columns(display),
                                       "Fair odds (top 10)": st.column_config.NumberColumn(format="%.2f")},
                    )
                    st.download_button(
                        "📥 CSV", display.to_csv(index=False), f"preds_{str(stage_date)[:10]}.csv",
                        "text/csv", key=f"dl_{stage_date}",
                    )

                    with st.expander("💰 EV calculator"):
                        st.caption("Paste top-10 odds as 'Rider Name: odds' lines (e.g. 'Pogačar Tadej: 1.20'). One per line.")
                        odds_text = st.text_area("Bookmaker odds", key=f"ev_{stage_date}", height=120)
                        if odds_text.strip():
                            book_odds = {}
                            for line in odds_text.strip().splitlines():
                                parts = line.strip().rsplit(None, 1)
                                if len(parts) == 2:
                                    try:
                                        book_odds[parts[0].rstrip(":").strip().lower()] = float(parts[1])
                                    except ValueError:
                                        pass
                            ev_rows = []
                            for _, row in group.iterrows():
                                odds = book_odds.get(str(row["rider_name"]).lower())
                                if odds and row["top10_prob"] > 0:
                                    ev_rows.append({
                                        "Rider": row["rider_name"],
                                        "P(top 10)": row["top10_prob"],
                                        "Fair odds": row["fair_odds"],
                                        "Book odds": odds,
                                        "EV %": round((row["top10_prob"] * odds - 1) * 100, 1),
                                    })
                            if ev_rows:
                                ev_df = pd.DataFrame(ev_rows).sort_values("EV %", ascending=False)
                                st.dataframe(
                                    ev_df, use_container_width=True, hide_index=True,
                                    column_config={
                                        "P(top 10)": st.column_config.NumberColumn(format="%.3f"),
                                        "Fair odds": st.column_config.NumberColumn(format="%.2f"),
                                        "Book odds": st.column_config.NumberColumn(format="%.2f"),
                                        "EV %": st.column_config.NumberColumn(format="%+.1f%%"),
                                    },
                                )
                            else:
                                st.caption("No pasted name matches a rider on this stage.")

                    st.divider()

# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — HISTORICAL BACKTEST
# ══════════════════════════════════════════════════════════════════════════════
with tab_hist:
    st.subheader("Historical Backtest")
    st.caption("How the model would have ranked the riders of a past race, next to what happened.")

    past = races[(races["stages_with_results"] > 0) & races["start_date"].notna()].sort_values(
        "start_date", ascending=False)

    if past.empty:
        st.info("No race with results in the database yet.")
    else:
        past_options = {
            f"{row['name']} ({str(row['start_date'])[:10]})": row["pcs_slug"]
            for _, row in past.iterrows()
        }
        bt_label = st.selectbox("Select race", list(past_options.keys()), key="bt_race")
        bt_slug  = past_options[bt_label]
        bt_row   = past[past["pcs_slug"] == bt_slug].iloc[0]

        if st.button("▶ Run backtest", type="primary", key="bt_btn"):
            st.session_state["bt_df"], st.session_state["bt_source"] = run_backtest(bt_slug, gender)
            st.session_state["bt_slug"] = bt_slug

        if st.session_state.get("bt_slug") == bt_slug and "bt_df" in st.session_state:
            bt_df = st.session_state["bt_df"]
            source = st.session_state.get("bt_source")
            known = bt_df.dropna(subset=["top10"]).copy() if not bt_df.empty else bt_df
            known = known[known["profile_type"] != "ttt"] if not known.empty else known

            if known.empty:
                st.warning("No results found — this race may not have results in the database yet.")
            else:
                _info = model_info(gender)
                trained_to = _info.get("last_training_stage", _info.get("train_cutoff", "?"))
                if source == SOURCE_WALK_FORWARD:
                    st.success("**Out of sample.** These are the saved walk-forward predictions: the models "
                               "behind them were trained only on stages before this part of the season.")
                elif source == SOURCE_SEEN:
                    st.warning(f"**In sample.** The current model was trained on results up to {trained_to}, "
                               "this race included, and the race is not covered by the saved walk-forward "
                               "run — so the numbers below flatter the model. "
                               "`python main.py evaluate` gives honest ones.")
                else:
                    st.success(f"**Out of sample.** The race is after the last result the current model was "
                               f"trained on ({trained_to}).")

                known["stage_date_str"] = known["stage_date"].apply(lambda d: str(d)[:10])
                scores = stage_scores(known.rename(columns={"top10_prob": "score"}))
                if "win_prob" in known.columns:
                    win = stage_scores(known.rename(columns={"win_prob": "score"}))
                    scores["winner_rank"] = win["winner_rank"]
                auc = (roc_auc_score(known["top10"], known["top10_prob"])
                       if known["top10"].nunique() > 1 else float("nan"))

                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Top-10 hits per stage", f"{scores['hits10'].mean():.1f} / 10")
                m2.metric("Winner's rank in the forecast",
                          f"{scores['winner_rank'].median():.0f}" if scores["winner_rank"].notna().any() else "—",
                          help="Median over the stages; 1 = the model's first pick won.")
                m3.metric("AUC", f"{auc:.3f}")
                m4.metric("Stages evaluated", len(scores))

                st.divider()

                st.markdown("**Per-stage results**")
                stage_dates = sorted(known["stage_date_str"].unique())
                labels = {
                    d: stage_label(known[known["stage_date_str"] == d], d) for d in stage_dates
                }
                stage_sel = st.selectbox("Stage", stage_dates, format_func=lambda d: labels[d],
                                         key=f"stage_sel_{bt_slug}")
                stage_group = known[known["stage_date_str"] == stage_sel].copy()
                stage_group["actual"] = stage_group["position"].apply(
                    lambda x: str(int(x)) if pd.notna(x) else "DNF")
                stage_group["hit"] = stage_group["top10"].apply(lambda x: "✓" if x == 1 else "")
                disp = probability_table(stage_group, top_n, {"actual": "Finished", "hit": "Top 10?"})

                sid = stage_group["stage_id"].iloc[0]
                if sid in scores.index:
                    s = scores.loc[sid]
                    winner = (f" · winner was the forecast's #{int(s['winner_rank'])}"
                              if pd.notna(s["winner_rank"]) else "")
                    st.caption(f"{int(s['hits10'])} of the forecast's top 10 finished in the top 10{winner}")

                st.dataframe(disp, use_container_width=True, column_config=probability_columns(disp),
                             height=38 + 35 * len(disp))

                actual = (stage_group[stage_group["position"] <= 10].sort_values("position")
                          [["position", "rider_name", "top10_prob"]])
                ranks = stage_group["top10_prob"].rank(ascending=False, method="min")
                actual["Forecast rank"] = ranks.loc[actual.index].astype(int)
                actual = actual.rename(columns={"position": "Finished", "rider_name": "Rider",
                                                "top10_prob": "P(top 10)"})
                actual["Finished"] = actual["Finished"].astype(int)
                with st.expander("Actual top 10 and where the forecast had them"):
                    st.dataframe(actual, use_container_width=True, hide_index=True,
                                 column_config={"P(top 10)": st.column_config.NumberColumn(format="%.2f")})

                if bool(bt_row["is_stage_race"]) and known["stage_id"].nunique() > 1:
                    with st.expander("Most-picked riders across the race"):
                        picks = (
                            known.sort_values("top10_prob", ascending=False)
                            .groupby("stage_id").head(10)
                            .groupby("rider_name")
                            .agg(Picked=("top10_prob", "count"), Top10s=("top10", "sum"))
                            .sort_values(["Picked", "Top10s"], ascending=False)
                            .head(20).reset_index()
                            .rename(columns={"rider_name": "Rider", "Picked": "Stages in the forecast's top 10",
                                             "Top10s": "Of those, finished top 10"})
                        )
                        picks["Of those, finished top 10"] = picks["Of those, finished top 10"].astype(int)
                        st.dataframe(picks, use_container_width=True, hide_index=True)

# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — MODEL
# ══════════════════════════════════════════════════════════════════════════════
with tab_model:
    st.subheader("How accurate is it?")

    ev = load_evaluation(gender)
    if not ev:
        st.info("No walk-forward evaluation saved for this gender yet. Run "
                f"`python main.py evaluate{'' if gender == 'men' else ' --gender women'}` (several minutes).")
    else:
        o = ev["overall"]
        st.caption(
            f"Walk-forward test on **{ev['stages']} stages** ({ev['first_stage']} → {ev['last_stage']}): "
            "every stage was predicted by models trained only on results from before its part of the season "
            f"(windows starting {', '.join(ev['config']['folds'])}). Evaluated {ev['config']['generated']}."
        )
        best_simple = max(ev.get("baselines", []), key=lambda b: b["win_top1"], default=None)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Top-10 hits per stage", f"{o['p_at_10'] * 10:.2f} / 10",
                  help=f"Of the model's 10 picks, how many finished in the top 10. "
                       f"Picking at random gets {ev['random_p_at_10'] * 10:.2f}.")
        c2.metric("Podium hits per stage", f"{o['hits3']:.2f} / 3")
        c3.metric("Winner called", f"{o['win_top1']:.0%}",
                  help="Stages where the model's first pick won.")
        c4.metric("Winner in the top 3 picks", f"{o['win_top3']:.0%}")
        if best_simple:
            st.caption(
                f"Picking at random gets {ev['random_p_at_10'] * 10:.2f} top-10 hits. The best single-number "
                f"ranking, *{best_simple['group']}*, gets {best_simple['p_at_10'] * 10:.2f} "
                f"and calls {best_simple['win_top1']:.0%} of winners."
            )

        st.markdown("**By terrain**")
        terrain = [{**r, "group": PROFILE_LABELS.get(r["group"], "Profile unknown")} for r in ev["by_profile"]]
        st.dataframe(metric_table(terrain, "Terrain"), hide_index=True, use_container_width=True,
                     column_config=METRIC_FORMAT)
        st.markdown("**By race tier and type**")
        st.dataframe(metric_table(ev["by_tier"] + ev["by_race_type"], "Races"), hide_index=True,
                     use_container_width=True, column_config=METRIC_FORMAT)
        st.markdown("**By season, and against single-number rankings**")
        rows = [{**r, "group": str(r["group"])} for r in ev["by_year"]] + ev.get("baselines", [])
        st.dataframe(metric_table(rows, "Season / ranking"), hide_index=True, use_container_width=True,
                     column_config=METRIC_FORMAT)

        cal = ev.get("calibration")
        if cal:
            st.markdown("**Are the probabilities honest?**")
            rel = pd.DataFrame(cal["reliability"])
            col_c, col_d = st.columns([3, 2])
            with col_c:
                chart = rel.rename(columns={"predicted": "Predicted P(top 10)", "actual": "Finished top 10"})
                chart["Perfect"] = chart["Predicted P(top 10)"]
                st.line_chart(chart, x="Predicted P(top 10)", y=["Finished top 10", "Perfect"],
                              x_label="Predicted P(top 10)", y_label="Share that finished top 10", height=300)
            with col_d:
                st.dataframe(
                    rel.rename(columns={"bin": "P(top 10)", "n": "Riders", "predicted": "Predicted",
                                        "actual": "Finished top 10"}),
                    hide_index=True, use_container_width=True,
                    column_config={"Predicted": st.column_config.NumberColumn(format="%.3f"),
                                   "Finished top 10": st.column_config.NumberColumn(format="%.3f")},
                )
            st.caption(f"Brier score {cal['brier']:.4f} · log loss {cal['log_loss']:.4f} · "
                       f"average calibration gap {cal['ece']:.3f}")

    st.divider()
    st.subheader("Current model")

    _, features_path, _ = model_paths(gender)
    features_path = ROOT / features_path
    info = model_info(gender)
    if info:
        c1, c2, c3 = st.columns(3)
        c1.metric("Trained on results up to", info.get("last_training_stage", info.get("train_cutoff", "?")))
        c2.metric("Training rows", f"{info.get('train_rows', 0):,}")
        c3.metric("Features", info.get("n_features", "?"))
        if "val_auc" in info:
            st.caption(f"Held-out race {info.get('val_race')}: AUC {info['val_auc']:.3f}, "
                       f"average precision {info['val_ap']:.3f} ({info.get('val_rows', 0):,} rows)")

        fi = info.get("feature_importances")
        if fi:
            fi_df = (pd.DataFrame(list(fi.items()), columns=["Feature", "Importance"])
                     .sort_values("Importance", ascending=False).head(20))
            st.markdown("**What the model leans on** (drop in average precision when a feature is shuffled)")
            st.altair_chart(
                alt.Chart(fi_df).mark_bar().encode(
                    x=alt.X("Importance:Q", title="Importance"),
                    y=alt.Y("Feature:N", sort="-x", title=None, axis=alt.Axis(labelLimit=280)),
                    tooltip=["Feature", alt.Tooltip("Importance:Q", format=".4f")],
                ).properties(height=24 * len(fi_df) + 40),
                use_container_width=True,
            )
    else:
        st.info("No trained model found. Run `python main.py train`.")

    if features_path.exists():
        features = json.loads(features_path.read_text())
        with st.expander(f"All {len(features)} features"):
            st.write(", ".join(f"`{f}`" for f in features))

    st.divider()
    st.markdown("**Database**")
    with get_conn() as conn:
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ["races", "stages", "results", "riders"]}
        last = conn.execute(
            """SELECT MAX(s.date) FROM stages s
               WHERE EXISTS (SELECT 1 FROM results x WHERE x.stage_id = s.id)""").fetchone()[0]
    d1, d2, d3, d4, d5 = st.columns(5)
    d1.metric("Races", f"{counts['races']:,}")
    d2.metric("Stages", f"{counts['stages']:,}")
    d3.metric("Results", f"{counts['results']:,}")
    d4.metric("Riders", f"{counts['riders']:,}")
    d5.metric("Last result", last or "—")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — BET TRACKER
# ══════════════════════════════════════════════════════════════════════════════
with tab_clv:
    st.subheader("💰 Bet Tracker")
    st.caption("Track your bets and measure ROI over time. Run 'python main.py init' once to create the bets table.")

    # Load bets (handle missing table gracefully)
    try:
        with get_conn() as conn:
            bets_df = pd.read_sql("SELECT * FROM bets ORDER BY created_at DESC", conn)
    except Exception:
        bets_df = pd.DataFrame()

    if not bets_df.empty:
        settled = bets_df[bets_df["result"].notna()]
        pending = bets_df[bets_df["result"].isna()]

        total_pnl   = float(settled["pnl"].sum()) if not settled.empty else 0.0
        total_staked = float(settled["stake"].sum()) if not settled.empty else 0.0
        roi         = (total_pnl / total_staked * 100) if total_staked > 0 else 0.0
        avg_odds    = float(settled["odds_decimal"].mean()) if not settled.empty else 0.0

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("P&L", f"{total_pnl:+.2f}u")
        m2.metric("ROI", f"{roi:+.1f}%")
        m3.metric("Bets", f"{len(settled)} settled · {len(pending)} pending")
        m4.metric("Avg odds", f"{avg_odds:.2f}" if avg_odds else "—")

        if not pending.empty:
            if st.button("🔄 Settle pending bets (look up results)"):
                with get_conn() as conn:
                    for _, bet in pending.iterrows():
                        if not bet["stage_id"] or not bet["rider_id"]:
                            continue
                        row = conn.execute(
                            "SELECT position FROM results WHERE stage_id=? AND rider_id=?",
                            (int(bet["stage_id"]), int(bet["rider_id"]))
                        ).fetchone()
                        if row and row["position"] is not None:
                            won = 1 if row["position"] <= 10 else 0
                            pnl = float(bet["stake"]) * (float(bet["odds_decimal"]) - 1) if won else -float(bet["stake"])
                            conn.execute(
                                "UPDATE bets SET result=?, pnl=? WHERE id=?",
                                (won, pnl, int(bet["id"]))
                            )
                st.rerun()

        disp_cols = ["rider_name", "stage_date", "race_slug", "top10_prob",
                     "odds_decimal", "stake", "result", "pnl", "created_at"]
        disp_cols = [c for c in disp_cols if c in bets_df.columns]
        disp = bets_df[disp_cols].rename(columns={
            "rider_name": "Rider", "stage_date": "Stage", "race_slug": "Race",
            "top10_prob": "P(top10)", "odds_decimal": "Odds",
            "stake": "Stake", "result": "Result", "pnl": "P&L", "created_at": "Logged",
        })
        disp["Result"] = disp["Result"].apply(
            lambda x: "✓ Won" if x == 1 else ("✗ Lost" if x == 0 else "⏳")
        )
        st.dataframe(
            disp, use_container_width=True, height=400, hide_index=True,
            column_config={
                "P(top10)": st.column_config.NumberColumn(format="%.3f"),
                "Odds": st.column_config.NumberColumn(format="%.2f"),
                "Stake": st.column_config.NumberColumn(format="%.1f"),
                "P&L": st.column_config.NumberColumn(format="%+.2f"),
            },
        )

        # Calibration: compare predicted top10_prob vs actual win rate
        if not settled.empty and "top10_prob" in settled.columns:
            st.markdown("**Calibration check**")
            settled2 = settled[settled["top10_prob"].notna()].copy()
            if not settled2.empty:
                settled2["prob_bucket"] = pd.cut(settled2["top10_prob"], bins=5).astype(str)
                cal = settled2.groupby("prob_bucket").agg(
                    predicted=("top10_prob", "mean"),
                    actual=("result", "mean"),
                    count=("result", "count"),
                ).reset_index()
                st.dataframe(
                    cal, hide_index=True,
                    column_config={"predicted": st.column_config.NumberColumn(format="%.3f"),
                                   "actual": st.column_config.NumberColumn(format="%.3f")},
                )
    else:
        st.info("No bets logged yet.")

    st.divider()
    with st.expander("➕ Log a new bet"):
        with st.form("log_bet_form"):
            c1, c2 = st.columns(2)
            with c1:
                b_race       = st.text_input("Race slug", placeholder="race/tour-de-france/2026")
                b_rider      = st.text_input("Rider name", placeholder="Pogačar Tadej")
                b_stage_date = st.date_input("Stage date", value=date.today())
            with c2:
                b_odds  = st.number_input("Decimal odds", min_value=1.01, value=5.0, format="%.2f")
                b_stake = st.number_input("Stake (units)", min_value=0.1, value=1.0, format="%.1f")
                b_prob  = st.number_input("P(top10)", min_value=0.0, max_value=1.0, value=0.0, format="%.3f")
            submitted = st.form_submit_button("Log bet")
            if submitted and b_rider and b_race:
                try:
                    with get_conn() as conn:
                        stage_row = conn.execute(
                            """SELECT s.id FROM stages s JOIN races r ON s.race_id=r.id
                               WHERE r.pcs_slug=? AND s.date=?""",
                            (b_race, str(b_stage_date))
                        ).fetchone()
                        rider_row = conn.execute(
                            "SELECT id FROM riders WHERE LOWER(name)=LOWER(?)", (b_rider,)
                        ).fetchone()
                        stage_id = stage_row["id"] if stage_row else None
                        rider_id = rider_row["id"] if rider_row else None
                        conn.execute(
                            """INSERT INTO bets (stage_id, rider_id, rider_name, stage_date,
                               race_slug, top10_prob, odds_decimal, stake)
                               VALUES (?,?,?,?,?,?,?,?)""",
                            (stage_id, rider_id, b_rider, str(b_stage_date),
                             b_race, b_prob if b_prob > 0 else None, b_odds, b_stake)
                        )
                    st.success(f"Bet logged: {b_rider} @ {b_odds:.2f}")
                    st.rerun()
                except Exception as e:
                    st.error(f"Error logging bet: {e}")
