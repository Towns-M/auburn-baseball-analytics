import os
import io
from datetime import date, timedelta

import streamlit as st
import pandas as pd
from azure.storage.blob import BlobServiceClient

# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Auburn Baseball Analytics",
    page_icon="⚾",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Auburn brand colours ──────────────────────────────────────────────────────
AUBURN_ORANGE = "#E87722"
AUBURN_NAVY   = "#03244D"
AUBURN_WHITE  = "#FFFFFF"

st.markdown(f"""
<style>
  .stApp {{ background-color: #f5f5f5; }}
  [data-testid="stSidebar"] {{
      background-color: {AUBURN_NAVY};
  }}
  [data-testid="stSidebar"] * {{
      color: {AUBURN_WHITE} !important;
  }}
  [data-testid="stSidebar"] .stSelectbox label,
  [data-testid="stSidebar"] .stTextInput label,
  [data-testid="stSidebar"] .stDateInput label,
  [data-testid="stSidebar"] .stCheckbox label {{
      color: {AUBURN_WHITE} !important;
  }}
  .auburn-header {{
      background: linear-gradient(90deg, {AUBURN_NAVY} 0%, #05336B 100%);
      color: {AUBURN_WHITE};
      padding: 18px 24px;
      border-radius: 8px;
      margin-bottom: 20px;
      display: flex;
      align-items: center;
      gap: 16px;
  }}
  .auburn-header h1 {{
      margin: 0;
      font-size: 1.8rem;
      color: {AUBURN_ORANGE};
      font-weight: 700;
  }}
  .auburn-header p {{
      margin: 4px 0 0;
      font-size: 0.9rem;
      color: #ccc;
  }}
  .metric-card {{
      background: {AUBURN_WHITE};
      border-left: 4px solid {AUBURN_ORANGE};
      border-radius: 6px;
      padding: 14px 18px;
      box-shadow: 0 1px 4px rgba(0,0,0,0.08);
      margin-bottom: 10px;
  }}
  .metric-label {{ font-size: 0.78rem; color: #666; text-transform: uppercase; letter-spacing: 0.05em; }}
  .metric-value {{ font-size: 1.6rem; font-weight: 700; color: {AUBURN_NAVY}; margin-top: 2px; }}
  .section-header {{
      color: {AUBURN_NAVY};
      font-weight: 700;
      font-size: 1.1rem;
      border-bottom: 2px solid {AUBURN_ORANGE};
      padding-bottom: 6px;
      margin: 20px 0 12px;
  }}
  .stTabs [data-baseweb="tab-list"] {{
      background: {AUBURN_NAVY};
      border-radius: 8px 8px 0 0;
      padding: 4px 8px 0;
  }}
  .stTabs [data-baseweb="tab"] {{
      color: #ccc !important;
      border-radius: 6px 6px 0 0 !important;
  }}
  .stTabs [aria-selected="true"] {{
      background: {AUBURN_ORANGE} !important;
      color: {AUBURN_WHITE} !important;
  }}
  .stDataFrame {{ border-radius: 6px; overflow: hidden; }}
  .stButton > button {{
      background: {AUBURN_ORANGE};
      color: {AUBURN_WHITE};
      border: none;
      border-radius: 6px;
      font-weight: 600;
  }}
  .stButton > button:hover {{
      background: #c5631a;
  }}
</style>
""", unsafe_allow_html=True)

# ── Data loading ──────────────────────────────────────────────────────────────
CONN_STR   = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "")
CONTAINER  = "processed-stats"

