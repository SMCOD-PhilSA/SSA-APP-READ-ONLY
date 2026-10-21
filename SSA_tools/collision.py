# -----------------------------
# collision.py - UPDATED (April 20, 2026) - RT_from_TCA LIVE + BACKFILL FIX
# • Historical Event Summary now shows live RT_from_TCA
# • Forced recompute of TCA_PH + RT_from_TCA on every load (fixes empty table for old data)
# • All other features unchanged
# -----------------------------

import streamlit as st
import pandas as pd
import hashlib
import os
from datetime import datetime, timezone, timedelta
import numpy as np
import math
import gspread
import plotly.express as px
import plotly.graph_objects as go
import requests
from oauth2client.service_account import ServiceAccountCredentials
from streamlit_option_menu import option_menu
from gspread.utils import rowcol_to_a1
from sgp4.api import Satrec, jday

# ══════════════════════════════════════════════════════════════════════════
# DUMMY DISPLAY MODE (optional, for demos/screenshots)
#
# When enabled, EVENT_ID / Primary_Object / Secondary_Object values shown in
# the UI (Events list, Event Analyzer, CDM Database) are swapped for
# deterministic "Event A" / "Object A" style dummy labels. All underlying
# filtering, event-summary computation, action-log lookups, TLE fetches, and
# orbit propagation still use the REAL EVENT_ID / object values -- only what
# gets *displayed* changes.
#
# To disable completely: leave/set DEMO_MODE = False. Nothing else needs to
# change -- every mapping below becomes an identity map (real value shown
# as-is) and every call site behaves exactly as before.
# ══════════════════════════════════════════════════════════════════════════
DEMO_MODE = False
# DEMO_MODE = True   # uncomment this line to enable dummy Event/Object names


def _demo_event_maps(df: pd.DataFrame):
    """
    Build (event_map, primary_map, secondary_map): real value -> display label.

    When DEMO_MODE is True, each unique real EVENT_ID / Primary_Object /
    Secondary_Object value in df maps to a deterministic "Event A" / "Object A"
    style label. When DEMO_MODE is False, every value maps to itself, so
    callers that use these maps for on-screen labels show the real names
    unchanged.

    These maps are for display only -- always pass the REAL EVENT_ID /
    Primary_Object / Secondary_Object values (not the mapped labels) into
    data lookups, filtering, and orbit propagation.
    """
    event_map, primary_map, secondary_map = {}, {}, {}

    if "EVENT_ID" in df.columns:
        unique_events = sorted(df["EVENT_ID"].dropna().unique())
        if DEMO_MODE:
            event_map = {eid: f"Event {chr(65 + i)}" for i, eid in enumerate(unique_events)}
        else:
            event_map = {eid: eid for eid in unique_events}

    if "Primary_Object" in df.columns:
        unique_primary = sorted(df["Primary_Object"].dropna().unique())
        if DEMO_MODE:
            primary_map = {obj: f"Object {chr(65 + i)}" for i, obj in enumerate(unique_primary)}
        else:
            primary_map = {obj: obj for obj in unique_primary}

    if "Secondary_Object" in df.columns:
        unique_secondary = sorted(df["Secondary_Object"].dropna().unique())
        if DEMO_MODE:
            secondary_map = {obj: f"Object {chr(65 + len(primary_map) + i)}"
                              for i, obj in enumerate(unique_secondary)}
        else:
            secondary_map = {obj: obj for obj in unique_secondary}

    return event_map, primary_map, secondary_map


# ─────────────────────────────────────────────────────────────────────────────
# TLE_CDM — Google Sheet cache for conjunction TLEs
# Mirrors dashboard.py's TLE_CACHE pattern.
# Uses the same gcp_service_account + Demo-CDM-Conjunction sheet as the rest
# of collision.py, so no new secrets are needed.
# ─────────────────────────────────────────────────────────────────────────────

TLE_CDM_SHEET       = "TLE_CDM"   # worksheet auto-created on first run
MAX_TLE_CDM_HOURS   = 12          # refresh at most twice a day
SAFE_REFRESH_MINUTE = 16          # staggered to avoid :00 / :30 rate-limit spikes
ST_BASE             = "https://www.space-track.org"


ODR_SHEET_ID = "1isa_rOueh0yDY9OM43Q2AWZoKPGrlvEo7JOSHJdW3jE"

