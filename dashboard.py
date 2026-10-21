import streamlit as st
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import random
import plotly.express as px
import requests
from datetime import datetime, timezone
import json
import streamlit.components.v1 as components
from skyfield.api import EarthSatellite, load, wgs84

from SSA_tools.rocket import (
    load_launch_database
)

from SSA_tools.collision import (
    compute_event_summary,
    load_database
)

import os
import gspread
from google.oauth2.service_account import Credentials

# ══════════════════════════════════════════════════════════════════════════
# DUMMY DISPLAY MODE (optional, for demos/screenshots)
#
# When enabled, the rocket/launch site names, conjunction Event IDs, and
# re-entry object names shown across the Activity tabs (Alerts, Recent
# Launches, Conjunction Events, Recent Re-entries) are swapped for
# deterministic "Rocket A" / "Site A" / "Event A" / "Object A" style dummy
# labels. All underlying data loading, filtering, and sorting still use the
# REAL values -- only what gets *displayed* changes. Space Weather Events
# has no object/rocket names, so it's unaffected either way.
#
# To disable completely: leave/set DEMO_MODE = False. Nothing else needs to
# change -- every mapping below becomes an identity map (or falls back to
# the real value) and every tab behaves exactly as before.
# ══════════════════════════════════════════════════════════════════════════
DEMO_MODE = False
# DEMO_MODE = True   # uncomment this line to enable dummy names


def _demo_label_map(values, prefix):
    """
    Build a real-value -> display-label dict for a column/list of real values.
    DEMO_MODE True: deterministic "<prefix> A", "<prefix> B", ... labels,
    assigned in sorted order so the same value always gets the same label.
    DEMO_MODE False: identity map, so callers display the real value.
    """
    unique_vals = sorted({v for v in values if pd.notna(v)})
    if DEMO_MODE:
        return {v: f"{prefix} {chr(65 + i)}" for i, v in enumerate(unique_vals)}
    return {v: v for v in unique_vals}


def _demo_positional_label(i, prefix, real_value=None):
    """
    Label for the i-th item in an already-ordered list that has no stable
    real ID to map from (e.g. a re-entry object keyed only by its position
    in the alerts list).
    DEMO_MODE True: deterministic "<prefix> A", "<prefix> B", ...
    DEMO_MODE False: the real value if given, else "<prefix> <n>".
    """
    if DEMO_MODE:
        return f"{prefix} {chr(65 + i)}"
    if real_value not in (None, "", "—"):
        return real_value
    return f"{prefix} {i + 1}"


# ============================================================
# CELESTRAK GOOGLE SHEET CACHE
# Spreadsheet: SMCOD SSA ODR
# Worksheets:
#   - SATCAT
#   - GP_TLE
#   - metadata
# ============================================================

CELESTRAK_SPREADSHEET_NAME = "SMCOD SSA ODR"
SATCAT_WORKSHEET = "SATCAT"
GP_TLE_WORKSHEET = "GP_TLE"


@st.cache_data(ttl=600)
def fetch_satcat_full():
    try:
        client = _get_gspread_client()
        if client is None:
            return None, "GCP credentials not configured."

        sh = client.open(CELESTRAK_SPREADSHEET_NAME)
        ws = sh.worksheet(SATCAT_WORKSHEET)
        rows = ws.get_all_records()

        if not rows:
            return None, "SATCAT worksheet is empty."

        df = pd.DataFrame(rows)
        df.columns = [str(c).strip() for c in df.columns]

        return df, None

    except Exception as e:
        return None, str(e)


@st.cache_data(ttl=600)
def fetch_gp_tle(catnr=None, name=None, intdes=None):
    try:
        client = _get_gspread_client()
        if client is None:
            return [], "GCP credentials not configured."

        sh = client.open(CELESTRAK_SPREADSHEET_NAME)
        ws = sh.worksheet(GP_TLE_WORKSHEET)
        rows = ws.get_all_records()

        if not rows:
            return [], "GP_TLE worksheet is empty."

        df = pd.DataFrame(rows)
        df.columns = [str(c).strip() for c in df.columns]

        if catnr is not None and "NORAD_CAT_ID" in df.columns:
            df = df[df["NORAD_CAT_ID"].astype(str) == str(catnr)]

        if name is not None and "OBJECT_NAME" in df.columns:
            df = df[
                df["OBJECT_NAME"]
                .astype(str)
                .str.contains(str(name), case=False, na=False)
            ]

        if intdes is not None and "OBJECT_ID" in df.columns:
            df = df[
                df["OBJECT_ID"]
                .astype(str)
                .str.contains(str(intdes), case=False, na=False)
            ]

        return df.to_dict("records"), None

    except Exception as e:
        return [], str(e)


def compute_kpis_from_satcat(catalog):
    try:
        if catalog is None or catalog.empty:
            return None

        df = catalog.copy()
        df.columns = [str(c).strip().upper() for c in df.columns]

        total_tracked = len(df)

        object_type_col = None
        for possible in ["OBJECT_TYPE", "OBJECT TYPE", "OBJECT_TYPE_NAME", "TYPE"]:
            if possible in df.columns:
                object_type_col = possible
                break

        status_col = None
        for possible in ["OPS_STATUS_CODE", "OPERATIONAL_STATUS", "STATUS"]:
            if possible in df.columns:
                status_col = possible
                break

        if object_type_col is None:
            return {
                "active_sats": 0,
                "debris_count": 0,
                "rocket_bodies": 0,
                "total_tracked": total_tracked,
            }

        obj = df[object_type_col].astype(str).str.upper().str.strip()

        payloads = (
            obj.str.contains("PAYLOAD", na=False)
            | obj.eq("PAY")
            | obj.eq("PL")
        )

        debris = (
            obj.str.contains("DEBRIS", na=False)
            | obj.eq("DEB")
        )

        rockets = (
            obj.str.contains("ROCKET", na=False)
            | obj.str.contains("R/B", na=False)
            | obj.eq("RB")
        )

        debris_count = int(debris.sum())
        rocket_bodies = int(rockets.sum())

        if status_col:
            status = df[status_col].astype(str).str.upper().str.strip()

            active_status = (
                status.eq("+")
                | status.eq("ACTIVE")
                | status.eq("OPERATIONAL")
                | status.eq("OP")
            )

            active_sats = int((payloads & active_status).sum())
        else:
            active_sats = int(payloads.sum())

        return {
            "active_sats": active_sats,
            "debris_count": debris_count,
            "rocket_bodies": rocket_bodies,
            "total_tracked": total_tracked,
        }

    except Exception:
        return None


def search_satcat(catnr=None, name=None):
    catalog, err = fetch_satcat_full()

    if err or catalog is None:
        return [], err or "No SATCAT data available."

    df = catalog.copy()
    df.columns = [str(c).strip() for c in df.columns]

    if catnr is not None and "NORAD_CAT_ID" in df.columns:
        df = df[df["NORAD_CAT_ID"].astype(str) == str(catnr)]

    if name is not None and "OBJECT_NAME" in df.columns:
        df = df[
            df["OBJECT_NAME"]
            .astype(str)
            .str.contains(str(name), case=False, na=False)
        ]

    return df.head(25).to_dict("records"), None


def lookup_names(norad_list):
    catalog, err = fetch_satcat_full()

    if err or catalog is None or catalog.empty:
        return {}

    df = catalog.copy()
    df.columns = [str(c).strip() for c in df.columns]

    if "NORAD_CAT_ID" not in df.columns or "OBJECT_NAME" not in df.columns:
        return {}

    df["NORAD_CAT_ID"] = df["NORAD_CAT_ID"].astype(str)

    result = {}
    for norad in norad_list:
        match = df[df["NORAD_CAT_ID"] == str(norad)]
        if not match.empty:
            try:
                result[int(norad)] = match.iloc[0]["OBJECT_NAME"]
            except Exception:
                result[norad] = match.iloc[0]["OBJECT_NAME"]

    return result


def parse_manual_tle(tle_text):
    lines = [line.strip() for line in tle_text.splitlines() if line.strip()]

    if len(lines) == 2:
        name = "CUSTOM SATELLITE"
        line1, line2 = lines
    elif len(lines) >= 3:
        name = lines[0]
        line1, line2 = lines[1], lines[2]
    else:
        return None, "Please paste a valid 2-line or 3-line TLE."

    if not line1.startswith("1 ") or not line2.startswith("2 "):
        return None, "Invalid TLE format."

    return {
        "OBJECT_NAME": name,
        "NORAD_CAT_ID": line1[2:7].strip(),
        "TLE_LINE1": line1,
        "TLE_LINE2": line2,
        "EPOCH": line1[18:32].strip(),
    }, None

st.set_page_config(page_title="SSA - Dashboard", page_icon=":utils/PhilSA_v1_White.png:", layout="wide")

# ---------------- REMOVE TOP PADDING ----------------
st.markdown("""
<style>
    .block-container {
        padding-top: 4.8rem !important;
    }
</style>
""", unsafe_allow_html=True)

# ---------------- HEADER ----------------
# bg_color = "#1a4fa3"
# text_color = "#ffffff"

# st.markdown(f"""
# <div style="
#     text-align: center;
#     padding: 15px 0;
#     margin: 0;
#     border-radius: 10px;
#     background-color: {bg_color};
#     color: {text_color};
# ">
#     <div style="font-size: 32px; font-weight: 700; margin-bottom: 8px;">
#         Welcome to the Space Situational Awareness Application (SSA App)
#     </div>
#     <div style="font-size: 18px; font-weight: 400; font-style: italic; opacity: 0.8;">
#         Compiled and operated by the Space Mission Control and Operations Division (SMCOD) of Philippine Space Agency (PhilSA)
#     </div>
# </div>
# """, unsafe_allow_html=True)



# ---------------- NAVIGATION ----------------
query_params = st.query_params
if "nav" in query_params:
    page = query_params["nav"]
    if page == "collision":
        st.switch_page("SSA_tools/collision.py")
    elif page == "rocket":
        st.switch_page("SSA_tools/rocket.py")
    elif page == "reentry":
        st.switch_page("SSA_tools/reenter.py")
    elif page == "weather":
        st.switch_page("SSA_tools/weather.py")