@st.cache_data(ttl=3600, show_spinner="Loading stats from Azure…")
def load_data():
    if not CONN_STR:
        raise ValueError("AZURE_STORAGE_CONNECTION_STRING is not set.")
    client = BlobServiceClient.from_connection_string(CONN_STR)
    cont   = client.get_container_client(CONTAINER)

    def _read(name):
        raw = cont.get_blob_client(name).download_blob().readall()
        return pd.read_csv(io.BytesIO(raw))

    pitcher_stats    = _read("pitcher_stats.csv")
    batter_stats     = _read("batter_stats.csv")
    pitcher_game_log = _read("pitcher_game_log.csv")
    batter_game_log  = _read("batter_game_log.csv")

    # Normalise game date and extract season year
    for df in [pitcher_game_log, batter_game_log]:
        df["GameDate"] = df["GameDate"].astype(str).str.zfill(8)
        df["Season"]   = df["GameDate"].str[:4]
        # Human-readable date
        df["GameDateDisplay"] = pd.to_datetime(
            df["GameDate"], format="%Y%m%d", errors="coerce"
        ).dt.strftime("%b %d, %Y")

    return pitcher_stats, batter_stats, pitcher_game_log, batter_game_log


# ── Aggregate helpers ─────────────────────────────────────────────────────────

def build_pitcher_stats_from_log(game_log: pd.DataFrame) -> pd.DataFrame:
    """Re-aggregate pitcher stats from game log rows."""
    sum_cols = [c for c in
        ["Pitches", "Strikeouts", "Walks", "HitsAllowed", "HRsAllowed", "OutsRecorded"]
        if c in game_log.columns]

    agg_dict = {(c if c != "Pitches" else "TotalPitches"): (c, "sum") for c in sum_cols}
    if "AvgVelo" in game_log.columns:
        agg_dict["AvgVelocity"] = ("AvgVelo", "mean")

    agg = game_log.groupby(["Pitcher", "PitcherTeam"]).agg(**agg_dict).reset_index()

    if "AvgVelocity" in agg.columns:
        agg["AvgVelocity"] = agg["AvgVelocity"].round(1)
    if "OutsRecorded" in agg.columns:
        agg["IP"] = (agg["OutsRecorded"] / 3).round(1)
    tp = agg.get("TotalPitches", None)
    if tp is not None:
        if "Strikeouts" in agg.columns:
            agg["K_pct"]  = (agg["Strikeouts"] / tp.replace(0, float("nan")) * 100).round(1)
        if "Walks" in agg.columns:
            agg["BB_pct"] = (agg["Walks"] / tp.replace(0, float("nan")) * 100).round(1)
    return agg


def build_batter_stats_from_log(game_log: pd.DataFrame) -> pd.DataFrame:
    """Re-aggregate batter stats from game log rows."""
    sum_cols = [c for c in
        ["Pitches", "Hits", "Singles", "Doubles", "Triples", "HomeRuns",
         "Strikeouts", "Walks", "HBP", "AtBats", "TotalBases"]
        if c in game_log.columns]

    rename = {"Pitches": "TotalPitches"}
    agg_dict = {rename.get(c, c): (c, "sum") for c in sum_cols}
    if "AvgExitVelo" in game_log.columns:
        agg_dict["AvgExitVelo"] = ("AvgExitVelo", "mean")

    agg = game_log.groupby(["Batter", "BatterTeam"]).agg(**agg_dict).reset_index()

    if "AvgExitVelo" in agg.columns:
        agg["AvgExitVelo"] = agg["AvgExitVelo"].round(1)

    # Derived rate stats
    ab = agg.get("AtBats")
    if ab is not None:
        safe_ab = ab.replace(0, float("nan"))
        if "Hits" in agg.columns:
            agg["BA"] = (agg["Hits"] / safe_ab).round(3)
        if "TotalBases" in agg.columns:
            agg["SLG"] = (agg["TotalBases"] / safe_ab).round(3)
        walks = agg.get("Walks", 0)
        hbp   = agg.get("HBP", 0)
        if "Hits" in agg.columns:
            pa_denom = (ab + walks + hbp).replace(0, float("nan"))
            agg["OBP"] = ((agg["Hits"] + walks + hbp) / pa_denom).round(3)
        if "OBP" in agg.columns and "SLG" in agg.columns:
            agg["OPS"] = (agg["OBP"] + agg["SLG"]).round(3)

    tp = agg.get("TotalPitches", None)
    if tp is not None:
        if "Strikeouts" in agg.columns:
            agg["K_pct"]  = (agg["Strikeouts"] / tp.replace(0, float("nan")) * 100).round(1)
        if "Walks" in agg.columns:
            agg["BB_pct"] = (agg["Walks"] / tp.replace(0, float("nan")) * 100).round(1)
    return agg


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## ⚾ Auburn Baseball")
    st.markdown("---")
    view = st.radio("View", ["📊 Season Leaderboards", "🔎 Player Profile"])
    st.markdown("---")

    # Season filter
    season = st.selectbox("Season", ["All", "2026", "2025"], index=0)

    # Date range filter
    use_date = st.checkbox("Filter by date range", value=False)
    date_range_val = None
    if use_date:
        today = date.today()
        date_range_val = st.date_input(
            "Date range",
            value=(date(today.year, 1, 1), today),
            min_value=date(2024, 1, 1),
            max_value=today,
            format="MM/DD/YYYY",
        )

    # Team scope
    st.markdown("---")
    team_scope = st.radio(
        "Show players from",
        ["All teams", "Auburn only", "Opponents only"],
        index=0,
    )

    st.markdown("---")
    role = st.radio("Filter by role", ["All", "Pitchers", "Batters"])
    st.markdown("---")

    if st.button("🔄 Refresh Data"):
        st.cache_data.clear()
        st.rerun()

    st.markdown(
        "<div style='position:absolute;bottom:20px;font-size:0.75rem;color:#999'>"
        "Data: Trackman · Updated via Azure ADF</div>",
        unsafe_allow_html=True,
    )