def _get_odr_gspread_client():
    """Auth for SMCOD SSA ODR sheet — mirrors dashboard.py _get_gspread_client()."""
    from google.oauth2.service_account import Credentials as GCredentials
    try:
        creds = GCredentials.from_service_account_info(
            st.secrets["gcp_service_account_odr"],
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
    except Exception:
        path = os.getenv("GCP_JSON_ODR")
        if not path or not os.path.exists(path):
            raise RuntimeError("ODR service account credentials not found.")
        creds = GCredentials.from_service_account_file(
            path, scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
    return gspread.authorize(creds)


def _get_tle_cdm_ws():
    """Return the TLE_CDM worksheet inside SMCOD SSA ODR, creating it if absent."""
    client = _get_odr_gspread_client()
    sh     = client.open_by_key(ODR_SHEET_ID)
    try:
        return sh.worksheet(TLE_CDM_SHEET)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=TLE_CDM_SHEET, rows=500, cols=8)
        ws.append_row(["updated_at_utc", "norad_cat_id", "object_name",
                       "epoch", "tle_line1", "tle_line2", "tca_utc"])
        return ws


def _read_tle_cdm_cache(norad_id: str, all_rows: list = None):
    """
    Return cached TLE for norad_id or None.
    Reads by column position (matches _write_tle_cdm_cache order) so that an
    existing TLE_CDM sheet with different header text doesn't cause a KeyError.

    Column order: updated_at_utc(0) | norad_cat_id(1) | object_name(2) |
                  epoch(3) | tle_line1(4) | tle_line2(5) | tca_utc(6)

    Shape: { age_hours, rec: {NORAD_CAT_ID, OBJECT_NAME, EPOCH, TLE_LINE1, TLE_LINE2} }
    """
    if all_rows is None:
        ws       = _get_tle_cdm_ws()
        all_rows = ws.get_all_values()   # list of lists, row 0 = header

    # Need at least a header row + one data row
    if len(all_rows) < 2:
        return None

    # Skip header row; filter data rows that have enough columns and match norad_id
    matching = [
        row for row in all_rows[1:]
        if len(row) >= 6 and str(row[1]).strip() == str(norad_id).strip()
    ]
    if not matching:
        return None

    latest = matching[-1]
    try:
        age_hours = (pd.Timestamp.utcnow() - pd.to_datetime(
            latest[0], utc=True
        )).total_seconds() / 3600
    except Exception:
        age_hours = 999   # force refresh if timestamp is unparseable

    return {
        "age_hours": age_hours,
        "rec": {
            "NORAD_CAT_ID": str(latest[1]),
            "OBJECT_NAME":  str(latest[2]),
            "EPOCH":        str(latest[3]),
            "TLE_LINE1":    str(latest[4]),
            "TLE_LINE2":    str(latest[5]),
        }
    }


def _write_tle_cdm_cache(norad_id: str, rec: dict, tca_dt=None):
    """
    Append a TLE row to TLE_CDM worksheet.
    Also stores tca_utc so historical TLEs can be matched back to their event.
    Raises on failure — callers surface the error instead of silently dropping it.
    """
    ws = _get_tle_cdm_ws()
    ws.append_row([
        datetime.now(timezone.utc).isoformat(),
        str(norad_id),
        rec.get("OBJECT_NAME", ""),
        rec.get("EPOCH", ""),
        rec.get("TLE_LINE1", ""),
        rec.get("TLE_LINE2", ""),
        tca_dt.strftime("%Y-%m-%dT%H:%M:%S") if tca_dt else "",
    ])


def _should_refresh_tle(cache, tca_dt=None) -> bool:
    """
    Active events  : refresh if cache absent or older than MAX_TLE_CDM_HOURS.
    Lapsed events  : the cached TLE epoch must be within ±1 day of TCA to be
                     valid for back-propagation; if it's from the wrong era,
                     force a gp_history re-fetch.  Once a good historical TLE
                     is cached it is kept permanently (never re-fetched).
    """
    if cache is None:
        return True
    if tca_dt is not None:
        tca_aware = tca_dt if tca_dt.tzinfo else tca_dt.replace(tzinfo=timezone.utc)
        if tca_aware < datetime.now(timezone.utc):          # lapsed event
            try:
                cached_epoch = pd.to_datetime(cache["rec"]["EPOCH"], utc=True).to_pydatetime()
                if abs((cached_epoch - tca_aware).total_seconds()) / 86400 > 1.0:
                    return True   # wrong-era TLE; need historical one
            except Exception:
                return True
            return False          # valid historical TLE already cached
    if cache["age_hours"] >= MAX_TLE_CDM_HOURS:
        return True
    if datetime.now(timezone.utc).minute == SAFE_REFRESH_MINUTE and cache["age_hours"] >= 1:
        return True
    return False


def _st_login(user: str, pwd: str):
    """Open a logged-in Space-Track session. Returns (session, err)."""
    sess = requests.Session()
    r = sess.post(f"{ST_BASE}/ajaxauth/login",
                  data={"identity": user, "password": pwd}, timeout=20)
    if r.status_code != 200 or "Failed" in r.text:
        return None, f"Space-Track login failed: {r.text[:200]}"
    return sess, None


def _fetch_tle_spacetrack(norad_id: str):
    """Fetch the latest GP record for norad_id. Returns (rec, err)."""
    try:
        user = st.secrets["spacetrack"]["username"]
        pwd  = st.secrets["spacetrack"]["password"]
    except Exception:
        return None, "Space-Track credentials not found under [spacetrack] in secrets.toml."

    sess, err = _st_login(user, pwd)
    if err:
        return None, err

    url  = (f"{ST_BASE}/basicspacedata/query/class/gp"
            f"/NORAD_CAT_ID/{norad_id}/orderby/EPOCH%20desc/limit/1/format/json")
    resp = sess.get(url, timeout=20)
    sess.get(f"{ST_BASE}/ajaxauth/logout", timeout=10)

    if resp.status_code != 200:
        return None, f"Space-Track GP query failed (HTTP {resp.status_code})."
    data = resp.json()
    if not data:
        return None, f"No GP record found for NORAD {norad_id}."

    gp = data[0]
    return {
        "NORAD_CAT_ID": str(gp.get("NORAD_CAT_ID", norad_id)),
        "OBJECT_NAME":  str(gp.get("OBJECT_NAME", "")),
        "EPOCH":        str(gp.get("EPOCH", "")),
        "TLE_LINE1":    str(gp.get("TLE_LINE1", "")),
        "TLE_LINE2":    str(gp.get("TLE_LINE2", "")),
    }, None


def _fetch_tle_historical(norad_id: str, tca_dt: datetime):
    """
    Query /class/gp_history for the record whose epoch is closest to tca_dt.
    Uses a ±3-day window around TCA so there is always at least one TLE
    available even for objects with infrequent updates.
    Returns (rec, err).
    """
    try:
        user = st.secrets["spacetrack"]["username"]
        pwd  = st.secrets["spacetrack"]["password"]
    except Exception:
        return None, "Space-Track credentials not found under [spacetrack] in secrets.toml."

    sess, err = _st_login(user, pwd)
    if err:
        return None, err

    tca_aware    = tca_dt if tca_dt.tzinfo else tca_dt.replace(tzinfo=timezone.utc)
    window_start = (tca_aware - timedelta(days=3)).strftime("%Y-%m-%d")
    window_end   = (tca_aware + timedelta(days=1)).strftime("%Y-%m-%d")

    url = (f"{ST_BASE}/basicspacedata/query/class/gp_history"
           f"/NORAD_CAT_ID/{norad_id}"
           f"/EPOCH/{window_start}--{window_end}"
           f"/orderby/EPOCH%20desc/format/json")
    resp = sess.get(url, timeout=30)
    sess.get(f"{ST_BASE}/ajaxauth/logout", timeout=10)

    if resp.status_code != 200:
        return None, f"Space-Track gp_history query failed (HTTP {resp.status_code})."
    data = resp.json()
    if not data:
        return None, (f"No historical GP records for NORAD {norad_id} "
                      f"in window {window_start} – {window_end}.")

    def _epoch_diff(gp):
        try:
            return abs((pd.to_datetime(gp["EPOCH"], utc=True).to_pydatetime() - tca_aware).total_seconds())
        except Exception:
            return float("inf")

    best = min(data, key=_epoch_diff)
    return {
        "NORAD_CAT_ID": str(best.get("NORAD_CAT_ID", norad_id)),
        "OBJECT_NAME":  str(best.get("OBJECT_NAME", "")),
        "EPOCH":        str(best.get("EPOCH", "")),
        "TLE_LINE1":    str(best.get("TLE_LINE1", "")),
        "TLE_LINE2":    str(best.get("TLE_LINE2", "")),
    }, None


def fetch_tle_cached(norad_id: str, tca_dt=None, all_rows: list = None):
    """
    Return (rec, warning|None) for norad_id.

    Active events  → /class/gp  (latest TLE), refreshed every MAX_TLE_CDM_HOURS.
    Lapsed events  → /class/gp_history record closest to TCA epoch, cached
                     permanently once a valid near-TCA TLE is stored.
    Write failures are surfaced as warnings rather than silently discarded.

    Pass all_rows (from ws.get_all_values()) to avoid a redundant sheet read
    when fetching both primary and secondary objects in the same render cycle.
    """
    cache = _read_tle_cdm_cache(norad_id, all_rows=all_rows)

    if not _should_refresh_tle(cache, tca_dt):
        return cache["rec"], None

    tca_aware = None
    if tca_dt is not None:
        tca_aware = tca_dt if tca_dt.tzinfo else tca_dt.replace(tzinfo=timezone.utc)

    is_lapsed = tca_aware is not None and tca_aware < datetime.now(timezone.utc)

    if is_lapsed:
        live_rec, err = _fetch_tle_historical(norad_id, tca_aware)
    else:
        live_rec, err = _fetch_tle_spacetrack(norad_id)

    if err:
        if cache:
            return cache["rec"], (
                f"Using cached TLE for NORAD {norad_id} "
                f"({cache['age_hours']:.1f}h old). Live update failed: {err}"
            )
        return None, err

    try:
        _write_tle_cdm_cache(norad_id, live_rec, tca_dt=tca_aware)
    except Exception as write_err:
        return live_rec, f"TLE fetched but failed to write to TLE_CDM sheet: {write_err}"

    return live_rec, None


# ─────────────────────────────────────────────────────────────────────────────
# SGP4 PROPAGATION + GLOBE RENDERING  (inlined from services/conjunction/globe.py)
# ─────────────────────────────────────────────────────────────────────────────

def _gmst(t: datetime) -> float:
    """Approximate GMST in radians."""
    J0  = (367 * t.year
           - int(7 * (t.year + int((t.month + 9) / 12)) / 4)
           + int(275 * t.month / 9)
           + t.day + 1721013.5)
    J0 += (t.hour + t.minute / 60 + t.second / 3600) / 24
    D   = J0 - 2451545.0
    return np.radians((280.46061837 + 360.98564736629 * D) % 360)


def _propagate_orbit(tle1: str, tle2: str, center_time: datetime,
                     window_minutes: int = 60, steps: int = 120):
    """
    Propagate TLE across a window centered on center_time (typically TCA).
    Returns list of (lat, lon, alt_km, timestamp_iso) sorted by time.
    """
    sat  = Satrec.twoline2rv(tle1, tle2)
    half = timedelta(minutes=window_minutes / 2)
    pts  = []
    for i in range(steps + 1):
        t = center_time - half + timedelta(minutes=i * window_minutes / steps)
        jd, fr = jday(t.year, t.month, t.day, t.hour, t.minute,
                      t.second + t.microsecond / 1e6)
        e, r, _ = sat.sgp4(jd, fr)
        if e != 0 or not r:
            continue
        x, y, z = r
        lon  = np.degrees(np.arctan2(y, x)) - np.degrees(_gmst(t))
        lon  = (lon + 180) % 360 - 180
        r_km = np.sqrt(x**2 + y**2 + z**2)
        lat  = np.degrees(np.arcsin(z / r_km))
        pts.append((lat, lon, r_km - 6371.0, t.strftime("%Y-%m-%dT%H:%M:%S")))
    return pts


def _read_tle_sheet_rows():
    """
    Read all rows from TLE_CDM once. Returns (all_rows, err_str|None).
    Call this once per render cycle and pass all_rows to fetch_tle_cached
    for both primary and secondary objects to avoid double sheet reads.
    """
    try:
        ws = _get_tle_cdm_ws()
        return ws.get_all_values(), None
    except Exception as e:
        return None, str(e)


def get_orbit_points_for_event(df: pd.DataFrame, event_id: str, object_num: int = 1,
                                all_rows: list = None):
    """
    Resolve NORAD ID, fetch TLE via TLE_CDM cache, propagate centred on TCA.
    Returns (list of (lat, lon, alt_km, ts_iso), tca_datetime | None).

    Pass all_rows from _read_tle_sheet_rows() to avoid a redundant sheet read
    when called for both primary and secondary objects.
    """
    scc_col = "Primary_SCC"    if object_num == 1 else "Secondary_SCC"
    obj_col = "Primary_Object" if object_num == 1 else "Secondary_Object"

    event_rows = df[df["EVENT_ID"] == event_id]
    if event_rows.empty:
        return [], None

    row = event_rows.iloc[-1]

    # Extract TCA
    tca_dt = None
    try:
        tca_raw = row.get("TCA_UTC") or row.get("TCA", "")
        if tca_raw and str(tca_raw).strip():
            tca_dt = pd.to_datetime(tca_raw, utc=True).to_pydatetime()
    except Exception:
        pass
    center_time = tca_dt if tca_dt else datetime.now(timezone.utc)

    norad_id = None
    try:
        raw = row.get(scc_col, "")
        if raw and str(raw).strip() not in ("", "0", "nan"):
            norad_id = str(int(float(str(raw).strip())))
    except Exception:
        pass

    if not norad_id:
        st.warning(f"⚠️ No NORAD ID in `{scc_col}` for event {event_id} "
                   f"(object: {row.get(obj_col, '—')}).")
        return [], tca_dt

    rec, warn = fetch_tle_cached(norad_id, tca_dt=tca_dt, all_rows=all_rows)
    if warn:
        st.warning(f"TLE cache warning (NORAD {norad_id}): {warn}")
    if not rec:
        st.error(f"❌ Could not fetch TLE for NORAD {norad_id}.")
        return [], tca_dt

    tle1 = rec.get("TLE_LINE1", "")
    tle2 = rec.get("TLE_LINE2", "")
    if not tle1 or not tle2:
        st.error(f"❌ TLE lines empty for NORAD {norad_id}.")
        return [], tca_dt

    try:
        pts = _propagate_orbit(tle1, tle2, center_time)
        return pts, tca_dt
    except Exception as exc:
        st.error(f"❌ SGP4 propagation failed for NORAD {norad_id}: {exc}")
        return [], tca_dt


def render_globe_animated(pts1, name1: str, pts2, name2: str, tca_dt=None):
    """
    Animated orthographic globe centred on TCA.

    Each animation frame advances both satellites along their ground tracks.
    The frame at TCA is the start position; a red close-approach marker pulses
    at the TCA positions of both objects.  The user can Play/Pause or scrub the
    slider.  The globe rotates so the conjunction point stays roughly centred.
    """
    if not pts1 or not pts2:
        st.warning("Insufficient orbit data to render globe.")
        return

    n_frames = min(len(pts1), len(pts2))
    if n_frames < 2:
        st.warning("Not enough propagated points for animation.")
        return

    # ── Find the frame closest to TCA ──────────────────────────────────────
    tca_frame = 0
    if tca_dt:
        tca_iso = tca_dt.strftime("%Y-%m-%dT%H:%M:%S")
        timestamps = [p[3] for p in pts1]
        diffs = [abs((datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S") -
                      tca_dt.replace(tzinfo=None)).total_seconds())
                 for ts in timestamps]
        tca_frame = int(np.argmin(diffs))

    # TCA positions (for close-approach marker)
    tca_lat1, tca_lon1 = pts1[tca_frame][0], pts1[tca_frame][1]
    tca_lat2, tca_lon2 = pts2[tca_frame][0], pts2[tca_frame][1]

    # ── Helper: build one Plotly frame ────────────────────────────────────
    def _make_frame(i):
        lat1, lon1, _, ts = pts1[i]
        lat2, lon2, _, _  = pts2[i]

        # Trail: last 20 points before current frame
        trail_start = max(0, i - 20)
        trail_lats1 = [p[0] for p in pts1[trail_start:i+1]]
        trail_lons1 = [p[1] for p in pts1[trail_start:i+1]]
        trail_lats2 = [p[0] for p in pts2[trail_start:i+1]]
        trail_lons2 = [p[1] for p in pts2[trail_start:i+1]]

        at_tca = (i == tca_frame)

        traces = [
            # Trail 1
            go.Scattergeo(lat=trail_lats1, lon=trail_lons1,
                          mode="lines", name=name1,
                          line=dict(color="#FFD700", width=1.5),
                          showlegend=False),
            # Trail 2
            go.Scattergeo(lat=trail_lats2, lon=trail_lons2,
                          mode="lines", name=name2,
                          line=dict(color="#FF69B4", width=1.5),
                          showlegend=False),
            # Satellite 1 marker
            go.Scattergeo(lat=[lat1], lon=[lon1],
                          mode="markers+text",
                          name=name1,
                          marker=dict(size=10, color="#FFD700",
                                      symbol="circle",
                                      line=dict(color="white", width=1)),
                          text=[name1], textposition="top right",
                          textfont=dict(color="#FFD700", size=10),
                          showlegend=(i == 0)),
            # Satellite 2 marker
            go.Scattergeo(lat=[lat2], lon=[lon2],
                          mode="markers+text",
                          name=name2,
                          marker=dict(size=10, color="#FF69B4",
                                      symbol="circle",
                                      line=dict(color="white", width=1)),
                          text=[name2], textposition="top right",
                          textfont=dict(color="#FF69B4", size=10),
                          showlegend=(i == 0)),
            # Close-approach flash (only at TCA frame)
            go.Scattergeo(
                lat=[tca_lat1, tca_lat2],
                lon=[tca_lon1, tca_lon2],
                mode="markers",
                name="Close Approach",
                marker=dict(
                    size=18 if at_tca else 0,
                    color="rgba(255,50,50,0.85)",
                    symbol="circle",
                    line=dict(color="white", width=2),
                ),
                showlegend=False,
            ),
        ]
        return go.Frame(data=traces, name=ts,
                        layout=go.Layout(
                            geo=dict(projection_rotation=dict(lon=lon1, lat=lat1))
                        ))

    frames = [_make_frame(i) for i in range(n_frames)]
    slider_steps = [
        dict(args=[[f.name], dict(frame=dict(duration=0, redraw=True),
                                  mode="immediate", transition=dict(duration=0))],
             label=f.name[11:16],   # HH:MM
             method="animate")
        for f in frames
    ]

    # ── Globe colours ─────────────────────────────────────────────────────
    # paper_bgcolor / plot_bgcolor are transparent so the chart blends
    # with the Streamlit page in both light and dark mode.
    # The geo sphere itself uses a space-themed palette so it always looks
    # like a proper globe visualization regardless of page theme.
    _land_col    = "#2a4a2a"   # dark forest green land
    _ocean_col   = "#0d1b2a"   # deep navy ocean
    _coast_col   = "#4a8aaa"   # lighter blue coastlines — visible on dark ocean
    _country_col = "#3a6080"   # muted blue-grey borders
    _globe_bg    = "rgba(0,0,0,0)"    # transparent — page bg shows outside the sphere

    # Text/control colours — fixed neutral values that work on both themes.
    # st.get_option("theme.base") always returns the config.toml default
    # ("light"), never the live user-selected theme, so we cannot use it.
    # Instead:
    #   • Plotly font.color uses a light value (#e0e0e0) since the globe
    #     and controls always have a dark background (ocean/control bg).
    #   • The CSS injection below overrides slider tick labels using
    #     var(--text-color) which IS the live Streamlit theme variable.
    _txt      = "#e0e0e0"               # light — readable on dark ctrl bg
    _ctrl_bg  = "rgba(14,34,75,0.88)"   # dark navy, works on any page bg
    _ctrl_bdr = "#1a4fa3"
    _leg_bg   = "rgba(10,20,50,0.88)"

    # ── Initial figure (start at TCA frame) ──────────────────────────────
    init_frame = frames[tca_frame]
    fig = go.Figure(
        data=init_frame.data,
        frames=frames,
        layout=go.Layout(
            title=None,
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color=_txt),
            geo=dict(
                projection_type="orthographic",
                projection_rotation=dict(lon=pts1[tca_frame][1], lat=pts1[tca_frame][0]),
                showland=True,       landcolor=_land_col,
                showocean=True,      oceancolor=_ocean_col,
                showcoastlines=True, coastlinecolor=_coast_col,
                showcountries=True,  countrycolor=_country_col,
                bgcolor=_globe_bg,
                showframe=False,
                showlakes=True,  lakecolor=_ocean_col,
            ),
            legend=dict(font=dict(color=_txt), bgcolor=_leg_bg,
                        bordercolor=_ctrl_bdr, x=0.01, y=0.99),
            margin=dict(l=0, r=0, t=50, b=120),
            height=700,
            updatemenus=[dict(
                type="buttons",
                showactive=False,
                x=0.5, xanchor="center",
                y=-0.16, yanchor="top",
                buttons=[
                    dict(label="▶  Play",
                         method="animate",
                         args=[None, dict(frame=dict(duration=120, redraw=True),
                                          fromcurrent=True,
                                          transition=dict(duration=0))]),
                    dict(label="⏸  Pause",
                         method="animate",
                         args=[[None], dict(frame=dict(duration=0, redraw=False),
                                             mode="immediate",
                                             transition=dict(duration=0))]),
                ],
                font=dict(color=_txt),
                bgcolor=_ctrl_bg,
                bordercolor=_ctrl_bdr,
            )],
            sliders=[dict(
                active=tca_frame,
                currentvalue=dict(prefix="Time: ", font=dict(color=_txt, size=12), xanchor="center"),
                pad=dict(t=50, b=10),
                steps=slider_steps,
                font=dict(color=_txt),
                bgcolor=_ctrl_bg,
                bordercolor=_ctrl_bdr,
                tickcolor=_txt,
            )],
            annotations=[dict(
                text=(f"⚠ Close Approach @ TCA" if tca_dt else ""),
                xref="paper", yref="paper",
                x=0.5, y=1.04,
                showarrow=False,
                font=dict(color="#FF4444", size=11),
            )],
        )
    )

    tca_label = (f"  ·  TCA {tca_dt.strftime('%Y-%m-%d %H:%M:%S')} UTC" if tca_dt else "")
    st.markdown(
        f"**{name1}** vs **{name2}**{tca_label}",
        unsafe_allow_html=False,
    )
    st.plotly_chart(fig, use_container_width=True)

    # ── JS injection: recolor Plotly slider text after render ─────────────
    # Plotly sets inline `fill` attributes directly on SVG <text> elements,
    # which beats CSS `fill` declarations (inline style wins over stylesheet).
    # The only reliable fix is JavaScript that reads the live Streamlit CSS
    # variable and writes it back as an inline attribute after Plotly renders.
    # We also preserve satellite marker label colors (#FFD700, #FF69B4) and
    # the red annotation by skipping those specific elements.
    import streamlit.components.v1 as components
    components.html("""
    <script>
    (function recolorPlotlyText() {
        // Detect light vs dark from the parent page body background color.
        // Light mode bg = #ffffff (lum~255), Dark mode bg = #0a0f1c (lum~12).
        function getThemeColor() {
            var bg = window.parent.getComputedStyle(
                window.parent.document.body
            ).backgroundColor;
            var rgb = bg.match(/\d+/g);
            if (rgb) {
                var lum = 0.299*parseInt(rgb[0]) + 0.587*parseInt(rgb[1]) + 0.114*parseInt(rgb[2]);
                return lum > 128 ? '#0e224b' : '#e0e0e0';  // config.toml exact values
            }
            return '#0e224b';
        }

        // Colors to preserve — satellite labels and close approach annotation
        var preserveSet = new Set([
            '#ffd700','#ff69b4','#ff4444',
            'rgb(255, 215, 0)','rgb(255, 105, 180)','rgb(255, 68, 68)',
            'rgb(255,215,0)','rgb(255,105,180)','rgb(255,68,68)',
        ]);

        function shouldPreserve(color) {
            if (!color) return false;
            return preserveSet.has(color.toLowerCase().replace(/\s/g,''));
        }

        function applyColors(textColor) {
            // Plotly charts in Streamlit render inside <iframe> elements
            // with data-testid="stIFrame" wrapping stPlotlyChart.
            // We need to reach into those iframes.
            var iframes = window.parent.document.querySelectorAll('iframe');
            iframes.forEach(function(iframe) {
                try {
                    var idoc = iframe.contentDocument || iframe.contentWindow.document;
                    var texts = idoc.querySelectorAll('.js-plotly-plot text, .js-plotly-plot tspan');
                    texts.forEach(function(el) {
                        var fill = (el.getAttribute('fill') || '').toLowerCase().replace(/\s/g,'');
                        var styleFill = (el.style.fill || '').toLowerCase().replace(/\s/g,'');
                        if (shouldPreserve(fill) || shouldPreserve(styleFill)) return;
                        el.setAttribute('fill', textColor);
                        el.style.fill = textColor;
                    });
                } catch(e) {}  // cross-origin iframes will throw — skip them
            });

            // Also try direct (non-iframe Plotly, some Streamlit versions)
            var plots = window.parent.document.querySelectorAll('.js-plotly-plot');
            plots.forEach(function(plot) {
                var texts = plot.querySelectorAll('text, tspan');
                texts.forEach(function(el) {
                    var fill = (el.getAttribute('fill') || '').toLowerCase().replace(/\s/g,'');
                    var styleFill = (el.style.fill || '').toLowerCase().replace(/\s/g,'');
                    if (shouldPreserve(fill) || shouldPreserve(styleFill)) return;
                    el.setAttribute('fill', textColor);
                    el.style.fill = textColor;
                });
            });
        }

        function run() {
            var color = getThemeColor();
            applyColors(color);
        }

        run();
        setTimeout(run, 200);
        setTimeout(run, 600);
        setTimeout(run, 1200);
        setTimeout(run, 2500);
    })();
    </script>
    """, height=0)