# ---------------- CARD STYLES ----------------
st.markdown("""
<link href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined" rel="stylesheet">
<style>
.kpi-link, .kpi-link:link, .kpi-link:visited, .kpi-link:hover, .kpi-link:active {
    text-decoration: none !important;
    color: white !important;
}
.kpi-link * { color: white !important; }
.kpi-card {
    border-radius: 15px;
    padding: 18px;
    height: 160px;
    width: 100%;
    box-sizing: border-box;
    margin-top: 15px;
    margin-bottom: 20px;
    box-shadow: 0 4px 15px rgba(0,0,0,0.25);
    cursor: pointer;
    transition: all 0.2s ease;
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    justify-content: flex-start;
}
.kpi-card:hover {
    transform: translateY(-4px);
    box-shadow: 0 8px 20px rgba(26,79,163,0.5);
}
.material-symbols-outlined { font-size: 35px; margin-bottom: 25px; }
.kpi-title  { font-size: 25px; font-weight: 600; margin-bottom: 4px; }
.kpi-value  { font-size: 15px; font-weight: 800; margin-bottom: 2px; opacity: 0.9; }
.kpi-delta  { font-size: 12px; opacity: 0.75; }
.new-kpi-card {
    background: var(--secondaryBackgroundColor);
    border-radius: 16px;
    padding: 24px 16px;
    text-align: center;
    border: 2px solid rgba(255,255,255,1);
    box-shadow: 0 4px 15px rgba(0,0,0,0.4);
    height: 100%;
    transition: transform 0.2s ease, box-shadow 0.2s ease;
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: center;
}
.new-kpi-card:hover {
    transform: translateY(-4px);
    box-shadow: 0 8px 25px rgba(0,0,0,0.15);
}
.new-kpi-icon  { font-size: 42px; margin-bottom: 12px; color: #1a4fa3; }
.new-kpi-title { font-size: 1.05rem; color: #b0b0b0; margin-bottom: 10px; font-weight: 500; }
.new-kpi-value { font-size: 2.85rem; font-weight: 700; margin: 0; line-height: 1.1; }
.quick-action-card {
    border-radius: 14px;
    padding: 14px 16px;
    height: 92px;
    box-shadow: 0 4px 12px rgba(0,0,0,0.25);
    transition: all 0.3s ease;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 14px;
}
.quick-action-card:hover {
    transform: translateY(-4px);
    box-shadow: 0 8px 20px rgba(26,79,163,0.5);
}
.quick-action-icon  { font-size: 32px; min-width: 32px; color: white; }
.quick-action-title { font-size: 1.0rem; font-weight: 600; color: white; line-height: 1.3; text-align: left; }
</style>
""", unsafe_allow_html=True)
# ==================== RISK STYLING (from collision.py) ====================
st.markdown("""
<style>
.risk-btn {
    display: inline-block;
    padding: 5px 12px;
    border-radius: 10px;
    font-weight: 600;
    font-size: 13px;
    color: white;
    margin-top: 4px;
}
.risk-critical { background-color: #dc2626; }
.risk-high { background-color: #f97316; }
.risk-medium { background-color: #eab308; color: black; }
.risk-low { background-color: #90EE90; color: black; }
.risk-lapsed { background-color: #9ca3af; }
</style>
""", unsafe_allow_html=True)

# ---------------- CARD FUNCTIONS ----------------
def kpi_card(icon, title, value, delta, page_path, color):
    st.markdown(f"""
    <a href="/{page_path}" target="_self" class="kpi-link">
        <div class="kpi-card" style="background: {color};">
            <span class="material-symbols-outlined">{icon}</span>
            <div class="kpi-title">{title}</div>
            <div class="kpi-value">{value}</div>
            <div class="kpi-delta">{delta}</div>
        </div>
    </a>
    """, unsafe_allow_html=True)

def new_kpi_card(icon, title, value):
    st.markdown(f"""
    <div class="new-kpi-card">
        <span class="material-symbols-outlined new-kpi-icon">{icon}</span>
        <div class="new-kpi-title">{title}</div>
        <div class="new-kpi-value">{value}</div>
    </div>
    """, unsafe_allow_html=True)

def quick_action_card(icon, title, color, page_path=None):
    if page_path:
        html = f"""
        <a href="/{page_path}" target="_self" style="text-decoration:none; color:inherit;">
            <div class="quick-action-card" style="background: {color};">
                <span class="material-symbols-outlined quick-action-icon">{icon}</span>
                <div class="quick-action-title">{title}</div>
            </div>
        </a>"""
    else:
        html = f"""
        <div class="quick-action-card" style="background: {color};">
            <span class="material-symbols-outlined quick-action-icon">{icon}</span>
            <div class="quick-action-title">{title}</div>
        </div>"""
    st.markdown(html, unsafe_allow_html=True)

# ============================================================
# CONSTANTS
# ============================================================
DIWATA2_NORAD = 43678

# ============================================================
# REAL DATA: KPI METRICS FROM GOOGLE SHEET CELESTRAK CACHE (SATCAT)
# ============================================================
# CelesTrak data is now fetched outside Streamlit Cloud by the local
# scheduled relay script and stored in Google Sheets. The dashboard reads
# through SSA_tools/celestrak.py, which should be replaced by the
# Google-Sheet-backed celestrak_gsheet.py module from the relay package.
def fetch_dashboard_kpis():
    """Return SATCAT-derived KPI counts computed from the Google Sheet cache."""
    catalog, err = fetch_satcat_full()
    kpis = compute_kpis_from_satcat(catalog)
    if kpis is None:
        return None, err or "No SATCAT data available from the Google Sheet cache. Run the CelesTrak relay first."
    return kpis, err

# ============================================================
# REAL DATA: KP INDEX FORECAST FROM NOAA SWPC
# ============================================================

# NOAA SWPC blocks requests without a browser User-Agent (returns 403)
_NOAA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.swpc.noaa.gov/",
}

@st.cache_data(ttl=180)   # 3-min cache — Kp updates every 3 hours
def fetch_kp_forecast():
    """
    Builds a 7-day Kp picture for the dashboard bar chart:
      - Past + today:  noaa-planetary-k-index.json
        Format: list of dicts {"time_tag": "2026-05-25T00:00:00", "Kp": 2.67}
        Key is capital "Kp", ISO-8601 time with "T" separator.
      - Future days:   noaa-planetary-k-index-forecast.json
        Format: list-of-lists, row[0] = header.
    Returns (days_list, kp_values_list) — daily max Kp, up to 7 days.
    """
    def _parse_tt(s):
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                pass
        return None

    today = datetime.utcnow().date()
    seen_dates = {}   # day_str -> max Kp

    # ── Step 1: observed 3-hourly Kp (past 30 days + today) ──────────────────
    try:
        r = requests.get(
            "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json",
            headers=_NOAA_HEADERS, timeout=15
        )
        r.raise_for_status()
        for item in r.json():
            try:
                dt = _parse_tt(item["time_tag"])
                if dt is None:
                    continue
                kp_val = float(item.get("Kp") or item.get("kp") or 0)
                if kp_val < 0:
                    continue
                day_str = dt.strftime("%b %d")
                seen_dates[day_str] = max(seen_dates.get(day_str, 0.0), kp_val)
            except Exception:
                continue
    except Exception:
        pass

    # ── Step 2: forecast (future 3 days) ─────────────────────────────────────
    try:
        r = requests.get(
            "https://services.swpc.noaa.gov/products/noaa-planetary-k-index-forecast.json",
            headers=_NOAA_HEADERS, timeout=15
        )
        r.raise_for_status()
        for row in r.json()[1:]:   # row[0] = header
            try:
                dt = _parse_tt(row[0])
                if dt is None or dt.date() <= today:
                    continue
                kp_val  = float(row[1])
                day_str = dt.strftime("%b %d")
                seen_dates[day_str] = max(seen_dates.get(day_str, 0.0), kp_val)
            except Exception:
                continue
    except Exception:
        pass

    # ── Step 3: build 7-day window ending today+3 ────────────────────────────
    if seen_dates:
        # Collect last 4 observed days + up to 3 forecast days
        all_days_ordered = sorted(
            seen_dates.keys(),
            key=lambda d: datetime.strptime(d + f" {datetime.utcnow().year}", "%b %d %Y")
        )
        # Take last 4 observed + 3 future = 7 bars
        observed = [d for d in all_days_ordered
                    if datetime.strptime(d + f" {datetime.utcnow().year}", "%b %d %Y").date() <= today]
        future   = [d for d in all_days_ordered
                    if datetime.strptime(d + f" {datetime.utcnow().year}", "%b %d %Y").date() > today]
        window = observed[-4:] + future[:3]
        if window:
            return window, [round(seen_dates[d], 1) for d in window]

    # ── Fallback: synthetic data ──────────────────────────────────────────────
    today_dt = datetime.utcnow()
    days, values = [], []
    for i in range(7):
        days.append((today_dt + timedelta(days=i - 3)).strftime("%b %d"))
        r = random.random()
        kp = random.uniform(0, 2.5) if r < 0.5 else \
             random.uniform(3, 4)   if r < 0.75 else \
             random.uniform(5, 6)   if r < 0.9  else \
             random.uniform(7, 9)
        values.append(round(kp, 1))
    return days, values

# ============================================================
# REAL DATA: SATELLITE TLE & POSITION (GOOGLE SHEET CELESTRAK CACHE)
# ============================================================
def fetch_gp_record(catnr=None, name=None, intdes=None):
    """
    Fetch a single GP/TLE record from the Google Sheet CelesTrak cache for a NORAD ID or name.
    Returns (record: dict|None, error: str|None) in the shape
    {OBJECT_NAME, NORAD_CAT_ID, TLE_LINE1, TLE_LINE2, EPOCH}.
    Used both for Diwata-2 and for the Satellite Tracker search feature.
    """
    records, err = fetch_gp_tle(catnr=catnr, name=name, intdes=intdes)
    if err or not records:
        return None, err or "No GP record found."
    return records[0], None

@st.cache_data(ttl=300)
def build_real_time_orbit(tle1, tle2, name="SATELLITE", duration_seconds=6000, step_seconds=1):
    ts = load.timescale()
    satellite = EarthSatellite(tle1, tle2, name, ts)

    now_utc = datetime.now(timezone.utc)
    now_ts = ts.from_datetime(now_utc)

    geocentric_now = satellite.at(now_ts)
    subpoint_now = wgs84.subpoint(geocentric_now)

    current_lat = subpoint_now.latitude.degrees
    current_lon = subpoint_now.longitude.degrees

    points = []

    for second in range(0, duration_seconds + 1, step_seconds):
        t = ts.utc(
            now_utc.year,
            now_utc.month,
            now_utc.day,
            now_utc.hour,
            now_utc.minute,
            now_utc.second + second,
        )

        geocentric = satellite.at(t)
        subpoint = wgs84.subpoint(geocentric)

        points.append({
            "t": second,
            "lat": round(subpoint.latitude.degrees, 6),
            "lon": round(subpoint.longitude.degrees, 6),
        })

    return {
        "generated_at_ms": int(now_utc.timestamp() * 1000),
        "current_lat": round(current_lat, 6),
        "current_lon": round(current_lon, 6),
        "points": points,
    }

