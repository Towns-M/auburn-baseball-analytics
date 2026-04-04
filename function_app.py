import azure.functions as func
import logging
import os
import io
import re
import threading as _threading

from azure.storage.blob import BlobServiceClient
import pandas as pd

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

CONN_STR = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "")
RAW_CONTAINER = "raw-stats"
PROCESSED_CONTAINER = "processed-stats"

_status = {"state": "idle", "msg": ""}

# Only columns we actually need (subset of 167 Trackman columns)
KEEP_COLS = {
    "PitchNo", "Date", "Pitcher", "PitcherId", "PitcherThrows", "PitcherTeam",
    "Batter", "BatterId", "BatterSide", "BatterTeam",
    "Inning", "Top/Bottom", "Outs", "Balls", "Strikes",
    "TaggedPitchType", "AutoPitchType", "RelSpeed", "SpinRate",
    "InducedVertBreak", "HorzBreak", "PlateLocHeight", "PlateLocSide",
    "TaggedHitType", "PlayResult", "KorBB", "PitchCall",
    "ExitSpeed", "Angle", "Direction", "Distance",
    "PitcherSet", "HomeTeam", "AwayTeam",
}

# Pitch type normalisation â canonical bucket
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


def is_game_csv(blob_name: str) -> bool:
    n = blob_name.lower()
    return (
        n.endswith(".csv")
        and "_playertracking" not in n
        and "_playerpositioning" not in n
    )


def _safe_mean(values):
    v = pd.to_numeric(values, errors="coerce").dropna()
    return round(float(v.mean()), 2) if len(v) > 0 else None


def _first_val(series):
    """Return the first non-null value from a series as a string, or ''."""
    s = series.dropna()
    return str(s.iloc[0]).strip() if len(s) > 0 else ""


def _get_opponent(team: str, home: str, away: str) -> str:
    if not home or not away:
        return ""
    if team.upper() == home.upper():
        return away
    if team.upper() == away.upper():
        return home
    return ""