# Keep old name as alias so any other callers don't break
def render_globe_dual(pts1, name1: str, pts2, name2: str, tca_dt=None):
    render_globe_animated(pts1, name1, pts2, name2, tca_dt)

#---------------- REMOVE TOP PADDING ----------------
st.markdown("""
<style>
    .block-container {
        padding-top: 4.8rem !important;
    }
</style>
""", unsafe_allow_html=True)

# # ---------------- HEADER ----------------
# theme = st.get_option("theme.base")

# bg_color = "#1a4fa3"
# text_color = "#ffffff"

# st.markdown(f"""
# <div style="
#     text-align: center;
#     font-size: 42px;
#     font-weight: 700;
#     padding: 15px 0;
#     margin: 0;
#     border-radius: 10px;
#     background-color: {bg_color};
#     color: {text_color};
# ">
#     Conjunction
# </div>
# """, unsafe_allow_html=True)

# st.space()
# -----------------------------
# OPTION MENU + CONFIG
# -----------------------------
selected_option = option_menu(
    menu_title=None,
    options=["CDM Dashboard", "Event Analyzer", "CDM Upload", "CDM Database"],
    icons=["bi bi-bar-chart-fill", "bi bi-activity", "bi-cloud-upload", "bi bi-filter"],
    menu_icon="cast",
    default_index=0,
    orientation="horizontal",
)

if "selected" not in st.session_state:
    st.session_state.selected = "CDM Dashboard"
if selected_option != st.session_state.selected:
    st.session_state.selected = selected_option
    st.rerun()

st.set_page_config(page_title="SSA - Conjunction", page_icon=":material/compare_arrows:", layout="wide")

SHEET_NAME = "Demo-CDM-Conjunction"
WORKSHEET_NAME = "Sheet1"
SHEET_COLUMNS = [
    "EVENT_ID", "CDM_ID", "Date_Received", "Primary_SCC", "Primary_Object",
    "Secondary_SCC", "Secondary_Object", "TCA_UTC", "TCA_PH",
    "HOURS_TO_TCA",
    "RT_from_TCA",          # ← real-time hours to TCA (live + backfilled)
    "Pc", "Miss_Distance", "R", "T", "N",
    "EVENT_MAX_PC", "EVENT_MIN_MISS", "EVENT_CDM_COUNT",
    "EVENT_FIRST_SEEN", "EVENT_LATEST_UPDATE", "EVENT_RISK",
    "EVENT_STATUS", "EVENT_TREND", "RECOMMENDED_ACTION", "ACTION_EXECUTED", "NOTES",
    "SOURCE_FILE", "CDM_HASH", "CREATION_DATE"
]

# -----------------------------
# PHILIPPINE TIME HELPER
# -----------------------------
def get_current_ph_time():
    return pd.Timestamp.now('Asia/Manila')

# -----------------------------
# HELPERS
# -----------------------------
def sanitize_for_gsheets(value):
    # None / NaN
    if value is None or pd.isna(value):
        return ""

    # Handle float NaN/inf
    if isinstance(value, (float, np.floating)):
        if math.isnan(float(value)) or math.isinf(float(value)):
            return ""
        return float(value)

    # Convert numpy integers
    if isinstance(value, np.integer):
        return int(value)

    # Convert numpy booleans
    if isinstance(value, np.bool_):
        return bool(value)

    # Convert pandas timestamps
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d %H:%M:%S")

    # Convert numpy arrays/lists if needed
    if isinstance(value, np.ndarray):
        return value.tolist()

    # Everything else
    return value

def convert_utc_to_phst(tca_utc_str):
    if pd.isna(tca_utc_str) or str(tca_utc_str).strip() == "":
        return ""
    dt = pd.to_datetime(tca_utc_str, utc=True, errors="coerce")
    if pd.isna(dt):
        return ""
    return (dt + pd.Timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")

def format_tca_display(tca_str):
    if pd.isna(tca_str) or str(tca_str).strip() == "":
        return ""
    dt = pd.to_datetime(tca_str, utc=True, errors="coerce")
    if pd.isna(dt):
        return str(tca_str)
    return dt.strftime("%Y-%m-%d %H:%M:%S")

def compute_rt_from_tca(tca_ph_str):
    """Compute REAL-TIME hours from current Philippine time to TCA_PH."""
    if pd.isna(tca_ph_str) or str(tca_ph_str).strip() == "":
        return pd.NA

    try:
        current_ph = get_current_ph_time()
        tca_ph_dt = pd.to_datetime(tca_ph_str, errors="coerce")

        if pd.isna(tca_ph_dt):
            return pd.NA

        # timezone-safe handling
        if getattr(tca_ph_dt, "tzinfo", None) is None:
            tca_ph_dt = tca_ph_dt.tz_localize(
                "Asia/Manila",
                ambiguous="NaT",
                nonexistent="NaT"
            )
        else:
            tca_ph_dt = tca_ph_dt.tz_convert("Asia/Manila")

        hours = (
            tca_ph_dt - current_ph
        ).total_seconds() / 3600

        return round(hours, 2)

    except Exception:
        return pd.NA

def generate_event_id_safe(row):
    try:
        tca_dt = pd.to_datetime(row["TCA"], utc=True)
        tca_local = tca_dt.tz_convert("Asia/Manila")
        #tca_day = tca_dt.strftime("%Y-%m-%d")
        tca_day = tca_local.strftime("%Y-%m-%d")
    except:
        return f"INVALID_TCA_{row.name}"
    sat1 = str(row.get("SAT_1_ID", "UNKNOWN") or "UNKNOWN")
    sat2 = str(row.get("SAT_2_ID", "UNKNOWN") or "UNKNOWN")
    return f"{sat1}_{sat2}_{tca_day}"

def generate_cdm_id(tca, sequence_num: int):
    dt = pd.to_datetime(tca, utc=True)
    return f"CDM-{dt.strftime('%y%m%d')}-{str(sequence_num).zfill(4)}"

def generate_cdm_hash(row):
    """Generate a unique hash for each CDM, now including CREATION_DATE for better uniqueness."""
    # Prioritize CREATION_DATE (most reliable unique field from the provider)
    creation_date = str(row.get("CREATION_DATE", "")).strip()
    if not creation_date:
        creation_date = str(row.get("CREATION_DATE_UTC", "")).strip()
    
    # Fallback fields (in case CREATION_DATE is missing)
    message_id = str(row.get("MESSAGE_ID", "")).strip()
    tca = str(row.get("TCA", "")).strip()
    sat1 = str(row.get("SAT_1_ID", "")).strip()
    sat2 = str(row.get("SAT_2_ID", "")).strip()
    
    # Build the key with CREATION_DATE as the highest priority
    key = f"{creation_date}_{message_id}_{tca}_{sat1}_{sat2}"
    
    return hashlib.md5(key.encode('utf-8')).hexdigest()

def classify_risk(pc, miss_distance, tca):
    try:
        tca_dt = pd.to_datetime(tca, utc=True)
        tca_local = tca_dt.tz_convert("Asia/Manila")
        now_ph = get_current_ph_time()
        # hours_to_tca = (tca_dt - now_ph.tz_convert('UTC')).total_seconds() / 3600
        hours_to_tca = (tca_local - now_ph).total_seconds() / 3600
    except:
        return "UNKNOWN", "Invalid TCA"
    if hours_to_tca < 0:
        return "LAPSED", "CDM has expired"
    if hours_to_tca < 24:
        return "CRITICAL", "Contact secondary operator immediately"
    if hours_to_tca > 72:
        return "LOW", "Acknowledge / Do nothing"
    if pc >= 5e-4:
        return "CRITICAL", "Contact secondary operator immediately"
    if pc >= 1e-4:
        if miss_distance <= 125:
            return "HIGH", "Contact secondary operator"
        else:
            return "MEDIUM", "Close monitoring"
    return "LOW", "Routine monitoring"

def get_risk_label(pc, miss, hours):
    if hours < 0:
        return "LAPSED"
    if hours < 24:
        return "CRITICAL"
    if pc >= 0.0005:
        return "CRITICAL"
    if pc >= 0.0001:
        if miss <= 125:
            return "HIGH"
        else:
            return "MEDIUM"
    return "LOW"

def normalize_risk_label(risk):
    risk = str(risk).strip()
    if "CRITICAL" in risk: return "CRITICAL"
    elif "HIGH" in risk: return "HIGH"
    elif "MEDIUM" in risk: return "MEDIUM"
    elif "LOW" in risk: return "LOW"
    elif "LAPSED" in risk: return "LAPSED"
    return risk

def compute_trend_and_action(group):
    group = group.copy()

    # ===== Update #4 use Date_Received instead of TCA =====
    if "Date_Received" in group.columns:
        group["sort_temp"] = pd.to_datetime(
            group["Date_Received"],
            errors="coerce"
        )
    else:
        group["sort_temp"] = pd.to_datetime(
            group["TCA"],
            utc=True,
            errors="coerce"
        )

    group = (
        group
        .sort_values("sort_temp")
        .reset_index(drop=True)
    )

    group.drop(
        columns=["sort_temp"],
        inplace=True,
        errors="ignore"
    )

    if len(group) == 1:
        trend = "NEW"
    else:
        prev_pc = pd.to_numeric(
            group["PC"].iloc[-2],
            errors="coerce"
        )
        prev_miss = pd.to_numeric(
            group["MISS_DISTANCE"].iloc[-2],
            errors="coerce"
        )

        new_pc = pd.to_numeric(
            group["PC"].iloc[-1],
            errors="coerce"
        )
        new_miss = pd.to_numeric(
            group["MISS_DISTANCE"].iloc[-1],
            errors="coerce"
        )

        if new_pc > prev_pc or new_miss < prev_miss:
            trend = "WORSENING"
        elif new_pc < prev_pc or new_miss > prev_miss:
            trend = "IMPROVING"
        else:
            trend = "STABLE"

    latest_tca = group["TCA"].iloc[-1]
    max_pc = group["PC"].max()
    min_miss = group["MISS_DISTANCE"].min()

    _, action = classify_risk(
        max_pc,
        min_miss,
        latest_tca
    )

    return pd.Series({
        "TREND": trend,
        "ACTION": action
    })

def compute_event_summary(df_input, use_persisted_summary=False):
    df = df_input.copy()
    if "Pc" in df.columns and "PC" not in df.columns: df["PC"] = df["Pc"]
    if "Miss_Distance" in df.columns and "MISS_DISTANCE" not in df.columns: df["MISS_DISTANCE"] = df["Miss_Distance"]
    if "TCA_UTC" in df.columns and "TCA" not in df.columns: df["TCA"] = df["TCA_UTC"]

    if use_persisted_summary and {"EVENT_MAX_PC", "EVENT_MIN_MISS", "EVENT_RISK", "EVENT_TREND", "RECOMMENDED_ACTION", "ACTION_EXECUTED", "HOURS_TO_TCA"}.issubset(df.columns):
        event_summary = df.groupby("EVENT_ID").agg({
            "EVENT_MAX_PC": "max", "EVENT_MIN_MISS": "min", "HOURS_TO_TCA": "min",
            "EVENT_RISK": "last", "EVENT_TREND": "last",
            "RECOMMENDED_ACTION": "last", "ACTION_EXECUTED": "last",
        }).reset_index()
        event_summary = event_summary.rename(columns={
            "EVENT_MAX_PC": "MAX_PC", "EVENT_MIN_MISS": "MIN_MISS",
            "EVENT_RISK": "RISK", "EVENT_TREND": "TREND",
            "RECOMMENDED_ACTION": "ACTION"
        })
    else:
        if "TCA" in df.columns:
            df["TCA_dt_temp"] = pd.to_datetime(df["TCA"], utc=True, errors="coerce")
            now_ph = get_current_ph_time()
            df["HOURS_TO_TCA"] = (df["TCA_dt_temp"] - now_ph.tz_convert('UTC')).dt.total_seconds() / 3600
            df.drop(columns=["TCA_dt_temp"], inplace=True, errors="ignore")
        if "TCA" in df.columns and "TCA_PH" not in df.columns:
            df["TCA_PH"] = df["TCA"].apply(convert_utc_to_phst)
        event_summary = df.groupby("EVENT_ID").agg({
            "PC": "max", "MISS_DISTANCE": "min", "HOURS_TO_TCA": "min"
        }).reset_index()
        event_summary = event_summary.rename(columns={"PC": "MAX_PC", "MISS_DISTANCE": "MIN_MISS"})
        event_summary["MAX_PC"] = pd.to_numeric(event_summary["MAX_PC"], errors="coerce").fillna(0)
        event_summary["MIN_MISS"] = pd.to_numeric(event_summary["MIN_MISS"], errors="coerce").fillna(999999)
            # --- Get latest CDM per EVENT_ID ---
        if "Date_Received" in df.columns:
            df["sort_temp"] = pd.to_datetime(df["Date_Received"], errors="coerce")
        else:
            df["sort_temp"] = pd.to_datetime(df["TCA"], utc=True, errors="coerce")

        latest_rows = (
            df.sort_values("sort_temp")
            .groupby("EVENT_ID")
            .tail(1)
            .copy()
        )

        df.drop(columns=["sort_temp"], inplace=True, errors="ignore")

        # --- Compute event summary (still keep historical max/min if you want) ---
        event_summary = df.groupby("EVENT_ID").agg({
            "PC": "max",
            "MISS_DISTANCE": "min",
            "HOURS_TO_TCA": "min"
        }).reset_index()

        event_summary = event_summary.rename(columns={
            "PC": "MAX_PC",
            "MISS_DISTANCE": "MIN_MISS"
        })

        # --- Merge latest values ---
        latest_rows = latest_rows.rename(columns={
            "PC": "CURRENT_PC",
            "MISS_DISTANCE": "CURRENT_MISS",
            "HOURS_TO_TCA": "CURRENT_HOURS"
        })

        event_summary = event_summary.merge(
            latest_rows[["EVENT_ID", "CURRENT_PC", "CURRENT_MISS", "CURRENT_HOURS"]],
            on="EVENT_ID",
            how="left"
        )

        # --- 🔥 Compute RISK using CURRENT values ---
        event_summary["RISK"] = event_summary.apply(
            lambda row: get_risk_label(
                row["CURRENT_PC"],
                row["CURRENT_MISS"],
                row["CURRENT_HOURS"]
            ),
            axis=1
        )
        event_metrics = df.groupby("EVENT_ID").apply(compute_trend_and_action).reset_index()
        event_summary = event_summary.merge(event_metrics, on="EVENT_ID", how="left")

    if not df.empty and "TCA" in df.columns:
        latest_tca = (df.sort_values("TCA", key=lambda x: pd.to_datetime(x, utc=True, errors="coerce"))
                      .groupby("EVENT_ID").tail(1)[["EVENT_ID", "TCA", "TCA_PH"]]
                      .rename(columns={"TCA": "TCA_UTC"}))
        latest_tca["TCA_UTC"] = latest_tca["TCA_UTC"].apply(format_tca_display)
        event_summary = event_summary.merge(latest_tca, on="EVENT_ID", how="left")
    else:
        event_summary["TCA_UTC"] = ""
        event_summary["TCA_PH"] = ""

    event_summary["RISK"] = event_summary["RISK"].apply(normalize_risk_label)
    risk_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "LAPSED": 4}
    event_summary["RISK_SORT"] = event_summary["RISK"].map(risk_order)
    event_summary = event_summary.sort_values(by=["RISK_SORT", "MAX_PC"], ascending=[True, False]).drop(columns=["RISK_SORT"]).reset_index(drop=True)

    # Always attach fresh real-time RT_from_TCA
    if "TCA_PH" in event_summary.columns:
        event_summary["RT_from_TCA"] = (
            event_summary["TCA_PH"]
            .apply(compute_rt_from_tca)
        )
    else:
        event_summary["RT_from_TCA"] = pd.NA

    # ===== Update #2 force all logic to use live value =====
    event_summary["HOURS_TO_TCA"] = event_summary["RT_from_TCA"]

    return event_summary