def propagate_tle_simple(tle1, tle2, minutes_ahead=0):
    """
    Rough circular-orbit propagation to estimate sub-satellite lat/lon.
    Returns (lat, lon) in degrees.
    """
    try:
        inc         = float(tle2[8:16])
        raan        = float(tle2[17:25])
        arg_perigee = float(tle2[34:42])
        mean_anomaly= float(tle2[43:51])
        mean_motion = float(tle2[52:63])   # rev/day

        period_min  = 1440.0 / mean_motion
        deg_per_min = 360.0  / period_min
        ma = (mean_anomaly + deg_per_min * minutes_ahead) % 360.0

        nu  = np.radians(ma)
        om  = np.radians(arg_perigee)
        i   = np.radians(inc)
        W   = np.radians(raan)

        x_orb, y_orb = np.cos(nu), np.sin(nu)

        x_eci = (np.cos(W)*np.cos(om) - np.sin(W)*np.sin(om)*np.cos(i))*x_orb + \
                (-np.cos(W)*np.sin(om) - np.sin(W)*np.cos(om)*np.cos(i))*y_orb
        y_eci = (np.sin(W)*np.cos(om) + np.cos(W)*np.sin(om)*np.cos(i))*x_orb + \
                (-np.sin(W)*np.sin(om) + np.cos(W)*np.cos(om)*np.cos(i))*y_orb
        z_eci = (np.sin(om)*np.sin(i))*x_orb + (np.cos(om)*np.sin(i))*y_orb

        lat = np.degrees(np.arcsin(np.clip(z_eci, -1, 1)))

        # Parse epoch for GMST
        epoch_year = int(tle1[18:20])
        epoch_day  = float(tle1[20:32])
        full_year  = 2000 + epoch_year if epoch_year < 57 else 1900 + epoch_year
        epoch_dt   = datetime(full_year, 1, 1) + timedelta(days=epoch_day - 1)
        now        = datetime.utcnow() + timedelta(minutes=minutes_ahead)
        elapsed    = (now - epoch_dt).total_seconds() / 86400.0

        gmst_offset = (elapsed * 360.98564724) % 360
        lon = (np.degrees(np.arctan2(y_eci, x_eci)) - gmst_offset) % 360
        if lon > 180:
            lon -= 360

        return round(float(lat), 2), round(float(lon), 2)
    except Exception:
        return 14.6, 121.1   # Manila area fallback

# ============================================================
# REENTRY DATA FROM GOOGLE SHEETS
# ============================================================
SHEET_ID   = "1isa_rOueh0yDY9OM43Q2AWZoKPGrlvEo7JOSHJdW3jE"
DATA_SHEET = "Data"

def _get_gspread_client():
    """Google Sheets client."""
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    try:
        creds = Credentials.from_service_account_info(
            st.secrets["gcp_service_account_odr"],
            scopes=scopes,
        )
    except Exception:
        path = os.getenv("GCP_JSON_ODR")
        if not path or not os.path.exists(path):
            return None
        creds = Credentials.from_service_account_file(
            path,
            scopes=scopes,
        )

    return gspread.authorize(creds)


# ============================================================
# GOOGLE SHEET CACHE — TIP COUNT
# ============================================================
TIP_CACHE_SHEET     = "Spacetrack"
SAFE_REFRESH_MINUTE = 16   # only refresh the TIP GSheet scan at HH:16
MAX_CACHE_HOURS     = 24


def _get_or_create_worksheet(sh, title, rows=10, cols=10, header=None):
    """Return worksheet by title, creating it with an optional header row if missing."""
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=rows, cols=cols)
        if header:
            ws.append_row(header)
        return ws


def _get_tip_cache_ws():
    client = _get_gspread_client()
    sh = client.open_by_key("1isa_rOueh0yDY9OM43Q2AWZoKPGrlvEo7JOSHJdW3jE")
    return _get_or_create_worksheet(
        sh, TIP_CACHE_SHEET,
        header=["updated_at_utc", "tip_count_7d"]
    )


def _read_cache(ws, age_hours_col_default=None):
    """
    Generic cache reader. Returns dict with keys:
      updated_at_utc, age_hours, row (the raw record dict)
    Returns None if no rows exist or parsing fails.
    """
    try:
        rows = ws.get_all_records()
        if not rows:
            return None
        latest = rows[-1]
        updated_at = pd.to_datetime(latest["updated_at_utc"], utc=True)
        age_hours = (pd.Timestamp.utcnow() - updated_at).total_seconds() / 3600
        return {"updated_at_utc": latest["updated_at_utc"], "age_hours": age_hours, "row": latest}
    except Exception:
        return None


def _should_refresh(cache):
    """Return True if the cache should be refreshed from the live API."""
    if cache is None:
        return True
    now = datetime.now(timezone.utc)
    if cache["age_hours"] >= MAX_CACHE_HOURS:
        return True
    # Staggered refresh: only update at the safe minute to avoid hitting limits at :00/:30
    if now.minute == SAFE_REFRESH_MINUTE and cache["age_hours"] >= 1:
        return True
    return False


# ---- TIP count cache read/write ----
def _read_tip_cache():
    try:
        ws = _get_tip_cache_ws()
        cache = _read_cache(ws)
        if cache is None:
            return None
        return {**cache, "tip_count": int(cache["row"]["tip_count_7d"])}
    except Exception:
        return None


def _write_tip_cache(count):
    ws = _get_tip_cache_ws()
    ws.append_row([datetime.now(timezone.utc).isoformat(), count])


def fetch_tip_count_sheet_cached():
    """
    Return TIP count for last 7 days. Uses the 'Spacetrack' Google Sheet tab as a
    persistent cache so the GSheet data scan runs at most once per refresh window.
    """
    cache = _read_tip_cache()

    if not _should_refresh(cache):
        return cache["tip_count"], None

    live_count, err = fetch_tip_count_last7days()

    if err:
        if cache:
            return cache["tip_count"], f"Using cached TIP count. Live update failed: {err}"
        return None, err

    _write_tip_cache(live_count)
    return live_count, None


def fetch_diwata2_gp_cached():
    """
    Return the Diwata-2 GP record (incl. TLE lines) from the Google Sheet CelesTrak cache.
    The local Windows relay script refreshes the sheet on schedule.
    """
    return fetch_gp_record(catnr=DIWATA2_NORAD)


def fetch_norad_names(norad_list: tuple):
    """Look up OBJECT_NAMEs for a tuple of NORAD IDs from the cached SATCAT sheet."""
    return lookup_names(norad_list)


@st.cache_data(ttl=120)
def fetch_reentry_last7days():
    """
    Fetch last-7-days re-entry events from Google Sheets (same sheet as reentry_event_analyzer).
    Returns list of dicts, each with: norad, name, severity, hit_utc, hit_ph, distance.
    """
    try:
        client = _get_gspread_client()
        if client is None:
            return [], "GCP credentials not configured."
        ws   = client.open_by_key(SHEET_ID).worksheet(DATA_SHEET)
        rows = ws.get_all_records()
        if not rows:
            return [], None

        import pandas as _pd
        df = _pd.DataFrame(rows)
        df.columns = [c.strip() for c in df.columns]
        df["sheet_row"] = df.index + 2
        df["Hit UTC"]   = _pd.to_datetime(df["Hit UTC"], errors="coerce")
        df["Distance"]  = _pd.to_numeric(df["Distance"], errors="coerce")

        valid = df.dropna(subset=["Hit UTC"])
        if valid.empty:
            return [], None

        cutoff   = valid["Hit UTC"].max() - _pd.Timedelta(days=7)
        df_week  = valid[valid["Hit UTC"] >= cutoff].copy()

        # Per-NORAD: pick highest sheet_row (latest TIP)
        records = []
        for norad, grp in df_week.groupby("NORAD"):
            latest = grp.sort_values("sheet_row", ascending=False).iloc[0]
            records.append({
                "norad"    : int(norad),
                "severity" : str(latest.get("Severity", "MINOR")),
                "hit_utc"  : latest["Hit UTC"],
                "hit_ph"   : str(latest.get("Hit PH", "")),
                "distance" : grp["Distance"].min(),
                "total_tips": len(grp),
                "has_major": (grp["Severity"] == "MAJOR").any(),
            })

        # Sort: MAJOR first, then by hit_utc descending
        records.sort(key=lambda r: (0 if r["severity"] == "MAJOR" else 1,
                                    -r["hit_utc"].timestamp() if _pd.notna(r["hit_utc"]) else 0))

        # Batch-fetch object names from Space-Track
        norad_tuple = tuple(r["norad"] for r in records)
        names = fetch_norad_names(norad_tuple)
        for r in records:
            r["name"] = names.get(r["norad"], "—")

        return records, None
    except Exception as e:
        return [], str(e)


@st.cache_data(ttl=120)
def fetch_tip_count_last7days():
    """
    Count total TIP rows received in the last 7 days from the same GSheet.
    Returns (count: int, error: str|None)
    """
    try:
        client = _get_gspread_client()
        if client is None:
            return None, "GCP credentials not configured."
        ws   = client.open_by_key(SHEET_ID).worksheet(DATA_SHEET)
        rows = ws.get_all_records()
        if not rows:
            return 0, None
        import pandas as _pd
        df = _pd.DataFrame(rows)
        df.columns = [c.strip() for c in df.columns]
        df["Hit UTC"] = _pd.to_datetime(df["Hit UTC"], errors="coerce")
        valid = df.dropna(subset=["Hit UTC"])
        cutoff = valid["Hit UTC"].max() - _pd.Timedelta(days=7)
        count = int((valid["Hit UTC"] >= cutoff).sum())
        return count, None
    except Exception as e:
        return None, str(e)


