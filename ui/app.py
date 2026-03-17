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
tab_pred, tab_hist, tab_model = st.tabs(["🔮 Predictions", "📊 Historical Backtest", "🧠 Model"])

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

                    display = group.head(top_n)[["rider_name", "top10_prob"]].copy()
                    display.index = range(1, len(display) + 1)
                    display.columns = ["Rider", "P(top10)"]

                    # Colour by probability
                    st.dataframe(
                        display.style.background_gradient(
                            subset=["P(top10)"], cmap="YlGn", vmin=0, vmax=1
                        ).format({"P(top10)": "{:.3f}"}),
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

    metrics_path = ROOT / "model" / "metrics.json"
    features_path = ROOT / "model" / "feature_names.json"

    if metrics_path.exists():
        metrics = json.loads(metrics_path.read_text())
        c1, c2, c3 = st.columns(3)
        c1.metric("Validation AUC", f"{metrics.get('val_auc', 0):.4f}")
        c2.metric("Validation AP", f"{metrics.get('val_ap', 0):.4f}")
        c3.metric("Val rows", f"{metrics.get('val_rows', 0):,}")

    st.divider()

    # Accuracy by terrain (2025 test data)
    st.markdown("**Accuracy by terrain — 2025 test set**")
    terrain_data = pd.DataFrame({
        "Terrain":    ["Mountain", "Flat", "Hilly"],
        "AUC":        [0.928,      0.897,  0.833],
        "Avg p@10":   [0.565,      0.454,  0.339],
    })
    st.dataframe(terrain_data.set_index("Terrain"), use_container_width=False)

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
