"""
Auburn Baseball Analytics — Streamlit front-end.

Defaults to Auburn-only views for players & coaches, with a Scouting mode
for opponent data.
"""
import streamlit as st
import pandas as pd
import os
from io import BytesIO
from azure.storage.blob import BlobServiceClient
from concurrent.futures import ThreadPoolExecutor
# ─── Config ──────────────────────────────────────────────────────────────────
AUBURN              = "AUB_TIG"
PROCESSED_CONTAINER = "processed-stats"

st.set_page_config(
    page_title="Auburn Baseball Analytics",
    page_icon="⚾",
    layout="wide",
    initial_sidebar_state="expanded",
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
    .metric-label { color: #9aa3af; font-size: 0.8rem; margin-bottom: 4px; letter-spacing: 0.03em; }
    .metric-value { color: #fafafa; font-size: 1.35rem; font-weight: 700; }
    .section-header { border-bottom: 2px solid #e87722; padding-bottom: 4px; margin-bottom: 16px; }
    .section-header h3 { color: #e87722; margin: 0; }
    .mode-pill { display: inline-block; padding: 2px 10px; border-radius: 999px;
                 background: #e87722; color: #fff; font-size: 0.75rem; font-weight: 700;
                 letter-spacing: 0.05em; }
    .mode-pill-scout { background: #444; }
</style>
""", unsafe_allow_html=True)


# ─── Data loading ────────────────────────────────────────────────────────────
@st.cache_resource
def get_container():
    """Cached Azure container client — reused across reruns."""
    conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "")
    if not conn_str:
        return None
    return (BlobServiceClient.from_connection_string(conn_str)
            .get_container_client(PROCESSED_CONTAINER))


@st.cache_data(ttl=3600, show_spinner="Loading Auburn data…")
def load_data():
    container = get_container()
    if container is None:
        return None, None, None, None, "AZURE_STORAGE_CONNECTION_STRING not set"
    try:
        def read_csv(name):
            data = container.get_blob_client(name).download_blob().readall()
            return pd.read_csv(BytesIO(data), low_memory=False)

        names = [
            "pitcher_stats.csv",
            "batter_stats.csv",
            "pitcher_game_log.csv",
            "batter_game_log.csv",
        ]
        with ThreadPoolExecutor(max_workers=4) as ex:
            ps, bs, pg, bg = list(ex.map(read_csv, names))
        return ps, bs, pg, bg, None
    except Exception as e:
        return None, None, None, None, str(e)


# ─── Helpers ─────────────────────────────────────────────────────────────────
def fmt_pct(val):
    if pd.isna(val): return "—"
    return f"{val:.1f}%"

def fmt_avg(val):
    if pd.isna(val): return "—"
    return f"{val:.3f}"

def fmt_float(val, decimals=2):
    if pd.isna(val): return "—"
    return f"{val:.{decimals}f}"

def fmt_int(val):
    try:
        if pd.isna(val): return "—"
    except Exception:
        pass
    try:
        return f"{int(val)}"
    except Exception:
        return "—"

def parse_game_date(d):
    try:
        return pd.to_datetime(str(int(float(str(d)))), format="%Y%m%d")
    except Exception:
        return pd.NaT

def format_display_date(d):
    dt = parse_game_date(d)
    if pd.isna(dt):
        return str(d)
    try:
        return dt.strftime("%-m/%-d/%Y")     # Linux/Mac
    except ValueError:
        return dt.strftime("%#m/%#d/%Y")     # Windows

def add_season_col(df, date_col="GameDate"):
    if date_col not in df.columns:
        return df
    df = df.copy()
    df["Season"] = df[date_col].apply(
        lambda d: parse_game_date(d).year if not pd.isna(parse_game_date(d)) else None
    )
    df["Season"] = pd.to_numeric(df["Season"], errors="coerce").astype("Int64")
    return df

def add_ip_col(df):
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

def apply_team_filter(df, team_col, mode):
    if df is None or team_col not in df.columns:
        return df
    t = df[team_col].astype(str).str.upper().str.strip()
    if mode == "Auburn":
        return df[t == AUBURN]
    if mode == "Opponents":
        return df[t != AUBURN]
    return df  # All


# ─── Load ────────────────────────────────────────────────────────────────────
pitcher_stats, batter_stats, pitcher_log, batter_log, load_error = load_data()

if load_error:
    st.error(f"Failed to load data: {load_error}")
    st.stop()

pitcher_log = add_season_col(add_ip_col(pitcher_log), "GameDate")
batter_log  = add_season_col(batter_log, "GameDate")


# ═════════════════════════════════════════════════════════════════════════════
# Sidebar
# ═════════════════════════════════════════════════════════════════════════════
st.sidebar.image(
    "https://upload.wikimedia.org/wikipedia/commons/thumb/3/30/Auburn_Tigers_logo.svg/200px-Auburn_Tigers_logo.svg.png",
    width=120,
)
st.sidebar.title("Auburn Baseball")
st.sidebar.caption("2026 season analytics")

view = st.sidebar.radio("View", ["📊 Season Leaderboards", "🔎 Player Profile"])

st.sidebar.markdown("---")
scouting_mode = st.sidebar.toggle(
    "Scouting mode",
    value=False,
    help="When off, you see Auburn players only. Turn on to view opponents for scouting.",
)
st.sidebar.caption(
    "Auburn-only" if not scouting_mode else "Scouting mode — opponents visible"
)


# Default team-filter mode based on toggle
default_mode = "Auburn" if not scouting_mode else "All"
team_filter_options = ["Auburn", "All", "Opponents"]
default_index = team_filter_options.index(default_mode)


# ═════════════════════════════════════════════════════════════════════════════
# SEASON LEADERBOARDS
# ═════════════════════════════════════════════════════════════════════════════
if view == "📊 Season Leaderboards":
    pill = '<span class="mode-pill">AUBURN ONLY</span>' if not scouting_mode \
           else '<span class="mode-pill mode-pill-scout">SCOUTING MODE</span>'
    st.markdown(f"## 📊 Season Leaderboards &nbsp; {pill}", unsafe_allow_html=True)

    tab_p, tab_b, tab_pg, tab_bg = st.tabs([
        "⚾ Pitchers", "🏏 Batters", "📅 Pitcher Game Log", "📅 Batter Game Log"
    ])

    # ─── Pitchers ──────────────────────────────────────────────────────────
    with tab_p:
        st.markdown('<div class="section-header"><h3>Pitcher Leaderboard</h3></div>',
                    unsafe_allow_html=True)

        col1, col2, col3 = st.columns([2, 2, 1])
        with col1:
            team_p = st.selectbox(
                "Team",
                team_filter_options,
                index=default_index,
                format_func=lambda x: {"Auburn": "Auburn Only",
                                         "All": "All Teams",
                                         "Opponents": "Opponents Only"}[x],
                key="p_team",
            )
        sort_options_p = [
            "IP", "BF", "TotalPitches", "Strikeouts",
            "K_pct", "BB_pct", "FPS_pct", "Win_pct",
            "Edge_pct", "Zone_pct", "GB_pct",
            "AvgVelocity", "MaxVelocity", "AvgSpinRate", "AvgIVB", "AvgIHB",
        ]
        with col2:
            sort_col_p = st.selectbox("Sort by", sort_options_p, key="p_sort")
        with col3:
            min_bf = st.number_input("Min BF", value=0, step=10, key="p_min_bf")

        df_p = apply_team_filter(pitcher_stats, "PitcherTeam", team_p)
        if min_bf > 0 and "BF" in df_p.columns:
            df_p = df_p[df_p["BF"] >= min_bf]

        display_cols_p = [
            "Pitcher", "PitcherTeam", "PitcherThrows",
            "IP", "BF", "TotalPitches",
            "K_pct", "BB_pct", "FPS_pct", "Win_pct",
            "Edge_pct", "Zone_pct", "GB_pct",
            "AvgVelocity", "MaxVelocity", "AvgSpinRate", "AvgIVB", "AvgIHB",
            "Strikeouts", "Walks", "HitsAllowed", "HRsAllowed",
            "FBMix_pct", "SIMix_pct", "CTMix_pct", "SLMix_pct",
            "CBMix_pct", "CHMix_pct", "SPMix_pct", "KNMix_pct",
        ]
        display_cols_p = [c for c in display_cols_p if c in df_p.columns]

        sort_by_p = sort_col_p if sort_col_p in display_cols_p else ("BF" if "BF" in display_cols_p else display_cols_p[0])
        df_p = df_p[display_cols_p].sort_values(sort_by_p, ascending=False)

        st.dataframe(df_p, use_container_width=True, hide_index=True)
        st.caption(f"Showing {len(df_p)} pitchers  •  2026 season")

    # ─── Batters ───────────────────────────────────────────────────────────
    with tab_b:
        st.markdown('<div class="section-header"><h3>Batter Leaderboard</h3></div>',
                    unsafe_allow_html=True)

        col1, col2, col3 = st.columns([2, 2, 1])
        with col1:
            team_b = st.selectbox(
                "Team",
                team_filter_options,
                index=default_index,
                format_func=lambda x: {"Auburn": "Auburn Only",
                                         "All": "All Teams",
                                         "Opponents": "Opponents Only"}[x],
                key="b_team",
            )
        sort_options_b = [
            "OPS", "SLG", "OBP", "BA", "HomeRuns", "AtBats", "Hits",
            "K_pct", "BB_pct", "ZSwing_pct", "Chase_pct",
            "AvgExitVelo", "MaxExitVelo",
        ]
        with col2:
            sort_col_b = st.selectbox("Sort by", sort_options_b, key="b_sort")
        with col3:
            min_pa = st.number_input("Min PA", value=0, step=5, key="b_min_pa")

        df_b = apply_team_filter(batter_stats, "BatterTeam", team_b)
        if min_pa > 0 and "PA" in df_b.columns:
            df_b = df_b[df_b["PA"] >= min_pa]

        display_cols_b = [
            "Batter", "BatterTeam", "BatterSide",
            "PA", "AtBats", "Hits",
            "BA", "OBP", "SLG", "OPS",
            "Singles", "Doubles", "Triples", "HomeRuns", "TotalBases",
            "Strikeouts", "Walks", "HBP",
            "K_pct", "BB_pct", "ZSwing_pct", "Chase_pct",
            "AvgExitVelo", "MaxExitVelo", "AvgLaunchAngle",
        ]
        display_cols_b = [c for c in display_cols_b if c in df_b.columns]

        sort_by_b = sort_col_b if sort_col_b in display_cols_b else ("OPS" if "OPS" in display_cols_b else display_cols_b[0])
        df_b = df_b[display_cols_b].sort_values(sort_by_b, ascending=False)

        st.dataframe(df_b, use_container_width=True, hide_index=True)
        st.caption(f"Showing {len(df_b)} batters  •  2026 season")

    # ─── Pitcher Game Log ──────────────────────────────────────────────────
    with tab_pg:
        st.markdown('<div class="section-header"><h3>Pitcher Game Log</h3></div>',
                    unsafe_allow_html=True)

        col1, col2, col3 = st.columns(3)
        with col1:
            seasons = sorted(
                [int(s) for s in pitcher_log["Season"].dropna().unique() if s > 0],
                reverse=True,
            )
            season_sel = st.selectbox("Season", ["All Seasons"] + [str(s) for s in seasons], key="pg_season")
        with col2:
            pg_team = st.selectbox(
                "Team",
                team_filter_options,
                index=default_index,
                format_func=lambda x: {"Auburn": "Auburn Only",
                                         "All": "All Teams",
                                         "Opponents": "Opponents Only"}[x],
                key="pg_team",
            )

        df_pg = pitcher_log.copy()
        df_pg = apply_team_filter(df_pg, "PitcherTeam", pg_team)

        with col3:
            pitcher_names = sorted(df_pg["Pitcher"].dropna().unique()) if "Pitcher" in df_pg.columns else []
            pg_pitcher = st.selectbox("Pitcher", ["All Pitchers"] + list(pitcher_names), key="pg_pitcher")

        if season_sel != "All Seasons":
            df_pg = df_pg[df_pg["Season"] == int(season_sel)]
        if pg_pitcher != "All Pitchers":
            df_pg = df_pg[df_pg["Pitcher"] == pg_pitcher]

        df_pg = df_pg.copy()
        df_pg["Date"]     = df_pg["GameDate"].apply(format_display_date)
        df_pg["_sort_dt"] = df_pg["GameDate"].apply(parse_game_date)

        display_cols_pg = [
            "Date", "Pitcher", "PitcherTeam", "Opponent",
            "Pitches", "BF", "IP",
            "AvgVelo", "Strikeouts", "Walks", "HitsAllowed",
        ]
        display_cols_pg = [c for c in display_cols_pg if c in df_pg.columns]

        df_pg = df_pg.sort_values("_sort_dt", ascending=False)[display_cols_pg]
        st.dataframe(df_pg, use_container_width=True, hide_index=True)
        st.caption(f"{len(df_pg)} game appearances")

    # ─── Batter Game Log ───────────────────────────────────────────────────
    with tab_bg:
        st.markdown('<div class="section-header"><h3>Batter Game Log</h3></div>',
                    unsafe_allow_html=True)

        col1, col2, col3 = st.columns(3)
        with col1:
            seasons = sorted(
                [int(s) for s in batter_log["Season"].dropna().unique() if s > 0],
                reverse=True,
            )
            season_sel = st.selectbox("Season", ["All Seasons"] + [str(s) for s in seasons], key="bg_season")
        with col2:
            bg_team = st.selectbox(
                "Team",
                team_filter_options,
                index=default_index,
                format_func=lambda x: {"Auburn": "Auburn Only",
                                         "All": "All Teams",
                                         "Opponents": "Opponents Only"}[x],
                key="bg_team",
            )

        df_bg = batter_log.copy()
        df_bg = apply_team_filter(df_bg, "BatterTeam", bg_team)

        with col3:
            batter_names = sorted(df_bg["Batter"].dropna().unique()) if "Batter" in df_bg.columns else []
            bg_batter = st.selectbox("Batter", ["All Batters"] + list(batter_names), key="bg_batter")

        if season_sel != "All Seasons":
            df_bg = df_bg[df_bg["Season"] == int(season_sel)]
        if bg_batter != "All Batters":
            df_bg = df_bg[df_bg["Batter"] == bg_batter]

        df_bg = df_bg.copy()
        df_bg["Date"]     = df_bg["GameDate"].apply(format_display_date)
        df_bg["_sort_dt"] = df_bg["GameDate"].apply(parse_game_date)

        display_cols_bg = [
            "Date", "Batter", "BatterTeam", "Opponent",
            "PA", "AtBats", "Hits",
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
    pill = '<span class="mode-pill">AUBURN ONLY</span>' if not scouting_mode \
           else '<span class="mode-pill mode-pill-scout">SCOUTING MODE</span>'
    st.markdown(f"## 🔎 Player Profile &nbsp; {pill}", unsafe_allow_html=True)

    player_type = st.radio("Player type", ["Pitcher", "Batter"], horizontal=True)

    # ─────── Pitcher Profile ────────────────────────────────────────────────
    if player_type == "Pitcher":
        df_src = pitcher_stats.copy()
        if not scouting_mode:
            df_src = apply_team_filter(df_src, "PitcherTeam", "Auburn")

        if df_src.empty:
            st.info("No pitchers available in this view. Toggle Scouting mode to see opponents.")
            st.stop()

        # ID-keyed dropdown — prevents name-collision splits
        if "PitcherId" in df_src.columns:
            id_to_label = {
                str(row["PitcherId"]): f'{row.get("Pitcher","?")}  ({row.get("PitcherTeam","")})'
                for _, row in df_src.iterrows()
            }
            # Stable order: by total BF desc
            order = df_src.sort_values("BF", ascending=False)["PitcherId"].astype(str).tolist() \
                        if "BF" in df_src.columns else list(id_to_label.keys())
            pid_sel = st.selectbox(
                "Select pitcher",
                order,
                format_func=lambda pid: id_to_label.get(pid, pid),
            )
            stats = df_src[df_src["PitcherId"].astype(str) == pid_sel].iloc[0]
        else:
            names = sorted(df_src["Pitcher"].dropna().unique())
            sel   = st.selectbox("Select pitcher", names)
            stats = df_src[df_src["Pitcher"] == sel].iloc[0]

        team = stats.get("PitcherTeam", "")
        hand = stats.get("PitcherThrows", "")
        st.subheader(f"{stats.get('Pitcher','?')}  •  {team}  ({hand})")

        # Row 1 — volume
        c1, c2, c3, c4, c5, c6 = st.columns(6)
        with c1: metric_card("IP",           fmt_float(stats.get("IP"), 1))
        with c2: metric_card("BF",           fmt_int(stats.get("BF")))
        with c3: metric_card("Pitches",      fmt_int(stats.get("TotalPitches")))
        with c4: metric_card("K",            fmt_int(stats.get("Strikeouts")))
        with c5: metric_card("BB",           fmt_int(stats.get("Walks")))
        with c6: metric_card("Hits Allowed", fmt_int(stats.get("HitsAllowed")))

        st.markdown("&nbsp;", unsafe_allow_html=True)

        # Row 2 — command / plate discipline
        c1, c2, c3, c4, c5, c6 = st.columns(6)
        with c1: metric_card("K%",     fmt_pct(stats.get("K_pct")))
        with c2: metric_card("BB%",    fmt_pct(stats.get("BB_pct")))
        with c3: metric_card("FPS%",   fmt_pct(stats.get("FPS_pct")))
        with c4: metric_card("Win%",   fmt_pct(stats.get("Win_pct")))
        with c5: metric_card("Edge%",  fmt_pct(stats.get("Edge_pct")))
        with c6: metric_card("GB%",    fmt_pct(stats.get("GB_pct")))

        st.markdown("&nbsp;", unsafe_allow_html=True)

        # Row 3 — stuff
        c1, c2, c3, c4, c5, c6 = st.columns(6)
        with c1: metric_card("Avg Velo",  fmt_float(stats.get("AvgVelocity")))
        with c2: metric_card("Max Velo",  fmt_float(stats.get("MaxVelocity")))
        with c3: metric_card("Avg Spin",  fmt_float(stats.get("AvgSpinRate"), 0))
        with c4: metric_card("IVB",       fmt_float(stats.get("AvgIVB")))
        with c5: metric_card("IHB",       fmt_float(stats.get("AvgIHB")))
        with c6: metric_card("Zone%",     fmt_pct(stats.get("Zone_pct")))

        # Pitch mix
        st.markdown("---")
        st.markdown("**Pitch Mix**")
        pitch_types = [
            ("FB", "Fastball"), ("SI", "Sinker"), ("CT", "Cutter"), ("SL", "Slider"),
            ("CB", "Curveball"), ("CH", "Changeup"), ("SP", "Splitter"), ("KN", "Knuckleball"),
        ]
        active = []
        for code, name in pitch_types:
            col = f"{code}Mix_pct"
            if col in stats.index and not pd.isna(stats[col]) and stats[col] > 0:
                active.append((name, stats[col]))

        if active:
            cols = st.columns(min(len(active), 8))
            for i, (name, pct) in enumerate(active):
                with cols[i % 8]:
                    metric_card(name, fmt_pct(pct))
        else:
            st.caption("No pitch-type data recorded yet.")

        # Game log
        st.markdown("---")
        st.subheader("Game Log")
        pid_str = str(stats.get("PitcherId", ""))
        if "PitcherId" in pitcher_log.columns and pid_str:
            log = pitcher_log[pitcher_log["PitcherId"].astype(str) == pid_str].copy()
        else:
            log = pitcher_log[pitcher_log["Pitcher"] == stats.get("Pitcher", "")].copy()

        if len(log):
            log["Date"]     = log["GameDate"].apply(format_display_date)
            log["_sort_dt"] = log["GameDate"].apply(parse_game_date)
            log_cols = [c for c in [
                "Date", "Opponent", "Pitches", "BF", "IP",
                "AvgVelo", "Strikeouts", "Walks", "HitsAllowed",
            ] if c in log.columns]
            st.dataframe(
                log.sort_values("_sort_dt", ascending=False)[log_cols],
                use_container_width=True, hide_index=True,
            )
        else:
            st.caption("No game-log rows yet for this pitcher.")

    # ─────── Batter Profile ─────────────────────────────────────────────────
    else:
        df_src = batter_stats.copy()
        if not scouting_mode:
            df_src = apply_team_filter(df_src, "BatterTeam", "Auburn")

        if df_src.empty:
            st.info("No batters available in this view. Toggle Scouting mode to see opponents.")
            st.stop()

        if "BatterId" in df_src.columns:
            id_to_label = {
                str(row["BatterId"]): f'{row.get("Batter","?")}  ({row.get("BatterTeam","")})'
                for _, row in df_src.iterrows()
            }
            order = df_src.sort_values("PA", ascending=False)["BatterId"].astype(str).tolist() \
                        if "PA" in df_src.columns else list(id_to_label.keys())
            bid_sel = st.selectbox(
                "Select batter",
                order,
                format_func=lambda bid: id_to_label.get(bid, bid),
            )
            stats = df_src[df_src["BatterId"].astype(str) == bid_sel].iloc[0]
        else:
            names = sorted(df_src["Batter"].dropna().unique())
            sel   = st.selectbox("Select batter", names)
            stats = df_src[df_src["Batter"] == sel].iloc[0]

        team = stats.get("BatterTeam", "")
        side = stats.get("BatterSide", "")
        st.subheader(f"{stats.get('Batter','?')}  •  {team}  ({side})")

        # Row 1 — slash line
        c1, c2, c3, c4, c5, c6 = st.columns(6)
        with c1: metric_card("PA",      fmt_int(stats.get("PA")))
        with c2: metric_card("AB",      fmt_int(stats.get("AtBats")))
        with c3: metric_card("BA",      fmt_avg(stats.get("BA")))
        with c4: metric_card("OBP",     fmt_avg(stats.get("OBP")))
        with c5: metric_card("SLG",     fmt_avg(stats.get("SLG")))
        with c6: metric_card("OPS",     fmt_avg(stats.get("OPS")))

        st.markdown("&nbsp;", unsafe_allow_html=True)

        # Row 2 — counting
        c1, c2, c3, c4, c5, c6 = st.columns(6)
        with c1: metric_card("Hits", fmt_int(stats.get("Hits")))
        with c2: metric_card("1B",   fmt_int(stats.get("Singles")))
        with c3: metric_card("2B",   fmt_int(stats.get("Doubles")))
        with c4: metric_card("3B",   fmt_int(stats.get("Triples")))
        with c5: metric_card("HR",   fmt_int(stats.get("HomeRuns")))
        with c6: metric_card("TB",   fmt_int(stats.get("TotalBases")))

        st.markdown("&nbsp;", unsafe_allow_html=True)

        # Row 3 — plate discipline + batted ball
        c1, c2, c3, c4, c5, c6 = st.columns(6)
        with c1: metric_card("K%",        fmt_pct(stats.get("K_pct")))
        with c2: metric_card("BB%",       fmt_pct(stats.get("BB_pct")))
        with c3: metric_card("Z-Swing%",  fmt_pct(stats.get("ZSwing_pct")))
        with c4: metric_card("Chase%",    fmt_pct(stats.get("Chase_pct")))
        with c5: metric_card("Avg EV",    fmt_float(stats.get("AvgExitVelo")))
        with c6: metric_card("Max EV",    fmt_float(stats.get("MaxExitVelo")))

        # Game log
        st.markdown("---")
        st.subheader("Game Log")
        bid_str = str(stats.get("BatterId", ""))
        if "BatterId" in batter_log.columns and bid_str:
            log = batter_log[batter_log["BatterId"].astype(str) == bid_str].copy()
        else:
            log = batter_log[batter_log["Batter"] == stats.get("Batter", "")].copy()

        if len(log):
            log["Date"]     = log["GameDate"].apply(format_display_date)
            log["_sort_dt"] = log["GameDate"].apply(parse_game_date)
            log_cols = [c for c in [
                "Date", "Opponent",
                "PA", "AtBats", "Hits", "Singles", "Doubles", "Triples", "HomeRuns",
                "TotalBases", "Walks", "Strikeouts", "HBP", "AvgExitVelo",
            ] if c in log.columns]
            st.dataframe(
                log.sort_values("_sort_dt", ascending=False)[log_cols],
                use_container_width=True, hide_index=True,
            )
        else:
            st.caption("No game-log rows yet for this batter.")