# ============================================================
# SW EVENTS: LAST 7 DAYS KP FROM NOAA OBSERVED FEED
# ============================================================
@st.cache_data(ttl=180)
def fetch_sw_events_last7days():
    """
    Fetch last-7-days 3-hourly observed Kp from NOAA SWPC.
    Same source and User-Agent as weather.py (noaa-planetary-k-index.json).
    Returns list of dicts for periods with Kp >= 3, plus the current period regardless.
    Keys: time_tag (datetime), kp (float), level (str), color (str), impact (str)
    """
    def _parse_tt(s):
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                pass
        return None

    def _classify(kp):
        if kp >= 7:
            return "Severe Storm G3+", "#9c27b0",                    "Major GPS/HF disruptions · Satellite drag anomalies · S/C attitude disturbances"
        elif kp >= 5:
            return "Storm G1-G2", "#f44336",                    "GPS degradation · Magnetorquer performance reduced · Increased orbital drag at LEO"
        elif kp >= 4:
            return "Active G1", "#ff9800",                    "Minor HF radio disturbances · Slight increase in atmospheric drag"
        else:
            return "Unsettled", "#ffc107",                    "Elevated geomagnetic activity · Monitor sensitive instruments"

    cutoff = datetime.utcnow() - timedelta(days=7)
    all_rows = []

    # Primary: 3-hourly observed (capital "Kp", ISO-8601 with T)
    try:
        r = requests.get(
            "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json",
            headers=_NOAA_HEADERS, timeout=15
        )
        r.raise_for_status()
        for item in r.json():
            try:
                dt = _parse_tt(item["time_tag"])
                if dt is None or dt < cutoff:
                    continue
                kp = float(item.get("Kp") or item.get("kp") or 0)
                if kp < 0:
                    continue
                all_rows.append((dt, kp))
            except Exception:
                continue
    except Exception:
        pass

    # Sort newest-first; find current period (most recent row)
    all_rows.sort(key=lambda x: x[0], reverse=True)

    events = []
    now = datetime.utcnow()
    for i, (dt, kp) in enumerate(all_rows):
        is_current = (i == 0)          # most recent row = current period
        if kp < 3 and not is_current:  # skip quiet periods unless it's current
            continue
        level, color, impact = _classify(kp)
        if is_current and kp < 3:
            level, color, impact = "Quiet", "#4caf50",                 "Nominal geomagnetic conditions · No disruptions expected"
        events.append({
            "time_tag"  : dt,
            "kp"        : kp,
            "level"     : level,
            "color"     : color,
            "impact"    : impact,
            "is_current": is_current,
        })

    return events


# ============================================================
# MOCK ACTIVITY FEED
# ============================================================
def get_mock_past_events():
    events = []
    today = datetime.utcnow()
    for i in range(1, 16):
        date = today - timedelta(days=i)
        etype = random.choice(["Conjunction","Re-entry","Launch","Space Weather Alert"])
        if etype == "Conjunction":
            obj = random.choice(["STARLINK-3000","COSMOS 2251 DEB","IRIDIUM 33 DEB","FENGYUN 1C DEB","HUBBLE"])
            events.append(f"**{date.strftime('%b %d')}** - Close Approach involving {obj}")
        elif etype == "Re-entry":
            obj = random.choice(["SL-16 R/B","COSMOS 1408 DEB","ARIANE 5 DEB","FENGYUN 1C DEB"])
            events.append(f"**{date.strftime('%b %d')}** - Re-entry: {obj}")
        elif etype == "Launch":
            mission = random.choice(["Falcon 9 • Starlink","Electron • Smallsat","Long March 5","Atlas V"])
            events.append(f"**{date.strftime('%b %d')}** - Successful Launch: {mission}")
        else:
            kp = random.randint(5, 8)
            events.append(f"**{date.strftime('%b %d')}** - Geomagnetic Storm (Kp Index: {kp})")
    return events

def get_mock_alerts():
    alerts = []
    today = datetime.utcnow()
    for _ in range(5):
        date   = today - timedelta(days=random.randint(0, 5))
        level  = random.choice(["High","Medium","Critical"])
        atype  = random.choice(["Collision Risk","Re-entry Warning","Space Weather","Launch Delay"])
        alerts.append(f"**{date.strftime('%b %d')}** | **{level}** - {atype}")
    return alerts

# ============================================================
# FETCH ALL REAL DATA
# ============================================================
with st.spinner("Loading cached CelesTrak data from Google Sheets, plus NOAA & app data…"):
    kpi_results, kpi_err         = fetch_dashboard_kpis()
    diwata2_rec, tle_err         = fetch_diwata2_gp_cached()
    kp_days, kp_values           = fetch_kp_forecast()
    reentry_events, reentry_err  = fetch_reentry_last7days()
    sw_events                    = fetch_sw_events_last7days()
    tip_count, tip_err           = fetch_tip_count_sheet_cached()

# --- Format KPI values ---
def fmt(val, default="N/A"):
    if isinstance(val, int):
        return f"{val:,}"
    return default

if kpi_results:
    active_sats    = fmt(kpi_results.get("active_sats"))
    debris_count   = fmt(kpi_results.get("debris_count"))
    rocket_bodies  = fmt(kpi_results.get("rocket_bodies"))
    total_tracked  = fmt(kpi_results.get("total_tracked"))
else:
    active_sats = debris_count = rocket_bodies = total_tracked = "N/A"
    if kpi_err:
        st.warning(f"CelesTrak cache KPI error: {kpi_err}")

# Manually maintained: not reliably derivable from CelesTrak's OWNER field.
sovereign_sats = "2"

# TIP count from GSheets (last 7 days)
tip_count_display = fmt(tip_count) if isinstance(tip_count, int) else "N/A"

# --- Diwata-2 position ---
diwata2_lat, diwata2_lon = 14.6, 121.1
diwata2_epoch = "Unknown"
diwata2_name  = "DIWATA-2"
tle1 = tle2 = ""

if diwata2_rec:
    tle1          = diwata2_rec.get("TLE_LINE1", "")
    tle2          = diwata2_rec.get("TLE_LINE2", "")
    diwata2_epoch = diwata2_rec.get("EPOCH", "Unknown")
    diwata2_name  = diwata2_rec.get("OBJECT_NAME", "DIWATA-2")
    if tle1 and tle2:
        diwata2_lat, diwata2_lon = propagate_tle_simple(tle1, tle2)
elif tle_err:
    st.warning(f"Diwata-2 TLE error: {tle_err}")

# ============================================================
# MAIN LAYOUT
# ============================================================
main_col1, main_col2 = st.columns([3, 1])