# ── Header ────────────────────────────────────────────────────────────────────
if use_date and date_range_val and len(date_range_val) == 2:
    season_label = f"{date_range_val[0].strftime('%b %d')} – {date_range_val[1].strftime('%b %d, %Y')}"
elif season != "All":
    season_label = season
else:
    season_label = "2025 + 2026"

st.markdown(f"""
<div class="auburn-header">
  <div>
    <h1>⚾ Auburn Baseball Analytics</h1>
    <p>Trackman pitch-by-pitch data · {season_label}</p>
  </div>
</div>
""", unsafe_allow_html=True)

# ── Load data ─────────────────────────────────────────────────────────────────
try:
    pitcher_stats, batter_stats, pitcher_game_log, batter_game_log = load_data()
except Exception as e:
    st.error(f"Could not load data: {e}")
    st.info("Make sure AZURE_STORAGE_CONNECTION_STRING is set and the transform has been run.")
    st.stop()

# ── Apply season + date filters to game logs ──────────────────────────────────
p_log_filtered = pitcher_game_log.copy()
b_log_filtered = batter_game_log.copy()

has_season = season != "All"
has_date   = use_date and date_range_val is not None and len(date_range_val) == 2

if has_season:
    p_log_filtered = p_log_filtered[p_log_filtered["Season"] == season]
    b_log_filtered = b_log_filtered[b_log_filtered["Season"] == season]

if has_date:
    start_s = date_range_val[0].strftime("%Y%m%d")
    end_s   = date_range_val[1].strftime("%Y%m%d")
    p_log_filtered = p_log_filtered[
        (p_log_filtered["GameDate"] >= start_s) & (p_log_filtered["GameDate"] <= end_s)
    ]
    b_log_filtered = b_log_filtered[
        (b_log_filtered["GameDate"] >= start_s) & (b_log_filtered["GameDate"] <= end_s)
    ]

# Re-aggregate from logs when a filter is active; otherwise use pre-computed CSVs
if has_season or has_date:
    pitcher_stats_view = build_pitcher_stats_from_log(p_log_filtered)
    batter_stats_view  = build_batter_stats_from_log(b_log_filtered)
else:
    pitcher_stats_view = pitcher_stats.copy()
    batter_stats_view  = batter_stats.copy()

