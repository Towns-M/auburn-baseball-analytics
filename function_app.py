"""
Auburn Baseball — stats transform.

Reads raw TrackMan game CSVs from blob container `raw-stats`, aggregates to
pitcher / batter season stats + game logs, and writes 4 CSVs to `processed-stats`.

Checklist coverage:
- Pitchers: K%, BB%, FPS%, Win%, Edge%, GB%, Velocity (avg/max), IVB, IHB, Spin
- Batters:  AVG / OBP / SLG / OPS, K%, BB%, Z-Swing%, Chase%

All rate stats use baseball-correct denominators (BF for pitchers, PA for
batters) — not raw pitch counts.
"""
import azure.functions as func
import logging
import os
import io
import re
import threading as _threading
from collections import Counter


from azure.storage.blob import BlobServiceClient
import pandas as pd


app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)


CONN_STR = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "")
RAW_CONTAINER = "raw-stats"
PROCESSED_CONTAINER = "processed-stats"
SEASON = "2026"


_status = {"state": "idle", "msg": ""}


# ─── Columns we read from raw CSVs ───────────────────────────────────────────
KEEP_COLS = {
    "PitchNo", "Date", "Pitcher", "PitcherId", "PitcherThrows", "PitcherTeam",
    "Batter", "BatterId", "BatterSide", "BatterTeam",
    "Inning", "Top/Bottom", "Outs", "Balls", "Strikes",
    "TaggedPitchType", "AutoPitchType", "RelSpeed", "SpinRate",
    "InducedVertBreak", "HorzBreak", "PlateLocHeight", "PlateLocSide",
    "TaggedHitType", "PlayResult", "KorBB", "PitchCall",
    "ExitSpeed", "Angle", "Direction", "Distance",
    "PitcherSet", "HomeTeam", "AwayTeam",
    # Needed for cross-file pitch deduplication (TrackMan publishes each
    # game twice — live and "_v3" verified — and both pass is_game_csv).
    "GameUID", "PitchUID",
}


# ─── Pitch-type normalisation ────────────────────────────────────────────────
PITCH_TYPE_MAP = {
    "fastball": "FB", "four-seam": "FB", "fourseamfastball": "FB",
    "ff": "FB", "fa": "FB",
    "sinker": "SI", "twoseamfastball": "SI", "two-seam": "SI",
    "si": "SI", "ft": "SI",
    "cutter": "CT", "cut": "CT", "fc": "CT",
    "slider": "SL", "sl": "SL",
    "curveball": "CB", "curve": "CB", "cb": "CB", "cu": "CB",
    "12-6 curveball": "CB", "knuckle curve": "CB",
    "changeup": "CH", "change": "CH", "ch": "CH", "chs": "CH",
    "splitter": "SP", "split-finger": "SP", "fs": "SP",
    "knuckleball": "KN", "kn": "KN",
}
PITCH_BUCKETS = ["FB", "SI", "CT", "SL", "CB", "CH", "SP", "KN"]


# ─── Strike-zone geometry (standard MLB zone, height-averaged) ───────────────
ZONE_LEFT, ZONE_RIGHT = -0.83, 0.83       # ft from center of plate
ZONE_BOTTOM, ZONE_TOP = 1.5, 3.5          # ft off the ground
EDGE_BAND = 0.15                          # "edge" = within 0.15 ft of zone boundary (inside)


# ─── PitchCall sets ──────────────────────────────────────────────────────────
STRIKE_PITCHCALLS = {"StrikeCalled", "StrikeSwinging", "FoulBall", "InPlay"}
SWING_PITCHCALLS  = {"StrikeSwinging", "FoulBall", "InPlay"}


# ─── PlayResult sets ─────────────────────────────────────────────────────────
BATTER_OUT_RESULTS = {"Out", "FieldersChoice", "Sacrifice", "SacrificeFly"}
AB_OUT_RESULTS     = {"Out", "FieldersChoice", "Error"}  # "Sacrifice*" don't count as at-bats
HIT_RESULTS        = {"Single", "Double", "Triple", "HomeRun"}


# ─── Hit-type buckets (for GB%, LD%, FB%, PU%) ───────────────────────────────
GB_TYPES = {"groundball"}
LD_TYPES = {"linedrive"}
FB_TYPES = {"flyball"}
PU_TYPES = {"popup", "pop-up", "popfly"}


# File-name filter: accept only "real" game CSVs in 2026 with an 8-digit date path
GAME_DATE_RE = re.compile(r"(20\d{2})(\d{2})(\d{2})")




# ═════════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════════
def is_game_csv(blob_name: str, season: str = SEASON) -> bool:
    n = blob_name.lower()
    if not n.endswith(".csv"):
        return False
    if "_playertracking" in n or "_playerpositioning" in n:
        return False
    m = GAME_DATE_RE.search(blob_name)
    if not m:
        return False
    return m.group(1) == season