with main_col1:
    st.header("**Dashboard**")

    # ---- KPI CARDS (real data) ----
    kpi_data = [
        ("satellite_alt",        "Active Satellites",              active_sats),
        ("crisis_alert",  "Tracking and Impact Prediction Messages (7d)",              tip_count_display),
        ("delete", "Cataloged Debris",               debris_count),
        ("rocket_launch", "Rocket Bodies",                  rocket_bodies),
        ("flag",    "Sovereign Satellites",                 sovereign_sats),
        ("track_changes", "Total Tracked Objects",          total_tracked),
    ]

    cols = st.columns(3)
    for col, (icon, title, value) in zip(cols, kpi_data[:3]):
        with col:
            new_kpi_card(icon, title, value)

    st.markdown("<br>", unsafe_allow_html=True)

    cols = st.columns(3)
    for col, (icon, title, value) in zip(cols, kpi_data[3:]):
        with col:
            new_kpi_card(icon, title, value)

    st.space(size = "xsmall")
    st.markdown("_Source: CelesTrak\u00ae_")
    st.markdown("---")
    
    globe_col, forecast_col = st.columns([2, 1])

    # ---- SATELLITE TRACKER ----
    with globe_col:
        st.subheader("Satellite Tracker")
        st.markdown("_Source: CelesTrak\u00ae_")

        tracker_mode = st.radio(
            "Tracking mode",
            ["Diwata-2 (default)", "Search satellite", "Paste custom TLE"],
            horizontal=True,
            label_visibility="collapsed",
        )

        tracker_name  = diwata2_name
        tracker_norad = DIWATA2_NORAD
        tracker_epoch = diwata2_epoch
        tracker_tle1  = tle1
        tracker_tle2  = tle2

        if tracker_mode == "Search satellite":
            search_col1, search_col2 = st.columns([3, 1])
            with search_col1:
                search_query = st.text_input(
                    "Satellite name or NORAD ID",
                    value="",
                    placeholder="e.g. ISS or 25544",
                    label_visibility="collapsed",
                )
            with search_col2:
                search_clicked = st.button("Search", use_container_width=True)

            if search_clicked and search_query.strip():
                if search_query.strip().isdigit():
                    results, search_err = search_satcat(catnr=search_query.strip())
                else:
                    results, search_err = search_satcat(name=search_query.strip())

                if search_err:
                    st.warning(f"Cached SATCAT search error: {search_err}")
                    st.session_state.pop("tracker_search_results", None)
                else:
                    st.session_state["tracker_search_results"] = results

            results = st.session_state.get("tracker_search_results", [])
            if results:
                options = {
                    f"{r.get('OBJECT_NAME', 'UNKNOWN')} (NORAD {r.get('NORAD_CAT_ID', '?')})": r
                    for r in results
                }
                chosen_label = st.selectbox("Matching objects", list(options.keys()))
                chosen_norad = options[chosen_label].get("NORAD_CAT_ID")

                gp_rec, gp_err = fetch_gp_record(catnr=chosen_norad)
                if gp_err:
                    st.warning(f"Cached TLE error: {gp_err}")
                elif gp_rec:
                    tracker_name  = gp_rec.get("OBJECT_NAME", tracker_name)
                    tracker_norad = gp_rec.get("NORAD_CAT_ID", chosen_norad)
                    tracker_epoch = gp_rec.get("EPOCH", "Unknown")
                    tracker_tle1  = gp_rec.get("TLE_LINE1", "")
                    tracker_tle2  = gp_rec.get("TLE_LINE2", "")

        elif tracker_mode == "Paste custom TLE":
            custom_tle_text = st.text_area(
                "Paste a 2-line or 3-line TLE",
                value="",
                height=90,
                placeholder="ISS (ZARYA)\n1 25544U 98067A   ...\n2 25544  51.6400 ...",
                label_visibility="collapsed",
            )
            if custom_tle_text.strip():
                custom_rec, custom_err = parse_manual_tle(custom_tle_text)
                if custom_err:
                    st.warning(custom_err)
                else:
                    tracker_name  = custom_rec["OBJECT_NAME"]
                    tracker_norad = custom_rec["NORAD_CAT_ID"] or "N/A"
                    tracker_epoch = custom_rec["EPOCH"]
                    tracker_tle1  = custom_rec["TLE_LINE1"]
                    tracker_tle2  = custom_rec["TLE_LINE2"]

        if tracker_tle1 and tracker_tle2:
            orbit_data = build_real_time_orbit(
                tracker_tle1,
                tracker_tle2,
                name=str(tracker_name),
                duration_seconds=6000,
                step_seconds=1,
            )

            payload = {
                "name": tracker_name,
                "norad": tracker_norad,
                "epoch": tracker_epoch,
                "generated_at_ms": orbit_data["generated_at_ms"],
                "current_lat": orbit_data["current_lat"],
                "current_lon": orbit_data["current_lon"],
                "points": orbit_data["points"],
            }

            payload_json = json.dumps(payload)

            components.html(
                f"""
                <!DOCTYPE html>
                <html>
                <head>
                    <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>

                    <style>
                        html, body {{
                            margin: 0;
                            padding: 0;
                            background: transparent;
                            font-family: Arial, sans-serif;
                        }}

                        #tracker {{
                            width: 100%;
                            height: 480px;
                        }}

                        #info {{
                            position: absolute;
                            z-index: 10;
                            top: 18px;
                            left: 18px;
                            background: rgba(5, 18, 42, 0.82);
                            color: white;
                            padding: 12px 16px;
                            border-radius: 10px;
                            font-size: 13px;
                            line-height: 1.5;
                        }}

                        .label {{
                            color: rgba(255,255,255,0.65);
                            font-size: 11px;
                            text-transform: uppercase;
                            letter-spacing: 1px;
                        }}

                        .value {{
                            font-weight: 700;
                        }}
                    </style>
                </head>

                <body>
                    <div style="position: relative;">
                        <div id="info">
                            <div class="label">Satellite Tracker</div>
                            <div><span class="value" id="sat-name"></span> | NORAD <span id="norad"></span></div>
                            <div>Lat: <span class="value" id="lat"></span>°</div>
                            <div>Lon: <span class="value" id="lon"></span>°</div>
                            <div>Elapsed: <span class="value" id="elapsed"></span>s</div>
                        </div>

                        <div id="tracker"></div>
                    </div>

                    <script>
                        const data = {payload_json};
                        const points = data.points;
                        const generatedAt = data.generated_at_ms;

                        document.getElementById("sat-name").innerText = data.name;
                        document.getElementById("norad").innerText = data.norad;

                        const trackTrace = {{
                            type: "scattergeo",
                            mode: "lines",
                            lat: points.map(p => p.lat),
                            lon: points.map(p => p.lon),
                            line: {{
                                color: "gold",
                                width: 2
                            }},
                            name: "Ground Track"
                        }};

                        const satTrace = {{
                            type: "scattergeo",
                            mode: "markers",
                            lat: [data.current_lat],
                            lon: [data.current_lon],
                            marker: {{
                                size: 18,
                                color: "#1a9fff",
                                line: {{
                                    color: "white",
                                    width: 2
                                }}
                            }},
                            name: data.name
                        }};

                        const layout = {{
                            height: 480,
                            margin: {{ l: 0, r: 0, t: 0, b: 0 }},
                            paper_bgcolor: "rgba(0,0,0,0)",
                            plot_bgcolor: "rgba(0,0,0,0)",
                            geo: {{
                                projection: {{
                                    type: "orthographic",
                                    rotation: {{
                                        lon: data.current_lon,
                                        lat: data.current_lat
                                    }}
                                }},
                                showland: true,
                                landcolor: "#1e3a5f",
                                showocean: true,
                                oceancolor: "#0a1f3d",
                                showcoastlines: true,
                                coastlinecolor: "rgba(255,255,255,0.55)",
                                showcountries: false,
                                showframe: false,
                                bgcolor: "rgba(0,0,0,0)"
                            }},
                            showlegend: false
                        }};

                        const config = {{
                            responsive: true,
                            displayModeBar: false
                        }};

                        Plotly.newPlot("tracker", [trackTrace, satTrace], layout, config);

                        function interpolatePosition(elapsedSeconds) {{
                            const maxIndex = points.length - 1;

                            if (elapsedSeconds <= 0) return points[0];
                            if (elapsedSeconds >= points[maxIndex].t) return points[maxIndex];

                            const i = Math.floor(elapsedSeconds);
                            const fraction = elapsedSeconds - i;

                            const p1 = points[i];
                            const p2 = points[i + 1];

                            let lon1 = p1.lon;
                            let lon2 = p2.lon;

                            if (Math.abs(lon2 - lon1) > 180) {{
                                if (lon1 > lon2) {{
                                    lon2 += 360;
                                }} else {{
                                    lon1 += 360;
                                }}
                            }}

                            let lon = lon1 + (lon2 - lon1) * fraction;

                            if (lon > 180) lon -= 360;

                            return {{
                                lat: p1.lat + (p2.lat - p1.lat) * fraction,
                                lon: lon
                            }};
                        }}

                        function updateSatellite() {{
                            const nowMs = Date.now();
                            const elapsedSeconds = (nowMs - generatedAt) / 1000.0;

                            const p = interpolatePosition(elapsedSeconds);

                            document.getElementById("lat").innerText = p.lat.toFixed(4);
                            document.getElementById("lon").innerText = p.lon.toFixed(4);
                            document.getElementById("elapsed").innerText = elapsedSeconds.toFixed(1);

                            Plotly.restyle("tracker", {{
                                lat: [[p.lat]],
                                lon: [[p.lon]]
                            }}, [1]);

                            requestAnimationFrame(updateSatellite);
                        }}

                        updateSatellite();
                    </script>
                </body>
                </html>
                """,
                height=500,
                scrolling=False,
            )

            st.caption(
                f"{tracker_name} | NORAD {tracker_norad} | TLE Epoch: {tracker_epoch} UTC"
            )

        else:
            st.warning("TLE unavailable. Cannot display a real-time tracker for this selection.")
    # ---- KP CHART (observed + forecast) ----
    with forecast_col:
        st.subheader("Geomagnetic Storm Forecast")
        st.markdown("_Source: NOAA SWPC_")

        if kp_days and kp_values:
            import plotly.graph_objects as _go
            today_str = datetime.utcnow().strftime("%b %d")

            # Colour each bar by Kp level; outline today's bar
            bar_colors = []
            for d, k in zip(kp_days, kp_values):
                if k < 4:    bar_colors.append("#4caf50")
                elif k < 6:  bar_colors.append("#ff9800")
                elif k < 8:  bar_colors.append("#f44336")
                else:        bar_colors.append("#9c27b0")

            # Bold outline on today
            line_colors = ["#ffffff" if d == today_str else "rgba(0,0,0,0)"
                           for d in kp_days]
            line_widths = [2.5 if d == today_str else 0 for d in kp_days]

            fig4 = _go.Figure(_go.Bar(
                x=kp_days,
                y=kp_values,
                marker=dict(
                    color=bar_colors,
                    line=dict(color=line_colors, width=line_widths),
                    cornerradius=8,
                ),
                hovertemplate="<b>%{x}</b><br>Kp: %{y:.1f}<extra></extra>",
            ))

            # G-scale threshold lines
            for thresh, label, col_t in [
                (4, "G1", "#ff9800"),
                (5, "G2", "#ff5722"),
                (6, "G3", "#f44336"),
            ]:
                fig4.add_hline(
                    y=thresh, line_dash="dot", line_color=col_t, line_width=1,
                    annotation_text=label, annotation_font_color=col_t,
                    annotation_position="top right", annotation_font_size=9,
                )

            # "TODAY" annotation
            if today_str in kp_days:
                today_idx = kp_days.index(today_str)
                today_kp  = kp_values[today_idx]
                fig4.add_annotation(
                    x=today_str, y=today_kp + 0.5,
                    text="TODAY", showarrow=False,
                    font=dict(size=9, color="#ffffff"),
                    bgcolor="rgba(26,79,163,0.8)",
                    borderpad=2,
                )

            fig4.update_layout(
                height=290,
                plot_bgcolor="rgba(0,0,0,0)",
                paper_bgcolor="rgba(0,0,0,0)",
                yaxis=dict(range=[0, 9.5], showgrid=False, tickvals=[0,2,4,6,8],
                           showline=True, linewidth=1, linecolor="#666",
                           ticks="outside", color="#888", title="Kp"),
                xaxis=dict(showgrid=False, showline=True, linewidth=1, linecolor="#666",
                           ticks="outside", color="#888"),
                margin=dict(t=20, b=20, l=40, r=10),
                showlegend=False,
            )
            st.plotly_chart(fig4, use_container_width=True)

            # Operational Impact — use today's actual Kp
            curr_kp = kp_values[kp_days.index(today_str)] if today_str in kp_days else kp_values[-1]
            if curr_kp < 3:
                level_label, impact_text = "Quiet", "• Quiet conditions • No significant disruptions expected"
            elif curr_kp < 5:
                level_label, impact_text = "Unsettled", "• Minor disturbances • Possible HF radio degradation at high latitudes"
            elif curr_kp < 7:
                level_label, impact_text = "Active", "• Active/Storm conditions • GPS degradation possible • Auroras likely at mid-latitudes"
            else:
                level_label, impact_text = "Storm", "• Severe storm • Significant GPS/HF disruptions • Satellite drag anomalies possible"

            kp_col = "#4caf50" if curr_kp < 4 else "#ff9800" if curr_kp < 6 else "#f44336"
            st.markdown("**Operational Impact**")
            st.markdown(f"""
            <div style="background:rgba(255,255,255,0.06); padding:12px; border-radius:8px; font-size:0.85rem;">
                <strong style="color:{kp_col};">Kp {curr_kp:.1f} today &bull; {today_str} ({level_label})</strong><br>
                {impact_text}
            </div>
            """, unsafe_allow_html=True)
        else:
            st.info("Kp data unavailable from NOAA SWPC.")

# # ---- ACTIVITY FEED OG (11 July 2026) ----
# with main_col2:
#     st.subheader("Activity")

#     # === SCROLLABLE CONTAINER WITH FIXED HEIGHT ===
#     with st.container(height=1100, border=True):
    
#         tab1, tab2, tab3, tab4, tab5 = st.tabs([
#             "Alerts",
#             "Recent Launches", 
#             "Conjunction Events", 
#             "Recent Re-entries", 
#             "Space Weather Events", 
#         ])

#         # ------------------- TAB 1: ALERTS -------------------
#         with tab1:
#             #st.markdown("**Active Alerts**")
            
#             #re-entry alerts: only show MAJOR severity
#             reentry_alerts = [ev for ev in reentry_events if ev["severity"] == "MAJOR"]
            
#             #space-weather alerts: only show Kp >= 5
#             sw_alerts_dedup = {}   # day_key -> worst event
#             for ev in sw_events:
#                 if ev["kp"] >= 5:
#                     dk = ev["time_tag"].strftime("%b %d")
#                     if dk not in sw_alerts_dedup or ev["kp"] > sw_alerts_dedup[dk]["kp"]:
#                         sw_alerts_dedup[dk] = ev
#             sw_alerts = sorted(sw_alerts_dedup.values(),
#                             key=lambda e: e["time_tag"], reverse=True)
           
#             # === Upcoming Rocket Launches ===
#             df = load_launch_database()
#             launch_alerts = []

#             if not df.empty and "LAUNCH_DATE_PARSED" in df.columns:
#                 now = datetime.now()
#                 upcoming = df[df["LAUNCH_DATE_PARSED"] > now].copy()
                
#                 if not upcoming.empty:
#                     upcoming = upcoming.sort_values("LAUNCH_DATE_PARSED", ascending=True)  # Soonest first
#                     launch_alerts = upcoming.head(6)  # Show next 6 upcoming launches
            
#             # ===Active Conjunctions===
#             conjunction_alerts = []
#             try:
#                 raw_conjunction_df = load_database()                    # ← your function
#                 event_summary = compute_event_summary(raw_conjunction_df, use_persisted_summary=False)
                
#                 if not event_summary.empty:
#                     events_df = event_summary.copy()
                    