def run_transform():
    if not CONN_STR:
        raise ValueError("AZURE_STORAGE_CONNECTION_STRING is not set")

    client = BlobServiceClient.from_connection_string(CONN_STR)
    raw = client.get_container_client(RAW_CONTAINER)

    blobs = [b.name for b in raw.list_blobs() if is_game_csv(b.name)]
    total = len(blobs)
    logging.info(f"Found {total} game CSV files to process")

    # --- Streaming accumulators ---
    pitch_acc = {}   # keyed by (PitcherId, Pitcher, PitcherTeam, PitcherThrows)
    bat_acc = {}     # keyed by (BatterId, Batter, BatterTeam, BatterSide)
    pitch_game_rows = []
    bat_game_rows = []

    errors = 0
    for i, blob_name in enumerate(blobs):
        try:
            raw_bytes = raw.get_blob_client(blob_name).download_blob().readall()
            df = pd.read_csv(
                io.BytesIO(raw_bytes),
                usecols=lambda c: c in KEEP_COLS,
                low_memory=False,
            )

            # Extract 8-digit date from blob name path
            m = re.search(r"(\d{8})", blob_name)
            game_date = m.group(1) if m else "unknown"

            # Game-level home/away (consistent across all rows in a file)
            file_home = _first_val(df["HomeTeam"]) if "HomeTeam" in df.columns else ""
            file_away = _first_val(df["AwayTeam"]) if "AwayTeam" in df.columns else ""
            is_game = file_home != "" and file_away != "" and file_home.upper() != file_away.upper()

            # ---- PITCHER aggregation ----------------------------------------
            for keys, sub in df.groupby(
                ["PitcherId", "Pitcher", "PitcherTeam", "PitcherThrows"],
                dropna=False,
            ):
                pid, pname, pteam, pthrows = keys
                key = (str(pid), str(pname), str(pteam), str(pthrows))

                velos = pd.to_numeric(sub.get("RelSpeed", pd.Series(dtype=float)), errors="coerce").dropna()
                spins = pd.to_numeric(sub.get("SpinRate", pd.Series(dtype=float)), errors="coerce").dropna()
                ivb   = pd.to_numeric(sub.get("InducedVertBreak", pd.Series(dtype=float)), errors="coerce").dropna()
                hb    = pd.to_numeric(sub.get("HorzBreak", pd.Series(dtype=float)), errors="coerce").dropna()

                ks   = int((sub.get("KorBB",      pd.Series(dtype=str)) == "Strikeout").sum())
                bbs  = int((sub.get("KorBB",      pd.Series(dtype=str)) == "Walk").sum())
                hits = int(sub.get("PlayResult",   pd.Series(dtype=str)).isin(["Single","Double","Triple","HomeRun"]).sum())
                hrs  = int((sub.get("PlayResult",  pd.Series(dtype=str)) == "HomeRun").sum())

                # Outs recorded: K + in-play outs (used for IP)
                outs_in_play = int(sub.get("PlayResult", pd.Series(dtype=str)).isin(
                    ["Out", "FieldersChoice", "Error", "Sacrifice", "SacrificeFly"]
                ).sum())
                outs_rec = ks + outs_in_play

                # Pitch type counts
                pt_counts = {b: 0 for b in PITCH_BUCKETS}
                if "TaggedPitchType" in sub.columns:
                    for raw_type in sub["TaggedPitchType"].dropna():
                        bucket = PITCH_TYPE_MAP.get(str(raw_type).strip().lower(), None)
                        if bucket:
                            pt_counts[bucket] += 1

                # Opponent
                opp = _get_opponent(str(pteam), file_home, file_away)

                if key not in pitch_acc:
                    pitch_acc[key] = {
                        "PitcherId": str(pid), "Pitcher": str(pname),
                        "PitcherTeam": str(pteam), "PitcherThrows": str(pthrows),
                        "TotalPitches": 0,
                        "VeloSum": 0.0, "VeloCount": 0, "VeloMax": 0.0,
                        "SpinSum": 0.0, "SpinCount": 0,
                        "IVBSum": 0.0, "IVBCount": 0,
                        "HBSum": 0.0, "HBCount": 0,
                        "Strikeouts": 0, "Walks": 0,
                        "HitsAllowed": 0, "HRsAllowed": 0,
                        "OutsRecorded": 0,
                        **{f"{b}_Count": 0 for b in PITCH_BUCKETS},
                    }
                r = pitch_acc[key]
                r["TotalPitches"]  += len(sub)
                r["VeloSum"]       += float(velos.sum())
                r["VeloCount"]     += len(velos)
                if len(velos) > 0:
                    r["VeloMax"]    = max(r["VeloMax"], float(velos.max()))
                r["SpinSum"]       += float(spins.sum())
                r["SpinCount"]     += len(spins)
                r["IVBSum"]        += float(ivb.sum())
                r["IVBCount"]      += len(ivb)
                r["HBSum"]         += float(hb.sum())
                r["HBCount"]       += len(hb)
                r["Strikeouts"]    += ks
                r["Walks"]         += bbs
                r["HitsAllowed"]   += hits
                r["HRsAllowed"]    += hrs
                r["OutsRecorded"]  += outs_rec
                for b in PITCH_BUCKETS:
                    r[f"{b}_Count"] += pt_counts[b]

                # Pitcher game log row (only real games)
                if is_game:
                    pitch_game_rows.append({
                        "PitcherId":   str(pid),
                        "Pitcher":     str(pname),
                        "PitcherTeam": str(pteam),
                        "GameDate":    game_date,
                        "Opponent":    opp,
                        "Pitches":     len(sub),
                        "AvgVelo":     round(float(velos.mean()), 2) if len(velos) > 0 else None,
                        "Strikeouts":  ks,
                        "Walks":       bbs,
                        "HitsAllowed": hits,
                        "OutsRecorded": outs_rec,
                    })

            # ---- BATTER aggregation -----------------------------------------
            for keys, sub in df.groupby(
                ["BatterId", "Batter", "BatterTeam", "BatterSide"],
                dropna=False,
            ):
                bid, bname, bteam, bside = keys
                key = (str(bid), str(bname), str(bteam), str(bside))

                evs = pd.to_numeric(sub.get("ExitSpeed", pd.Series(dtype=float)), errors="coerce").dropna()
                las = pd.to_numeric(sub.get("Angle",     pd.Series(dtype=float)), errors="coerce").dropna()

                ks      = int((sub.get("KorBB",      pd.Series(dtype=str)) == "Strikeout").sum())
                bbs     = int((sub.get("KorBB",      pd.Series(dtype=str)) == "Walk").sum())
                singles = int((sub.get("PlayResult",  pd.Series(dtype=str)) == "Single").sum())
                doubles = int((sub.get("PlayResult",  pd.Series(dtype=str)) == "Double").sum())
                triples = int((sub.get("PlayResult",  pd.Series(dtype=str)) == "Triple").sum())
                hrs     = int((sub.get("PlayResult",  pd.Series(dtype=str)) == "HomeRun").sum())
                hbp     = int((sub.get("PitchCall",   pd.Series(dtype=str)) == "HitByPitch").sum()) if "PitchCall" in sub.columns else 0

                # In-play outs (for AtBat calculation)
                in_play_outs = int(sub.get("PlayResult", pd.Series(dtype=str)).isin(
                    ["Out", "FieldersChoice", "Error"]
                ).sum())

                at_bats     = ks + singles + doubles + triples + hrs + in_play_outs
                total_bases = singles + 2*doubles + 3*triples + 4*hrs

                # Opponent
                opp = _get_opponent(str(bteam), file_home, file_away)

                if key not in bat_acc:
                    bat_acc[key] = {
                        "BatterId": str(bid), "Batter": str(bname),
                        "BatterTeam": str(bteam), "BatterSide": str(bside),
                        "TotalPitches": 0,
                        "Strikeouts": 0, "Walks": 0, "HBP": 0,
                        "Singles": 0, "Doubles": 0, "Triples": 0, "HomeRuns": 0,
                        "AtBats": 0, "TotalBases": 0,
                        "EVSum": 0.0, "EVCount": 0,
                        "LASum": 0.0, "LACount": 0,
                    }
                r = bat_acc[key]
                r["TotalPitches"] += len(sub)
                r["Strikeouts"]   += ks
                r["Walks"]        += bbs
                r["HBP"]          += hbp
                r["Singles"]      += singles
                r["Doubles"]      += doubles
                r["Triples"]      += triples
                r["HomeRuns"]     += hrs
                r["AtBats"]       += at_bats
                r["TotalBases"]   += total_bases
                r["EVSum"]        += float(evs.sum())
                r["EVCount"]      += len(evs)
                r["LASum"]        += float(las.sum())
                r["LACount"]      += len(las)

                # Batter game log row (only real games)
                if is_game:
                    bat_game_rows.append({
                        "BatterId":   str(bid),
                        "Batter":     str(bname),
                        "BatterTeam": str(bteam),
                        "GameDate":   game_date,
                        "Opponent":   opp,
                        "Pitches":    len(sub),
                        "Strikeouts": ks,
                        "Walks":      bbs,
                        "Singles":    singles,
                        "Doubles":    doubles,
                        "Triples":    triples,
                        "HomeRuns":   hrs,
                        "Hits":       singles + doubles + triples + hrs,
                        "AtBats":     at_bats,
                        "TotalBases": total_bases,
                        "HBP":        hbp,
                        "AvgExitVelo": round(float(evs.mean()), 2) if len(evs) > 0 else None,
                    })

        except Exception as e:
            logging.warning(f"Skipping {blob_name}: {e}")
            errors += 1

        if (i + 1) % 200 == 0:
            logging.info(f"Progress: {i+1}/{total} files, {errors} errors, "
                         f"{len(pitch_acc)} pitchers, {len(bat_acc)} batters")

    logging.info(f"Done reading. {errors} errors. Building output DataFrames...")

    # ---- Build pitcher_stats.csv ----------------------------------------
    p_df = pd.DataFrame(list(pitch_acc.values()))
    if len(p_df) > 0:
        p_df["AvgVelocity"]  = (p_df["VeloSum"] / p_df["VeloCount"].replace(0, float("nan"))).round(2)
        p_df["MaxVelocity"]  = p_df["VeloMax"].round(2)
        p_df["AvgSpinRate"]  = (p_df["SpinSum"] / p_df["SpinCount"].replace(0, float("nan"))).round(1)
        p_df["AvgVertBreak"] = (p_df["IVBSum"]  / p_df["IVBCount"].replace(0, float("nan"))).round(2)
        p_df["AvgHorzBreak"] = (p_df["HBSum"]   / p_df["HBCount"].replace(0, float("nan"))).round(2)
        p_df["IP"]           = (p_df["OutsRecorded"] / 3).round(1)
        p_df["K_pct"]        = (p_df["Strikeouts"] / p_df["TotalPitches"].replace(0, float("nan")) * 100).round(1)
        p_df["BB_pct"]       = (p_df["Walks"]      / p_df["TotalPitches"].replace(0, float("nan")) * 100).round(1)

        # Pitch type percentages
        for b in PITCH_BUCKETS:
            col = f"{b}_pct"
            p_df[col] = (p_df[f"{b}_Count"] / p_df["TotalPitches"].replace(0, float("nan")) * 100).round(1)

        # Drop raw accumulator columns
        drop_cols = (
            ["VeloSum","VeloCount","VeloMax","SpinSum","SpinCount",
             "IVBSum","IVBCount","HBSum","HBCount","OutsRecorded"]
            + [f"{b}_Count" for b in PITCH_BUCKETS]
        )
        p_df = p_df.drop(columns=[c for c in drop_cols if c in p_df.columns])

    # ---- Build batter_stats.csv -----------------------------------------
    b_df = pd.DataFrame(list(bat_acc.values()))
    if len(b_df) > 0:
        b_df["Hits"]          = b_df["Singles"] + b_df["Doubles"] + b_df["Triples"] + b_df["HomeRuns"]
        b_df["AvgExitVelo"]   = (b_df["EVSum"] / b_df["EVCount"].replace(0, float("nan"))).round(2)
        b_df["AvgLaunchAngle"]= (b_df["LASum"] / b_df["LACount"].replace(0, float("nan"))).round(2)
        b_df["BA"]            = (b_df["Hits"] / b_df["AtBats"].replace(0, float("nan"))).round(3)
        b_df["OBP"]           = (
            (b_df["Hits"] + b_df["Walks"] + b_df["HBP"]) /
            (b_df["AtBats"] + b_df["Walks"] + b_df["HBP"]).replace(0, float("nan"))
        ).round(3)
        b_df["SLG"]           = (b_df["TotalBases"] / b_df["AtBats"].replace(0, float("nan"))).round(3)
        b_df["OPS"]           = (b_df["OBP"] + b_df["SLG"]).round(3)
        b_df["K_pct"]         = (b_df["Strikeouts"] / b_df["TotalPitches"].replace(0, float("nan")) * 100).round(1)
        b_df["BB_pct"]        = (b_df["Walks"]      / b_df["TotalPitches"].replace(0, float("nan")) * 100).round(1)
        b_df = b_df.drop(columns=["EVSum","EVCount","LASum","LACount"])

    # ---- Build game logs ------------------------------------------------
    pg_df = pd.DataFrame(pitch_game_rows) if pitch_game_rows else pd.DataFrame()
    bg_df = pd.DataFrame(bat_game_rows)   if bat_game_rows   else pd.DataFrame()

    # Add IP to pitcher game log
    if not pg_df.empty and "OutsRecorded" in pg_df.columns:
        pg_df["IP"] = (pg_df["OutsRecorded"] / 3).round(1)

    # Upload all 4 CSVs
    proc = client.get_container_client(PROCESSED_CONTAINER)

    def _upload(df, name):
        if df.empty:
            logging.warning(f"{name} is empty, skipping upload")
            return
        buf = io.StringIO()
        df.to_csv(buf, index=False)
        data = buf.getvalue().encode("utf-8")
        proc.get_blobClient(name).upload_blob(data, overwrite=True)
        logging.info(f"Uploaded {name}: {len(df)} rows, {len(data):,} bytes")

    _upload(p_df,  "pitcher_stats.csv")
    _upload(b_df,  "batter_stats.csv")
    _upload(pg_df, "pitcher_game_log.csv")
    _upload(bg_df, "batter_game_log.csv")

    return p_df, b_df


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

    t = _threading.Thread(target=_run, daemon=True)
    t.start()
    return func.HttpResponse("Transform started", status_code=202)


@app.route(route="status", methods=["GET"])
def status(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse(f"{_status['state']}: {_status['msg']}", status_code=200)