def _get_opponent(team: str, home: str, away: str) -> str:
    if not home or not away:
        return ""
    t = team.upper()
    if t == home.upper():
        return away
    if t == away.upper():
        return home
    return ""




def _annotate_pitch_flags(df: pd.DataFrame) -> pd.DataFrame:
    """Add derived boolean/categorical columns used across all aggregations."""
    if len(df) == 0:
        return df


    # Plate location → in-zone / on-edge
    pls = pd.to_numeric(df.get("PlateLocSide"),  errors="coerce") if "PlateLocSide"  in df else pd.Series(dtype=float)
    plh = pd.to_numeric(df.get("PlateLocHeight"), errors="coerce") if "PlateLocHeight" in df else pd.Series(dtype=float)
    if len(pls) and len(plh):
        in_zone = (pls >= ZONE_LEFT) & (pls <= ZONE_RIGHT) & (plh >= ZONE_BOTTOM) & (plh <= ZONE_TOP)
        on_edge = in_zone & (
            (pls <= ZONE_LEFT + EDGE_BAND) | (pls >= ZONE_RIGHT - EDGE_BAND) |
            (plh <= ZONE_BOTTOM + EDGE_BAND) | (plh >= ZONE_TOP - EDGE_BAND)
        )
        df["_in_zone"] = in_zone.fillna(False)
        df["_on_edge"] = on_edge.fillna(False)
        df["_has_loc"] = pls.notna() & plh.notna()
    else:
        df["_in_zone"] = False
        df["_on_edge"] = False
        df["_has_loc"] = False


    # Pitch outcomes
    pc = df.get("PitchCall", pd.Series([""] * len(df))).fillna("").astype(str)
    df["_is_strike"] = pc.isin(STRIKE_PITCHCALLS)
    df["_is_swing"]  = pc.isin(SWING_PITCHCALLS)
    df["_is_hbp"]    = (pc == "HitByPitch")


    # First pitch of PA (Balls==0 AND Strikes==0 before the pitch)
    if "Balls" in df.columns and "Strikes" in df.columns:
        b = pd.to_numeric(df["Balls"],  errors="coerce")
        s = pd.to_numeric(df["Strikes"], errors="coerce")
        df["_is_first_pitch"] = (b == 0) & (s == 0)
        df["_at_1_2"]        = (b == 1) & (s == 2)
    else:
        df["_is_first_pitch"] = False
        df["_at_1_2"]        = False


    # PA-ending pitch
    korbb = df.get("KorBB",     pd.Series([""] * len(df))).fillna("").astype(str)
    pr    = df.get("PlayResult", pd.Series([""] * len(df))).fillna("").astype(str)
    df["_pa_end"] = (
        korbb.isin(["Strikeout", "Walk"]) |
        df["_is_hbp"] |
        (~pr.isin(["", "Undefined"]))
    )


    # Hit-type bucket
    tht = df.get("TaggedHitType", pd.Series([""] * len(df))).fillna("").astype(str).str.lower()
    df["_ht_gb"] = tht.isin(GB_TYPES)
    df["_ht_ld"] = tht.isin(LD_TYPES)
    df["_ht_fb"] = tht.isin(FB_TYPES)
    df["_ht_pu"] = tht.isin(PU_TYPES)
    df["_bip"]   = df["_ht_gb"] | df["_ht_ld"] | df["_ht_fb"] | df["_ht_pu"]


    return df




def _pa_wins_for_pitcher(pa_pitches: pd.DataFrame) -> int:
    """1 if the PA was 'won' (count hit 1-2 OR batter retired), else 0."""
    any_1_2 = bool(pa_pitches["_at_1_2"].any())
    last = pa_pitches.iloc[-1]
    korbb_last = str(last.get("KorBB") or "")
    pr_last    = str(last.get("PlayResult") or "")
    retired = korbb_last == "Strikeout" or pr_last in BATTER_OUT_RESULTS
    return int(any_1_2 or retired)




# ═════════════════════════════════════════════════════════════════════════════
# Per-file aggregation
# ═════════════════════════════════════════════════════════════════════════════
def _init_pitcher_acc():
    return {
        # identity
        "Pitcher": "", "PitcherTeam": "", "PitcherThrows": "",
        # volume
        "TotalPitches": 0, "BF": 0,
        # velo / stuff
        "VeloSum": 0.0, "VeloCount": 0, "VeloMax": 0.0,
        "SpinSum": 0.0, "SpinCount": 0,
        "IVBSum": 0.0,  "IVBCount": 0,
        "HBSum":  0.0,  "HBCount":  0,
        # outcomes
        "Strikeouts": 0, "Walks": 0, "HBP": 0,
        "HitsAllowed": 0, "HRsAllowed": 0, "OutsRecorded": 0,
        # command / plate discipline
        "FirstPitches": 0, "FirstPitchStrikes": 0,
        "PAWins": 0, "PATotal": 0,
        "LocatedPitches": 0, "EdgePitches": 0, "InZonePitches": 0,
        # batted ball
        "BIP": 0, "GB": 0, "LD": 0, "FB": 0, "PU": 0,
        # pitch type
        **{f"{b}_Count": 0 for b in PITCH_BUCKETS},
    }