#                     events_df["TCA_dt"] = pd.to_datetime(events_df["TCA_UTC"], utc=True, errors="coerce")
#                     events_df = events_df.dropna(subset=["TCA_dt"])
                    
#                     # Filter only ACTIVE (future) events
#                     now = datetime.now(timezone.utc)
#                     active_events = events_df[events_df["TCA_dt"] > now].copy()
                    
#                     if not active_events.empty:
#                         # Sort by soonest TCA first (most urgent)
#                         active_events = active_events.sort_values("TCA_dt", ascending=True)
#                         conjunction_alerts = active_events.head(8)   # Show top 8 most imminent

#             # ==

#             except Exception as e:
#                 st.warning("⚠️ Could not load conjunction events.")
#                 # Optionally: st.error(str(e)) during development

#             if not reentry_alerts and not sw_alerts:
#                 st.success("✅ No critical alerts at the moment.")
#                 st.caption("Shows MAJOR re-entries and Storm (Kp≥5) space weather events.")
#             else:
#                 if reentry_alerts:
#                     for ev in reentry_alerts:
#                         norad    = ev["norad"]
#                         obj_name = ev.get("name", "—")
#                         hit_str  = (ev["hit_utc"].strftime("%Y-%m-%d %H:%M PhST")
#                                     if ev["hit_utc"] and str(ev["hit_utc"]) != "NaT" else "—")
#                         dist_str = (f"{ev['distance']:.1f} km"
#                                     if ev["distance"] and str(ev["distance"]) != "nan" else "—")
#                         st.markdown(f"""
#                         <div style="background-color:#1a4fa3;padding:14px;border-radius:10px;
#                                     margin-bottom:12px;color:white;">
#                             <strong>NORAD {norad} — {obj_name}</strong><br>
#                             <small>{hit_str} • Closest: <b>{dist_str}</b></small><br>
#                             <span class="risk-btn risk-critical">MAJOR</span>
#                             <span class="risk-btn risk-lapsed" style="margin-left:4px;">RE-ENTRY</span>
#                         </div>
#                         """, unsafe_allow_html=True)

#                 if sw_alerts:
#                     for ev in sw_alerts:
#                         lv = ev["level"]
#                         badge_class = "risk-critical" if "Severe" in lv or "G3" in lv else "risk-high"
#                         day_label = ev["time_tag"].strftime("%b %d")
#                         st.markdown(f"""
#                         <div style="background-color:#1a4fa3;padding:14px;border-radius:10px;
#                                     margin-bottom:12px;color:white;">
#                             <strong>{day_label} — Geomagnetic Storm</strong><br>
#                             <small>Kp: <b>{ev["kp"]:.1f}</b> • {ev["time_tag"].strftime("%H:%M UTC")}</small><br>
#                             <small>⚡ {ev["impact"]}</small><br>
#                             <span class="risk-btn {badge_class}">{lv}</span>
#                             <span class="risk-btn risk-lapsed" style="margin-left:4px;">SW EVENT</span>
#                         </div>
#                         """, unsafe_allow_html=True)
                
#                 # === New: Upcoming Rocket Launches ===
#                 if len(launch_alerts) > 0:
#                     for _, row in launch_alerts.iterrows():
#                         date_str = row.get("LAUNCH DATE", "—")
#                         rocket = row.get("ROCKET NAME", "Unknown Rocket")
#                         site = row.get("LAUNCH CENTER", "—")
#                         country = row.get("LAUNCHING STATE", "—")
#                         window = row.get("START TO END TIME", "—")
#                         launch_time = row["LAUNCH_DATE_PARSED"]
                        
#                         time_until = launch_time - datetime.now()
#                         days_left = time_until.days
#                         hours_left = time_until.seconds // 3600
                        
#                         countdown = f"in {days_left}d {hours_left}h" if days_left > 0 else f"in {hours_left}h"

#                         st.markdown(f"""
#                         <div style="background-color:#1a4fa3;padding:14px;border-radius:10px;
#                                     margin-bottom:12px;color:white;">
#                             <strong>{rocket}</strong><br>
#                             <small>{date_str} • {site}</small><br>
#                             <small>{country} | {window}</small><br>
#                             <small><b>{countdown}</b></small>
#                             <span class="risk-btn risk-lapsed" style="margin-left:4px;">ROCKET LAUNCH</span>
#                         </div>
#                         """, unsafe_allow_html=True)

#                 # ==================== NEW: Active Conjunction Alerts ====================
#                 if conjunction_alerts is not None and len(conjunction_alerts) > 0:
                    
#                     for _, row in conjunction_alerts.iterrows():
#                         risk_label = row.get("RISK", "LOW")
#                         clean_risk = str(risk_label).strip().replace("🔴","").replace("🟠","").replace("🟡","").replace("⚪","").replace("⚫","").strip()
#                         risk_class = clean_risk.lower()

#                         event_id = row.get("EVENT_ID", "Unknown")
#                         tca_ph = str(row.get("TCA_PH", ""))[:16] if row.get("TCA_PH") else "—"
#                         hours = row.get("RT_from_TCA", 0)
#                         hours_str = f"{hours:.1f}h" if hours > 0 else "Soon"

#                         pc = row.get("MAX_PC", 0)
#                         miss = row.get("MIN_MISS", 0)

#                         st.markdown(f"""
#                         <div style="background-color:#1a4fa3;padding:14px;border-radius:10px;
#                                     margin-bottom:12px;color:white;">
#                             <strong>{event_id}</strong><br>
#                             <small>{tca_ph} PhST • <b>{hours_str}</b></small><br>
#                             <small>PC: <b>{pc:.2e}</b> • Miss: <b>{miss:.1f} m</b></small><br>
#                             <span class="risk-btn risk-{risk_class}">{risk_label}</span>
#                             <span class="risk-btn risk-lapsed" style="margin-left:4px;">CONJUNCTION</span>
#                         </div>
#                         """, unsafe_allow_html=True)

#                     if len(active_events) > 8:   # Note: using the full active_events from above
#                         st.caption(f"... and {len(active_events)-8} more active conjunctions")
        
#         with tab2:
#             st.markdown("**Recent Launches (Last 15 Days)**")
#             df = load_launch_database()
#             events_df = df.copy()
            
#             if "LAUNCH_DATE_PARSED" in events_df.columns and not events_df.empty:
#                 cutoff = datetime.now() - timedelta(days=15)
#                 recent = events_df[events_df["LAUNCH_DATE_PARSED"] >= cutoff].copy()
#                 recent = recent.sort_values("LAUNCH_DATE_PARSED", ascending=False)
                
#                 if recent.empty:
#                     st.info("No launches recorded in the last 15 days.")
#                 else:
#                     for _, row in recent.head(8).iterrows():
#                         date_str = row.get("LAUNCH DATE", "—")
#                         rocket = row.get("ROCKET NAME", "Unknown")
#                         site = row.get("LAUNCH CENTER", "—")
#                         country = row.get("LAUNCHING STATE", "—")
#                         window = row.get("START TO END TIME", "—")
                        
#                         st.markdown(f"""
#                         <div style="background-color: #1a4fa3; padding: 10px; border-radius: 8px;
#                                     margin-bottom: 8px; color: white;">
#                             <strong>{rocket}</strong><br>
#                             <small> {date_str} • {site}</small><br>
#                             <small> {country} | {window}</small>
#                         </div>
#                         """, unsafe_allow_html=True)
#             else:
#                 st.info("No launch data available yet.")

#                 # ------------------- TAB 2: CONJUNCTION EVENTS -------------------
#         with tab3:
#             st.markdown("**Conjunction Events**")
            
#             try:
#                 # Load raw conjunction data and compute summary
#                 raw_conjunction_df = load_database()   # ← Your raw data loader
#                 event_summary = compute_event_summary(raw_conjunction_df, use_persisted_summary=False)
                
#                 if event_summary.empty:
#                     st.info("No conjunction events available.")
#                 else:
#                     # === Your exact logic ===
#                     events_df = event_summary.copy()
                    
#                     # === PURE DATE SORT: Soonest TCA First ===
#                     events_df["TCA_dt"] = pd.to_datetime(
#                         events_df["TCA_UTC"],
#                         utc=True,
#                         errors="coerce"
#                     )
                    
#                     events_df = events_df.sort_values(
#                         by="RT_from_TCA",
#                         ascending=False
#                     ).reset_index(drop=True)
                    
#                     displayed = 0
#                     for _, row in events_df.iterrows():
#                         if displayed >= 6:
#                             break
                            
#                         risk_label = row.get("RISK", "LOW")
#                         # Clean risk for CSS class
#                         clean_risk = str(risk_label).strip()
#                         clean_risk = clean_risk.replace("🔴", "").replace("🟠", "").replace("🟡", "").replace("⚪", "").replace("⚫", "")
#                         clean_risk = clean_risk.strip()
#                         risk_class = clean_risk.lower()
                        
#                         event_id = row.get("EVENT_ID", "Unknown")
#                         tca_ph = str(row.get("TCA_PH", ""))[:16] if row.get("TCA_PH") else "—"
#                         hours = row.get("RT_from_TCA", 0)
#                         hours_str = f"{hours:.1f}h" if hours >= 0 else "Lapsed"
                        
#                         pc = row.get("MAX_PC", 0)
#                         miss = row.get("MIN_MISS", 0)
                        
#                         st.markdown(f"""
#                         <div style="background-color: #1a4fa3; padding: 14px; border-radius: 10px;
#                                     margin-bottom: 12px; color: white;">
#                             <strong>{event_id}</strong><br>
#                             <small>{tca_ph} PhST • {hours_str}</small><br>
#                             <small>PC: <b>{pc:.2e}</b> • Miss: <b>{miss:.1f} m</b></small><br>
#                             <span class="risk-btn risk-{risk_class}">{risk_label}</span>
#                         </div>
#                         """, unsafe_allow_html=True)
                        
#                         displayed += 1
                        
#                     if len(events_df) > 6:
#                         st.caption(f"... and {len(events_df)-6} more events")
                        
#             except Exception as e:
#                 st.warning("⚠️ Could not load conjunction events.")
#                 st.info("Make sure `load_conjunction_database()` and `compute_event_summary()` are available.")

#         # ------------------- TAB 3: RE-ENTRIES -------------------
#         with tab4:
#             st.markdown("**Re-entry Events — Last 7 Days**")

#             if reentry_err:
#                 st.warning(f"⚠️ Could not load re-entry data: {reentry_err}")
#             elif not reentry_events:
#                 st.info("No re-entry events recorded in the last 7 days.")
#             else:
#                 for ev in reentry_events[:8]:
#                     sev      = ev["severity"]
#                     norad    = ev["norad"]
#                     obj_name = ev.get("name", "—")