# ── Apply team scope filter ───────────────────────────────────────────────────
AUBURN = "AUB_TIG"
if team_scope == "Auburn only":
    pitcher_stats_view = pitcher_stats_view[
        pitcher_stats_view["PitcherTeam"].str.upper() == AUBURN
    ]
    batter_stats_view = batter_stats_view[
        batter_stats_view["BatterTeam"].str.upper() == AUBURN
    ]
    p_log_filtered = p_log_filtered[p_log_filtered["PitcherTeam"].str.upper() == AUBURN]
    b_log_filtered = b_log_filtered[b_log_filtered["BatterTeam"].str.upper() == AUBURN]
elif team_scope == "Opponents only":
    pitcher_stats_view = pitcher_stats_view[
        pitcher_stats_view["PitcherTeam"].str.upper() != AUBURN
    ]
    batter_stats_view = batter_stats_view[
        batter_stats_view["BatterTeam"].str.upper() != AUBURN
    ]
    p_log_filtered = p_log_filtered[p_log_filtered["PitcherTeam"].str.upper() != AUBURN]
    b_log_filtered = b_log_filtered[b_log_filtered["BatterTeam"].str.upper() != AUBURN]


# ── Helper: metric card ───────────────────────────────────────────────────────
def metric_card(label, value):
    disp = "—" if pd.isna(value) or value is None else str(value)
    st.markdown(f"""
    <div class="metric-card">
      <div class="metric-label">{label}</div>
      <div class="metric-value">{disp}</div>
    </div>""", unsafe_allow_html=True)


def _val(df, col):
    """Safely get a column mean/sum for metric cards."""
    if col not in df.columns or len(df) == 0:
        return "—"
    return df[col].dropna()