def _init_batter_acc():
    return {
        "Batter": "", "BatterTeam": "", "BatterSide": "",
        "TotalPitches": 0, "PA": 0, "AtBats": 0,
        "Strikeouts": 0, "Walks": 0, "HBP": 0,
        "Singles": 0, "Doubles": 0, "Triples": 0, "HomeRuns": 0,
        "TotalBases": 0,
        "EVSum": 0.0, "EVCount": 0, "EVMax": 0.0,
        "LASum": 0.0, "LACount": 0,
        # plate discipline
        "InZonePitches": 0, "InZoneSwings": 0,
        "OutZonePitches": 0, "OutZoneSwings": 0,
        "LocatedPitches": 0,
    }




def _update_pitcher(acc, sub, file_home, file_away):
    """Roll one file's worth of a pitcher's pitches into the accumulator."""
    n = len(sub)
    if n == 0:
        return


    # Velocity / stuff
    velos = pd.to_numeric(sub.get("RelSpeed"),        errors="coerce").dropna()
    spins = pd.to_numeric(sub.get("SpinRate"),        errors="coerce").dropna()
    ivb   = pd.to_numeric(sub.get("InducedVertBreak"), errors="coerce").dropna()
    hb    = pd.to_numeric(sub.get("HorzBreak"),       errors="coerce").dropna()


    # Outcome counts (per pitch where PA terminates)
    korbb = sub.get("KorBB",      pd.Series([""] * n)).fillna("").astype(str)
    pr    = sub.get("PlayResult", pd.Series([""] * n)).fillna("").astype(str)


    ks   = int((korbb == "Strikeout").sum())
    bbs  = int((korbb == "Walk").sum())
    hbp  = int(sub["_is_hbp"].sum())
    hits = int(pr.isin(HIT_RESULTS).sum())
    hrs  = int((pr == "HomeRun").sum())


    outs_in_play = int(pr.isin(["Out", "FieldersChoice", "Error", "Sacrifice", "SacrificeFly"]).sum())
    outs_rec     = ks + outs_in_play


    # Pitch-type buckets
    pt_counts = Counter()
    for raw_type in sub.get("TaggedPitchType", pd.Series(dtype=str)).dropna():
        bucket = PITCH_TYPE_MAP.get(str(raw_type).strip().lower())
        if bucket:
            pt_counts[bucket] += 1


    # First-pitch strikes
    first_pitches       = int(sub["_is_first_pitch"].sum())
    first_pitch_strikes = int((sub["_is_first_pitch"] & sub["_is_strike"]).sum())


    # Edge / in-zone (only count pitches with valid location data)
    located     = int(sub["_has_loc"].sum())
    in_zone     = int((sub["_in_zone"] & sub["_has_loc"]).sum())
    on_edge     = int((sub["_on_edge"] & sub["_has_loc"]).sum())


    # Batted ball
    bip = int(sub["_bip"].sum())
    gb  = int(sub["_ht_gb"].sum())
    ld  = int(sub["_ht_ld"].sum())
    fb  = int(sub["_ht_fb"].sum())
    pu  = int(sub["_ht_pu"].sum())


    # BF = number of PA-terminating pitches
    bf = int(sub["_pa_end"].sum())


    # Accumulate
    acc["TotalPitches"] += n
    acc["BF"]           += bf
    acc["VeloSum"]      += float(velos.sum())
    acc["VeloCount"]    += len(velos)
    if len(velos):
        acc["VeloMax"]  = max(acc["VeloMax"], float(velos.max()))
    acc["SpinSum"]      += float(spins.sum())
    acc["SpinCount"]    += len(spins)
    acc["IVBSum"]       += float(ivb.sum())
    acc["IVBCount"]     += len(ivb)
    acc["HBSum"]        += float(hb.sum())
    acc["HBCount"]      += len(hb)


    acc["Strikeouts"]   += ks
    acc["Walks"]        += bbs
    acc["HBP"]          += hbp
    acc["HitsAllowed"]  += hits
    acc["HRsAllowed"]   += hrs
    acc["OutsRecorded"] += outs_rec


    acc["FirstPitches"]        += first_pitches
    acc["FirstPitchStrikes"]   += first_pitch_strikes
    acc["LocatedPitches"]      += located
    acc["InZonePitches"]       += in_zone
    acc["EdgePitches"]         += on_edge


    acc["BIP"] += bip
    acc["GB"]  += gb
    acc["LD"]  += ld
    acc["FB"]  += fb
    acc["PU"]  += pu


    for b in PITCH_BUCKETS:
        acc[f"{b}_Count"] += pt_counts.get(b, 0)