#                     hit_str  = (ev["hit_utc"].strftime("%Y-%m-%d %H:%M PhST")
#                                 if ev["hit_utc"] and str(ev["hit_utc"]) != "NaT" else "—")
#                     hit_ph   = ev["hit_ph"] or "—"
#                     dist_str = (f"{ev['distance']:.1f} km"
#                                 if ev["distance"] and str(ev["distance"]) != "nan" else "—")
#                     tips_str = str(ev["total_tips"])

#                     # Severity badge — same CSS classes as conjunction "risk-btn"
#                     sev_class = "risk-critical" if sev == "MAJOR" else "risk-low"

#                     major_note = ""
#                     if ev.get("has_major") and sev != "MAJOR":
#                         major_note = " <span style='color:#ff9800;font-size:0.7rem;'>⚠ had MAJOR</span>"

#                     st.markdown(f"""
#                     <div style="background-color:#1a4fa3;padding:14px;border-radius:10px;
#                                 margin-bottom:12px;color:white;">
#                         <strong>NORAD {norad} — {obj_name}</strong><br>
#                         <small>{hit_str} • TIP updates: {tips_str}</small><br>
#                         <small>Hit PH: <b>{hit_ph}</b> • Closest: <b>{dist_str}</b></small><br>
#                         <span class="risk-btn {sev_class}">{sev}</span>{major_note}
#                     </div>
#                     """, unsafe_allow_html=True)

#                 if len(reentry_events) > 8:
#                     st.caption(f"... and {len(reentry_events)-8} more objects")

#         # ------------------- TAB 4: SW EVENTS -------------------
#         with tab5:
#             st.markdown("**Space Weather Events — Last 7 Days**")

#             if not sw_events:
#                 st.success("No significant geomagnetic activity in the last 7 days.")
#             else:
#                 from collections import defaultdict as _dd
#                 by_day = _dd(list)
#                 for ev in sw_events:
#                     day_key = ev["time_tag"].strftime("%b %d")
#                     by_day[day_key].append(ev)

#                 for day_key in sorted(by_day.keys(),
#                                     key=lambda d: datetime.strptime(
#                                         d + f" {datetime.utcnow().year}", "%b %d %Y"),
#                                     reverse=True):
#                     day_evs  = by_day[day_key]
#                     max_kp   = max(e["kp"] for e in day_evs)
#                     max_ev   = max(day_evs, key=lambda e: e["kp"])
#                     is_today = day_key == datetime.utcnow().strftime("%b %d")

#                     # Current-period Kp for today
#                     curr_ev = next((e for e in day_evs if e.get("is_current")), None)
#                     curr_str = f"Current: <b>{curr_ev['kp']:.1f}</b> • " if curr_ev and is_today else ""

#                     # Map level → same risk-btn CSS class as conjunction tab
#                     lv = max_ev["level"]
#                     if "Severe" in lv or "G3" in lv:
#                         badge_class = "risk-critical"
#                     elif "Storm" in lv or "G1" in lv or "G2" in lv:
#                         badge_class = "risk-high"
#                     elif "Active" in lv:
#                         badge_class = "risk-medium"
#                     elif "Unsettled" in lv:
#                         badge_class = "risk-medium"
#                     else:
#                         badge_class = "risk-low"

#                     now_tag = " · NOW" if is_today else ""

#                     st.markdown(f"""
#                     <div style="background-color:#1a4fa3;padding:14px;border-radius:10px;
#                                 margin-bottom:12px;color:white;">
#                         <strong>{day_key}{now_tag}</strong><br>
#                         <small>{curr_str}Max Kp: <b>{max_kp:.1f}</b> • {len(day_evs)} period(s)</small><br>
#                         <small>{max_ev["impact"]}</small><br>
#                         <span class="risk-btn {badge_class}">{max_ev["level"]}</span>
#                     </div>
#                     """, unsafe_allow_html=True)