def display_event_summary_table(event_summary):
    st.dataframe(
        event_summary[["EVENT_ID", "TCA_UTC", "TCA_PH", "MAX_PC", "MIN_MISS", 
                       "RT_from_TCA", "RISK", "TREND", "ACTION"]],
        width='stretch',
        hide_index=True
    )

    st.markdown("""
        <style>
        .risk-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 14px;
            table-layout: fixed;  /* 👈 KEY */
        }

        .risk-table th, .risk-table td {
            padding: 10px;
            text-align: left;
            border-bottom: 1px solid #ddd;
        }

        /* Force equal column widths */
        .risk-table th:nth-child(1),
        .risk-table td:nth-child(1) {
            width: 30%;
        }

        .risk-table th:nth-child(2),
        .risk-table td:nth-child(2) {
            width: 70%;
        }

        .risk-table th {
            background-color: #1a4fa3;
            color: white;
        }

        .risk-badge {
            font-weight: bold;
        }
        </style>
        """, unsafe_allow_html=True)
    
    st.markdown("""
        <style>
        .risk-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 14px;
            table-layout: fixed;
        }

        .risk-table th, .risk-table td {
            padding: 10px;
            text-align: left;
            border-bottom: 1px solid #ddd;
        }

        .risk-table th {
            background-color: #1a4fa3;
            color: white;
        }

        /* Column widths */
        .risk-table th:nth-child(1),
        .risk-table td:nth-child(1) {
            width: 30%;
        }

        .risk-table th:nth-child(2),
        .risk-table td:nth-child(2) {
            width: 70%;
        }

        /* Button-style badges */
        .risk-btn {
            display: inline-block;
            padding: 5px 12px;
            border-radius: 10px;
            font-weight: 600;
            font-size: 13px;
            color: white;
        }

        /* Colors */
        .risk-critical { background-color: #dc2626; }
        .risk-high     { background-color: #f97316; }
        .risk-medium   { background-color: #eab308; color: black; }
        .risk-low      { background-color: #90EE90; color: black; }
        .risk-lapsed   { background-color: #9ca3af; }

        </style>

        <table class="risk-table">
        <tr>
            <th>Risk Level</th>
            <th>Description</th>
        </tr>
        <tr>
            <td><span class="risk-btn risk-critical">CRITICAL</span></td>
            <td>TCA &lt; 24h or PC ≥ 0.0005</td>
        </tr>
        <tr>
            <td><span class="risk-btn risk-high">HIGH</span></td>
            <td>PC ≥ 0.0001 and Miss Distance ≤ 125</td>
        </tr>
        <tr>
            <td><span class="risk-btn risk-medium">MEDIUM</span></td>
            <td>PC ≥ 0.0001 and Miss Distance &gt; 125</td>
        </tr>
        <tr>
            <td><span class="risk-btn risk-low">LOW</span></td>
            <td>PC &lt; 0.0001</td>
        </tr>
        <tr>
            <td><span class="risk-btn risk-lapsed">LAPSED</span></td>
            <td>CDM has expired</td>
        </tr>
        </table>
        """, unsafe_allow_html=True)

    st.markdown("""
        <table class="risk-table">
        <tr>
            <th>Variable</th>
            <th>Definition</th>
        </tr>
        <tr>
            <td>⏱ RT_from_TCA</td>
            <td>REAL-TIME hours to TCA (computed from current PH time)</td>
        </tr>
        <tr>
            <td>🌍 TCA_UTC</td>
            <td>Time of Closest Approach (UTC)</td>
        </tr>
        <tr>
            <td>🇵🇭 TCA_PH</td>
            <td>Time of Closest Approach (PhST)</td>
        </tr>
        </table>
        """, unsafe_allow_html=True)

def update_action_executed(event_id: str, executed_action: str):
    sheet = connect_gsheet()
    records = sheet.get_all_records()
    if not records:
        st.error("No data in sheet.")
        return

    headers = sheet.row_values(1)
    try:
        exec_col_idx = headers.index("ACTION_EXECUTED") + 1
    except ValueError:
        st.error("❌ ACTION_EXECUTED column not found in Google Sheet!")
        return

    try:
        latest_col_idx = headers.index("EVENT_LATEST_UPDATE") + 1
        update_latest = True
    except ValueError:
        update_latest = False

    now_str = str(datetime.utcnow())

    matching_rows = []
    for row_num, rec in enumerate(records, start=2):
        if str(rec.get("EVENT_ID", "")) == event_id:
            matching_rows.append(row_num)

    if not matching_rows:
        st.warning(f"No rows found for event {event_id}")
        return

    latest_row_num = max(matching_rows)

    cell = rowcol_to_a1(latest_row_num, exec_col_idx)
    sheet.update_acell(cell, executed_action)

    if update_latest:
        latest_cell = rowcol_to_a1(latest_row_num, latest_col_idx)
        sheet.update_acell(latest_cell, now_str)

    st.success(f"✅ Saved **{executed_action}** for the **latest CDM** of event **{event_id}**")
    load_database.clear()
    st.rerun()

def show_risk_alerts(event_summary, is_upload=False, editable=False, alert_df=None):
    if editable:
        alert_events = alert_df.copy() if alert_df is not None and not alert_df.empty else pd.DataFrame()
        st.info(f"📋 **{len(alert_events)} Active Event(s)**")
    else:
        alert_events = event_summary[
            (event_summary["RISK"].isin(["CRITICAL", "HIGH"])) &
            (event_summary.get("HOURS_TO_TCA", event_summary.get("RT_from_TCA", 0)) >= 0)
        ].copy()

    if alert_events.empty:
        if is_upload:
            st.success("✅ No critical or high-risk events in this upload.")
        else:
            st.success("✅ No active events in the database.")
        return

    # Safe column mapping
    max_pc_col = next((col for col in ["max_pc", "MAX_PC", "EVENT_MAX_PC"] if col in alert_events.columns), "EVENT_MAX_PC")
    current_pc_col = "current_pc" if "current_pc" in alert_events.columns else "Pc"
    min_miss_col = next((col for col in ["min_miss", "MIN_MISS", "EVENT_MIN_MISS"] if col in alert_events.columns), "EVENT_MIN_MISS")
    current_miss_col = "current_miss" if "current_miss" in alert_events.columns else "Miss_Distance"

    if editable and not alert_events.empty:
        for i in range(0, len(alert_events), 2):
            cols = st.columns(2)
            for j, col in enumerate(cols):
                if i + j < len(alert_events):
                    row = alert_events.iloc[i + j]
                    
                    # Safe numeric extraction
                    max_pc_val = pd.to_numeric(row.get(max_pc_col, 0), errors='coerce') or 0
                    curr_pc_val = pd.to_numeric(row.get(current_pc_col, 0), errors='coerce') or 0
                    min_miss_val = pd.to_numeric(row.get(min_miss_col, 999999), errors='coerce') or 999999
                    curr_miss_val = pd.to_numeric(row.get(current_miss_col, 999999), errors='coerce') or 999999
                    hours_val = pd.to_numeric(row.get("RT_from_TCA", row.get("HOURS_TO_TCA", 0)), errors='coerce') or 0

                    color = "#FF4444" if row.get("RISK") == "🔴 CRITICAL" else "#FFAA00" if row.get("RISK") == "🟠 HIGH" else "#4CAF50"
                    
                    with col:
                        with st.container(border=True):
                            st.markdown(f"<h4 style='color:{color}; margin-bottom:8px;'>{row.get('RISK', 'UNKNOWN')} — {row.get('EVENT_ID', '')}</h4>", unsafe_allow_html=True)
                            
                            # Updated display with all four fields you requested
                            st.markdown(f"""
                            **Max PC:** {max_pc_val:.2e}  
                            **Current PC:** {curr_pc_val:.2e}<br>
                            **Min Miss:** {min_miss_val:.2f} m  
                            **Current Miss:** {curr_miss_val:.2f} m<br>
                            **Hours to TCA (Real-time):** {hours_val:.1f} h
                            """, unsafe_allow_html=True)
                            
                            st.markdown(f"**TCA (PhST):** {row.get('TCA_PH', '')}  **TCA (UTC):** {row.get('TCA_UTC', '')}")
                            st.markdown(f"**Trend:** {row.get('TREND', '—')}   **Recommended:** {row.get('ACTION', row.get('RECOMMENDED_ACTION', '—'))}")
                           
                            current_executed = row.get("ACTION_EXECUTED", "") or "Not set yet"
                            st.info(f"**Current Executed Action:** {current_executed}")
                           
                            possible_actions = [
                                "Contact secondary operator immediately",
                                "Contact secondary operator",
                                "Close monitoring",
                                "Routine monitoring",
                                "Acknowledge / Do nothing",
                                "Others - please specify"
                            ]
                           
                            default_idx = 0
                            if current_executed in possible_actions:
                                default_idx = possible_actions.index(current_executed)
                           
                            executed_choice = st.selectbox(
                                "Action Actually Executed",
                                options=possible_actions,
                                index=default_idx,
                                key=f"exec_select_{row.get('EVENT_ID', '')}_{i+j}"
                            )
                           
                            if executed_choice == "Others - please specify":
                                other_text = st.text_input(
                                    "Please specify the action you took",
                                    value="",
                                    key=f"other_text_{row.get('EVENT_ID', '')}_{i+j}"
                                )
                                final_action = other_text.strip() if other_text.strip() else "Others (unspecified)"
                            else:
                                final_action = executed_choice
                           
                            if st.button("💾 Save to Google Sheet", 
                                       key=f"save_btn_{row.get('EVENT_ID', '')}_{i+j}", 
                                       type="primary", 
                                       use_container_width=True):
                                if final_action:
                                    update_action_executed(row.get("EVENT_ID", ""), final_action)
    else:
        # Non-editable version (used in Upload tab)
        for _, row in alert_events.iterrows():
            max_pc_val = pd.to_numeric(row.get(max_pc_col, 0), errors='coerce') or 0
            curr_pc_val = pd.to_numeric(row.get(current_pc_col, 0), errors='coerce') or 0
            min_miss_val = pd.to_numeric(row.get(min_miss_col, 999999), errors='coerce') or 999999
            curr_miss_val = pd.to_numeric(row.get(current_miss_col, 999999), errors='coerce') or 999999
            
            color = "#FF4444" if row.get("RISK") == "🔴 CRITICAL" else "#FFAA00" if row.get("RISK") == "🟠 HIGH" else "#4CAF50"
            
            st.markdown(f"""
            <div style="background-color:{color}20; padding:15px; border-radius:10px;
                        border-left:6px solid {color}; margin-bottom:10px;">
                <h4>{row.get('RISK', 'UNKNOWN')} — {row.get('EVENT_ID', '')}</h4>
                <b>Max PC:</b> {max_pc_val:.2e}  
                <b>Current PC:</b> {curr_pc_val:.2e}<br>
                <b>Min Miss:</b> {min_miss_val:.2f} m  
                <b>Current Miss:</b> {curr_miss_val:.2f} m<br>
                <b>Hours to TCA:</b> {row.get('RT_from_TCA', row.get('HOURS_TO_TCA', 0)):.1f} h<br>
                <b>TCA (PhST):</b> {row.get('TCA_PH', '')}
            </div>
            """, unsafe_allow_html=True)

def connect_gsheet():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(st.secrets["gcp_service_account"], scope)
    client = gspread.authorize(creds)
    return client.open(SHEET_NAME).worksheet(WORKSHEET_NAME)

@st.cache_data(ttl=300)
def load_database():
    sheet = connect_gsheet()
    records = sheet.get_all_records()
    if not records:
        return pd.DataFrame(columns=SHEET_COLUMNS)
    df = pd.DataFrame(records)
   
    col_map = {"Pc": "PC", "Miss_Distance": "MISS_DISTANCE", "TCA_UTC": "TCA"}
    for old_col, new_col in col_map.items():
        if old_col in df.columns and new_col not in df.columns:
            df[new_col] = df[old_col]
   
    if "ACTION_TAKEN" in df.columns and "RECOMMENDED_ACTION" not in df.columns:
        df = df.rename(columns={"ACTION_TAKEN": "RECOMMENDED_ACTION"})
   
    if "ACTION_EXECUTED" not in df.columns:
        df["ACTION_EXECUTED"] = ""
   
    if "THOURS_TO_TCA" in df.columns and "HOURS_TO_TCA" not in df.columns:
        df["HOURS_TO_TCA"] = df["THOURS_TO_TCA"]
    elif "TCA" in df.columns and "HOURS_TO_TCA" not in df.columns:
        df["TCA_dt_temp"] = pd.to_datetime(df["TCA"], utc=True, errors="coerce")
        df["HOURS_TO_TCA"] = (df["TCA_dt_temp"] - pd.Timestamp.now('UTC')).dt.total_seconds() / 3600
        df.drop(columns=["TCA_dt_temp"], inplace=True, errors="ignore")
   
    if "TCA" in df.columns:
        df["TCA"] = df["TCA"].apply(format_tca_display)
    if "TCA_UTC" in df.columns:
        df["TCA_UTC"] = df["TCA_UTC"].apply(format_tca_display)
   
    # === FORCE RECOMPUTE TCA_PH + RT_from_TCA (fixes old blank rows) ===
    if "TCA_UTC" in df.columns or "TCA" in df.columns:
        tca_col = "TCA" if "TCA" in df.columns else "TCA_UTC"
        df["TCA_PH"] = df[tca_col].apply(convert_utc_to_phst)
    
    if "TCA_PH" in df.columns:
        df["RT_from_TCA"] = df["TCA_PH"].apply(compute_rt_from_tca)
    else:
        df["RT_from_TCA"] = pd.NA
   
    return df