def _update_batter(acc, sub):
    n = len(sub)
    if n == 0:
        return


    evs = pd.to_numeric(sub.get("ExitSpeed"), errors="coerce").dropna()
    las = pd.to_numeric(sub.get("Angle"),     errors="coerce").dropna()


    korbb = sub.get("KorBB",      pd.Series([""] * n)).fillna("").astype(str)
    pr    = sub.get("PlayResult", pd.Series([""] * n)).fillna("").astype(str)


    ks      = int((korbb == "Strikeout").sum())
    bbs     = int((korbb == "Walk").sum())
    hbp     = int(sub["_is_hbp"].sum())
    singles = int((pr == "Single").sum())
    doubles = int((pr == "Double").sum())
    triples = int((pr == "Triple").sum())
    hrs     = int((pr == "HomeRun").sum())


    in_play_outs = int(pr.isin(AB_OUT_RESULTS).sum())
    at_bats      = ks + singles + doubles + triples + hrs + in_play_outs
    total_bases  = singles + 2*doubles + 3*triples + 4*hrs


    pa_total = int(sub["_pa_end"].sum())


    # Zone discipline
    located     = int(sub["_has_loc"].sum())
    in_zone     = int((sub["_in_zone"]  & sub["_has_loc"]).sum())
    out_zone    = int((~sub["_in_zone"] & sub["_has_loc"]).sum())
    in_swings   = int((sub["_in_zone"]  & sub["_has_loc"] & sub["_is_swing"]).sum())
    out_swings  = int((~sub["_in_zone"] & sub["_has_loc"] & sub["_is_swing"]).sum())


    acc["TotalPitches"] += n
    acc["PA"]           += pa_total
    acc["AtBats"]       += at_bats
    acc["Strikeouts"]   += ks
    acc["Walks"]        += bbs
    acc["HBP"]          += hbp
    acc["Singles"]      += singles
    acc["Doubles"]      += doubles
    acc["Triples"]      += triples
    acc["HomeRuns"]     += hrs
    acc["TotalBases"]   += total_bases


    acc["EVSum"]   += float(evs.sum())
    acc["EVCount"] += len(evs)
    if len(evs):
        acc["EVMax"] = max(acc["EVMax"], float(evs.max()))
    acc["LASum"]   += float(las.sum())
    acc["LACount"] += len(las)


    acc["LocatedPitches"] += located
    acc["InZonePitches"]  += in_zone
    acc["OutZonePitches"] += out_zone
    acc["InZoneSwings"]   += in_swings
    acc["OutZoneSwings"]  += out_swings




def _norm_pid(raw, name_fallback=None):
    """Canonicalize a PitcherId / BatterId across files.

    - NaN / empty  -> fall back to 'NAME::<UPPERCASE NAME>' if provided
    - numeric-like -> int-string form ('12345.0' -> '12345')
    - otherwise    -> stripped string
    Returns None if the row cannot be identified at all.
    """
    try:
        if pd.isna(raw):
            if name_fallback and str(name_fallback).strip():
                return "NAME::" + str(name_fallback).strip().upper()
            return None
    except Exception:
        pass
    s = str(raw).strip()
    if s == "" or s.lower() == "nan":
        if name_fallback and str(name_fallback).strip():
            return "NAME::" + str(name_fallback).strip().upper()
        return None
    try:
        return str(int(float(s)))
    except Exception:
        return s




def _merge_by_name(acc_dict, name_field, team_field):
    """Collapse accumulators that share the same (name, team) tuple.

    Handles the rare case where TrackMan re-assigned an ID to the same
    human mid-season. Numeric fields are summed; identity fields are kept
    from the first entry seen.
    """
    canon = {}
    order = []
    for key, v in acc_dict.items():
        ident = (str(v.get(name_field, "")).upper().strip(),
                 str(v.get(team_field, "")).upper().strip())
        if not ident[0]:
            canon[key] = v
            order.append(key)
            continue
        if ident not in canon:
            canon[ident] = v
            order.append(ident)
        else:
            for k, val in v.items():
                cur = canon[ident].get(k)
                if isinstance(val, (int, float)) and isinstance(cur, (int, float)):
                    canon[ident][k] = cur + val
    # Return a dict keyed by unique string keys so downstream code is unchanged.
    out = {}
    for i, k in enumerate(order):
        out[f"{i}"] = canon[k]
    return out