# ----DUMMY ACTIVITY FEED ----
with main_col2:
    st.subheader("Activity")
    with st.container(height=1100, border=True):
   
        tab1, tab2, tab3, tab4, tab5 = st.tabs([
            "Alerts", "Recent Launches", "Conjunction Events", 
            "Recent Re-entries", "Space Weather Events"
        ])
                
        # ------------------- TAB 1: ALERTS -------------------
        with tab1:
            #re-entry alerts: only show MAJOR severity
            reentry_alerts = [ev for ev in reentry_events if ev["severity"] == "MAJOR"]
          
            #space-weather alerts: only show Kp >= 5
            sw_alerts_dedup = {}
            for ev in sw_events:
                if ev["kp"] >= 5:
                    dk = ev["time_tag"].strftime("%b %d")
                    if dk not in sw_alerts_dedup or ev["kp"] > sw_alerts_dedup[dk]["kp"]:
                        sw_alerts_dedup[dk] = ev
            sw_alerts = sorted(sw_alerts_dedup.values(),
                            key=lambda e: e["time_tag"], reverse=True)
         
            # === Upcoming Rocket Launches ===
            df = load_launch_database()
            launch_alerts = []
            if not df.empty and "LAUNCH_DATE_PARSED" in df.columns:
                now = datetime.now()
                upcoming = df[df["LAUNCH_DATE_PARSED"] > now].copy()
                if not upcoming.empty:
                    upcoming = upcoming.sort_values("LAUNCH_DATE_PARSED", ascending=True)
                    launch_alerts = upcoming.head(6)

            # Pseudo for Launches (dummy display when DEMO_MODE)
            if len(launch_alerts) > 0:
                launch_alerts = launch_alerts.copy()
                rocket_map = _demo_label_map(launch_alerts["ROCKET NAME"], "Rocket")
                launch_alerts["Pseudo_Rocket"] = launch_alerts["ROCKET NAME"].map(rocket_map)

                site_map = _demo_label_map(launch_alerts["LAUNCH CENTER"], "Site")
                launch_alerts["Pseudo_Site"] = launch_alerts["LAUNCH CENTER"].map(site_map)

            # === Active Conjunction Alerts ===
            conjunction_alerts = []
            active_events = []  # for caption
            try:
                raw_conjunction_df = load_database()
                event_summary = compute_event_summary(raw_conjunction_df, use_persisted_summary=False)
              
                if not event_summary.empty:
                    events_df = event_summary.copy()
                    events_df["TCA_dt"] = pd.to_datetime(events_df["TCA_UTC"], utc=True, errors="coerce")
                    events_df = events_df.dropna(subset=["TCA_dt"])
                  
                    now = datetime.now(timezone.utc)
                    active_events = events_df[events_df["TCA_dt"] > now].copy()
                  
                    if not active_events.empty:
                        active_events = active_events.sort_values("TCA_dt", ascending=True)
                        conjunction_alerts = active_events.head(8).copy()
                        conjunction_alerts = conjunction_alerts.reset_index(drop=True)
                        if DEMO_MODE:
                            conjunction_alerts["Pseudo_Event"] = [f"Event {chr(65 + i)}" for i in range(len(conjunction_alerts))]
                        else:
                            conjunction_alerts["Pseudo_Event"] = conjunction_alerts["EVENT_ID"]
            except Exception as e:
                st.warning("⚠️ Could not load conjunction events.")

            # Display Section
            if not reentry_alerts and not sw_alerts and len(conjunction_alerts) == 0:
                st.success("✅ No critical alerts at the moment.")
                st.caption("Shows MAJOR re-entries and Storm (Kp≥5) space weather events.")
            else:
                # === Re-entry Alerts (Clean Format) ===
                if reentry_alerts:
                    for i, ev in enumerate(reentry_alerts):
                        pseudo_name = _demo_positional_label(i, "Object", real_value=ev.get("name"))
                        hit_str = (ev["hit_utc"].strftime("%Y-%m-%d %H:%M PhST")
                                    if ev.get("hit_utc") and str(ev["hit_utc"]) != "NaT" else "—")
                        dist_str = (f"{ev['distance']:.1f} km"
                                    if ev.get("distance") and str(ev["distance"]) != "nan" else "—")
                       
                        st.markdown(f"""
                        <div style="background-color:#1a4fa3;padding:14px;border-radius:10px;
                                    margin-bottom:12px;color:white;">
                            <strong>Object {i+1} — {pseudo_name}</strong><br>
                            <small>{hit_str} • Closest: <b>{dist_str}</b></small><br>
                            <span class="risk-btn risk-critical">MAJOR</span>
                            <span class="risk-btn risk-lapsed" style="margin-left:4px;">RE-ENTRY</span>
                        </div>
                        """, unsafe_allow_html=True)

                # Space Weather Alerts
                if sw_alerts:
                    for ev in sw_alerts:
                        lv = ev["level"]
                        badge_class = "risk-critical" if "Severe" in lv or "G3" in lv else "risk-high"
                        day_label = ev["time_tag"].strftime("%b %d")
                        st.markdown(f"""
                        <div style="background-color:#1a4fa3;padding:14px;border-radius:10px;
                                    margin-bottom:12px;color:white;">
                            <strong>{day_label} — Geomagnetic Storm</strong><br>
                            <small>Kp: <b>{ev["kp"]:.1f}</b> • {ev["time_tag"].strftime("%H:%M UTC")}</small><br>
                            <small>⚡ {ev["impact"]}</small><br>
                            <span class="risk-btn {badge_class}">{lv}</span>
                            <span class="risk-btn risk-lapsed" style="margin-left:4px;">SW EVENT</span>
                        </div>
                        """, unsafe_allow_html=True)
              
                # Rocket Launches
                if len(launch_alerts) > 0:
                    for _, row in launch_alerts.iterrows():
                        date_str = row.get("LAUNCH DATE", "—")
                        rocket = row.get("Pseudo_Rocket", row.get("ROCKET NAME", "Unknown"))
                        site = row.get("Pseudo_Site", row.get("LAUNCH CENTER", "—"))
                        window = row.get("START TO END TIME", "—")
                        launch_time = row["LAUNCH_DATE_PARSED"]
                        time_until = launch_time - datetime.now()
                        days_left = time_until.days
                        hours_left = time_until.seconds // 3600
                        countdown = f"in {days_left}d {hours_left}h" if days_left > 0 else f"in {hours_left}h"
                        st.markdown(f"""
                        <div style="background-color:#1a4fa3;padding:14px;border-radius:10px;
                                    margin-bottom:12px;color:white;">
                            <strong>{rocket}</strong><br>
                            <small>{date_str} • {site}</small><br>
                            <small>{window}</small><br>
                            <small><b>{countdown}</b></small>
                            <span class="risk-btn risk-lapsed" style="margin-left:4px;">ROCKET LAUNCH</span>
                        </div>
                        """, unsafe_allow_html=True)

                # Conjunction Alerts
                if len(conjunction_alerts) > 0:
                    for _, row in conjunction_alerts.iterrows():
                        risk_label = row.get("RISK", "LOW")
                        clean_risk = str(risk_label).strip().replace("🔴","").replace("🟠","").replace("🟡","").replace("⚪","").replace("⚫","").strip()
                        risk_class = clean_risk.lower()
                        pseudo_event = row.get("Pseudo_Event", "Event Unknown")
                        tca_ph = str(row.get("TCA_PH", ""))[:16] if row.get("TCA_PH") else "—"
                        hours = row.get("RT_from_TCA", 0)
                        hours_str = f"{hours:.1f}h" if hours > 0 else "Soon"
                        pc = row.get("MAX_PC", 0)
                        miss = row.get("MIN_MISS", 0)
                        st.markdown(f"""
                        <div style="background-color:#1a4fa3;padding:14px;border-radius:10px;
                                    margin-bottom:12px;color:white;">
                            <strong>{pseudo_event}</strong><br>
                            <small>{tca_ph} PhST • <b>{hours_str}</b></small><br>
                            <small>PC: <b>{pc:.2e}</b> • Miss: <b>{miss:.1f} m</b></small><br>
                            <span class="risk-btn risk-{risk_class}">{risk_label}</span>
                            <span class="risk-btn risk-lapsed" style="margin-left:4px;">CONJUNCTION</span>
                        </div>
                        """, unsafe_allow_html=True)
                    if len(active_events) > 8:
                        st.caption(f"... and {len(active_events)-8} more active conjunctions")

        # ------------------- TAB 2: RECENT LAUNCHES -------------------
        with tab2:
            st.markdown("**Recent Launches (Last 15 Days)**")
            df = load_launch_database()
            if "LAUNCH_DATE_PARSED" in df.columns and not df.empty:
                cutoff = datetime.now() - timedelta(days=15)
                recent = df[df["LAUNCH_DATE_PARSED"] >= cutoff].copy()
                recent = recent.sort_values("LAUNCH_DATE_PARSED", ascending=False)
               
                if recent.empty:
                    st.info("No launches recorded in the last 15 days.")
                else:
                    recent = recent.copy()
                    r_map = _demo_label_map(recent["ROCKET NAME"], "Rocket")
                    recent["Pseudo_Rocket"] = recent["ROCKET NAME"].map(r_map)

                    s_map = _demo_label_map(recent["LAUNCH CENTER"], "Site")
                    recent["Pseudo_Site"] = recent["LAUNCH CENTER"].map(s_map)
                    
                    for _, row in recent.head(8).iterrows():
                        date_str = row.get("LAUNCH DATE", "—")
                        rocket = row.get("Pseudo_Rocket", row.get("ROCKET NAME", "Unknown"))
                        site = row.get("Pseudo_Site", row.get("LAUNCH CENTER", "—"))
                        window = row.get("START TO END TIME", "—")
                       
                        st.markdown(f"""
                        <div style="background-color: #1a4fa3; padding: 10px; border-radius: 8px;
                                    margin-bottom: 8px; color: white;">
                            <strong>{rocket}</strong><br>
                            <small>{date_str} • {site}</small><br>
                            <small>{window}</small>
                        </div>
                        """, unsafe_allow_html=True)
            else:
                st.info("No launch data available yet.")

        # ------------------- TAB 3: CONJUNCTION EVENTS -------------------
        with tab3:
            st.markdown("**Conjunction Events**")
            try:
                raw_conjunction_df = load_database()
                event_summary = compute_event_summary(raw_conjunction_df, use_persisted_summary=False)
               
                if event_summary.empty:
                    st.info("No conjunction events available.")
                else:
                    events_df = event_summary.copy()
                    events_df["TCA_dt"] = pd.to_datetime(events_df["TCA_UTC"], utc=True, errors="coerce")
                    events_df = events_df.sort_values(by="RT_from_TCA", ascending=False).reset_index(drop=True)

                    if DEMO_MODE:
                        events_df["Pseudo_Event"] = [f"Event {chr(65 + i)}" for i in range(len(events_df))]
                    else:
                        events_df["Pseudo_Event"] = events_df["EVENT_ID"]
                    
                    displayed = 0
                    for _, row in events_df.iterrows():
                        if displayed >= 6: break
                        risk_label = row.get("RISK", "LOW")
                        clean_risk = str(risk_label).strip().replace("🔴", "").replace("🟠", "").replace("🟡", "").replace("⚪", "").replace("⚫", "").strip()
                        risk_class = clean_risk.lower()
                       
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
                       
                    if len(events_df) > 6:
                        st.caption(f"... and {len(events_df)-6} more events")
            except Exception as e:
                st.warning("⚠️ Could not load conjunction events.")

        # ------------------- TAB 4: RE-ENTRIES -------------------
        with tab4:
            st.markdown("**Re-entry Events — Last 7 Days**")
            if reentry_err:
                st.warning(f"⚠️ Could not load re-entry data: {reentry_err}")
            elif not reentry_events:
                st.info("No re-entry events recorded in the last 7 days.")
            else:
                # Apply Pseudo Names + Numbering (dummy display when DEMO_MODE)
                for i, ev in enumerate(reentry_events[:8]):
                    sev = ev["severity"]
                    pseudo_name = _demo_positional_label(i, "Object", real_value=ev.get("name"))
                    hit_str = (ev["hit_utc"].strftime("%Y-%m-%d %H:%M PhST")
                                if ev["hit_utc"] and str(ev["hit_utc"]) != "NaT" else "—")
                    hit_ph = ev["hit_ph"] or "—"
                    dist_str = (f"{ev['distance']:.1f} km"
                                if ev["distance"] and str(ev["distance"]) != "nan" else "—")
                    tips_str = str(ev["total_tips"])
                    sev_class = "risk-critical" if sev == "MAJOR" else "risk-low"
                    major_note = " <span style='color:#ff9800;font-size:0.7rem;'>⚠ had MAJOR</span>" if ev.get("has_major") and sev != "MAJOR" else ""
                   
                    st.markdown(f"""
                    <div style="background-color:#1a4fa3;padding:14px;border-radius:10px;
                                margin-bottom:12px;color:white;">
                        <strong>Object {i+1} — {pseudo_name}</strong><br>
                        <small>{hit_str} • TIP updates: {tips_str}</small><br>
                        <small>Hit PH: <b>{hit_ph}</b> • Closest: <b>{dist_str}</b></small><br>
                        <span class="risk-btn {sev_class}">{sev}</span>{major_note}
                    </div>
                    """, unsafe_allow_html=True)
               
                if len(reentry_events) > 8:
                    st.caption(f"... and {len(reentry_events)-8} more objects")

        # ------------------- TAB 5: SPACE WEATHER (Unchanged) -------------------
        with tab5:
            st.markdown("**Space Weather Events — Last 7 Days**")
            if not sw_events:
                st.success("No significant geomagnetic activity in the last 7 days.")
            else:
                from collections import defaultdict as _dd
                by_day = _dd(list)
                for ev in sw_events:
                    day_key = ev["time_tag"].strftime("%b %d")
                    by_day[day_key].append(ev)
                for day_key in sorted(by_day.keys(),
                                    key=lambda d: datetime.strptime(d + f" {datetime.utcnow().year}", "%b %d %Y"),
                                    reverse=True):
                    day_evs = by_day[day_key]
                    max_kp = max(e["kp"] for e in day_evs)
                    max_ev = max(day_evs, key=lambda e: e["kp"])
                    is_today = day_key == datetime.utcnow().strftime("%b %d")
                    curr_ev = next((e for e in day_evs if e.get("is_current")), None)
                    curr_str = f"Current: <b>{curr_ev['kp']:.1f}</b> • " if curr_ev and is_today else ""
                    lv = max_ev["level"]
                    if "Severe" in lv or "G3" in lv:
                        badge_class = "risk-critical"
                    elif "Storm" in lv or "G1" in lv or "G2" in lv:
                        badge_class = "risk-high"
                    elif "Active" in lv:
                        badge_class = "risk-medium"
                    elif "Unsettled" in lv:
                        badge_class = "risk-medium"
                    else:
                        badge_class = "risk-low"
                    now_tag = " · NOW" if is_today else ""
                    st.markdown(f"""
                    <div style="background-color:#1a4fa3;padding:14px;border-radius:10px;
                                margin-bottom:12px;color:white;">
                        <strong>{day_key}{now_tag}</strong><br>
                        <small>{curr_str}Max Kp: <b>{max_kp:.1f}</b> • {len(day_evs)} period(s)</small><br>
                        <small>{max_ev["impact"]}</small><br>
                        <span class="risk-btn {badge_class}">{max_ev["level"]}</span>
                    </div>
                    """, unsafe_allow_html=True)
    
# ---- SSA TOOLS CARDS ----
st.markdown("---")
st.subheader("SSA Tools")
kpis = [
    ("compare_arrows",        "Conjunction",    "Upload and analyze CDM events",                   "collision", "#0b1f3a"),
    ("local_fire_department", "Re-entry",       "Assess and monitor object re-entry events",       "reenter",   "#0e2a57"),
    ("rocket_launch",         "Rocket Launch",  "Track and monitor upcoming and recent launches",  "rocket",    "#123a7a"),
    ("wb_sunny",              "Space Weather",  "View solar activity and space weather conditions","weather",   "#1a4fa3"),
]
cols = st.columns(4)
for col, (icon, title, desc, nav, color) in zip(cols, kpis):
    with col:
        kpi_card(icon, title, desc, "", nav, color)

# ---- QUICK ACTIONS ----
st.markdown("---")
st.subheader("Quick Actions")

quick_actions = [
    ("radar",            "Analyze Close Approach",   "#0b1f3a", "collision"),
    ("upload_file",      "Upload CDM",               "#0b1f3a", "collision"),
    ("satellite_alt",    "Analyze Re-entry",         "#0e2a57", "reenter?tab=analyzer"),
    ("flight_takeoff",   "Predict Re-entry",         "#0e2a57", "reenter?tab=predictor"),
    ("document_scanner", "NOTAM Parser",             "#123a7a", "rocket"),
    ("rocket_launch",    "See Upcoming Launches",    "#123a7a", "rocket"),
    ("wb_twilight",      "Predict Auroral Activity", "#1a4fa3", "weather"),
    ("electric_bolt",    "Assess Potential Outages", "#1a4fa3", "weather"),
]

for i in range(0, len(quick_actions), 4):
    cols = st.columns(4)
    for j in range(4):
        if i + j < len(quick_actions):
            with cols[j]:
                icon, title, color, page = quick_actions[i + j]
                quick_action_card(icon, title, color, page)
    if i + 4 < len(quick_actions):
        st.markdown("<br>", unsafe_allow_html=True)

st.markdown("<br><br>", unsafe_allow_html=True)
st.caption("Compiled by the Space Mission Control and Operations Division (SMCOD) of the Philippine Space Agency (PhilSA)")