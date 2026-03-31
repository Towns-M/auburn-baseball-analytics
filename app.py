import os
import io
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
  /* Global background */
  .stApp {{ background-color: #f5f5f5; }}

  /* Sidebar */
  [data-testid="stSidebar"] {{
      background-color: {AUBURN_NAVY};
  }}
  [data-testid="stSidebar"] * {{
      color: {AUBURN_WHITE} !important;
  }}
  [data-testid="stSidebar"] .stSelectbox label,
  [data-testid="stSidebar"] .stTextInput label {{
      color: {AUBURN_WHITE} !important;
  }}

  /* Top header bar */
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

  /* Metric cards */
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

  /* Section headers */
  .section-header {{
      color: {AUBURN_NAVY};
      font-weight: 700;
      font-size: 1.1rem;
      border-bottom: 2px solid {AUBURN_ORANGE};
      padding-bottom: 6px;
      margin: 20px 0 12px;
  }}

  /* Tab styling */
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

  /* Dataframe tweaks */
  .stDataFrame {{ border-radius: 6px; overflow: hidden; }}

  /* Buttons */
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

    # Normalise game date to string for sorting
    for df in [pitcher_game_log, batter_game_log]:
        df["GameDate"] = df["GameDate"].astype(str)

    return pitcher_stats, batter_stats, pitcher_game_log, batter_game_log


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## ⚾ Auburn Baseball")
    st.markdown("---")
    view = st.radio("View", ["📊 Season Leaderboards", "🔎 Player Profile"])
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
st.markdown("""
<div class="auburn-header">
  <div>
    <h1>⚾ Auburn Baseball Analytics</h1>
    <p>Trackman pitch-by-pitch data · Season statistics</p>
  </div>
</div>
""", unsafe_allow_html=True)

# ── Load data ─────────────────────────────────────────────────────────────────
try:
    pitcher_stats, batter_stats, pitcher_game_log, batter_game_log = load_data()
except Exception as e:
    st.error(f"Could not load data: {e}")
    st.info("Make sure the AZURE_STORAGE_CONNECTION_STRING environment variable is set and the transform has been run.")
    st.stop()

# ── Helper: metric card ───────────────────────────────────────────────────────
def metric_card(label, value):
    disp = "—" if pd.isna(value) or value is None else str(value)
    st.markdown(f"""
    <div class="metric-card">
      <div class="metric-label">{label}</div>
      <div class="metric-value">{disp}</div>
    </div>""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# VIEW 1: SEASON LEADERBOARDS
# ══════════════════════════════════════════════════════════════════════════════
if view == "📊 Season Leaderboards":

    tab_pitch, tab_bat = st.tabs(["⚾  Pitcher Leaderboard", "🏏  Batter Leaderboard"])

    # ── Pitcher leaderboard ──────────────────────────────────────────────────
    with tab_pitch:
        st.markdown('<div class="section-header">Season Pitcher Stats</div>', unsafe_allow_html=True)

        # Quick KPI row
        if len(pitcher_stats) > 0:
            col1, col2, col3, col4 = st.columns(4)
            with col1:
                metric_card("Total Pitchers", len(pitcher_stats))
            with col2:
                metric_card("Avg Velocity (mph)", round(pitcher_stats["AvgVelocity"].mean(), 1) if "AvgVelocity" in pitcher_stats else "—")
            with col3:
                metric_card("Avg Spin Rate (rpm)", round(pitcher_stats["AvgSpinRate"].mean(), 0) if "AvgSpinRate" in pitcher_stats else "—")
            with col4:
                metric_card("Total Strikeouts", int(pitcher_stats["Strikeouts"].sum()) if "Strikeouts" in pitcher_stats else "—")

        # Filter
        search = st.text_input("Search pitcher name", placeholder="e.g. Smith", key="p_search")
        teams  = ["All"] + sorted(pitcher_stats["PitcherTeam"].dropna().unique().tolist())
        team   = st.selectbox("Team", teams, key="p_team")

        df = pitcher_stats.copy()
        if search:
            df = df[df["Pitcher"].str.contains(search, case=False, na=False)]
        if team != "All":
            df = df[df["PitcherTeam"] == team]

        # Column order
        display_cols = [c for c in [
            "Pitcher", "PitcherTeam", "PitcherThrows", "TotalPitches",
            "AvgVelocity", "MaxVelocity", "AvgSpinRate",
            "AvgVertBreak", "AvgHorzBreak",
            "Strikeouts", "Walks", "HitsAllowed", "HRsAllowed",
        ] if c in df.columns]

        st.dataframe(
            df[display_cols].sort_values("TotalPitches", ascending=False).reset_index(drop=True),
            use_container_width=True, height=440,
        )

    # ── Batter leaderboard ───────────────────────────────────────────────────
    with tab_bat:
        st.markdown('<div class="section-header">Season Batter Stats</div>', unsafe_allow_html=True)

        if len(batter_stats) > 0:
            col1, col2, col3, col4 = st.columns(4)
            with col1:
                metric_card("Total Batters", len(batter_stats))
            with col2:
                metric_card("Avg Exit Velo (mph)", round(batter_stats["AvgExitVelo"].mean(), 1) if "AvgExitVelo" in batter_stats else "—")
            with col3:
                metric_card("Avg Launch Angle (°)", round(batter_stats["AvgLaunchAngle"].mean(), 1) if "AvgLaunchAngle" in batter_stats else "—")
            with col4:
                metric_card("Total Home Runs", int(batter_stats["HomeRuns"].sum()) if "HomeRuns" in batter_stats else "—")

        search_b = st.text_input("Search batter name", placeholder="e.g. Jones", key="b_search")
        teams_b  = ["All"] + sorted(batter_stats["BatterTeam"].dropna().unique().tolist())
        team_b   = st.selectbox("Team", teams_b, key="b_team")

        df_b = batter_stats.copy()
        if search_b:
            df_b = df_b[df_b["Batter"].str.contains(search_b, case=False, na=False)]
        if team_b != "All":
            df_b = df_b[df_b["BatterTeam"] == team_b]

        # Compute BA-like column if possible
        if all(c in df_b.columns for c in ["Hits", "TotalPitches"]):
            df_b["H"] = df_b["Hits"]

        display_cols_b = [c for c in [
            "Batter", "BatterTeam", "BatterSide", "TotalPitches",
            "Hits", "Singles", "Doubles", "Triples", "HomeRuns",
            "Strikeouts", "Walks",
            "AvgExitVelo", "AvgLaunchAngle",
        ] if c in df_b.columns]

        st.dataframe(
            df_b[display_cols_b].sort_values("TotalPitches", ascending=False).reset_index(drop=True),
            use_container_width=True, height=440,
        )


# ══════════════════════════════════════════════════════════════════════════════
# VIEW 2: PLAYER PROFILE
# ══════════════════════════════════════════════════════════════════════════════
elif view == "🔎 Player Profile":

    profile_type = st.radio("Player type", ["Pitcher", "Batter"], horizontal=True)

    if profile_type == "Pitcher":
        # Build display name list
        names = sorted(pitcher_stats["Pitcher"].dropna().unique().tolist())
        selected = st.selectbox("Select pitcher", names)

        p_row = pitcher_stats[pitcher_stats["Pitcher"] == selected].iloc[0]

        # ── Season stats cards ──
        st.markdown(f'<div class="section-header">📋 {selected} — Season Overview</div>', unsafe_allow_html=True)
        col1, col2, col3, col4, col5, col6 = st.columns(6)
        with col1: metric_card("Total Pitches", int(p_row.get("TotalPitches", 0)))
        with col2: metric_card("Avg Velo", f'{p_row.get("AvgVelocity", "—")} mph')
        with col3: metric_card("Max Velo", f'{p_row.get("MaxVelocity", "—")} mph')
        with col4: metric_card("Avg Spin", f'{p_row.get("AvgSpinRate", "—")} rpm')
        with col5: metric_card("Strikeouts", int(p_row.get("Strikeouts", 0)))
        with col6: metric_card("Walks", int(p_row.get("Walks", 0)))

        col7, col8, col9, col10 = st.columns(4)
        with col7:  metric_card("Hits Allowed",    int(p_row.get("HitsAllowed", 0)))
        with col8:  metric_card("HRs Allowed",     int(p_row.get("HRsAllowed", 0)))
        with col9:  metric_card("Avg Vert Break",  f'{p_row.get("AvgVertBreak", "—")} in')
        with col10: metric_card("Avg Horz Break",  f'{p_row.get("AvgHorzBreak", "—")} in')

        # ── Game log ──
        st.markdown('<div class="section-header">📅 Game-by-Game Log</div>', unsafe_allow_html=True)
        gl = pitcher_game_log[pitcher_game_log["Pitcher"] == selected].copy()
        if len(gl) == 0:
            st.info("No game log data found for this pitcher.")
        else:
            gl = gl.sort_values("GameDate")
            display_gl = [c for c in ["GameDate", "PitcherTeam", "Pitches", "AvgVelo", "Strikeouts", "Walks", "HitsAllowed"] if c in gl.columns]
            st.dataframe(gl[display_gl].reset_index(drop=True), use_container_width=True)

            # Velocity trend chart
            if "AvgVelo" in gl.columns and gl["AvgVelo"].notna().any():
                st.markdown('<div class="section-header">📈 Velocity Trend</div>', unsafe_allow_html=True)
                chart_data = gl[["GameDate", "AvgVelo"]].dropna().set_index("GameDate")
                st.line_chart(chart_data, color=AUBURN_ORANGE)

            # Pitch / K / BB bar chart
            if all(c in gl.columns for c in ["GameDate", "Strikeouts", "Walks"]):
                st.markdown('<div class="section-header">⚾ K vs BB by Game</div>', unsafe_allow_html=True)
                chart_kb = gl[["GameDate", "Strikeouts", "Walks"]].set_index("GameDate")
                st.bar_chart(chart_kb)

    else:  # Batter profile
        names_b   = sorted(batter_stats["Batter"].dropna().unique().tolist())
        selected_b = st.selectbox("Select batter", names_b)

        b_row = batter_stats[batter_stats["Batter"] == selected_b].iloc[0]

        st.markdown(f'<div class="section-header">📋 {selected_b} — Season Overview</div>', unsafe_allow_html=True)
        col1, col2, col3, col4, col5, col6 = st.columns(6)
        with col1: metric_card("Total Pitches",  int(b_row.get("TotalPitches", 0)))
        with col2: metric_card("Hits",           int(b_row.get("Hits", 0)))
        with col3: metric_card("Home Runs",      int(b_row.get("HomeRuns", 0)))
        with col4: metric_card("Strikeouts",     int(b_row.get("Strikeouts", 0)))
        with col5: metric_card("Walks",          int(b_row.get("Walks", 0)))
        with col6: metric_card("Avg Exit Velo",  f'{b_row.get("AvgExitVelo", "—")} mph')

        col7, col8, col9, col10 = st.columns(4)
        with col7:  metric_card("Singles",         int(b_row.get("Singles", 0)))
        with col8:  metric_card("Doubles",         int(b_row.get("Doubles", 0)))
        with col9:  metric_card("Triples",         int(b_row.get("Triples", 0)))
        with col10: metric_card("Avg Launch Angle",f'{b_row.get("AvgLaunchAngle", "—")}°')

        # ── Game log ──
        st.markdown('<div class="section-header">📅 Game-by-Game Log</div>', unsafe_allow_html=True)
        gl_b = batter_game_log[batter_game_log["Batter"] == selected_b].copy()
        if len(gl_b) == 0:
            st.info("No game log data found for this batter.")
        else:
            gl_b = gl_b.sort_values("GameDate")
            display_gl_b = [c for c in ["GameDate", "BatterTeam", "Pitches", "Hits", "HomeRuns", "Strikeouts", "Walks", "AvgExitVelo"] if c in gl_b.columns]
            st.dataframe(gl_b[display_gl_b].reset_index(drop=True), use_container_width=True)

            # Exit velo trend
            if "AvgExitVelo" in gl_b.columns and gl_b["AvgExitVelo"].notna().any():
                st.markdown('<div class="section-header">📈 Exit Velocity Trend</div>', unsafe_allow_html=True)
                chart_ev = gl_b[["GameDate", "AvgExitVelo"]].dropna().set_index("GameDate")
                st.line_chart(chart_ev, color=AUBURN_ORANGE)

            # Hits / K / BB bar chart
            if all(c in gl_b.columns for c in ["GameDate", "Hits", "Strikeouts"]):
                st.markdown('<div class="section-header">🏏 Hits vs Ks by Game</div>', unsafe_allow_html=True)
                chart_hk = gl_b[["GameDate", "Hits", "Strikeouts"]].set_index("GameDate")
                st.bar_chart(chart_hk)