# ═════════════════════════════════════════════════════════════════════════════
# Main transform
# ═════════════════════════════════════════════════════════════════════════════
def run_transform():
    if not CONN_STR:
        raise ValueError("AZURE_STORAGE_CONNECTION_STRING is not set")


    client = BlobServiceClient.from_connection_string(CONN_STR)
    raw    = client.get_container_client(RAW_CONTAINER)


    blobs = [b.name for b in raw.list_blobs() if is_game_csv(b.name)]
    total = len(blobs)
    logging.info(f"Found {total} game CSV files for {SEASON}")


    # Accumulators keyed by player ID string
    pitch_acc = {}   # key = str(PitcherId)
    bat_acc   = {}   # key = str(BatterId)
    pitch_game_rows = []
    bat_game_rows   = []


    errors = 0
    seen_pitch_keys: set = set()   # cross-file pitch dedup (see below)
    dup_pitches_dropped = 0
    for i, blob_name in enumerate(blobs):
        try:
            raw_bytes = raw.get_blob_client(blob_name).download_blob().readall()
            df = pd.read_csv(
                io.BytesIO(raw_bytes),
                usecols=lambda c: c in KEEP_COLS,
                low_memory=False,
            )
            if len(df) == 0:
                continue


            # ─── Cross-file pitch dedup ─────────────────────────────────
            # TrackMan publishes most games twice (live "<gameID>.csv" +
            # verified "<gameID>_v3.csv"). Both pass is_game_csv() and
            # contain identical pitch rows. Without this, every pitch in
            # such a game is counted twice, roughly doubling every pitcher
            # and batter season total.
            if "PitchUID" in df.columns and df["PitchUID"].notna().any():
                key_col = df["PitchUID"].fillna("").astype(str)
            elif {"GameUID", "PitchNo"}.issubset(df.columns):
                key_col = (df["GameUID"].fillna("").astype(str)
                           + "::" + df["PitchNo"].astype(str))
            else:
                key_col = None

            if key_col is not None:
                mask = (~key_col.isin(seen_pitch_keys)) & (key_col != "")
                dropped = int((~mask).sum())
                if dropped:
                    dup_pitches_dropped += dropped
                    logging.info(
                        f"  dedup: dropped {dropped}/{len(df)} duplicate pitches from {blob_name}"
                    )
                df = df.loc[mask].reset_index(drop=True)
                if len(df) == 0:
                    continue
                seen_pitch_keys.update(key_col[mask].tolist())


            # Deterministic order within the file
            if "PitchNo" in df.columns:
                df = df.sort_values("PitchNo", kind="mergesort").reset_index(drop=True)


            df = _annotate_pitch_flags(df)


            # Date from blob path
            m = GAME_DATE_RE.search(blob_name)
            game_date = m.group(1) + m.group(2) + m.group(3) if m else "unknown"


            # Home / away
            file_home = str(df["HomeTeam"].dropna().iloc[0]).strip() if "HomeTeam" in df.columns and df["HomeTeam"].notna().any() else ""
            file_away = str(df["AwayTeam"].dropna().iloc[0]).strip() if "AwayTeam" in df.columns and df["AwayTeam"].notna().any() else ""
            is_game   = file_home and file_away and file_home.upper() != file_away.upper()


            # ─── Pitcher aggregation ────────────────────────────────────────
            if "PitcherId" in df.columns:
                for pid, sub in df.groupby("PitcherId", dropna=False):
                    name_hint = None
                    if "Pitcher" in sub.columns and sub["Pitcher"].notna().any():
                        name_hint = sub["Pitcher"].dropna().iloc[0]
                    key = _norm_pid(pid, name_fallback=name_hint)
                    if key is None:
                        continue  # skip truly unidentifiable pitches
                    acc = pitch_acc.setdefault(key, _init_pitcher_acc())


                    # Identity — latest non-empty wins (stable for single-team players)
                    for field, col in [("Pitcher", "Pitcher"),
                                        ("PitcherTeam", "PitcherTeam"),
                                        ("PitcherThrows", "PitcherThrows")]:
                        if col in sub.columns:
                            s = sub[col].dropna()
                            if len(s):
                                acc[field] = str(s.iloc[0]).strip()


                    _update_pitcher(acc, sub, file_home, file_away)


                    # Game log row
                    if is_game:
                        velos = pd.to_numeric(sub.get("RelSpeed"), errors="coerce").dropna()
                        ks   = int((sub.get("KorBB", pd.Series([""] * len(sub))).fillna("").astype(str) == "Strikeout").sum())
                        bbs  = int((sub.get("KorBB", pd.Series([""] * len(sub))).fillna("").astype(str) == "Walk").sum())
                        hits = int(sub.get("PlayResult", pd.Series([""] * len(sub))).fillna("").astype(str).isin(HIT_RESULTS).sum())
                        outs_in_play = int(sub.get("PlayResult", pd.Series([""] * len(sub))).fillna("").astype(str).isin(
                            ["Out", "FieldersChoice", "Error", "Sacrifice", "SacrificeFly"]
                        ).sum())
                        bf_game = int(sub["_pa_end"].sum())


                        pitch_game_rows.append({
                            "PitcherId":   key,
                            "Pitcher":     acc["Pitcher"],
                            "PitcherTeam": acc["PitcherTeam"],
                            "GameDate":    game_date,
                            "Opponent":    _get_opponent(acc["PitcherTeam"], file_home, file_away),
                            "Pitches":     len(sub),
                            "BF":          bf_game,
                            "AvgVelo":     round(float(velos.mean()), 2) if len(velos) else None,
                            "Strikeouts":  ks,
                            "Walks":       bbs,
                            "HitsAllowed": hits,
                            "OutsRecorded": ks + outs_in_play,
                        })


            # ─── Per-PA Win% (pitcher perspective) ───────────────────────────
            pa_cols = ["PitcherId", "BatterId", "Inning", "Top/Bottom"]
            if all(c in df.columns for c in pa_cols):
                for pa_keys, pa_pitches in df.groupby(pa_cols, dropna=False):
                    # Use the same normalization so we hit the real accumulator
                    name_hint = None
                    if "Pitcher" in pa_pitches.columns and pa_pitches["Pitcher"].notna().any():
                        name_hint = pa_pitches["Pitcher"].dropna().iloc[0]
                    pid = _norm_pid(pa_keys[0], name_fallback=name_hint)
                    if pid is None or pid not in pitch_acc:
                        continue
                    # Only count PAs that actually ended (ignore interrupted innings)
                    if not bool(pa_pitches["_pa_end"].any()):
                        continue
                    pitch_acc[pid]["PATotal"] += 1
                    pitch_acc[pid]["PAWins"]  += _pa_wins_for_pitcher(pa_pitches)


            # ─── Batter aggregation ─────────────────────────────────────────
            if "BatterId" in df.columns:
                for bid, sub in df.groupby("BatterId", dropna=False):
                    name_hint = None
                    if "Batter" in sub.columns and sub["Batter"].notna().any():
                        name_hint = sub["Batter"].dropna().iloc[0]
                    key = _norm_pid(bid, name_fallback=name_hint)
                    if key is None:
                        continue
                    acc = bat_acc.setdefault(key, _init_batter_acc())


                    for field, col in [("Batter", "Batter"),
                                        ("BatterTeam", "BatterTeam"),
                                        ("BatterSide", "BatterSide")]:
                        if col in sub.columns:
                            s = sub[col].dropna()
                            if len(s):
                                acc[field] = str(s.iloc[0]).strip()


                    _update_batter(acc, sub)


                    if is_game:
                        evs = pd.to_numeric(sub.get("ExitSpeed"), errors="coerce").dropna()
                        korbb = sub.get("KorBB",      pd.Series([""] * len(sub))).fillna("").astype(str)
                        pr    = sub.get("PlayResult", pd.Series([""] * len(sub))).fillna("").astype(str)
                        ks   = int((korbb == "Strikeout").sum())
                        bbs  = int((korbb == "Walk").sum())
                        singles = int((pr == "Single").sum())
                        doubles = int((pr == "Double").sum())
                        triples = int((pr == "Triple").sum())
                        hrs     = int((pr == "HomeRun").sum())
                        in_play_outs = int(pr.isin(AB_OUT_RESULTS).sum())
                        at_bats      = ks + singles + doubles + triples + hrs + in_play_outs


                        bat_game_rows.append({
                            "BatterId":   key,
                            "Batter":     acc["Batter"],
                            "BatterTeam": acc["BatterTeam"],
                            "GameDate":   game_date,
                            "Opponent":   _get_opponent(acc["BatterTeam"], file_home, file_away),
                            "Pitches":    len(sub),
                            "PA":         int(sub["_pa_end"].sum()),
                            "AtBats":     at_bats,
                            "Hits":       singles + doubles + triples + hrs,
                            "Singles":    singles,
                            "Doubles":    doubles,
                            "Triples":    triples,
                            "HomeRuns":   hrs,
                            "TotalBases": singles + 2*doubles + 3*triples + 4*hrs,
                            "Walks":      bbs,
                            "Strikeouts": ks,
                            "HBP":        int(sub["_is_hbp"].sum()),
                            "AvgExitVelo": round(float(evs.mean()), 2) if len(evs) else None,
                        })


        except Exception as e:
            logging.warning(f"Skipping {blob_name}: {e}")
            errors += 1


        if (i + 1) % 200 == 0:
            logging.info(f"Progress: {i+1}/{total} files, {errors} errors, "
                         f"{len(pitch_acc)} pitchers, {len(bat_acc)} batters")


    logging.info(
        f"Done reading. {errors} errors, {dup_pitches_dropped} duplicate pitches dropped. "
        f"Building output DataFrames..."
    )


    # ═══════════════════════════════════════════════════════════════════════
    # Build pitcher_stats.csv
    # Collapse same-player duplicates that slipped through with different IDs
    pitch_acc = _merge_by_name(pitch_acc, "Pitcher", "PitcherTeam")
    bat_acc   = _merge_by_name(bat_acc,   "Batter",  "BatterTeam")
    # ═══════════════════════════════════════════════════════════════════════
    p_rows = []
    for pid, a in pitch_acc.items():
        p_rows.append({"PitcherId": pid, **a})
    p_df = pd.DataFrame(p_rows)


    if len(p_df):
        safe = lambda num, den: (num / den.replace(0, float("nan")))


        p_df["IP"]           = (p_df["OutsRecorded"] / 3).round(1)
        p_df["AvgVelocity"]  = (safe(p_df["VeloSum"], p_df["VeloCount"])).round(2)
        p_df["MaxVelocity"]  =  p_df["VeloMax"].round(2)
        p_df["AvgSpinRate"]  = (safe(p_df["SpinSum"], p_df["SpinCount"])).round(1)
        p_df["AvgIVB"]       = (safe(p_df["IVBSum"],  p_df["IVBCount"])).round(2)
        p_df["AvgIHB"]       = (safe(p_df["HBSum"],   p_df["HBCount"])).round(2)


        # Baseball-correct rate stats — use BF / PA denominators
        p_df["K_pct"]        = (safe(p_df["Strikeouts"], p_df["BF"]) * 100).round(1)
        p_df["BB_pct"]       = (safe(p_df["Walks"],      p_df["BF"]) * 100).round(1)
        p_df["FPS_pct"]      = (safe(p_df["FirstPitchStrikes"], p_df["FirstPitches"]) * 100).round(1)
        p_df["Win_pct"]      = (safe(p_df["PAWins"],     p_df["PATotal"])          * 100).round(1)
        p_df["Edge_pct"]     = (safe(p_df["EdgePitches"], p_df["LocatedPitches"])  * 100).round(1)
        p_df["Zone_pct"]     = (safe(p_df["InZonePitches"], p_df["LocatedPitches"]) * 100).round(1)
        p_df["GB_pct"]       = (safe(p_df["GB"], p_df["BIP"]) * 100).round(1)
        p_df["LD_pct"]       = (safe(p_df["LD"], p_df["BIP"]) * 100).round(1)
        p_df["FB_pct"]       = (safe(p_df["FB"], p_df["BIP"]) * 100).round(1)
        p_df["PU_pct"]       = (safe(p_df["PU"], p_df["BIP"]) * 100).round(1)


        # Pitch-mix percentages
        for b in PITCH_BUCKETS:
            p_df[f"{b}Mix_pct"] = (safe(p_df[f"{b}_Count"], p_df["TotalPitches"]) * 100).round(1)


        drop_cols = (
            ["VeloSum","VeloCount","VeloMax","SpinSum","SpinCount",
             "IVBSum","IVBCount","HBSum","HBCount",
             "FirstPitches","FirstPitchStrikes","PAWins","PATotal",
             "LocatedPitches","EdgePitches","InZonePitches"]
            + [f"{b}_Count" for b in PITCH_BUCKETS]
        )
        p_df = p_df.drop(columns=[c for c in drop_cols if c in p_df.columns])


        # Column order for readability
        lead = ["PitcherId", "Pitcher", "PitcherTeam", "PitcherThrows",
                "TotalPitches", "BF", "IP", "OutsRecorded",
                "Strikeouts", "Walks", "HBP", "HitsAllowed", "HRsAllowed",
                "K_pct", "BB_pct", "FPS_pct", "Win_pct", "Edge_pct", "Zone_pct",
                "AvgVelocity", "MaxVelocity", "AvgSpinRate", "AvgIVB", "AvgIHB",
                "BIP", "GB", "LD", "FB", "PU",
                "GB_pct", "LD_pct", "FB_pct", "PU_pct"]
        rest = [c for c in p_df.columns if c not in lead]
        p_df = p_df[[c for c in lead if c in p_df.columns] + rest]


    # ═══════════════════════════════════════════════════════════════════════
    # Build batter_stats.csv
    # ═══════════════════════════════════════════════════════════════════════
    b_rows = []
    for bid, a in bat_acc.items():
        b_rows.append({"BatterId": bid, **a})
    b_df = pd.DataFrame(b_rows)


    if len(b_df):
        safe = lambda num, den: (num / den.replace(0, float("nan")))


        b_df["Hits"]          = b_df["Singles"] + b_df["Doubles"] + b_df["Triples"] + b_df["HomeRuns"]
        b_df["AvgExitVelo"]   = (safe(b_df["EVSum"], b_df["EVCount"])).round(2)
        b_df["MaxExitVelo"]   =  b_df["EVMax"].round(2)
        b_df["AvgLaunchAngle"]= (safe(b_df["LASum"], b_df["LACount"])).round(2)
        b_df["BA"]            = (safe(b_df["Hits"],       b_df["AtBats"])).round(3)
        b_df["OBP"]           = (safe(
                                   b_df["Hits"] + b_df["Walks"] + b_df["HBP"],
                                   b_df["AtBats"] + b_df["Walks"] + b_df["HBP"]
                                )).round(3)
        b_df["SLG"]           = (safe(b_df["TotalBases"], b_df["AtBats"])).round(3)
        b_df["OPS"]           = (b_df["OBP"] + b_df["SLG"]).round(3)


        # PA-based rate stats
        b_df["K_pct"]         = (safe(b_df["Strikeouts"], b_df["PA"]) * 100).round(1)
        b_df["BB_pct"]        = (safe(b_df["Walks"],      b_df["PA"]) * 100).round(1)


        # Plate discipline
        b_df["ZSwing_pct"]    = (safe(b_df["InZoneSwings"],  b_df["InZonePitches"])  * 100).round(1)
        b_df["Chase_pct"]     = (safe(b_df["OutZoneSwings"], b_df["OutZonePitches"]) * 100).round(1)


        b_df = b_df.drop(columns=["EVSum","EVCount","EVMax","LASum","LACount",
                                    "InZoneSwings","InZonePitches",
                                    "OutZoneSwings","OutZonePitches",
                                    "LocatedPitches"],
                          errors="ignore")


        lead = ["BatterId", "Batter", "BatterTeam", "BatterSide",
                "TotalPitches", "PA", "AtBats",
                "Hits", "Singles", "Doubles", "Triples", "HomeRuns", "TotalBases",
                "BA", "OBP", "SLG", "OPS",
                "Strikeouts", "Walks", "HBP", "K_pct", "BB_pct",
                "ZSwing_pct", "Chase_pct",
                "AvgExitVelo", "MaxExitVelo", "AvgLaunchAngle"]
        rest = [c for c in b_df.columns if c not in lead]
        b_df = b_df[[c for c in lead if c in b_df.columns] + rest]


    # ═══════════════════════════════════════════════════════════════════════
    # Game logs
    # ═══════════════════════════════════════════════════════════════════════
    pg_df = pd.DataFrame(pitch_game_rows) if pitch_game_rows else pd.DataFrame()
    bg_df = pd.DataFrame(bat_game_rows)   if bat_game_rows   else pd.DataFrame()


    if not pg_df.empty and "OutsRecorded" in pg_df.columns:
        pg_df["IP"] = (pg_df["OutsRecorded"] / 3).round(1)


    # ═══════════════════════════════════════════════════════════════════════
    # Upload
    # ═══════════════════════════════════════════════════════════════════════
    proc = client.get_container_client(PROCESSED_CONTAINER)


    def _upload(df, name):
        if df.empty:
            logging.warning(f"{name} is empty, skipping upload")
            return
        buf = io.StringIO()
        df.to_csv(buf, index=False)
        data = buf.getvalue().encode("utf-8")
        proc.get_blob_client(name).upload_blob(data, overwrite=True)
        logging.info(f"Uploaded {name}: {len(df)} rows, {len(data):,} bytes")


    _upload(p_df,  "pitcher_stats.csv")
    _upload(b_df,  "batter_stats.csv")
    _upload(pg_df, "pitcher_game_log.csv")
    _upload(bg_df, "batter_game_log.csv")


    return p_df, b_df




# ═════════════════════════════════════════════════════════════════════════════
# HTTP endpoints (unchanged contract)
# ═════════════════════════════════════════════════════════════════════════════
@app.route(route="transform", methods=["GET", "POST"])
def transform(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("Auburn Baseball Transform triggered.")
    if _status["state"] == "running":
        return func.HttpResponse("Transform already running", status_code=200)


    def _run():
        _status["state"] = "running"
        _status["msg"]   = "running"
        try:
            p, b = run_transform()
            _status["msg"] = f"OK pitchers={len(p)} batters={len(b)}"
        except Exception as e:
            logging.error(f"Transform failed: {e}", exc_info=True)
            _status["msg"] = f"ERROR: {e}"
        finally:
            _status["state"] = "idle"


    _threading.Thread(target=_run, daemon=True).start()
    return func.HttpResponse("Transform started", status_code=202)




@app.route(route="status", methods=["GET"])
def status(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse(f"{_status['state']}: {_status['msg']}", status_code=200)