def generate_duplicate_key_for_check(row, is_existing=False):
    """
    Improved duplicate detection that prioritizes CREATION_DATE.
    This prevents re-ingestion of old CDMs when a new CSV contains full history + one new CDM.
    """
    # ------------------- PRIORITY 1: CREATION_DATE (Most Reliable) -------------------
    creation_date = None
    
    if is_existing:
        # For rows already in Google Sheet
        creation_date = str(row.get("CREATION_DATE", "")).strip() or \
                       str(row.get("CREATION_DATE_UTC", "")).strip()
    else:
        # For newly uploaded CSV
        creation_date = str(row.get("CREATION_DATE", "")).strip() or \
                       str(row.get("CREATION_DATE_UTC", "")).strip()
    
    if creation_date and creation_date != "":
        try:
            # Normalize timestamp to consistent format (removes microseconds variability)
            dt = pd.to_datetime(creation_date, errors="coerce")
            if pd.notna(dt):
                normalized_date = dt.strftime("%Y-%m-%dT%H:%M:%S")
                return f"CREATION_DATE:{normalized_date}"
        except Exception:
            pass  # fallback if parsing fails
    
    # ------------------- PRIORITY 2: Fallback (TCA + Objects + PC + Miss) -------------------
    if is_existing:
        # Existing data from Google Sheet
        tca = str(row.get("TCA_UTC", "") or row.get("TCA", "")).strip()
        sat1 = str(row.get("Primary_Object", "")).strip()
        sat2 = str(row.get("Secondary_Object", "")).strip()
        pc_str = f"{float(row.get('Pc', 0) or 0):.10e}"
        miss_str = f"{float(row.get('Miss_Distance', 0) or 999999):.2f}"
    else:
        # New data from uploaded CSV
        tca_str = str(row.get("TCA", "")).strip()
        tca = format_tca_display(tca_str) if tca_str else ""
        sat1 = str(row.get("SAT_1_ID", "")).strip()
        sat2 = str(row.get("SAT_2_ID", "")).strip()
        pc_str = f"{float(row.get('PC', 0) or 0):.10e}"
        miss_str = f"{float(row.get('MISS_DISTANCE', 0) or 999999):.2f}"
    
    return f"TCA:{tca}|SAT1:{sat1}|SAT2:{sat2}|PC:{pc_str}|MISS:{miss_str}"

# -----------------------------
# PAGES
# -----------------------------
#CDM Dashboard latest (09 July 2026)
if st.session_state.selected == "CDM Dashboard":
    df = load_database()
    if st.button("🔄 Refresh Data from Google Sheets"):
        load_database.clear()
        st.rerun()

    if df.empty or len(df) == 0:
        st.info("No data in the database yet. Upload some CDMs on the first tab to get started!")
    else:
        event_summary = compute_event_summary(df, use_persisted_summary=True)

        # ====================== KEY METRICS ======================
        #st.subheader("Key Metrics")

        st.markdown("""
        <style>
            /* KPI Cards */
            .kpi-card {
                background: var(--secondaryBackgroundColor);
                border-radius: 16px;
                padding: 24px 16px;
                text-align: center;
                box-shadow: 0 4px 15px rgba(0, 0, 0, 0.08);
                border: 2px solid rgba(0,0,0,0.12);
                height: 100%;
                transition: transform 0.2s ease, box-shadow 0.2s ease;
            }  

            .kpi-card:hover {
                transform: translateY(-4px);
                box-shadow: 0 8px 25px rgba(0, 0, 0, 0.15);
            }
                    
            .kpi-card {
                border: 2px solid rgba(255,255,255,1);
                box-shadow: 0 4px 15px rgba(0,0,0,0.4);
            }

            .kpi-title {
                font-size: 1.05rem;
                color: #b0b0b0;
                margin-bottom: 10px;
                font-weight: 500;
            }

            .kpi-value {
                font-size: 2.85rem;
                font-weight: 700;
                margin: 0;
                line-height: 1.1;
            }

            .critical { color: #ff4d4d; }
            .high     { color: #ffaa00; }
            .active   { color: #4dabf7; }

            /* ==================== RISK TABLE ==================== */
            .risk-table {
                width: 100%;
                border-collapse: collapse;
                font-size: 14.5px;
                margin: 20px 0 30px 0;
                background-color: var(--secondaryBackgroundColor);
                border-radius: 10px;
                overflow: hidden;
                box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            }

            .risk-table th, .risk-table td {
                padding: 12px 16px;
                text-align: left;
                border-bottom: 1px solid #e0e0e0;
            }

            .risk-table th {
                background-color: #1a4fa3;
                color: white;
                font-weight: 600;
            }

            .risk-table tr:last-child td {
                border-bottom: none;
            }

            .risk-btn {
                display: inline-block;
                padding: 6px 16px;
                border-radius: 20px;
                font-weight: 700;
                font-size: 13.5px;
                color: white;
                text-align: center;
                min-width: 118px;
            }

            .risk-critical { background-color: #dc2626; }
            .risk-high     { background-color: #f97316; }
            .risk-medium   { background-color: #eab308; color: black; }
            .risk-low      { background-color: #4ade80; color: black; }
            .risk-lapsed   { background-color: #9ca3af; }
        </style>
        """, unsafe_allow_html=True)

        # KPI Cards
        now_pht = pd.Timestamp.now(tz="Asia/Manila")
        
        active_events = event_summary[
            (event_summary["RT_from_TCA"] >= 0) &
            (pd.to_datetime(event_summary["TCA_UTC"], utc=True, errors="coerce")
             .dt.tz_convert("Asia/Manila") >= now_pht - pd.Timedelta(days=3))
        ].copy()

        k1, k2, k3, k4 = st.columns(4)

        with k1:
            st.markdown(f'''
                <div class="kpi-card">
                    <div class="kpi-title">Total CDMs</div>
                    <div class="kpi-value">{len(df)}</div>
                </div>
            ''', unsafe_allow_html=True)

        with k2:
            critical_count = len(event_summary[(event_summary["RISK"] == "🔴 CRITICAL") & (event_summary["RT_from_TCA"] >= 0)])
            st.markdown(f'''
                <div class="kpi-card">
                    <div class="kpi-title">Critical Events</div>
                    <div class="kpi-value critical">{critical_count}</div>
                </div>
            ''', unsafe_allow_html=True)

        with k3:
            high_count = len(event_summary[(event_summary["RISK"] == "🟠 HIGH") & (event_summary["RT_from_TCA"] >= 0)])
            st.markdown(f'''
                <div class="kpi-card">
                    <div class="kpi-title">High Risk Events</div>
                    <div class="kpi-value high">{high_count}</div>
                </div>
            ''', unsafe_allow_html=True)

        with k4:
            st.markdown(f'''
                <div class="kpi-card">
                    <div class="kpi-title">Active Events</div>
                    <div class="kpi-value active">{len(active_events)}</div>
                </div>
            ''', unsafe_allow_html=True)

        st.divider()

        # ==================== MAIN LAYOUT: Charts + Events Sidebar ====================
        left_col, right_col = st.columns([2.8, 1.0])

        # ==================== LEFT COLUMN - CHARTS ====================
        with left_col:
            # ====================== TWO CHARTS IN COLUMNS ======================
            st.subheader("CDM Activity Trends")

            df_graph = df.copy()
            df_graph["Month"] = pd.to_datetime(df_graph["TCA_UTC"], errors="coerce").dt.to_period("M").astype(str)

            col1, col2 = st.columns(2)

            con1 = col1.container(border=True)
            con2 = col2.container(border=True)

            # -------- LEFT: MONTHLY CDM COUNT --------
            with con1:
                st.markdown("**CDM Count per Month**")
                monthly_count = df_graph.groupby("Month").size().reset_index(name="CDM Count")
                
                fig = px.bar(
                    monthly_count,
                    x="Month",
                    y="CDM Count",
                    labels={"Month": "Month", "CDM Count": "Number of CDMs"},
                    template="plotly",
                    height=420
                )

                fig.update_layout(
                    xaxis_title="Month",
                    yaxis_title="Number of CDM",
                    xaxis_title_font=dict(size=16),
                    yaxis_title_font=dict(size=16),
                    xaxis_tickfont=dict(size=13),
                    yaxis_tickfont=dict(size=13),
                    barcornerradius=15
                )

                fig.update_traces(
                    marker=dict(line=dict(color="white", width=2))
                )
                
                st.plotly_chart(fig, use_container_width=True)

            # -------- RIGHT: RISK DISTRIBUTION --------
            with con2:
                st.markdown("**Current Risk Distribution**")
                active_df = event_summary[event_summary["RT_from_TCA"] >= 0]

                if len(active_df) == 0:
                    st.info("No active CDM events")
                else:
                    active_risks = (
                        active_df["RISK"]
                        .value_counts()
                        .reset_index()
                    )
                    active_risks.columns = ["Risk", "Count"]

                    active_risks["Risk"] = active_risks["Risk"].str.strip()

                    color_map = {
                        "CRITICAL": "#d62728",
                        "HIGH": "#ff7f0e",
                        "MEDIUM": "#f1c40f",
                        "LOW": "#7f8c8d"
                    }

                    fig_pie = px.pie(
                        active_risks,
                        names="Risk",
                        values="Count",
                        hole=0.45,
                        color="Risk",
                        color_discrete_map=color_map,
                        template="simple_white",
                        height=420
                    )

                    fig_pie.update_traces(
                        textinfo="percent",
                        marker=dict(line=dict(color="white", width=2)),
                        textfont_size=18
                    )

                    st.plotly_chart(fig_pie, use_container_width=True)

            # ====================== PC TREND (FULL WIDTH) ======================
            con3 = st.container(border=True)

            with con3:
                st.markdown("**Collision Probability (PC) per Month**")
                pc_col = "Pc" if "Pc" in df_graph.columns else "PC"
                df_graph["Month"] = pd.to_datetime(df_graph["Month"], errors="coerce")

                idx_max = df_graph.groupby("Month")[pc_col].idxmax()
                max_rows = df_graph.loc[idx_max, ["Month", pc_col, "EVENT_ID"]].copy()
                max_rows = max_rows.rename(columns={pc_col: "Max PC"})

                mean_rows = (
                    df_graph
                    .groupby("Month")[pc_col]
                    .mean()
                    .reset_index()
                    .rename(columns={pc_col: "Mean PC"})
                )

                monthly_stats = pd.merge(max_rows, mean_rows, on="Month")

                fig = px.line(
                    monthly_stats,
                    x="Month",
                    y=["Max PC", "Mean PC"],
                    labels={"value": "PC", "Month": "Month"},
                    markers=True,
                    template="plotly",
                    height=420
                )

                fig.for_each_trace(lambda trace: (
                    trace.update(
                        line=dict(color="#d62728", width=3),
                        marker=dict(size=9),
                        fill="tozeroy",
                        fillcolor="rgba(214,39,40,0.15)"
                    ) if trace.name == "Max PC"
                    else trace.update(
                        line=dict(color="#1f77b4", width=2),
                        marker=dict(size=9)
                    )
                ))

                fig.add_trace(
                    go.Scatter(
                        x=monthly_stats["Month"],
                        y=monthly_stats["Max PC"],
                        mode="markers",
                        marker=dict(
                            size=13,
                            color="#d62728",
                            line=dict(width=2, color="white")
                        ),
                        showlegend=False
                    )
                )

                fig.add_trace(
                    go.Scatter(
                        x=monthly_stats["Month"],
                        y=monthly_stats["Mean PC"],
                        mode="markers",
                        marker=dict(
                            size=10,
                            color="#1f77b4",
                            line=dict(width=2, color="white")
                        ),
                        showlegend=False
                    )
                )

                fig.for_each_trace(lambda trace: (
                    trace.update(
                        customdata=monthly_stats[["EVENT_ID"]],
                        hovertemplate=(
                            "<b>%{x|%b %Y}</b><br>"
                            "Max PC: %{y:.2e}<br>"
                            "EVENT_ID: %{customdata[0]}<extra></extra>"
                        )
                    ) if trace.name == "Max PC"
                    else trace.update(
                        hovertemplate=(
                            "<b>%{x|%b %Y}</b><br>"
                            "Mean PC: %{y:.2e}<extra></extra>"
                        )
                    )
                ))

                fig.add_trace(
                    go.Scatter(
                        x=[monthly_stats["Month"].min(), monthly_stats["Month"].max()],
                        y=[5e-4, 5e-4],
                        mode="lines",
                        name="Critical Threshold (5e-4)",
                        line=dict(color="orange", dash="dash", width=2)
                    )
                )

                fig.update_xaxes(
                    showgrid=False,
                    dtick="M1",
                    tickformat="%b %Y",
                    ticklabelmode="period"
                )

                fig.update_yaxes(
                    showgrid=True,
                    range=[0, max(monthly_stats["Max PC"].max(), 5e-4) * 1.2]
                )

                fig.update_layout(
                    xaxis_title="Month",
                    yaxis_title="Probability of Collision (Pc)",
                    xaxis_title_font=dict(size=16),
                    yaxis_title_font=dict(size=16),
                    xaxis_tickfont=dict(size=13),
                    yaxis_tickfont=dict(size=13),
                    legend=dict(
                        title=dict(text="Pc", font=dict(size=14)),
                        font=dict(size=12),
                    )
                )
                st.plotly_chart(fig, use_container_width=True)

        # ==================== RIGHT COLUMN - EVENTS LIST ====================
        with right_col:
            st.subheader("Events")
        
            container_events = st.container(border=True, height=950)
        
            with container_events:
                raw_conjunction_df = load_database()
                event_summary = compute_event_summary(raw_conjunction_df, use_persisted_summary=False)
                events_df = event_summary.copy()
            
                # === PURE DATE SORT: Soonest TCA First ===
                events_df["TCA_dt"] = pd.to_datetime(
                    events_df["TCA_UTC"],
                    utc=True,
                    errors="coerce"
                )
            
                events_df = events_df.sort_values(
                    by="RT_from_TCA",
                    ascending=False
                ).reset_index(drop=True)
            
                # ==================== EVENT LABELS (dummy display when DEMO_MODE) ====================
                if not events_df.empty:
                    events_df = events_df.reset_index(drop=True)
                    if DEMO_MODE:
                        # Deterministic dummy event names (Event A, Event B, ...)
                        events_df["Pseudo_Event"] = [f"Event {chr(65 + i)}" for i in range(len(events_df))]
                    else:
                        events_df["Pseudo_Event"] = events_df["EVENT_ID"]
                
                displayed = 0
                for _, row in events_df.iterrows():
                    if displayed >= 12:
                        break
                    
                    risk_label = row.get("RISK", "LOW")
                    # Clean risk for CSS class
                    clean_risk = str(risk_label).strip()
                    clean_risk = clean_risk.replace("🔴", "").replace("🟠", "").replace("🟡", "").replace("⚪", "").replace("⚫", "")
                    clean_risk = clean_risk.strip()
                    risk_class = clean_risk.lower()
                
                    # Use Pseudo Event Name instead of real EVENT_ID
                    pseudo_event = row.get("Pseudo_Event", "Event Unknown")
                    tca_ph = str(row.get("TCA_PH", ""))[:16] if row.get("TCA_PH") else "—"
                    hours = row.get("RT_from_TCA", 0)
                    hours_str = f"{hours:.1f}h" if hours >= 0 else "Lapsed"
                
                    pc = row.get("MAX_PC", 0)
                    miss = row.get("MIN_MISS", 0)
                
                    st.markdown(f"""
                    <div style="background-color: #1a4fa3; padding: 14px; border-radius: 10px;
                                margin-bottom: 12px; color: white;">
                        <strong>{pseudo_event}</strong><br>
                        <small>{tca_ph} PhST • {hours_str}</small><br>
                        <small>PC: <b>{pc:.2e}</b> • Miss: <b>{miss:.1f} m</b></small><br>
                        <span class="risk-btn risk-{risk_class}">{risk_label}</span>
                    </div>
                    """, unsafe_allow_html=True)
                
                    displayed += 1
                    
                if len(events_df) > 12:
                    st.caption(f"... and {len(events_df)-12} more events")



        # ====================== HISTORICAL EVENT SUMMARY TABLE ======================
        # st.subheader("Historical Event Summary")
        # st.caption("Active CDMs in the last 30 Days (using live RT_from_TCA)")

        # # Display dataframe
        # st.dataframe(
        #     event_summary[["EVENT_ID", "TCA_UTC", "TCA_PH", "MAX_PC", "MIN_MISS", 
        #                    "RT_from_TCA", "RISK", "TREND", "ACTION"]],
        #     width='stretch',
        #     hide_index=True,
        #     use_container_width=True
        # )
        # #st.divider()

        # Historical Summary (Last 30 Days) — now using LIVE RT_from_TCA
        now_utc = pd.Timestamp.now('UTC')
        historical_summary = event_summary[
            (event_summary["RT_from_TCA"] >= 0) &
            (pd.to_datetime(event_summary["TCA_UTC"], utc=True, errors="coerce") >= now_utc - pd.Timedelta(days=30))
        ].copy()

        # Disabled for dummy data, but can be enabled for real data
        
        # st.subheader("Historical Event Summary")
        # st.caption("Active CDMs in the last 30 Days")
        # display_event_summary_table(historical_summary)