# ══════════════════════════════════════════════════════════════════════════════
# VIEW 1: SEASON LEADERBOARDS
# ══════════════════════════════════════════════════════════════════════════════
if view == "📊 Season Leaderboards":

    tab_pitch, tab_bat = st.tabs(["⚾  Pitcher Leaderboard", "🏏  Batter Leaderboard"])

    # ── Pitcher leaderboard ──────────────────────────────────────────────────
    with tab_pitch:
        st.markdown('<div class="section-header">Season Pitcher Stats</div>', unsafe_allow_html=True)

        pv = pitcher_stats_view
        if len(pv) > 0:
            col1, col2, col3, col4, col5 = st.columns(5)
            with col1:
                metric_card("Total Pitchers", len(pv))
            with col2:
                avg_v = round(pv["AvgVelocity"].mean(), 1) if "AvgVelocity" in pv else "—"
                metric_card("Avg Velocity (mph)", avg_v)
            with col3:
                avg_s = round(pv["AvgSpinRate"].mean(), 0) if "AvgSpinRate" in pv else "—"
                metric_card("Avg Spin Rate (rpm)", avg_s)
            with col4:
                tot_k = int(pv["Strikeouts"].sum()) if "Strikeouts" in pv else "—"
                metric_card("Total Strikeouts", tot_k)
            with col5:
                avg_ip = round(pv["IP"].mean(), 1) if "IP" in pv.columns else "—"
                metric_card("Avg IP", avg_ip)

        # Search & team filter
        col_s, col_t = st.columns([2, 2])
        with col_s:
            search = st.text_input("Search pitcher name", placeholder="e.g. Smith", key="p_search")
        with col_t:
            p_teams  = ["All"] + sorted(pv["PitcherTeam"].dropna().unique().tolist())
            p_team   = st.selectbox("Team", p_teams, key="p_team")

        df_p = pv.copy()
        if search:
            df_p = df_p[df_p["Pitcher"].str.contains(search, case=False, na=False)]
        if p_team != "All":
            df_p = df_p[df_p["PitcherTeam"] == p_team]

        display_cols_p = [c for c in [
            "Pitcher", "PitcherTeam", "PitcherThrows", "TotalPitches", "IP",
            "AvgVelocity", "MaxVelocity", "AvgSpinRate",
            "AvgVertBreak", "AvgHorzBreak",
            "Strikeouts", "Walks", "K_pct", "BB_pct",
            "HitsAllowed", "HRsAllowed",
            "FB_pct", "SL_pct", "CB_pct", "CH_pct", "CT_pct", "SI_pct",
        ] if c in df_p.columns]

        sort_col = "TotalPitches" if "TotalPitches" in df_p.columns else display_cols_p[0]
        st.dataframe(
            df_p[display_cols_p].sort_values(sort_col, ascending=False).reset_index(drop=True),
            use_container_width=True, height=440,
        )

    # ── Batter leaderboard ───────────────────────────────────────────────────
    with tab_bat:
        st.markdown('<div class="section-header">Season Batter Stats</div>', unsafe_allow_html=True)

        bv = batter_stats_view
        if len(bv) > 0:
            col1, col2, col3, col4, col5 = st.columns(5)
            with col1:
                metric_card("Total Batters", len(bv))
            with col2:
                avg_ba = round(bv["BA"].mean(), 3) if "BA" in bv.columns else "—"
                metric_card("Avg BA", avg_ba)
            with col3:
                avg_obp = round(bv["OBP"].mean(), 3) if "OBP" in bv.columns else "—"
  2             metric_card("Avg OBP", avg_obp)
            with col4:
                avg_slg = round(bv["SLG"].mean(), 3) if "SLG" in bv.columns else "—"
                metric_card("Avg SLG", avg_slg)
            with col5:
                tot_hr = int(bv["HomeRuns"].sum()) if "HomeRuns" in bv.columns else "—"
                metric_card("Total Home Runs", tot_hr)

        col_sb, col_tb = st.columns([2, 2])
        with col_sb:
            search_b = st.text_input("Search batter name", placeholder="e.g. Jones", key="b_search")
        with col_tb:
            teams_b  = ["All"] + sorted(bv["BatterTeam"].dropna().unique().tolist())
            team_b   = st.selectbox("Team", teams_b, key="b_team")

        df_b = bv.copy()
        if search_b:
            df_b = df_b[df_b["Batter"].str.contains(search_b, case=False, na=False)]
        if team_b != "All":
            df_b = df_b[df_b["BatterTeam"] == team_b]

        display_cols_b = [c for c in [
            "Batter", "BatterTeam", "BatterSide", "TotalPitches",
            "AtBats", "Hits", "BA", "OBP", "SLG", "OPS",
            "Singles", "Doubles", "Triples", "HomeRuns", "TotalBases",
            "Strikeouts", "Walks", "K_pct", "BB_pct",
            "AvgExitVelo", "AvgLaunchAngle",
        ] if c in df_b.columns]

        sort_col_b = "TotalPitches" if "TotalPitches" in df_b.columns else display_cols_b[0]
        st.dataframe(
            df_b[display_cols_b].sort_values(sort_col_b, ascending=False).reset_index(drop=True),
            use_container_width=True, height=440,
        )


