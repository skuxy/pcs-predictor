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

import numpy as np
import pandas as pd
import streamlit as st
from sklearn.metrics import roc_auc_score, average_precision_score

# ── project root on path ──────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from db.database import get_conn
from model.predict import predict_race
from model.train import model_paths

logging.basicConfig(level=logging.WARNING)

st.set_page_config(
    page_title="Cycling Predictor",
    page_icon="🚴",
    layout="wide",
)
st.title("🚴 Cycling Race Predictor")

# ── helpers ───────────────────────────────────────────────────────────────────

@st.cache_data(ttl=300)
def load_races(gender: str = "men") -> pd.DataFrame:
    with get_conn() as conn:
        return pd.read_sql(
            """SELECT pcs_slug, name, year, start_date, end_date, is_stage_race
               FROM races WHERE gender = ? ORDER BY start_date DESC""",
            conn, params=(gender,),
        )


@st.cache_data(show_spinner="Running predictions…")
def run_predict(race_slug: str, cutoff: str, gender: str) -> pd.DataFrame:
    return predict_race(race_slug, cutoff, gender=gender)


def precision_at_n(group: pd.DataFrame, n: int = 10) -> float:
    top = group.nlargest(n, "top10_prob")
    return float(top["top10"].sum() / n) if "top10" in top.columns else float("nan")


def prob_bar(p: float) -> str:
    filled = int(p * 20)
    return "█" * filled + "░" * (20 - filled)


# ── sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Settings")
    gender = st.selectbox("Gender", ["men", "women"], index=0)
    top_n  = st.slider("Top N riders shown", 5, 30, 15)