# elif st.session_state.selected == "CDM Upload":
#     st.subheader("CDM Upload")
#     uploaded_file = st.file_uploader("Upload CDM CSV", type=["csv"])
#     if uploaded_file:
#         df = pd.read_csv(uploaded_file, skip_blank_lines=True)
        
#         df = df.rename(columns={
#             "COLLISION_PROBABILITY": "PC",
#             "SAT1_OBJECT_NAME": "SAT_1_ID",
#             "SAT2_OBJECT_NAME": "SAT_2_ID",
#             "TCA_UTC": "TCA",
#         })
#         col_map = {"Pc": "PC", "Miss_Distance": "MISS_DISTANCE", "TCA_UTC": "TCA"}
#         for old, new in col_map.items():
#             if old in df.columns and new not in df.columns:
#                 df[new] = df[old]
        
#         df["PC"] = pd.to_numeric(df["PC"], errors="coerce")
#         df["MISS_DISTANCE"] = pd.to_numeric(df["MISS_DISTANCE"], errors="coerce")
        
#         df["EVENT_ID"] = df.apply(generate_event_id_safe, axis=1)
#         df["CDM_HASH"] = df.apply(generate_cdm_hash, axis=1)
        
#         df["TCA_dt_temp"] = pd.to_datetime(df["TCA"], utc=True, errors="coerce")
#         df["HOURS_TO_TCA"] = (df["TCA_dt_temp"] - get_current_ph_time().tz_convert('UTC')).dt.total_seconds() / 3600
#         df.drop(columns=["TCA_dt_temp"], inplace=True, errors="ignore")

#         existing_df = load_database()
#         existing_events = set(existing_df["EVENT_ID"].unique()) if not existing_df.empty else set()
#         df["UPLOAD_STATUS"] = df["EVENT_ID"].apply(
#             lambda x: "🟢 NEW" if x not in existing_events else "🔄 DEVELOPING"
#         )

#         event_summary = compute_event_summary(df)
#         status_map = df.groupby("EVENT_ID")["UPLOAD_STATUS"].first()
#         event_summary["UPLOAD_STATUS"] = event_summary["EVENT_ID"].map(status_map)

# # ==================== AFTER computing event_summary ====================

#         # === NEW LOGIC: Show LATEST values based on CREATION_DATE ===
#         if not df.empty and "CREATION_DATE" in df.columns:
#             # Convert CREATION_DATE to datetime for proper sorting
#             df["CREATION_DATE_dt"] = pd.to_datetime(df["CREATION_DATE"], errors="coerce")
            
#             # Get the latest row per EVENT_ID based on CREATION_DATE
#             latest_rows = (
#                 df.sort_values("CREATION_DATE_dt", ascending=False)
#                   .groupby("EVENT_ID")
#                   .first()
#                   .reset_index()
#             )
            
#             # Extract latest PC and Miss Distance
#             latest_values = latest_rows[["EVENT_ID", "PC", "MISS_DISTANCE"]].rename(columns={
#                 "PC": "Latest_PC",
#                 "MISS_DISTANCE": "Latest_MD"
#             })
            
#             # Merge with event_summary
#             merged_df = event_summary[["EVENT_ID", "UPLOAD_STATUS", "TCA_UTC", "TCA_PH", 
#                                        "HOURS_TO_TCA", "MAX_PC", "MIN_MISS", "RISK"]].copy()
            
#             merged_df = merged_df.merge(latest_values, on="EVENT_ID", how="left")
            
#             # Rename columns for display
#             merged_df = merged_df.rename(columns={
#                 "UPLOAD_STATUS": "Upload_Stat",
#                 "MAX_PC": "Event_Max_PC",
#                 "MIN_MISS": "Event_Min_MD",
#                 "Latest_PC": "PC",      # ← This will now show latest PC
#                 "Latest_MD": "MD"       # ← This will now show latest MD
#             })
            
#         else:
#             # Fallback if CREATION_DATE is missing
#             merged_df = event_summary[["EVENT_ID", "UPLOAD_STATUS", "TCA_UTC", "TCA_PH", 
#                                        "HOURS_TO_TCA", "MAX_PC", "MIN_MISS", "RISK"]].copy()
#             merged_df = merged_df.rename(columns={
#                 "UPLOAD_STATUS": "Upload_Stat",
#                 "MAX_PC": "PC",
#                 "MIN_MISS": "MD"
#             })

#         st.subheader("Parsed CDM data table")
#         st.caption("Showing **Latest PC & MD** based on most recent CREATION_DATE per event")
#         st.dataframe(
#             merged_df[[
#                 "EVENT_ID", "Upload_Stat", "TCA_UTC", "TCA_PH", 
#                 "HOURS_TO_TCA", "PC", "MD", "Event_Max_PC", "Event_Min_MD", "RISK"
#             ]], 
#             width='stretch', 
#             hide_index=True
#         )

#         st.subheader("Raw CSV Preview")
#         st.dataframe(df.head(10), width='stretch')
     
#         # st.subheader("🚨 Event Risk Alerts")
#         # show_risk_alerts(event_summary, is_upload=True)
      
#         if st.button("Log to Google Sheets"):
#             sheet = connect_gsheet()
#             records = sheet.get_all_records()
#             existing_df = pd.DataFrame(records)
#             existing_keys = set()
#             if not existing_df.empty:
#                 for _, ex_row in existing_df.iterrows():
#                     key = generate_duplicate_key_for_check(ex_row, is_existing=True)
#                     existing_keys.add(key)
            
#             rows_to_append = []
#             skipped = 0
            
#             for event_id, event_group in df.groupby("EVENT_ID"):
#                 event_group = event_group.sort_values("TCA", key=lambda x: pd.to_datetime(x, utc=True, errors="coerce")).reset_index(drop=True)
#                 hist_df = existing_df[existing_df["EVENT_ID"] == event_id].copy() if not existing_df.empty else pd.DataFrame()
#                 current_count = len(hist_df)
                
#                 if not hist_df.empty:
#                     running_max_pc = pd.to_numeric(hist_df["EVENT_MAX_PC"].iloc[-1], errors='coerce') or 0
#                     running_min_miss = pd.to_numeric(hist_df["EVENT_MIN_MISS"].iloc[-1], errors='coerce') or 999999
#                 else:
#                     running_max_pc = 0
#                     running_min_miss = 999999
                
#                 for _, row in event_group.iterrows():
#                     dup_key = generate_duplicate_key_for_check(row, is_existing=False)
#                     if dup_key in existing_keys:
#                         skipped += 1
#                         continue
                    
#                     current_count += 1
#                     sequence_num = current_count - 1
#                     cdm_id = generate_cdm_id(row["TCA"], sequence_num)
                    
#                     new_pc = float(row["PC"]) if not pd.isna(row["PC"]) else 0
#                     new_miss = float(row["MISS_DISTANCE"]) if not pd.isna(row["MISS_DISTANCE"]) else 999999
                    
#                     running_max_pc = max(running_max_pc, new_pc)
#                     running_min_miss = min(running_min_miss, new_miss)
                    
#                     now_str = str(datetime.utcnow())
#                     tca_dt = pd.to_datetime(row["TCA"], utc=True, errors="coerce")
#                     tca_local = tca_dt.tz_convert("Asia/Manila")
#                     now_ph = get_current_ph_time()
#                     #hours_to_tca = (tca_dt - get_current_ph_time().tz_convert('UTC')).total_seconds() / 3600 if pd.notna(tca_dt) else 0
#                     hours_to_tca = (tca_local - now_ph).total_seconds() / 3600 if pd.notna(tca_dt) else 0



#                     if current_count == 1:
#                         trend = "NEW"
#                     else:
#                         prev_pc = pd.to_numeric(hist_df["Pc"].iloc[-1], errors='coerce') or new_pc
#                         prev_miss = pd.to_numeric(hist_df["Miss_Distance"].iloc[-1], errors='coerce') or new_miss
#                         if new_pc > prev_pc or new_miss < prev_miss:
#                             trend = "WORSENING"
#                         elif new_pc < prev_pc or new_miss > prev_miss:
#                             trend = "IMPROVING"
#                         else:
#                             trend = "STABLE"
                    
#                     risk, action = classify_risk(running_max_pc, running_min_miss, row["TCA"])
                    
#                     new_row_dict = {
#                         "EVENT_ID": event_id,
#                         "CDM_ID": cdm_id,
#                         "Date_Received": now_str,
#                         "Primary_SCC": row.get("SAT1_OBJECT_DESIGNATOR", ""),
#                         "Primary_Object": row.get("SAT_1_ID", ""),
#                         "Secondary_SCC": row.get("SAT2_OBJECT_DESIGNATOR", ""),
#                         "Secondary_Object": row.get("SAT_2_ID", ""),
#                         "TCA_UTC": format_tca_display(row["TCA"]),
#                         "TCA_PH": convert_utc_to_phst(row["TCA"]),
#                         "HOURS_TO_TCA": hours_to_tca,
#                         "RT_from_TCA": round(hours_to_tca, 2),
#                         "Pc": new_pc,
#                         "Miss_Distance": new_miss,
#                         "R": row.get("RELATIVE_POSITION_R", ""),
#                         "T": row.get("RELATIVE_POSITION_T", ""),
#                         "N": row.get("RELATIVE_POSITION_N", ""),
#                         "EVENT_MAX_PC": running_max_pc,
#                         "EVENT_MIN_MISS": running_min_miss,
#                         "EVENT_CDM_COUNT": current_count,
#                         "EVENT_FIRST_SEEN": now_str,
#                         "EVENT_LATEST_UPDATE": now_str,
#                         "EVENT_RISK": risk,
#                         "EVENT_STATUS": "OPEN",
#                         "EVENT_TREND": trend,
#                         "RECOMMENDED_ACTION": action,
#                         "ACTION_EXECUTED": "",
#                         "NOTES": "AUTO",
#                         "SOURCE_FILE": uploaded_file.name,
#                         "CDM_HASH": row["CDM_HASH"],
#                         "CREATION_DATE": str(row.get("CREATION_DATE", "")).strip() or 
#                                         str(row.get("CREATION_DATE_UTC", "")).strip()
#                     }
#                     rows_to_append.append(new_row_dict)
            
#             if rows_to_append:
#                 for new_row_dict in rows_to_append:
#                     values = [sanitize_for_gsheets(new_row_dict.get(col, "")) for col in SHEET_COLUMNS]
#                     sheet.append_row(values)
#                 st.success(f"✅ Logged {len(rows_to_append)} new CDMs!")
#                 st.info(f"⏭️ Skipped {skipped} duplicates")
#                 st.rerun()
#             else:
#                 st.info("ℹ️ No new CDMs to log — all were duplicates.")