# ══════════════════════════════════════════════════════════════════════════════
# VIEW 2: PLAYER PROFILE
# ══════════════════════════════════════════════════════════════════════════════
elif view == "🔎 Player Profile":

    profile_type = st.radio("Player type", ["Pitcher", "Batter"], horizontal=True)

    # ── PITCHER PROFILE ──────────────────────────────────────────────────────
    if profile_type == "Pitcher":
        names = sorted(pitcher_stats_view["Pitcher"].dropna().unique().tolist())
        if not names:
            st.info(f"No pitcher data found for the selected filters.")
            st.stop()
        selected = st.selectbox("Select pitcher", names)

        p_row = pitcher_stats_view[pitcher_stats_view["Pitcher"] == selected].iloc[0]

        st.markdown(f'<div class="section-header">📋 {selected} — {season_label} Overview</div>',
                    unsafe_allow_html=True)

        # Row 1: counting stats
        col1, col2, col3, col4, col5, col6 = st.columns(6)
        with col1: metric_card("Total Pitches", int(p_row.get("TotalPitches", 0) or 0))
        with col2: metric_card("IP",            p_row.get("IP", "—"))
        with col3: metric_card("Avg Velo",      f'{p_row.get("AvgVelocity", "—")} mph')
        with col4: metric_card("Max Velo",      f'{p_row.get("MaxVelocity", "—")} mph')
        with col5: metric_card("Strikeouts",    int(p_row.get("Strikeouts", 0) or 0))
        with col6: metric_card("Walks",         int(p_row.get("Walks", 0) or 0))

        # Row 2: rate stats + movement
        col7, col8, col9, col10, col11, col12 = st.columns(6)
        with col7:  metric_card("K%",           f'{p_row.get("K_pct", "—")}%')
        with col8:  metric_card("BB%",          f'{p_row.get("BB_pct", "—")}%')
        with col9:  metric_card("Hits Allowed", int(p_row.get("HitsAllowed", 0) or 0))
        with col10: metric_card("HRs Allowed",  int(p_row.get("HRsAllowed", 0) or 0))
        with col11: metric_card("Avg Spin",     f'{p_row.get("AvgSpinRate", "—")} rpm')
        with col12: metric_card("Avg Vert Brk", f'{p_row.get("AvgVertBreak", "—")} in')

        # Pitch type breakdown
        pitch_type_cols = [c for c in ["FB_pct","SI_pct","CT_pct","SL_pct","CB_pct","CH_pct","SP_pct"]
                           if c in p_row.index and pd.notna(p_row[c]) and p_row[c] > 0]
        if pitch_type_cols:
            st.markdown('<div class="section-header">🎯 Pitch Type Breakdown</div>', unsafe_allow_html=True)
            label_map = {"FB_pct":"Fastball","SI_pct":"Sinker","CT_pct":"Cutter",
                         "SL_pct":"Slider","CB_pct":"Curveball","CH_pct":"Changeup","SP_pct":"Splitter"}
            pt_cols = st.columns(len(pitch_type_cols))
            for i, c in enumerate(pitch_type_cols):
                with pt_cols[i]:
                    metric_card(label_map.get(c, c), f'{p_row[c]}%')

        # Game log
        st.markdown('<div class="section-header">📅 Game-by-Game Log</div>', unsafe_allow_html=True)
        gl = p_log_filtered[p_log_filtered["Pitcher"] == selected].copy()
        if len(gl) == 0:
            st.info("No game log data found for this pitcher with the current filters.")
        else:
            gl = gl.sort_values("GameDate", ascending=False)
            gl_display_cols = [c for c in [
                "GameDateDisplay", "Opponent", "PitcherTeam",
                "Pitches", "IP", "AvgVelo", "Strikeouts", "Walks", "HitsAllowed",
            ] if c in gl.columns]
            # Rename for clarity
            gl_renamed = gl[gl_display_cols].rename(columns={"GameDateDisplay": "Date"})
            st.dataframe(gl_renamed.reset_index(drop=True), use_container_width=True)

            # Velocity trend
            if "AvgVelo" in gl.columns and gl["AvgVelo"].notna().any():
                st.markdown('<div class="section-header">📈 Velocity Trend</div>', unsafe_allow_html=True)
                chart_data = gl.sort_values("GameDate")[["GameDateDisplay", "AvgVelo"]].dropna()
                chart_data = chart_data.set_index("GameDateDisplay")
                st.line_chart(chart_data, color=AUBURN_ORANGE)

            # K vs BB chart
            if all(c in gl.columns for c in ["Strikeouts", "Walks"]):
                st.markdown('<div class="section-header">⚾ K vs BB by Game</div>', unsafe_allow_html=True)
                chart_kb = gl.sort_values("GameDate")[["GameDateDisplay","Strikeouts","Walks"]].set_index("GameDateDisplay")
                st.bar_chart(chart_kb)

    # ── BATTER PROFILE ───────────────────────────────────────────────────────
    else:
        names_b = sorted(batter_stats_view["Batter"].dropna().unique().tolist())
        if not names_b:
            st.info(f"No batter data found for the selected filters.")
            st.stop()
        selected_b = st.selectbox("Select batter", names_b)

        b_row = batter_stats_view[batter_stats_view["Batter"] == selected_b].iloc[0]

        st.markdown(f'<div class="section-header">📋 {selected_b} — {season_label} Overview</div>',
                    unsafe_allow_html=True)

        # Row 1: counting stats
        col1, col2, col3, col4, col5, col6 = st.columns(6)
        with col1: metric_card("PA",       int(b_row.get("TotalPitches", 0) or 0))
        with col2: metric_card("At-Bats",  int(b_row.get("AtBats", 0) or 0))
        with col3: metric_card("Hits",     int(b_row.get("Hits", 0) or 0))
        with col4: metric_card("Home Runs",int(b_row.get("HomeRuns", 0) or 0))
        with col5: metric_card("Walks",    int(b_row.get("Walks", 0) or 0))
        with col6: metric_card("K's",      int(b_row.get("Strikeouts", 0) or 0))

        # Row 2: rate stats
        col7, col8, col9, col10, col11, col12 = st.columns(6)
        with col7:  metric_card("BA",          b_row.get("BA", "—"))
        with col8:  metric_card("OBP",         b_row.get("OBP", "—"))
        with col9:  metric_card("SLG",         b_row.get("SLG", "—"))
        with col10: metric_card("OPS",         b_row.get("OPS", "—"))
        with col11: metric_card("K%",          f'{b_row.get("K_pct", "—")}%')
        with col12: metric_card("BB%",         f'{b_row.get("BB_pct", "—")}%')

        # Row 3: hit breakdown + batted ball
        col13, col14, col15, col16 = st.columns(4)
        with col13: metric_card("Singles",      int(b_row.get("Singles", 0) or 0))
        with col14: metric_card("Doubles",      int(b_row.get("Doubles", 0) or 0))
        with col15: metric_card("Triples",      int(b_row.get("Triples", 0) or 0))
        with col16: metric_card("Avg Exit Velo",f'{b_row.get("AvgExitVelo", "—")} mph')

        # Game log
        st.markdown('<div class="section-header">📅 Game-by-Game Log</div>', unsafe_allow_html=True)
        gl_b = b_log_filtered[b_log_filtered["Batter"] == selected_b].copy()
        if len(gl_b) == 0:
            st.info("No game log data found for this batter with the current filters.")
        else:
            gl_b = gl_b.sort_values("GameDate", ascending=False)
            gl_b_display = [c for c in [
                "GameDateDisplay", "Opponent", "BatterTeam",
                "Hits", "Singles", "Doubles", "Triples", "HomeRuns",
                "Walks", "Strikeouts", "AtBats", "TotalBases",
                "AvgExitVelo",
            ] if c in gl_b.columns]
            gl_b_renamed = gl_b[gl_b_display].rename(columns={"GameDateDisplay": "Date"})
            st.dataframe(gl_b_renamed.reset_index(drop=True), use_container_width=True)

            # Exit velo trend
            if "AvgExitVelo" in gl_b.columns and gl_b["AvgExitVelo"].notna().any():
                st.markdown('<div class="section-header">📈 Exit Velocity Trend</div>', unsafe_allow_html=True)
                chart_ev = gl_b.sort_values("GameDate")[["GameDateDisplay","AvgExitVelo"]].dropna()
                chart_ev = chart_ev.set_index("GameDateDisplay")
                st.line_chart(chart_ev, color=AUBURN_ORANGE)

            # Hits vs Ks chart
            if all(c in gl_b.columns for c in ["Hits", "Strikeouts"]):
                st.markdown('<div class="section-header">🏏 Hits vs Ks by Game</div>', unsafe_allow_html=True)
                chart_hk = gl_b.sort_values("GameDate")[["GameDateDisplay","Hits","Strikeouts"]].set_index("GameDateDisplay")
                st.bar_chart(chart_hk)