# ── tabs ──────────────────────────────────────────────────────────────────────
tab_pred, tab_hist, tab_model, tab_clv = st.tabs(
    ["🔮 Predictions", "📊 Historical Backtest", "🧠 Model", "💰 Bet Tracker"]
)

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — PREDICTIONS
# ══════════════════════════════════════════════════════════════════════════════
with tab_pred:
    st.subheader("Upcoming Race Predictions")

    races = load_races(gender)
    today = date.today()

    # Split into upcoming vs past
    races["start_date"] = pd.to_datetime(races["start_date"], errors="coerce")
    upcoming = races[races["start_date"] >= pd.Timestamp(today - timedelta(days=3))].copy()
    upcoming = upcoming.sort_values("start_date")

    if upcoming.empty:
        st.info("No upcoming races found in the database. Run a scrape to add more.")
    else:
        col1, col2 = st.columns([3, 1])
        with col1:
            race_options = {
                f"{row['name']} ({str(row['start_date'])[:10]})": row["pcs_slug"]
                for _, row in upcoming.iterrows()
            }
            selected_label = st.selectbox("Select race", list(race_options.keys()))
            selected_slug  = race_options[selected_label]

        with col2:
            race_row   = upcoming[upcoming["pcs_slug"] == selected_slug].iloc[0]
            cutoff_default = race_row["start_date"].date() if pd.notna(race_row["start_date"]) else today
            cutoff = st.date_input("Feature cutoff", value=cutoff_default)

        run_btn = st.button("▶ Run predictions", type="primary")

        if run_btn or "pred_df" in st.session_state and st.session_state.get("pred_slug") == selected_slug:
            if run_btn:
                st.cache_data.clear()
            with st.spinner("Building features and predicting…"):
                df = run_predict(selected_slug, str(cutoff), gender)
                st.session_state["pred_df"]   = df
                st.session_state["pred_slug"] = selected_slug

        if "pred_df" in st.session_state and st.session_state.get("pred_slug") == selected_slug:
            df = st.session_state["pred_df"]
            if df.empty:
                st.warning("No predictions generated. Check that the race has a startlist on PCS.")
            else:
                for stage_date, group in df.groupby("stage_date"):
                    group = group.sort_values("top10_prob", ascending=False).reset_index(drop=True)
                    profile = group["profile_type"].iloc[0] if "profile_type" in group.columns else "?"
                    surface = ""
                    if group.get("is_cobbled", pd.Series([0])).iloc[0]:
                        surface = " · cobbled"
                    elif group.get("is_gravel", pd.Series([0])).iloc[0]:
                        surface = " · gravel"

                    st.markdown(f"**{str(stage_date)[:10]}** — {profile}{surface}")

                    # Compute fair odds via normalised probabilities
                    stage_p_sum = group["top10_prob"].sum()
                    n_starters = max(len(group), 1)
                    group = group.copy()
                    group["p_norm"] = (group["top10_prob"] * 10 / stage_p_sum).clip(upper=1.0)
                    group["fair_odds"] = (1.0 / group["p_norm"].replace(0, np.nan)).round(2)

                    # Build display columns
                    display_cols = ["rider_name", "top10_prob"]
                    if "p_win" in group.columns:
                        display_cols.append("p_win")
                    if "p_top3" in group.columns:
                        display_cols.append("p_top3")
                    display_cols += ["fair_odds"]

                    display = group.head(top_n)[display_cols].copy()
                    display.index = range(1, len(display) + 1)

                    rename_map = {
                        "rider_name": "Rider",
                        "top10_prob": "P(top10)",
                        "p_win": "P(win)",
                        "p_top3": "P(top3)",
                        "fair_odds": "Fair odds",
                    }
                    display = display.rename(columns={k: v for k, v in rename_map.items() if k in display.columns})

                    fmt = {"P(top10)": "{:.3f}", "Fair odds": "{:.2f}"}
                    if "P(win)" in display.columns:
                        fmt["P(win)"] = "{:.3f}"
                    if "P(top3)" in display.columns:
                        fmt["P(top3)"] = "{:.3f}"

                    # Colour by probability
                    st.dataframe(
                        display.style.background_gradient(
                            subset=["P(top10)"], cmap="YlGn", vmin=0, vmax=1
                        ).format(fmt),
                        use_container_width=True,
                    )

                    # CSV export button
                    csv_data = display.to_csv(index=False)
                    st.download_button(
                        "📥 CSV",
                        csv_data,
                        f"preds_{str(stage_date)[:10]}.csv",
                        "text/csv",
                        key=f"dl_{stage_date}",
                    )

                    # EV calculator expander
                    with st.expander("💰 EV calculator"):
                        st.caption("Paste odds as 'Rider Name: odds' lines (e.g. 'Pogacar: 4.50'). One per line.")
                        odds_text = st.text_area("Bookmaker odds", key=f"ev_{stage_date}", height=120)
                        if odds_text.strip():
                            book_odds = {}
                            for line in odds_text.strip().splitlines():
                                line = line.strip()
                                if not line:
                                    continue
                                parts = line.rsplit(None, 1)
                                if len(parts) == 2:
                                    try:
                                        name = parts[0].rstrip(":").strip()
                                        book_odds[name] = float(parts[1])
                                    except ValueError:
                                        pass
                            ev_rows = []
                            for _, row in group.iterrows():
                                odds = book_odds.get(row["rider_name"])
                                if odds and row["p_norm"] > 0:
                                    ev = row["p_norm"] * odds - 1
                                    ev_rows.append({
                                        "Rider": row["rider_name"],
                                        "P(top10)": row["top10_prob"],
                                        "Fair odds": row["fair_odds"],
                                        "Book odds": odds,
                                        "EV %": round(ev * 100, 1),
                                    })
                            if ev_rows:
                                ev_df = pd.DataFrame(ev_rows).sort_values("EV %", ascending=False)

                                def _color_ev(v):
                                    if isinstance(v, (int, float)):
                                        return "color: #1a8c4e; font-weight: bold" if v > 0 else "color: #c0392b"
                                    return ""

                                st.dataframe(
                                    ev_df.style.applymap(_color_ev, subset=["EV %"])
                                    .format({
                                        "P(top10)": "{:.3f}",
                                        "Fair odds": "{:.2f}",
                                        "Book odds": "{:.2f}",
                                        "EV %": "{:+.1f}%",
                                    }),
                                    use_container_width=True,
                                )

                    st.divider()

# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — HISTORICAL BACKTEST
# ══════════════════════════════════════════════════════════════════════════════
with tab_hist:
    st.subheader("Historical Backtest")
    st.caption("Compare model predictions against known results for a past race.")

    races_all = load_races(gender)
    races_all["start_date"] = pd.to_datetime(races_all["start_date"], errors="coerce")
    past = races_all[races_all["start_date"] < pd.Timestamp(today)].sort_values("start_date", ascending=False)

    col1, col2 = st.columns([3, 1])
    with col1:
        past_options = {
            f"{row['name']} ({str(row['start_date'])[:10]})": row["pcs_slug"]
            for _, row in past.iterrows()
            if pd.notna(row["start_date"])
        }
        bt_label = st.selectbox("Select race", list(past_options.keys()), key="bt_race")
        bt_slug  = past_options[bt_label]

    with col2:
        bt_row    = past[past["pcs_slug"] == bt_slug].iloc[0]
        bt_cutoff = bt_row["start_date"].date() if pd.notna(bt_row["start_date"]) else today
        bt_cutoff = st.date_input("Training cutoff", value=bt_cutoff, key="bt_cutoff")

    bt_btn = st.button("▶ Run backtest", type="primary", key="bt_btn")

    if bt_btn:
        with st.spinner("Running backtest…"):
            bt_df = run_predict(bt_slug, str(bt_cutoff), gender)
            st.session_state["bt_df"]   = bt_df
            st.session_state["bt_slug"] = bt_slug

    if "bt_df" in st.session_state and st.session_state.get("bt_slug") == bt_slug:
        bt_df = st.session_state["bt_df"]
        known = bt_df.dropna(subset=["top10", "position"])

        if known.empty:
            st.warning("No results found — this race may not have results in the database yet.")
        else:
            # Summary metrics
            if known["top10"].nunique() > 1:
                auc = roc_auc_score(known["top10"], known["top10_prob"])
                ap  = average_precision_score(known["top10"], known["top10_prob"])
            else:
                auc = ap = float("nan")

            stage_p10s = [
                precision_at_n(g, 10)
                for _, g in known.groupby("stage_date")
            ]
            avg_p10 = float(np.mean(stage_p10s))

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("AUC", f"{auc:.3f}")
            m2.metric("Avg precision@10", f"{avg_p10:.3f}")
            m3.metric("Stages evaluated", len(stage_p10s))
            m4.metric("Avg AP", f"{ap:.3f}")

            st.divider()

            # GC leaderboard
            gc = (
                bt_df.sort_values("top10_prob", ascending=False)
                .groupby("stage_date").head(10)
                .groupby("rider_name")
                .agg(
                    Appearances=("top10_prob", "count"),
                    Avg_prob=("top10_prob", "mean"),
                    Actual_top10s=("top10", "sum"),
                )
                .sort_values("Appearances", ascending=False)
                .head(20)
                .reset_index()
            )
            gc.columns = ["Rider", "Predicted top-10 appearances", "Avg P(top10)", "Actual top10s"]

            col_gc, col_stages = st.columns([1, 2])
            with col_gc:
                st.markdown("**GC Leaderboard**")
                st.dataframe(
                    gc.style.format({"Avg P(top10)": "{:.3f}", "Actual top10s": "{:.0f}"}),
                    use_container_width=True, height=400,
                )

            with col_stages:
                st.markdown("**Per-stage results**")
                stage_sel = st.selectbox(
                    "Stage",
                    [str(d)[:10] for d in sorted(known["stage_date"].unique())],
                    key="stage_sel",
                )
                stage_group = known[known["stage_date"].astype(str).str[:10] == stage_sel]
                stage_group = stage_group.sort_values("top10_prob", ascending=False).head(top_n).reset_index(drop=True)
                stage_group.index = range(1, len(stage_group) + 1)

                disp = stage_group[["rider_name", "top10_prob", "position", "top10"]].copy()
                disp.columns = ["Rider", "P(top10)", "Actual pos", "Top10?"]
                disp["Top10?"] = disp["Top10?"].apply(lambda x: "✓" if x == 1 else "")
                disp["Actual pos"] = disp["Actual pos"].apply(lambda x: int(x) if pd.notna(x) else "DNF")

                p10 = stage_p10s[
                    [str(d)[:10] for d in sorted(known["stage_date"].unique())].index(stage_sel)
                ]
                st.caption(f"Precision@10: {p10:.2f}  ({int(p10*10)}/10 correct)")

                st.dataframe(
                    disp.style.apply(
                        lambda row: ["background-color: #d4edda" if row["Top10?"] == "✓" else "" for _ in row],
                        axis=1,
                    ).format({"P(top10)": "{:.3f}"}),
                    use_container_width=True,
                )

# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — MODEL
# ══════════════════════════════════════════════════════════════════════════════
with tab_model:
    st.subheader("Model Performance")

    _, features_path, metrics_path = model_paths(gender)
    # model_paths returns relative paths; resolve from project root
    metrics_path  = ROOT / metrics_path
    features_path = ROOT / features_path

    if metrics_path.exists():
        metrics = json.loads(metrics_path.read_text())
        c1, c2, c3 = st.columns(3)
        c1.metric("Validation AUC", f"{metrics.get('val_auc', 0):.4f}")
        c2.metric("Validation AP", f"{metrics.get('val_ap', 0):.4f}")
        c3.metric("Val rows", f"{metrics.get('val_rows', 0):,}")
    else:
        st.info(f"No metrics file found at `{metrics_path}`. Train the model first.")

    st.divider()

    # Live feature importance (replaces static terrain table)
    if metrics_path.exists():
        if "feature_importances" in metrics:
            fi = metrics["feature_importances"]
            fi_df = pd.DataFrame(list(fi.items()), columns=["Feature", "Importance"])
            fi_df = fi_df.sort_values("Importance", ascending=False).head(20)
            st.markdown("**Top 20 features by importance**")
            st.bar_chart(fi_df.set_index("Feature"))
        else:
            st.info("Retrain the model to generate feature importances.")

    st.divider()

    if features_path.exists():
        features = json.loads(features_path.read_text())
        st.markdown(f"**Features ({len(features)} total)**")
        # Group features visually
        groups = {
            "Rolling form":       [f for f in features if any(x in f for x in ["avg_pos", "top10_rate", "win_rate", "dnf_rate", "races_last", "days_since"])],
            "Profile affinity":   [f for f in features if "avg_pos" in f and any(x in f for x in ["mountain", "flat", "hilly", "tt"])],
            "Stage context":      [f for f in features if any(x in f for x in ["distance", "elevation", "stage_num", "is_stage", "prev_stage"])],
            "Stage type":         [f for f in features if f.startswith("is_")],
            "Rider attributes":   [f for f in features if f.startswith("spec_") or f == "pcs_rank"],
        }
        cols = st.columns(len(groups))
        for col, (group_name, group_feats) in zip(cols, groups.items()):
            with col:
                st.markdown(f"*{group_name}*")
                for f in group_feats:
                    st.markdown(f"- `{f}`")

    st.divider()
    st.markdown("**Database summary**")
    with get_conn() as conn:
        for table in ["races", "stages", "results", "riders"]:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            st.markdown(f"- **{table}**: {count:,} rows")

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
            disp.style.format({
                "P(top10)": lambda x: f"{x:.3f}" if isinstance(x, float) else "—",
                "Odds": "{:.2f}",
                "Stake": "{:.1f}",
                "P&L": lambda x: f"{x:+.2f}" if isinstance(x, float) else "—",
            }),
            use_container_width=True,
            height=400,
        )

        # Calibration: compare predicted top10_prob vs actual win rate
        if not settled.empty and "top10_prob" in settled.columns:
            st.markdown("**Calibration check**")
            settled2 = settled[settled["top10_prob"].notna()].copy()
            if not settled2.empty:
                settled2["prob_bucket"] = pd.cut(settled2["top10_prob"], bins=5)
                cal = settled2.groupby("prob_bucket").agg(
                    predicted=("top10_prob", "mean"),
                    actual=("result", "mean"),
                    count=("result", "count"),
                ).reset_index()
                st.dataframe(
                    cal.style.format({"predicted": "{:.3f}", "actual": "{:.3f}"}),
                    use_container_width=False,
                )
    else:
        st.info("No bets logged yet.")

    st.divider()
    with st.expander("➕ Log a new bet"):
        with st.form("log_bet_form"):
            c1, c2 = st.columns(2)
            with c1:
                b_race       = st.text_input("Race slug", placeholder="race/tour-de-france/2026")
                b_rider      = st.text_input("Rider name", placeholder="Tadej Pogacar")
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