elif st.session_state.selected == "CDM Upload":
    st.subheader("CDM Upload")
    uploaded_file = st.file_uploader("Upload CDM CSV", type=["csv"])
    
    if uploaded_file:
        df = pd.read_csv(uploaded_file, skip_blank_lines=True)
       
        df = df.rename(columns={
            "COLLISION_PROBABILITY": "PC",
            "SAT1_OBJECT_NAME": "SAT_1_ID",
            "SAT2_OBJECT_NAME": "SAT_2_ID",
            "TCA_UTC": "TCA",
        })
        col_map = {"Pc": "PC", "Miss_Distance": "MISS_DISTANCE", "TCA_UTC": "TCA"}
        for old, new in col_map.items():
            if old in df.columns and new not in df.columns:
                df[new] = df[old]
       
        df["PC"] = pd.to_numeric(df["PC"], errors="coerce")
        df["MISS_DISTANCE"] = pd.to_numeric(df["MISS_DISTANCE"], errors="coerce")
       
        df["EVENT_ID"] = df.apply(generate_event_id_safe, axis=1)
        df["CDM_HASH"] = df.apply(generate_cdm_hash, axis=1)
       
        df["TCA_dt_temp"] = pd.to_datetime(df["TCA"], utc=True, errors="coerce")
        df["HOURS_TO_TCA"] = (df["TCA_dt_temp"] - get_current_ph_time().tz_convert('UTC')).dt.total_seconds() / 3600
        df.drop(columns=["TCA_dt_temp"], inplace=True, errors="ignore")

        existing_df = load_database()
        existing_events = set(existing_df["EVENT_ID"].unique()) if not existing_df.empty else set()
        df["UPLOAD_STATUS"] = df["EVENT_ID"].apply(
            lambda x: "🟢 NEW" if x not in existing_events else "🔄 DEVELOPING"
        )

        event_summary = compute_event_summary(df)
        status_map = df.groupby("EVENT_ID")["UPLOAD_STATUS"].first()
        event_summary["UPLOAD_STATUS"] = event_summary["EVENT_ID"].map(status_map)

        # ==================== LATEST VALUES LOGIC (unchanged) ====================
        if not df.empty and "CREATION_DATE" in df.columns:
            df["CREATION_DATE_dt"] = pd.to_datetime(df["CREATION_DATE"], errors="coerce")
            latest_rows = (
                df.sort_values("CREATION_DATE_dt", ascending=False)
                  .groupby("EVENT_ID")
                  .first()
                  .reset_index()
            )
            latest_values = latest_rows[["EVENT_ID", "PC", "MISS_DISTANCE"]].rename(columns={
                "PC": "Latest_PC",
                "MISS_DISTANCE": "Latest_MD"
            })
            
            merged_df = event_summary[["EVENT_ID", "UPLOAD_STATUS", "TCA_UTC", "TCA_PH",
                                       "HOURS_TO_TCA", "MAX_PC", "MIN_MISS", "RISK"]].copy()
            merged_df = merged_df.merge(latest_values, on="EVENT_ID", how="left")
            merged_df = merged_df.rename(columns={
                "UPLOAD_STATUS": "Upload_Stat",
                "MAX_PC": "Event_Max_PC",
                "MIN_MISS": "Event_Min_MD",
                "Latest_PC": "PC",
                "Latest_MD": "MD"
            })
        else:
            merged_df = event_summary[["EVENT_ID", "UPLOAD_STATUS", "TCA_UTC", "TCA_PH",
                                       "HOURS_TO_TCA", "MAX_PC", "MIN_MISS", "RISK"]].copy()
            merged_df = merged_df.rename(columns={
                "UPLOAD_STATUS": "Upload_Stat",
                "MAX_PC": "PC",
                "MIN_MISS": "MD"
            })

        # ====================== STYLED EVENT VIEWER ======================
        st.subheader("📋 Parsed Conjunction Events")
        st.caption("Preview with Event Viewer styling")

        for _, row in event_summary.iterrows():
            event_id = row["EVENT_ID"]
            latest = latest_rows[latest_rows["EVENT_ID"] == event_id].iloc[0] if 'latest_rows' in locals() and not latest_rows.empty else row

            risk = row.get("RISK", "LOW")
            risk_colors = {"CRITICAL": "#dc2626", "HIGH": "#f97316", "MEDIUM": "#eab308", 
                          "LOW": "#4ade80", "LAPSED": "#9ca3af"}
            color = risk_colors.get(risk, "#9ca3af")

            with st.container(border=True):
                # Risk Badge
                st.markdown(f"""
                    <div style="background-color:{color}20; padding:16px; border-radius:12px; 
                                text-align:center; margin-bottom:20px;">
                        <span style="font-size:2.2rem; font-weight:700; color:{color};">{risk}</span>
                    </div>
                """, unsafe_allow_html=True)

                st.markdown(f"**Event ID:** `{event_id}`  **Status:** {row.get('UPLOAD_STATUS', '—')}")

                # Metrics using your preferred styling
                metrics = [
                    ("Primary Object", row.get("Primary_Object", latest.get("SAT_1_ID", "—"))),
                    ("Secondary Object", row.get("Secondary_Object", latest.get("SAT_2_ID", "—"))),
                    ("TCA (PhST)", row.get("TCA_PH", "—")),
                    ("Time to TCA", f"{row.get('RT_from_TCA', row.get('HOURS_TO_TCA', 0)):.1f} hours"),
                    ("Current Pc", f"{latest.get('PC', 0):.2e}"),
                    ("Miss Distance", f"{latest.get('MISS_DISTANCE', 0):.1f} m"),
                ]

                met_col1, met_col2 = st.columns(2)
                for i, (title, value) in enumerate(metrics):
                    with (met_col1 if i % 2 == 0 else met_col2):
                        st.markdown(f":blue-badge[{title}]")
                        st.markdown(f"""
                            <div style="font-size: 1.28rem; font-weight: 700; margin: 8px 0 16px 0; line-height: 1.3;">
                                {value}
                            </div>
                        """, unsafe_allow_html=True)

        # Optional detailed table
        with st.expander("📊 View Full Parsed Data Table", expanded=False):
            st.caption("Showing **Latest PC & MD** based on most recent CREATION_DATE per event")
            st.dataframe(
                merged_df[[
                    "EVENT_ID", "Upload_Stat", "TCA_UTC", "TCA_PH",
                    "HOURS_TO_TCA", "PC", "MD", "Event_Max_PC", "Event_Min_MD", "RISK"
                ]],
                use_container_width=True,
                hide_index=True
            )

        st.subheader("Raw CSV Preview")
        st.dataframe(df.head(10), use_container_width=True)

        # Upload Button (unchanged)
        if st.button("Log to Google Sheets", type="primary"):
            sheet = connect_gsheet()
            records = sheet.get_all_records()
            existing_df = pd.DataFrame(records)
            existing_keys = set()
            if not existing_df.empty:
                for _, ex_row in existing_df.iterrows():
                    key = generate_duplicate_key_for_check(ex_row, is_existing=True)
                    existing_keys.add(key)
           
            rows_to_append = []
            skipped = 0
           
            for event_id, event_group in df.groupby("EVENT_ID"):
                event_group = event_group.sort_values("TCA", key=lambda x: pd.to_datetime(x, utc=True, errors="coerce")).reset_index(drop=True)
                hist_df = existing_df[existing_df["EVENT_ID"] == event_id].copy() if not existing_df.empty else pd.DataFrame()
                current_count = len(hist_df)
               
                if not hist_df.empty:
                    running_max_pc = pd.to_numeric(hist_df["EVENT_MAX_PC"].iloc[-1], errors='coerce') or 0
                    running_min_miss = pd.to_numeric(hist_df["EVENT_MIN_MISS"].iloc[-1], errors='coerce') or 999999
                else:
                    running_max_pc = 0
                    running_min_miss = 999999
               
                for _, row in event_group.iterrows():
                    dup_key = generate_duplicate_key_for_check(row, is_existing=False)
                    if dup_key in existing_keys:
                        skipped += 1
                        continue
                   
                    current_count += 1
                    sequence_num = current_count - 1
                    cdm_id = generate_cdm_id(row["TCA"], sequence_num)
                   
                    new_pc = float(row["PC"]) if not pd.isna(row["PC"]) else 0
                    new_miss = float(row["MISS_DISTANCE"]) if not pd.isna(row["MISS_DISTANCE"]) else 999999
                   
                    running_max_pc = max(running_max_pc, new_pc)
                    running_min_miss = min(running_min_miss, new_miss)
                   
                    now_str = str(datetime.utcnow())
                    tca_dt = pd.to_datetime(row["TCA"], utc=True, errors="coerce")
                    tca_local = tca_dt.tz_convert("Asia/Manila")
                    now_ph = get_current_ph_time()
                    hours_to_tca = (tca_local - now_ph).total_seconds() / 3600 if pd.notna(tca_dt) else 0

                    if current_count == 1:
                        trend = "NEW"
                    else:
                        prev_pc = pd.to_numeric(hist_df["Pc"].iloc[-1], errors='coerce') or new_pc
                        prev_miss = pd.to_numeric(hist_df["Miss_Distance"].iloc[-1], errors='coerce') or new_miss
                        if new_pc > prev_pc or new_miss < prev_miss:
                            trend = "WORSENING"
                        elif new_pc < prev_pc or new_miss > prev_miss:
                            trend = "IMPROVING"
                        else:
                            trend = "STABLE"
                   
                    risk, action = classify_risk(running_max_pc, running_min_miss, row["TCA"])
                   
                    new_row_dict = {
                        "EVENT_ID": event_id,
                        "CDM_ID": cdm_id,
                        "Date_Received": now_str,
                        "Primary_SCC": row.get("SAT1_OBJECT_DESIGNATOR", ""),
                        "Primary_Object": row.get("SAT_1_ID", ""),
                        "Secondary_SCC": row.get("SAT2_OBJECT_DESIGNATOR", ""),
                        "Secondary_Object": row.get("SAT_2_ID", ""),
                        "TCA_UTC": format_tca_display(row["TCA"]),
                        "TCA_PH": convert_utc_to_phst(row["TCA"]),
                        "HOURS_TO_TCA": hours_to_tca,
                        "RT_from_TCA": round(hours_to_tca, 2),
                        "Pc": new_pc,
                        "Miss_Distance": new_miss,
                        "R": row.get("RELATIVE_POSITION_R", ""),
                        "T": row.get("RELATIVE_POSITION_T", ""),
                        "N": row.get("RELATIVE_POSITION_N", ""),
                        "EVENT_MAX_PC": running_max_pc,
                        "EVENT_MIN_MISS": running_min_miss,
                        "EVENT_CDM_COUNT": current_count,
                        "EVENT_FIRST_SEEN": now_str,
                        "EVENT_LATEST_UPDATE": now_str,
                        "EVENT_RISK": risk,
                        "EVENT_STATUS": "OPEN",
                        "EVENT_TREND": trend,
                        "RECOMMENDED_ACTION": action,
                        "ACTION_EXECUTED": "",
                        "NOTES": "AUTO",
                        "SOURCE_FILE": uploaded_file.name,
                        "CDM_HASH": row["CDM_HASH"],
                        "CREATION_DATE": str(row.get("CREATION_DATE", "")).strip() or 
                                        str(row.get("CREATION_DATE_UTC", "")).strip()
                    }
                    rows_to_append.append(new_row_dict)
           
            if rows_to_append:
                for new_row_dict in rows_to_append:
                    values = [sanitize_for_gsheets(new_row_dict.get(col, "")) for col in SHEET_COLUMNS]
                    sheet.append_row(values)
                st.success(f"✅ Logged {len(rows_to_append)} new CDMs!")
                st.info(f"⏭️ Skipped {skipped} duplicates")
                st.rerun()
            else:
                st.info("ℹ️ No new CDMs to log — all were duplicates.")

# elif st.session_state.selected == "Event Analyzer":
#     #st.title("Event Analyzer")
#     df = load_database()
    
#     if st.button("🔄 Refresh Data from Google Sheets"):
#         load_database.clear()
#         st.rerun()
    
#     if df.empty or len(df) == 0:
#         st.info("No data in the database yet. Upload some CDMs on the first tab to get started!")
#     else:
#         event_summary = compute_event_summary(df, use_persisted_summary=True)
        
#         # Filter to active events (Past 3 days)
#         now_utc = pd.Timestamp.now('UTC')
#         active_latest = event_summary[
#             (event_summary["RT_from_TCA"] >= 0) &
#             (pd.to_datetime(event_summary["TCA_UTC"], utc=True, errors="coerce") >= now_utc - pd.Timedelta(days=3))
#         ].copy()
        
#         st.subheader("Event Action Log")
#         st.caption("Latest CDM per Active Event in the past 3 Days")
        
#         if active_latest.empty:
#             st.success("✅ No active events in the past 3 days.")
#         else:
#             active_latest = active_latest.copy()
            
#             # === Add current_pc and current_miss for the cards ===
#             latest_df = df.groupby("EVENT_ID").last().reset_index()
#             latest_values = latest_df[["EVENT_ID", "Pc", "Miss_Distance"]].rename(columns={
#                 "Pc": "current_pc",
#                 "Miss_Distance": "current_miss"
#             })
            
#             active_latest = active_latest.merge(latest_values, on="EVENT_ID", how="left")
            
#             # Normalize max_pc and min_miss
#             if "MAX_PC" in active_latest.columns:
#                 active_latest = active_latest.rename(columns={"MAX_PC": "max_pc"})
#             elif "EVENT_MAX_PC" in active_latest.columns:
#                 active_latest = active_latest.rename(columns={"EVENT_MAX_PC": "max_pc"})
            
#             if "MIN_MISS" in active_latest.columns:
#                 active_latest = active_latest.rename(columns={"MIN_MISS": "min_miss"})
#             elif "EVENT_MIN_MISS" in active_latest.columns:
#                 active_latest = active_latest.rename(columns={"EVENT_MIN_MISS": "min_miss"})
            
#             # Show only the editable action log (no table)
#             show_risk_alerts(
#                 event_summary, 
#                 is_upload=False, 
#                 editable=True, 
#                 alert_df=active_latest
#             )

#             # ====================== GLOBE VISUALIZATIONS ======================
#             st.subheader("Orbitial Comparison & Ground Tracks")
#             st.caption("Real-time ground tracks from Space-Track.org (SGP4)")

#             if active_latest.empty:
#                 st.info("No active conjunction events to visualize at the moment.")
#             else:
#                 for idx, row in active_latest.iterrows():
#                     event_id = str(row["EVENT_ID"]).strip()
                
#                     # === Improved Smart Name Extraction ===
#                     def parse_sat_names(eid: str, row):
#                         # Try to extract from Event ID
#                         if '_' in eid:
#                             parts = eid.split('_')
#                             sat1 = parts[0].strip()
#                             sat2 = parts[1].strip() if len(parts) > 1 else "Unknown"
#                         else:
#                             sat1 = eid.strip()
#                             sat2 = "Unknown"
                        
#                         # Fallback using SCC / Object columns
#                         if sat1 in ["Unknown", "NORAD 0", ""]:
#                             try:
#                                 norad1 = int(float(row.get("Primary_SCC", 0)))
#                                 if norad1 > 0:
#                                     sat1 = f"NORAD {norad1}"
#                             except:
#                                 pass
                                
#                         if sat2 in ["Unknown", "NORAD 0", ""]:
#                             try:
#                                 norad2 = int(float(row.get("Secondary_SCC", 0)))
#                                 if norad2 > 0:
#                                     sat2 = f"NORAD {norad2}"
#                             except:
#                                 pass
                        
#                         # Use Primary/Secondary Object names if available and better
#                         if row.get("Primary_Object") and str(row.get("Primary_Object")).strip():
#                             sat1 = str(row.get("Primary_Object")).strip()
#                         if row.get("Secondary_Object") and str(row.get("Secondary_Object")).strip():
#                             sat2 = str(row.get("Secondary_Object")).strip()
                        
#                         return sat1, sat2
                
#                     sat1_name, sat2_name = parse_sat_names(event_id, row)
                
#                     expander_title = f"Event {event_id}"
                
#                     with st.expander(expander_title, expanded=(idx == 0)):
#                         col1, col2 = st.columns([3, 1])
                    
#                         with col1:
#                             #st.markdown(f"**{sat1_name}** vs **{sat2_name}**")
                        
