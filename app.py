import streamlit as st
import pandas as pd
import os
from io import BytesIO
from azure.storage.blob import BlobServiceClient

# ─── Config ──────────────────────────────────────────────────────────────────
AUBURN = "AUB_TIG"
PROCESSED_CONTAINER = "processed-stats"

st.set_page_config(
    page_title="Auburn Baseball Analytics",
    page_icon="⚾",
    layout="wide",
)

# ─── CSS ─────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    .metric-card {
        background: #0e1117;
        border: 1px solid #262730;
        border-radius: 8px;
        padding: 12px 16px;
        text-align: center;
    }
    .metric-label { color: #9aa3af; font-size: 0.8rem; margin-bottom: 4px; }
    .metric-value { color: #fafafa; font-size: 1.4rem; font-weight: 700; }
    .section-header { border-bottom: 2px solid #e87722; padding-bottom: 4px; margin-bottom: 16px; }
</style>
""", unsafe_allow_html=True)

# ─── Data Loading ─────────────────────────────────────────────────────────────
@st.cache_data(ttl=300)
def load_data():
    conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "")
    if not conn_str:
        return None, None, None, None, "AZURE_STORAGE_CONNECTION_STRING not set"

    try:
        client = BlobServiceClient.from_connection_string(conn_str)
        container = client.get_container_client(PROCESSED_CONTAINER)

        def read_csv(name):
            data = container.get_blob_client(name).download_blob().readall()
            return pd.read_csv(BytesIO(data), low_memory=False)

        pitcher_stats = read_csv("pitcher_stats.csv")
        batter_stats  = read_csv("batter_stats.csv")
        pitcher_log   = read_csv("pitcher_game_log.csv")
        batter_log    = read_csv("batter_game_log.csv")

        return pitcher_stats, batter_stats, pitcher_log, batter_log, None
    except Exception as e:
        return None, None, None, None, str(e)


# ─── Helpers ──────────────────────────────────────────────────────────────────
def fmt_pct(val):
    if pd.isna(val): return "—"
    return f"{val:.1f}%"

def fmt_avg(val):
    if pd.isna(val): return "—"
    return f"{val:.3f}"

def fmt_float(val, decimals=2):
    if pd.isna(val): return "—"
    return f"{val:.{decimals}f}"

def parse_game_date(d):
    """Parse YYYYMMDD integer/string to pandas Timestamp."""
    try:
        return pd.to_datetime(str(int(float(str(d)))), format="%Y%m%d")
    except Exception:
        return pd.NaT

def format_display_date(d):
    """Format YYYYMMDD as M/D/YYYY for display."""
    dt = parse_game_date(d)
    if pd.isna(dt):
        return str(d)
    return dt.strftime("%-m/%-d/%Y")

def add_season_col(df, date_col="GameDate"):
    """Derive Season (year int) from GameDate column."""
    if date_col not in df.columns:
        return df
    df = df.copy()
    df["Season"] = df[date_col].apply(
        lambda d: parse_game_date(d).year if not pd.isna(parse_game_date(d)) else None
    )
    df["Season"] = pd.to_numeric(df["Season"], errors="coerce").astype("Int64")
    return df

def add_ip_col(df):
    """Compute IP from OutsRecorded if IP not already present."""
    if "OutsRecorded" in df.columns and "IP" not in df.columns:
        df = df.copy()
        df["IP"] = (pd.to_numeric(df["OutsRecorded"], errors="coerce") / 3).round(1)
    return df

def metric_card(label, value):
    st.markdown(
        f'<div class="metric-card">'
        f'<div class="metric-label">{label}</div>'
        f'<div class="metric-value">{value}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )


# ─── Load & Prep ──────────────────────────────────────────────────────────────
pitcher_stats, batter_stats, pitcher_log, batter_log, load_error = load_data()

if load_error:
    st.error(f"Failed to load data: {load_error}")
    st.stop()

pitcher_log = add_season_col(add_ip_col(pitcher_log), "GameDate")
batter_log  = add_season_col(batter_log, "GameDate")

# ─── Sidebar Navigation ───────────────────────────────────────────────────────
st.sidebar.image(
    "https://upload.wikimedia.org/wikipedia/commons/thumb/3/30/Auburn_Tigers_logo.svg/200px-Auburn_Tigers_logo.svg.png",
    width=120,
)
st.sidebar.title("Auburn Baseball")
view = st.sidebar.radio("View", ["📊 Season Leaderboards", "🔎 Player Profile"])


# ═════════════════════════════════════════════════════════════════════════════
# SEASON LEADERBOARDS
# ═════════════════════════════════════════════════════════════════════════════
if view == "📊 Season Leaderboards":
    st.title("📊 Season Leaderboards")

    tab_p, tab_b, tab_pg, tab_bg = st.tabs([
        "⚾ Pitchers", "🏏 Batters", "📅 Pitcher Game Log", "📅 Batter Game Log"
    ])

    # ── Pitchers ──────────────────────────────────────────────────────────────
    with tab_p:
        st.markdown('<div class="section-header"><h3>Pitcher Leaderboard</h3></div>', unsafe_allow_html=True)

        col1, col2, col3 = st.columns([2, 2, 1])
        with col1:
            team_filter_p = st.selectbox(
                "Team Filter", ["All Teams", "Auburn Only", "Opponents Only"], key="p_team"
            )
        with col2:
            sort_col_p = st.selectbox(
                "Sort By", ["TotalPitches", "IP", "Strikeouts", "K_pct", "AvgVelocity"], key="p_sort"
            )
        with col3:
            min_pitches = st.number_input("Min Pitches", value=0, step=50, key="p_min")

        df_p = pitcher_stats.copy()

        if team_filter_p == "Auburn Only":
            df_p = df_p[df_p["PitcherTeam"].str.upper().str.strip() == AUBURN]
        elif team_filter_p == "Opponents Only":
            df_p = df_p[df_p["PitcherTeam"].str.upper().str.strip() != AUBURN]

        if min_pitches > 0:
            df_p = df_p[df_p["TotalPitches"] >= min_pitches]

        display_cols_p = [
            "Pitcher", "PitcherTeam", "PitcherThrows",
            "TotalPitches", "IP",
            "AvgVelocity", "MaxVelocity",
            "AvgSpinRate", "AvgVertBreak", "AvgHorzBreak",
            "Strikeouts", "Walks", "K_pct", "BB_pct",
            "HitsAllowed", "HRsAllowed",
            "FB_pct", "SI_pct", "CT_pct", "SL_pct", "CB_pct", "CH_pct", "SP_pct", "KN_pct",
        ]
        display_cols_p = [c for c in display_cols_p if c in df_p.columns]

        sort_by_p = sort_col_p if sort_col_p in display_cols_p else "TotalPitches"
        df_p = df_p[display_cols_p].sort_values(sort_by_p, ascending=False)

        st.dataframe(df_p, use_container_width=True, hide_index=True)
        st.caption(f"Showing {len(df_p)} pitchers  •  2026 season stats")

    # ── Batters ───────────────────────────────────────────────────────────────
    with tab_b:
        st.markdown('<div class="section-header"><h3>Batter Leaderboard</h3></div>', unsafe_allow_html=True)

        col1, col2, col3 = st.columns([2, 2, 1])
        with col1:
            team_filter_b = st.selectbox(
                "Team Filter", ["All Teams", "Auburn Only", "Opponents Only"], key="b_team"
            )
        with col2:
            sort_col_b = st.selectbox(
                "Sort By", ["AtBats", "OPS", "BA", "HomeRuns", "Hits", "TotalPitches"], key="b_sort"
            )
        with col3:
            min_abs = st.number_input("Min At-Bats", value=0, step=5, key="b_min")

        df_b = batter_stats.copy()

        if team_filter_b == "Auburn Only":
            df_b = df_b[df_b["BatterTeam"].str.upper().str.strip() == AUBURN]
        elif team_filter_b == "Opponents Only":
            df_b = df_b[df_b["BatterTeam"].str.upper().str.strip() != AUBURN]

        if min_abs > 0:
            df_b = df_b[df_b["AtBats"] >= min_abs]

        display_cols_b = [
            "Batter", "BatterTeam", "BatterSide",
            "TotalPitches", "AtBats", "Hits",
            "BA", "OBP", "SLG", "OPS",
            "Singles", "Doubles", "Triples", "HomeRuns", "TotalBases",
            "Strikeouts", "Walks", "HBP", "K_pct", "BB_pct",
            "AvgExitVelo", "AvgLaunchAngle",
        ]
        display_cols_b = [c for c in display_cols_b if c in df_b.columns]

        sort_by_b = sort_col_b if sort_col_b in display_cols_b else "Hits"
        df_b = df_b[display_cols_b].sort_values(sort_by_b, ascending=False)

        st.dataframe(df_b, use_container_width=True, hide_index=True)
        st.caption(f"Showing {len(df_b)} batters  •  2026 season stats")

    # ── Pitcher Game Log ──────────────────────────────────────────────────────
    with tab_pg:
        st.markdown('<div class="section-header"><h3>Pitcher Game Log</h3></div>', unsafe_allow_html=True)

        pg_seasons = sorted(
            [int(s) for s in pitcher_log["Season"].dropna().unique() if s > 0],
            reverse=True,
        )
        pg_season_opts = ["All Seasons"] + [str(s) for s in pg_seasons]

        col1, col2, col3 = st.columns(3)
        with col1:
            sel_pg_season = st.selectbox("Season", pg_season_opts, key="pg_season")
        with col2:
            pg_team_filter = st.selectbox(
                "Team", ["All Teams", "Auburn Only", "Opponents Only"], key="pg_team"
            )
        with col3:
            pg_pitcher_names = sorted(pitcher_log["Pitcher"].dropna().unique())
            sel_pg_pitcher = st.selectbox(
                "Pitcher", ["All Pitchers"] + list(pg_pitcher_names), key="pg_pitcher"
            )

        df_pg = pitcher_log.copy()

        if sel_pg_season != "All Seasons":
            df_pg = df_pg[df_pg["Season"] == int(sel_pg_season)]
        if pg_team_filter == "Auburn Only":
            df_pg = df_pg[df_pg["PitcherTeam"].str.upper().str.strip() == AUBURN]
        elif pg_team_filter == "Opponents Only":
            df_pg = df_pg[df_pg["PitcherTeam"].str.upper().str.strip() != AUBURN]
        if sel_pg_pitcher != "All Pitchers":
            df_pg = df_pg[df_pg["Pitcher"] == sel_pg_pitcher]

        df_pg = df_pg.copy()
        df_pg["Date"] = df_pg["GameDate"].apply(format_display_date)
        df_pg["_sort_dt"] = df_pg["GameDate"].apply(parse_game_date)

        display_cols_pg = [
            "Date", "Pitcher", "PitcherTeam", "Opponent",
            "Pitches", "IP",
            "AvgVelo", "Strikeouts", "Walks", "HitsAllowed",
        ]
        display_cols_pg = [c for c in display_cols_pg if c in df_pg.columns]

        df_pg = df_pg.sort_values("_sort_dt", ascending=False)[display_cols_pg]

        st.dataframe(df_pg, use_container_width=True, hide_index=True)
        st.caption(f"{len(df_pg)} game appearances")

    # ── Batter Game Log ───────────────────────────────────────────────────────
    with tab_bg:
        st.markdown('<div class="section-header"><h3>Batter Game Log</h3></div>', unsafe_allow_html=True)

        bg_seasons = sorted(
            [int(s) for s in batter_log["Season"].dropna().unique() if s > 0],
            reverse=True,
        )
        bg_season_opts = ["All Seasons"] + [str(s) for s in bg_seasons]

        col1, col2, col3 = st.columns(3)
        with col1:
            sel_bg_season = st.selectbox("Season", bg_season_opts, key="bg_season")
        with col2:
            bg_team_filter = st.selectbox(
                "Team", ["All Teams", "Auburn Only", "Opponents Only"], key="bg_team"
            )
        with col3:
            bg_batter_names = sorted(batter_log["Batter"].dropna().unique())
            sel_bg_batter = st.selectbox(
                "Batter", ["All Batters"] + list(bg_batter_names), key="bg_batter"
            )

        df_bg = batter_log.copy()

        if sel_bg_season != "All Seasons":
            df_bg = df_bg[df_bg["Season"] == int(sel_bg_season)]
        if bg_team_filter == "Auburn Only":
            df_bg = df_bg[df_bg["BatterTeam"].str.upper().str.strip() == AUBURN]
        elif bg_team_filter == "Opponents Only":
            df_bg = df_bg[df_bg["BatterTeam"].str.upper().str.strip() != AUBURN]
        if sel_bg_batter != "All Batters":
            df_bg = df_bg[df_bg["Batter"] == sel_bg_batter]

        df_bg = df_bg.copy()
        df_bg["Date"] = df_bg["GameDate"].apply(format_display_date)
        df_bg["_sort_dt"] = df_bg["GameDate"].apply(parse_game_date)

        display_cols_bg = [
            "Date", "Batter", "BatterTeam", "Opponent",
            "AtBats", "Hits",
            "Singles", "Doubles", "Triples", "HomeRuns", "TotalBases",
            "Walks", "Strikeouts", "HBP",
            "AvgExitVelo",
        ]
        display_cols_bg = [c for c in display_cols_bg if c in df_bg.columns]

        df_bg = df_bg.sort_values("_sort_dt", ascending=False)[display_cols_bg]

        st.dataframe(df_bg, use_container_width=True, hide_index=True)
        st.caption(f"{len(df_bg)} game appearances")


# ═════════════════════════════════════════════════════════════════════════════
# PLAYER PROFILE
# ═════════════════════════════════════════════════════════════════════════════
elif view == "🔎 Player Profile":
    st.title("🔎 Player Profile")

    player_type = st.radio("Player Type", ["Pitcher", "Batter"], horizontal=True)

    if player_type == "Pitcher":
        all_pitchers = sorted(pitcher_stats["Pitcher"].dropna().unique())
        sel = st.selectbox("Select Pitcher", all_pitchers)

        if sel:
            stats = pitcher_stats[pitcher_stats["Pitcher"] == sel].iloc[0]
            team  = stats.get("PitcherTeam", "")
            hand  = stats.get("PitcherThrows", "")
            st.subheader(f"{sel}  •  {team}  ({hand})")

            # Row 1
            c1, c2, c3, c4, c5, c6 = st.columns(6)
            with c1: metric_card("Total Pitches", int(stats.get("TotalPitches", 0)))
            with c2: metric_card("IP", fmt_float(stats.get("IP"), 1))
            with c3: metric_card("Strikeouts", int(stats.get("Strikeouts", 0)))
            with c4: metric_card("Walks", int(stats.get("Walks", 0)))
            with c5: metric_card("K%", fmt_pct(stats.get("K_pct")))
            with c6: metric_card("BB%", fmt_pct(stats.get("BB_pct")))

            st.markdown("&nbsp;", unsafe_allow_html=True)

            # Row 2
            c1, c2, c3, c4, c5, c6 = st.columns(6)
            with c1: metric_card("Avg Velo", fmt_float(stats.get("AvgVelocity")))
            with c2: metric_card("Max Velo", fmt_float(stats.get("MaxVelocity")))
            with c3: metric_card("Avg Spin", fmt_float(stats.get("AvgSpinRate"), 1))
            with c4: metric_card("Vert Break", fmt_float(stats.get("AvgVertBreak")))
            with c5: metric_card("Horz Break", fmt_float(stats.get("AvgHorzBreak")))
            with c6: metric_card("Hits Allowed", int(stats.get("HitsAllowed", 0)))

            st.markdown("&nbsp;", unsafe_allow_html=True)

            # Pitch mix
            st.markdown("**— Pitch Mix —**")
            pitch_types = [
                ("FB", "Fastball"), ("SI", "Sinker"), ("CT", "Cutter"), ("SL", "Slider"),
                ("CB", "Curveball"), ("CH", "Changeup"), ("SP", "Splitter"), ("KN", "Knuckleball"),
            ]
            active = [(name, stats[f"{code}_pct"])
                      for code, name in pitch_types
                      if f"{code}_pct" in stats.index
                      and not pd.isna(stats[f"{code}_pct"])
                      and stats[f"{code}_pct"] > 0]

            if active:
                cols = st.columns(min(len(active), 8))
                for i, (name, pct) in enumerate(active):
                    with cols[i % 8]:
                        metric_card(name, fmt_pct(pct))

            # Game log
            st.markdown("---")
            st.subheader("Game Log")

            log = pitcher_log[pitcher_log["Pitcher"] == sel].copy()
            log["Date"] = log["GameDate"].apply(format_display_date)
            log["_sort_dt"] = log["GameDate"].apply(parse_game_date)

            log_cols = [c for c in [
                "Date", "Opponent", "Pitches", "IP",
                "AvgVelo", "Strikeouts", "Walks", "HitsAllowed",
            ] if c in log.columns]

            st.dataframe(
                log.sort_values("_sort_dt", ascending=False)[log_cols],
                use_container_width=True, hide_index=True,
            )

    else:  # Batter
        all_batters = sorted(batter_stats["Batter"].dropna().unique())
        sel = st.selectbox("Select Batter", all_batters)

        if sel:
            stats = batter_stats[batter_stats["Batter"] == sel].iloc[0]
            team  = stats.get("BatterTeam", "")
            side  = stats.get("BatterSide", "")
            st.subheader(f"{sel}  •  {team}  ({side})")

            # Row 1 — slash line
            c1, c2, c3, c4, c5, c6 = st.columns(6)
            with c1: metric_card("At-Bats", int(stats.get("AtBats", 0)))
            with c2: metric_card("Hits", int(stats.get("Hits", 0)))
            with c3: metric_card("BA", fmt_avg(stats.get("BA")))
            with c4: metric_card("OBP", fmt_avg(stats.get("OBP")))
            with c5: metric_card("SLG", fmt_avg(stats.get("SLG")))
            with c6: metric_card("OPS", fmt_avg(stats.get("OPS")))

            st.markdown("&nbsp;", unsafe_allow_html=True)

            # Row 2 — counting stats
            c1, c2, c3, c4, c5, c6 = st.columns(6)
            with c1: metric_card("1B", int(stats.get("Singles", 0)))
            with c2: metric_card("2B", int(stats.get("Doubles", 0)))
            with c3: metric_card("3B", int(stats.get("Triples", 0)))
            with c4: metric_card("HR", int(stats.get("HomeRuns", 0)))
            with c5: metric_card("TB", int(stats.get("TotalBases", 0)))
            with c6: metric_card("HBP", int(stats.get("HBP", 0)))

            st.markdown("&nbsp;", unsafe_allow_html=True)

            # Row 3 — plate discipline + batted ball
            c1, c2, c3, c4, c5, c6 = st.columns(6)
            with c1: metric_card("Strikeouts", int(stats.get("Strikeouts", 0)))
            with c2: metric_card("Walks", int(stats.get("Walks", 0)))
            with c3: metric_card("K%", fmt_pct(stats.get("K_pct")))
            with c4: metric_card("BB%", fmt_pct(stats.get("BB_pct")))
            with c5: metric_card("Avg Exit Velo", fmt_float(stats.get("AvgExitVelo")))
            with c6: metric_card("Avg LA", fmt_float(stats.get("AvgLaunchAngle")))

            # Game log
            st.markdown("---")
            st.subheader("Game Log")

            log = batter_log[batter_log["Batter"] == sel].copy()
            log["Date"] = log["GameDate"].apply(format_display_date)
            log["_sort_dt"] = log["GameDate"].apply(parse_game_date)

            log_cols = [c for c in [
                "Date", "Opponent",
                "AtBats", "Hits", "Singles", "Doubles", "Triples", "HomeRuns",
                "TotalBases", "Walks", "Strikeouts", "HBP", "AvgExitVelo",
            ] if c in log.columns]

            st.dataframe(
                log.sort_values("_sort_dt", ascending=False)[log_cols],
                use_container_width=True, hide_index=True,
            )