#                             with st.spinner(f"Propagating orbits for {event_id}..."):
#                                 tle_rows, tle_err = _read_tle_sheet_rows()
#                                 if tle_err:
#                                     st.warning(f"⚠️ Could not read TLE_CDM sheet: {tle_err}")
#                                 orbit_pts1, tca_dt1 = get_orbit_points_for_event(df, event_id, object_num=1, all_rows=tle_rows)
#                                 orbit_pts2, _       = get_orbit_points_for_event(df, event_id, object_num=2, all_rows=tle_rows)

#                             if orbit_pts1 and orbit_pts2:
#                                 render_globe_animated(orbit_pts1, sat1_name, orbit_pts2, sat2_name, tca_dt=tca_dt1)
#                             else:
#                                 st.warning("Could not generate orbit tracks.")
                    
#                         with col2:
#                             st.metric("Time to TCA", f"{row.get('RT_from_TCA', 'N/A'):.1f} hrs")
#                             st.metric("Current Pc", f"{row.get('current_pc', 0):.2e}")
#                             st.metric("Miss Distance", f"{row.get('current_miss', 0):.1f} km")
#                             st.caption(f"**TCA:** {str(row.get('TCA_UTC', ''))[:19]} UTC")

#Dummy Event Analyzer

elif st.session_state.selected == "Event Analyzer":
    df = load_database()

    if st.button("🔄 Refresh Data from Google Sheets"):
        load_database.clear()
        st.rerun()

    if df.empty or len(df) == 0:
        st.info("No data in the database yet. Upload some CDMs on the first tab to get started!")
    else:
        # ==================== EVENT/OBJECT LABELS (dummy display when DEMO_MODE) ====================
        df = df.copy()

        event_mapping, primary_mapping, secondary_mapping = _demo_event_maps(df)
        df["Pseudo_Event"] = df["EVENT_ID"].map(event_mapping)
        if "Primary_Object" in df.columns:
            df["Pseudo_Primary"] = df["Primary_Object"].map(primary_mapping)
        if "Secondary_Object" in df.columns:
            df["Pseudo_Secondary"] = df["Secondary_Object"].map(secondary_mapping)

        # ==================== MAIN CONTENT ====================
        event_summary = compute_event_summary(df, use_persisted_summary=True)

        # Filter to active events (Past 3 days)
        now_utc = pd.Timestamp.now('UTC')
        active_latest = event_summary[
            (event_summary["RT_from_TCA"] >= 0) &
            (pd.to_datetime(event_summary["TCA_UTC"], utc=True, errors="coerce") >= now_utc - pd.Timedelta(days=3))
        ].copy()

        st.success(f"Loaded **{len(df)}** CDM records • **{df['EVENT_ID'].nunique()}** unique events")

        st.subheader("Event Action Log")
        st.caption("Latest CDM per Active Event in the past 3 Days")

        if active_latest.empty:
            st.success("✅ No active events in the past 3 days.")
        else:
            active_latest = active_latest.copy()

            # Add pseudo event
            active_latest["Pseudo_Event"] = active_latest["EVENT_ID"].map(event_mapping)

            # Add current_pc and current_miss
            latest_df = df.groupby("EVENT_ID").last().reset_index()
            latest_values = latest_df[["EVENT_ID", "Pc", "Miss_Distance"]].rename(columns={
                "Pc": "current_pc",
                "Miss_Distance": "current_miss"
            })
            active_latest = active_latest.merge(latest_values, on="EVENT_ID", how="left")

            # Normalize column names
            if "MAX_PC" in active_latest.columns:
                active_latest = active_latest.rename(columns={"MAX_PC": "max_pc"})
            elif "EVENT_MAX_PC" in active_latest.columns:
                active_latest = active_latest.rename(columns={"EVENT_MAX_PC": "max_pc"})

            if "MIN_MISS" in active_latest.columns:
                active_latest = active_latest.rename(columns={"MIN_MISS": "min_miss"})
            elif "EVENT_MIN_MISS" in active_latest.columns:
                active_latest = active_latest.rename(columns={"EVENT_MIN_MISS": "min_miss"})

            # ====================== SHOW RISK ALERTS (with dummy Event ID) ======================
            # Create clean display version - replace EVENT_ID with Pseudo_Event
            display_alert_df = active_latest.copy()
            if "Pseudo_Event" in display_alert_df.columns:
                display_alert_df = display_alert_df.drop(columns=["EVENT_ID"], errors="ignore")
                display_alert_df = display_alert_df.rename(columns={"Pseudo_Event": "EVENT_ID"})

            show_risk_alerts(
                event_summary,
                is_upload=False,
                editable=True,
                alert_df=display_alert_df
            )

            # ====================== ORBITAL VISUALIZATIONS ======================
            st.subheader("Orbital Comparison & Ground Tracks")
            st.caption("Real-time ground tracks from Space-Track.org (SGP4)")

            # Render each active event with pseudo names
            for idx, row in active_latest.iterrows():
                real_event_id = str(row["EVENT_ID"]).strip()
                pseudo_event = event_mapping.get(real_event_id, real_event_id)

                # Get pseudo object names
                event_rows = df[df["EVENT_ID"] == real_event_id]
                pseudo_primary = event_rows["Pseudo_Primary"].iloc[0] if not event_rows.empty and "Pseudo_Primary" in event_rows.columns else row.get("Primary_Object", "Unknown")
                pseudo_secondary = event_rows["Pseudo_Secondary"].iloc[0] if not event_rows.empty and "Pseudo_Secondary" in event_rows.columns else row.get("Secondary_Object", "Unknown")

                expander_title = f"{pseudo_event} — {pseudo_primary} vs {pseudo_secondary}"

                with st.expander(expander_title, expanded=(idx == 0)):
                    left_col, right_col = st.columns([1, 3])

                    with left_col:
                        st.markdown("**Event Information**")
                        with st.container(border=True):
                            risk = row.get("RISK", row.get("EVENT_RISK", "LOW"))
                            risk_colors = {
                                "CRITICAL": "#dc2626",
                                "HIGH": "#f97316",
                                "MEDIUM": "#eab308",
                                "LOW": "#4ade80",
                                "LAPSED": "#9ca3af"
                            }
                            color = risk_colors.get(risk, "#9ca3af")

                            st.markdown(f"""
                                <div style="background-color:{color}20; padding:16px; border-radius:12px;
                                            text-align:center; margin-bottom:20px;">
                                    <span style="font-size:2.2rem; font-weight:700; color:{color};">{risk}</span>
                                </div>
                            """, unsafe_allow_html=True)

                            metrics = [
                                ("Event ID", pseudo_event),
                                ("Primary Object", pseudo_primary),
                                ("Secondary Object", pseudo_secondary),
                                ("TCA (UTC)", str(row.get('TCA_UTC', ''))[:19]),
                                ("Time to TCA", f"{row.get('RT_from_TCA', 0):.1f} hours"),
                                ("Current Pc", f"{row.get('current_pc', 0):.2e}"),
                                ("Miss Distance", f"{row.get('current_miss', 0):.1f} km"),
                            ]

                            met_col1, met_col2 = st.columns(2)
                            for i, (title, value) in enumerate(metrics):
                                with (met_col1 if i % 2 == 0 else met_col2):
                                    st.markdown(f":blue-badge[{title}]")
                                    st.markdown(f"""
                                        <div style="font-size: 1.28rem; font-weight: 700; margin: 8px 0 16px 0; line-height: 1.3;">
                                            {value}
                                        </div>
                                    """, unsafe_allow_html=True)

                    with right_col:
                        st.markdown("**Orbital Comparison & Ground Tracks**")
                        st.caption(f"Event: **{pseudo_event}**")

                        with st.spinner(f"Propagating orbits for {pseudo_event}..."):
                            tle_rows, tle_err = _read_tle_sheet_rows()
                            if tle_err:
                                st.warning(f"⚠️ Could not read TLE_CDM sheet: {tle_err}")
                            orbit_pts1, tca_dt1 = get_orbit_points_for_event(df, real_event_id, object_num=1, all_rows=tle_rows)
                            orbit_pts2, _ = get_orbit_points_for_event(df, real_event_id, object_num=2, all_rows=tle_rows)

                        if orbit_pts1 and orbit_pts2:
                            render_globe_animated(
                                orbit_pts1, pseudo_primary,
                                orbit_pts2, pseudo_secondary,
                                tca_dt=tca_dt1
                            )
                        else:
                            st.warning("Could not generate orbit tracks.")

#CDM Database as of 09 July 2026
elif st.session_state.selected == "CDM Database":
    df = load_database()
    if st.button("🔄 Refresh Data from Google Sheets"):
        load_database.clear()
        st.rerun()

    if df.empty:
        st.info("No data in the database yet.")
    else:
        # ==================== EVENT/OBJECT LABELS (dummy display when DEMO_MODE) ====================
        # Real EVENT_ID / Primary_Object / Secondary_Object stay in `df` and are
        # used for every underlying lookup, filter, and orbit propagation below.
        # Only what gets rendered on screen (selector, metrics, chart labels) is
        # swapped for deterministic "Event A" / "Object A" style labels when
        # DEMO_MODE is on — same helper used on the Event Analyzer tab.
        df = df.copy()

        event_mapping, primary_mapping, secondary_mapping = _demo_event_maps(df)
        df["Pseudo_Event"] = df["EVENT_ID"].map(event_mapping)
        if "Primary_Object" in df.columns:
            df["Pseudo_Primary"] = df["Primary_Object"].map(primary_mapping)
        if "Secondary_Object" in df.columns:
            df["Pseudo_Secondary"] = df["Secondary_Object"].map(secondary_mapping)

        st.success(f"Loaded **{len(df)}** CDM records • **{df['EVENT_ID'].nunique()}** unique events")

        # Filters
        col1, col2 = st.columns([1, 2])
        with col1:
            risk_options = sorted(df["EVENT_RISK"].dropna().unique().tolist()) if "EVENT_RISK" in df.columns else []
            selected_risk = st.multiselect("Risk Level", options=risk_options, default=[], placeholder="All risks")
        with col2:
            search_term = st.text_input("Search (Event / Object)", "")

        filtered_df = df.copy()
        if selected_risk:
            filtered_df = filtered_df[filtered_df["EVENT_RISK"].isin(selected_risk)]
        if search_term:
            mask = pd.Series(False, index=filtered_df.index)
            for col in ["Pseudo_Event", "Pseudo_Primary", "Pseudo_Secondary", "EVENT_ID", "CDM_ID", "Primary_Object", "Secondary_Object"]:
                if col in filtered_df.columns:
                    mask |= filtered_df[col].astype(str).str.contains(search_term, case=False, na=False)
            filtered_df = filtered_df[mask]

        # Event Viewer Section
        st.subheader("Event Viewer")
        st.caption("Select an event to view detailed information and orbit visualization")

        if not filtered_df.empty:
            event_summary = compute_event_summary(filtered_df)

            # Selector shows pseudo names only; real EVENT_ID never reaches the UI
            event_list = sorted(event_summary["EVENT_ID"].map(event_mapping).dropna().unique())

            selected_pseudo_event = st.selectbox(
                "Select Event to View",
                options=event_list,
                index=0
            )

            # Resolve the pseudo name back to its real EVENT_ID for all data lookups
            selected_event = [k for k, v in event_mapping.items() if v == selected_pseudo_event][0]

            # Get data for selected event
            selected_summary = event_summary[event_summary["EVENT_ID"] == selected_event].iloc[0]
            event_df = df[df["EVENT_ID"] == selected_event]
            latest_row = event_df.iloc[-1]

            left_col, right_col = st.columns([1, 3])

            with left_col:
                st.markdown("**Event Information**")
                with st.container(border=True):
                    # Risk Badge
                    risk_colors = {"CRITICAL": "#dc2626", "HIGH": "#f97316", "MEDIUM": "#eab308", "LOW": "#4ade80", "LAPSED": "#9ca3af"}
                    color = risk_colors.get(selected_summary.get("RISK"), "#9ca3af")
                    
                    st.markdown(f"""
                        <div style="background-color:{color}20; padding:16px; border-radius:12px; text-align:center; margin-bottom:20px;">
                            <span style="font-size:2.2rem; font-weight:700; color:{color};">{selected_summary.get('RISK', '—')}</span>
                        </div>
                    """, unsafe_allow_html=True)

                    # Split metrics into two columns (pseudo names for display)
                    metrics = [
                        ("Event ID", selected_pseudo_event),
                        ("Primary Object", latest_row.get("Pseudo_Primary", "—")),
                        ("Secondary Object", latest_row.get("Pseudo_Secondary", "—")),
                        ("TCA (UTC)", selected_summary.get("TCA_UTC", "—")),
                        ("TCA (PhST)", selected_summary.get("TCA_PH", "—")),
                        ("Time to TCA", f"{selected_summary.get('RT_from_TCA', 0):.1f} hours"),
                        ("Current Pc", f"{latest_row.get('Pc', latest_row.get('PC', 0)):.2e}"),
                        ("Miss Distance", f"{latest_row.get('Miss_Distance', latest_row.get('MISS_DISTANCE', 0)):.1f} m"),
                        ("Trend", selected_summary.get("TREND", "—")),
                    ]

                    met_col1, met_col2 = st.columns(2)

                    for i, (title, value) in enumerate(metrics):
                        with (met_col1 if i % 2 == 0 else met_col2):
                            st.markdown(f":blue-badge[{title}]")
                            st.markdown(f"""
                                <div style="font-size: 1.28rem; font-weight: 700; margin: 8px 0 16px 0; line-height: 1.3;">
                                    {value}
                                </div>
                            """, unsafe_allow_html=True)

                # Action Status - Read Only Summary
                st.markdown("**Action History**")
                with st.container(border=True):
                    
                    # Optional: Show historical actions if available
                    if len(event_df) > 1:
                        history = event_df[["Date_Received", "ACTION_EXECUTED"]].dropna(subset=["ACTION_EXECUTED"])
                        if not history.empty:
                            st.dataframe(history, hide_index=True, use_container_width=True)

            with right_col:
                st.markdown("**Orbital Comparison & Ground Tracks**")
                st.caption(f"Event: **{selected_pseudo_event}**")
                
                with st.spinner("Generating orbit visualization..."):
                    tle_rows, tle_err = _read_tle_sheet_rows()
                    if tle_err:
                        st.warning(f"⚠️ Could not read TLE_CDM sheet: {tle_err}")
                    orbit_pts1, tca_dt1 = get_orbit_points_for_event(df, selected_event, object_num=1, all_rows=tle_rows)
                    orbit_pts2, _       = get_orbit_points_for_event(df, selected_event, object_num=2, all_rows=tle_rows)

                if orbit_pts1 and orbit_pts2:
                    render_globe_animated(
                        orbit_pts1,
                        latest_row.get("Pseudo_Primary", "Primary"),
                        orbit_pts2,
                        latest_row.get("Pseudo_Secondary", "Secondary"),
                        tca_dt=tca_dt1,
                    )
                else:
                    st.warning("Could not generate orbit tracks for this event.")

        else:
            st.info("No events match your filters.")


st.caption("Compiled by the Space Mission Control and Operations Division (SMCOD) of the Philippine Space Agency (PhilSA)")