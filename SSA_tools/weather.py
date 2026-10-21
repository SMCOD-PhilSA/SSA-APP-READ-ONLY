import math
import time
from datetime import datetime, timezone, timedelta

import requests
import streamlit as st
from streamlit_option_menu import option_menu
# get_daily_kp import removed — Kp is now fetched directly from NOAA SWPC

# ── optional deps ─────────────────────────────────────────────────────────────
try:
    from sgp4.api import Satrec, jday
    SGP4_OK = True
except ImportError:
    SGP4_OK = False

try:
    import plotly.graph_objects as go
    PLOTLY_OK = True
except ImportError:
    PLOTLY_OK = False

# ── constants ─────────────────────────────────────────────────────────────────
NOAA_AURORA_NORTH   = "https://services.swpc.noaa.gov/images/animations/ovation/north/latest.jpg"
NOAA_AURORA_SOUTH   = "https://services.swpc.noaa.gov/images/animations/ovation/south/latest.jpg"
# Observed Kp (3-hourly, most recent entry = current period) — NOAA SWPC
KP_1MIN_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
# Estimated real-time Kp at 1-min cadence (more current)
KP_REALTIME_URL = "https://services.swpc.noaa.gov/json/planetary_k_index_1m.json"
# Forecast (used for the 24h chart only)
KP_FORECAST_URL         = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index-forecast.json"
GOES_ELECTRON_URL       = "https://services.swpc.noaa.gov/json/goes/primary/differential-electrons-3-day.json"
GOES_PROTON_URL         = "https://services.swpc.noaa.gov/json/goes/primary/integral-protons-3-day.json"
GOES_MAG_URL            = "https://services.swpc.noaa.gov/json/goes/primary/magnetometers-3-day.json"
FORECAST_3DAY_URL       = "https://services.swpc.noaa.gov/text/3-day-forecast.txt"
SPACETRACK_LOGIN   = "https://www.space-track.org/ajaxauth/login"
SPACETRACK_TLE_URL = "https://www.space-track.org/basicspacedata/query/class/gp/OBJECT_NAME/{name}/orderby/EPOCH%20desc/limit/1/format/tle"

AURORA_LAT_MIN = 60.0
AURORA_LAT_MAX = 75.0

# key = display label, value = (norad_id, display_name)
POPULAR_SATS = {
    "ISS (ZARYA)       — 25544" : (25544,  "ISS"),
    "DIWATA-2          — 43678" : (43678,  "DIWATA-2"),
    "HUBBLE (HST)      — 20580" : (20580,  "Hubble"),
    "TERRA             — 25994" : (25994,  "TERRA"),
    "AQUA              — 27424" : (27424,  "AQUA"),
    "SENTINEL-2A       — 40697" : (40697,  "SENTINEL-2A"),
    "NOAA-20           — 43013" : (43013,  "NOAA-20"),
    "SUOMI NPP         — 37849" : (37849,  "SUOMI NPP"),
    "STARLINK-1007     — 44713" : (44713,  "STARLINK-1007"),
}

OPERATIONAL_EFFECTS = [
    ("Increased Radiation",      "Single Event Upsets (SEUs) in electronics"),
    ("Surface Charging",         "Electrostatic discharge on solar panels / MLI"),
    ("Deep Dielectric Charging", "Internal charging in thick insulators"),
    ("Attitude Disturbance",     "Torque changes from enhanced magnetic field"),
    ("Increased Drag",           "Atmosphere heating → density rise → faster decay"),
    ("Comms Degradation",        "Ionospheric scintillation on UHF/VHF links"),
]


# ── helpers ───────────────────────────────────────────────────────────────────
def kp_risk_label(kp: float):
    if kp < 4:  return "Low",      "#4caf50"
    if kp < 6:  return "Moderate", "#ff9800"
    if kp < 8:  return "High",     "#f44336"
    return          "Extreme",      "#9c27b0"


def effect_severity(effect_name: str, kp: float):
    high   = {"Increased Radiation", "Surface Charging", "Deep Dielectric Charging"}
    medium = {"Attitude Disturbance", "Comms Degradation"}
    if kp >= 6 and effect_name in high:    return "#f44336", "HIGH"
    if kp >= 4 and effect_name in high:    return "#ff9800", "MOD"
    if kp >= 4 and effect_name in medium:  return "#ff9800", "MOD"
    return "#4caf50", "LOW"


# ── data fetching ─────────────────────────────────────────────────────────────
# NOAA SWPC requires a browser-like User-Agent; without it the API returns 403.
_NOAA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.swpc.noaa.gov/",
}


@st.cache_data(ttl=60)   # 1-min cache
def fetch_kp_realtime():
    """
    Fetch the latest observed Kp index from NOAA SWPC.

    Primary source — noaa-planetary-k-index.json:
      Format: list of dicts {"time_tag": "2026-05-25T00:00:00", "Kp": 2.67, ...}
      Key is capital "Kp", time_tag uses ISO-8601 with "T" separator.

    Fallback — planetary_k_index_1m.json (1-min estimated):
      Format: list of dicts {"time_tag": "2026-06-01 12:00:00", "kp": 2.3}
      Key is lowercase "kp", time_tag uses space separator.

    Returns list of (datetime_utc, kp_float) — most recent last.
    """
    def _parse_tt(s):
        """Parse either ISO-8601 with T or space-separated datetime."""
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        return None

    # --- Primary: 3-hourly observed Kp (capital "Kp", ISO-8601 time_tag) ---
    try:
        r = requests.get(KP_1MIN_URL, headers=_NOAA_HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
        rows = []
        for item in data:
            try:
                dt = _parse_tt(item["time_tag"])
                # "Kp" capital — as confirmed by actual NOAA feed format
                kp_val = item.get("Kp") if item.get("Kp") is not None else item.get("kp")
                if dt and kp_val is not None and float(kp_val) >= 0:
                    rows.append((dt, float(kp_val)))
            except Exception:
                pass
        if rows:
            return rows
    except Exception:
        pass

    # --- Fallback: 1-min estimated Kp (lowercase "kp", space separator) ---
    try:
        r = requests.get(KP_REALTIME_URL, headers=_NOAA_HEADERS, timeout=10)
        r.raise_for_status()
        rows = []
        for item in r.json():
            try:
                dt = _parse_tt(item["time_tag"])
                kp_val = item.get("kp") if item.get("kp") is not None else item.get("Kp")
                if dt and kp_val is not None and float(kp_val) >= 0:
                    rows.append((dt, float(kp_val)))
            except Exception:
                pass
        if rows:
            return rows
    except Exception:
        pass

    return []


@st.cache_data(ttl=300)
def fetch_kp_forecast():
    """
    Fetch Kp forecast from NOAA SWPC.
    Format: list-of-lists, row[0] = header.
    Row format: ["2026-06-01 12:00:00", "2.33", "observed", "None"]
    """
    try:
        r = requests.get(KP_FORECAST_URL, headers=_NOAA_HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
        rows = []
        for item in data[1:]:   # skip header row
            try:
                # item is a list: [time_str, kp_str, observed_str, scale_str]
                dt = datetime.strptime(item[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                rows.append((dt, float(item[1])))
            except Exception:
                pass
        return rows
    except Exception:
        return []


@st.cache_data(ttl=1800)
def fetch_kp_history():
    """
    Fetch 30-day daily geomagnetic index history from NOAA SWPC.
    URL: https://services.swpc.noaa.gov/text/daily-geomagnetic-indices.txt
    Parses the Estimated Planetary Kp values (8 per day, 3-hourly).
    Returns list of dicts with keys: date, kp_values (list of 8 floats), kp_max, kp_mean.
    """
    url = "https://services.swpc.noaa.gov/text/daily-geomagnetic-indices.txt"
    try:
        r = requests.get(url, headers=_NOAA_HEADERS, timeout=15)
        r.raise_for_status()
        return _parse_dgd_text(r.text)
    except Exception:
        return []


def _parse_dgd_text(text):
    """
    Parse the Daily Geomagnetic Data (DGD) text file.
    Each data line format:
      YYYY MM DD  A  K0 K1 K2 K3 K4 K5 K6 K7   A  K0...K7   A  Kp0 Kp1 ... Kp7
    The Planetary Kp values are the last 8 space-separated floats on the line.
    Lines starting with '#' or ':' are comments/headers.
    """
    import re
    records = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#') or line.startswith(':'):
            continue
        # Match: YYYY MM DD followed by numbers
        m = re.match(r'^(\d{4})\s+(\d{2})\s+(\d{2})\s+(.+)$', line)
        if not m:
            continue
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        rest = m.group(4).split()
        # The line has 3 sections: Mid-lat (A + 8 K), High-lat (A + 8 K), Planetary (A + 8 Kp)
        # Total: 3*(1+8) = 27 values. Planetary Kp starts at index 18 (0-based).
        try:
            vals = [float(x) for x in rest]
        except ValueError:
            # Some values are "-1" (missing) — keep them, skip invalid
            vals = []
            for x in rest:
                try:
                    vals.append(float(x))
                except ValueError:
                    vals.append(-1.0)
        if len(vals) < 27:
            continue
        kp_vals = vals[19:27]   # 8 planetary Kp values (index 19-26; index 18 = A index)
        # Filter out -1 sentinel (missing data)
        valid_kp = [k for k in kp_vals if k >= 0]
        if not valid_kp:
            continue
        try:
            date = datetime(year, month, day, tzinfo=timezone.utc)
        except ValueError:
            continue
        records.append({
            "date":     date,
            "kp_values": kp_vals,
            "kp_max":   max(valid_kp),
            "kp_mean":  sum(valid_kp) / len(valid_kp),
        })
    return records


@st.cache_data(ttl=300)
def fetch_sw_events_last7days():
    """
    Fetch last-7-days 3-hourly observed Kp from NOAA SWPC (KP_1MIN_URL) and
    bucket each period into a severity level for the SW Dashboard's events
    panel. Mirrors dashboard.py's fetch_sw_events_last7days() so the "SW
    Dashboard" side panel and the home page's "Space Weather Events" tab
    always agree, but reuses weather.py's own _NOAA_HEADERS/KP_1MIN_URL
    instead of duplicating them.

    Returns list of dicts, newest-first, with keys:
      time_tag (datetime), kp (float), level (str), color (str),
      impact (str), is_current (bool)
    """
    def _parse_tt(s):
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        return None

    def _classify(kp):
        if kp >= 7:
            return ("Severe Storm G3+", "#9c27b0",
                    "Major GPS/HF disruptions · Satellite drag anomalies · S/C attitude disturbances")
        elif kp >= 5:
            return ("Storm G1-G2", "#f44336",
                    "GPS degradation · Magnetorquer performance reduced · Increased orbital drag at LEO")
        elif kp >= 4:
            return ("Active G1", "#ff9800",
                    "Minor HF radio disturbances · Slight increase in atmospheric drag")
        else:
            return ("Unsettled", "#ffc107",
                    "Elevated geomagnetic activity · Monitor sensitive instruments")

    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    all_rows = []

    try:
        r = requests.get(KP_1MIN_URL, headers=_NOAA_HEADERS, timeout=15)
        r.raise_for_status()
        for item in r.json():
            try:
                dt = _parse_tt(item.get("time_tag"))
                if dt is None or dt < cutoff:
                    continue
                kp = float(item.get("Kp") if item.get("Kp") is not None else item.get("kp") or 0)
                if kp < 0:
                    continue
                all_rows.append((dt, kp))
            except Exception:
                continue
    except Exception:
        pass

    all_rows.sort(key=lambda x: x[0], reverse=True)

    events = []
    for i, (dt, kp) in enumerate(all_rows):
        is_current = (i == 0)
        if kp < 3 and not is_current:
            continue
        level, color, impact = _classify(kp)
        if is_current and kp < 3:
            level, color, impact = "Quiet", "#4caf50", "Nominal geomagnetic conditions · No disruptions expected"
        events.append({
            "time_tag"  : dt,
            "kp"        : kp,
            "level"     : level,
            "color"     : color,
            "impact"    : impact,
            "is_current": is_current,
        })

    return events


@st.cache_data(ttl=300)
def fetch_goes_electrons():
    """Fetch 3-day GOES differential electron flux (primary satellite)."""
    try:
        r = requests.get(GOES_ELECTRON_URL, headers=_NOAA_HEADERS, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception:
        return []


@st.cache_data(ttl=300)
def fetch_goes_protons():
    """Fetch 3-day GOES integral proton flux (primary satellite)."""
    try:
        r = requests.get(GOES_PROTON_URL, headers=_NOAA_HEADERS, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception:
        return []


@st.cache_data(ttl=300)
def fetch_goes_magnetometer():
    """Fetch 3-day GOES magnetometer data (primary satellite)."""
    try:
        r = requests.get(GOES_MAG_URL, headers=_NOAA_HEADERS, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception:
        return []


@st.cache_data(ttl=1800)
def fetch_3day_forecast_text():
    """Fetch NOAA 3-day geomagnetic forecast text."""
    try:
        r = requests.get(FORECAST_3DAY_URL, headers=_NOAA_HEADERS, timeout=15)
        r.raise_for_status()
        return r.text
    except Exception:
        return ""


def parse_3day_kp_table(forecast_text):
    """
    Parse the NOAA 3-day forecast text and extract the Kp breakdown table.
    Returns a dict with keys: dates (list of 3 date strings),
    periods (list of 8 "HH-HH UT" strings), and kp (8x3 list of floats).
    """
    import re
    result = {"dates": [], "periods": [], "kp": [], "summary": "", "rationale": ""}
    if not forecast_text:
        return result

    lines = forecast_text.splitlines()

    # Extract summary line
    for line in lines:
        if "greatest observed" in line.lower():
            result["summary"] = line.strip().lstrip("#").strip()
        if "rationale:" in line.lower():
            result["rationale"] = line.strip().lstrip("#").strip()

    # Find the Kp breakdown table — looks for "00-03UT" pattern
    table_start = -1
    for i, line in enumerate(lines):
        if re.search(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{2}', line)                 and re.search(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{2}', line):
            # header row with 3 dates
            dates_raw = re.findall(
                r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{2})', line)
            result["dates"] = [f"{m} {d}" for m, d in dates_raw]
            table_start = i + 1
            break

    if table_start == -1 or not result["dates"]:
        return result

    kp_rows = []
    periods = []
    for line in lines[table_start:]:
        m = re.match(r'\s*(\d{2}-\d{2}UT)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)', line)
        if m:
            periods.append(m.group(1))
            kp_rows.append([float(m.group(2)), float(m.group(3)), float(m.group(4))])
        if len(kp_rows) == 8:
            break

    result["periods"] = periods
    result["kp"] = kp_rows
    return result


def _spacecraft_implication_goes(data_type, value, threshold):
    """Return (level, color, implication text) for a GOES measurement."""
    if data_type == "electron":
        # electron flux thresholds (pfu equivalent at ~2 MeV)
        if value > 1e5:
            return "CRITICAL", "#9c27b0", (
                "Deep dielectric charging risk is CRITICAL. Internal charge buildup in thick "
                "insulators and cable harnesses may cause destructive ESD arcs. Suspend "
                "non-essential payload operations and increase anomaly monitoring."
            )
        elif value > 1e4:
            return "HIGH", "#f44336", (
                "Elevated electron flux — high risk of deep dielectric charging. "
                "Increased probability of Single Event Upsets (SEUs) in memory and logic. "
                "Avoid non-critical commands on affected subsystems."
            )
        elif value > 1e3:
            return "MODERATE", "#ff9800", (
                "Moderate electron flux. Monitor for anomalous telemetry on sensitive electronics. "
                "Surface charging on MLI and solar panels possible. "
                "Defer non-essential deployments."
            )
        else:
            return "LOW", "#4caf50", (
                "Quiet electron environment. No immediate charging threat. "
                "Normal spacecraft operations permitted."
            )
    elif data_type == "proton":
        # NOAA S-scale thresholds (≥10 MeV protons, pfu)
        if value > 10000:
            return "EXTREME (S5)", "#9c27b0", (
                "Extreme solar proton event (S5). Severe radiation hazard to all spacecraft. "
                "High risk of permanent SEL/latch-up and total ionizing dose damage. "
                "Initiate safe mode; protect sensitive instruments. EVA operations must cease."
            )
        elif value > 1000:
            return "SEVERE (S4)", "#f44336", (
                "Severe proton storm (S4). Significant SEU rate increase expected. "
                "High-gain antenna pointing may degrade. "
                "Enable radiation-safe mode for vulnerable subsystems."
            )
        elif value > 100:
            return "STRONG (S3)", "#ff5722", (
                "Strong proton event (S3). Star tracker and sun sensor noise expected. "
                "Onboard computers may experience increased bit errors. "
                "Increase command/telemetry verification."
            )
        elif value > 10:
            return "MODERATE (S2)", "#ff9800", (
                "Moderate proton flux (S2). Isolated SEUs possible on memory-intensive systems. "
                "Navigate satellites away from high-radiation orbits if feasible."
            )
        elif value > 1:
            return "MINOR (S1)", "#ffc107", (
                "Minor proton enhancement. Low SEU risk but monitor sensitive instruments. "
                "Normal operations permitted with increased telemetry scrutiny."
            )
        else:
            return "QUIET", "#4caf50", (
                "Quiet proton environment. No radiation enhancement detected. "
                "Normal spacecraft operations permitted."
            )
    elif data_type == "mag":
        # Magnetic field total field perturbation
        if value > 150:
            return "STORM", "#f44336", (
                "Strongly disturbed magnetosphere. Large attitude disturbances expected from "
                "enhanced magnetic torque. Magnetometer-based attitude control may lose lock. "
                "Switch to gyro/star tracker primary control."
            )
        elif value > 120:
            return "ACTIVE", "#ff9800", (
                "Active magnetic conditions. Attitude control systems using magnetorquers "
                "may experience increased correction cycles. "
                "Monitor spacecraft attitude error margins."
            )
        else:
            return "QUIET", "#4caf50", (
                "Nominal magnetic field conditions. "
                "Magnetorquer-based attitude control operating normally."
            )
    return "UNKNOWN", "#888888", "No implication data available."


def render_satellite_environment(curr_kp, is_dark):
    """
    Render the full Satellite Environment section:
      - GOES Electron Flux (3-day)
      - GOES Proton Flux (3-day)
      - GOES Magnetometer (3-day)
      - 3-Day Kp Forecast table (NOAA style)
    Each chart is followed by a spacecraft implication panel.
    """
    import plotly.graph_objects as pgo

    bg, border, tc, sc = _palette(is_dark)
    paper_bg = "rgba(0,0,0,0)"
    plot_bg  = "rgba(0,0,0,0)"
    grid_col = border
    axis_col = sc

    axis_style = dict(color=axis_col, gridcolor=grid_col, showgrid=True,
                      zeroline=False, tickfont=dict(size=10))

    def implication_box(level, col, text):
        return f"""
        <div style="background:var(--background-color);border:1px solid rgba(128,128,128,0.3);border-left:4px solid {col};
             border-radius:8px;padding:12px 16px;margin:10px 0 20px;">
          <div style="font-size:0.72rem;text-transform:uppercase;letter-spacing:.06em;
               font-weight:700;color:{col};margin-bottom:6px;">
               Spacecraft Implication — {level}</div>
          <div style="font-size:0.85rem;color:var(--text-color);line-height:1.6;">{text}</div>
        </div>"""

    # ── fetch all data ────────────────────────────────────────────────────────
    with st.spinner("Loading GOES satellite environment data…"):
        electrons = fetch_goes_electrons()
        protons   = fetch_goes_protons()
        mags      = fetch_goes_magnetometer()
        fcst_text = fetch_3day_forecast_text()

    st.markdown("""
    <div style="background:var(--background-color);border:1px solid rgba(128,128,128,0.3);border-radius:10px;
         padding:14px 18px;margin-bottom:18px;">
      <div style="font-weight:700;font-size:1rem;color:var(--text-color);margin-bottom:4px;">
          Satellite Environment · GOES-R Series</div>
      <div style="font-size:0.8rem;color:var(--text-color);opacity:0.7;">
          Real-time particle flux and magnetic field data from NOAA GOES primary satellite.
          Data updated every 5 minutes. Source:
          <a href="https://www.swpc.noaa.gov/" target="_blank"
             style="color:#1a4fa3;">NOAA Space Weather Prediction Center</a>
      </div>
    </div>""", unsafe_allow_html=True)

    # ════════════════════════════════════════════════════════════════════
    # 1. GOES ELECTRON FLUX
    # ════════════════════════════════════════════════════════════════════
    st.markdown(f"#### GOES Electron Flux",)
    st.caption("Differential electron flux · GOES Primary · 3-day")

    if electrons:
        import pandas as pd
        df_e = pd.DataFrame(electrons)
        df_e["time_tag"] = pd.to_datetime(df_e["time_tag"])

        # NOAA differential electrons: energy channels stored as separate rows
        # with "energy" column, or as e1_flux/e2_flux columns depending on version
        fig_e = pgo.Figure()

        energy_cols = [c for c in df_e.columns if c.startswith("flux") or c in
                       ("flux", "e1_flux", "e2_flux", "e3_flux", "e4_flux", "e5_flux")]

        # If there's a single "flux" column with separate "energy" rows
        if "flux" in df_e.columns and "energy" in df_e.columns:
            colors_e = ["#00bcd4", "#ff9800", "#4caf50", "#f44336", "#9c27b0"]
            for idx, eng in enumerate(df_e["energy"].unique()):
                sub = df_e[df_e["energy"] == eng].sort_values("time_tag")
                sub_pos = sub[sub["flux"] > 0]
                if sub_pos.empty:
                    continue
                fig_e.add_trace(pgo.Scatter(
                    x=sub_pos["time_tag"], y=sub_pos["flux"],
                    mode="lines", name=str(eng),
                    line=dict(width=1.2, color=colors_e[idx % len(colors_e)]),
                    hovertemplate="%{x|%b %d %H:%M}<br>Flux: %{y:.2e}<extra>" + str(eng) + "</extra>",
                ))
            latest_flux = df_e[df_e["flux"] > 0]["flux"].iloc[-1] if not df_e[df_e["flux"] > 0].empty else 0
        else:
            # Multi-column format
            ecols = [c for c in df_e.columns if "flux" in c.lower()]
            colors_e = ["#00bcd4", "#ff9800", "#4caf50", "#f44336", "#9c27b0"]
            latest_flux = 0
            for idx, col in enumerate(ecols[:5]):
                sub = df_e[["time_tag", col]].dropna()
                sub = sub[sub[col] > 0]
                if sub.empty:
                    continue
                fig_e.add_trace(pgo.Scatter(
                    x=sub["time_tag"], y=sub[col],
                    mode="lines", name=col,
                    line=dict(width=1.2, color=colors_e[idx % len(colors_e)]),
                    hovertemplate="%{x|%b %d %H:%M}<br>%{y:.2e}<extra>" + col + "</extra>",
                ))
                latest_flux = float(sub[col].iloc[-1])

        fig_e.update_layout(
            height=280, margin=dict(l=60, r=20, t=20, b=50),
            paper_bgcolor=paper_bg, plot_bgcolor=plot_bg,
            xaxis=dict(**axis_style, title="Universal Time"),
            yaxis=dict(**axis_style, type="log", title="Flux (particles cm⁻² s⁻¹ sr⁻¹ MeV⁻¹)",
                       exponentformat="power"),
            legend=dict(bgcolor="rgba(0,0,0,0.3)", font=dict(color=tc, size=10),
                        x=0.01, y=0.99, bordercolor=border, borderwidth=1),
            hovermode="x unified",
        )
        st.plotly_chart(fig_e, use_container_width=True, config={"displayModeBar": False})

        # Implication
        level_e, col_e, impl_e = _spacecraft_implication_goes("electron", latest_flux, None)
        st.markdown(implication_box(level_e, col_e, impl_e), unsafe_allow_html=True)
    else:
        st.info("Electron flux data unavailable — NOAA SWPC API may be temporarily down.")
        st.markdown(implication_box("UNKNOWN", "#888",
            "No live electron data. Assume moderate environment as a precaution."),
            unsafe_allow_html=True)

    st.divider()

    # ════════════════════════════════════════════════════════════════════
    # 2. GOES PROTON FLUX
    # ════════════════════════════════════════════════════════════════════
    st.markdown("#### GOES Proton Flux")
    st.caption("Integral proton flux · GOES Primary · 3-day")

    if protons:
        import pandas as pd
        df_p = pd.DataFrame(protons)
        df_p["time_tag"] = pd.to_datetime(df_p["time_tag"])

        fig_p = pgo.Figure()
        pcols_map = {
            "p1_flux":  "≥1 MeV",   "p2_flux":  "≥5 MeV",
            "p3_flux":  "≥10 MeV",  "p4_flux":  "≥30 MeV",
            "p5_flux":  "≥50 MeV",  "p6_flux":  "≥60 MeV",
            "p7_flux":  "≥100 MeV", "p8_flux":  "≥500 MeV",
        }
        pcols_color = {
            "p1_flux": "#ff0000", "p2_flux": "#ff6600",
            "p3_flux": "#ffcc00", "p4_flux": "#00cc00",
            "p5_flux": "#00cccc", "p6_flux": "#0066ff",
            "p7_flux": "#cc00ff", "p8_flux": "#ff99cc",
        }

        # Also handle "flux"+"energy" format
        latest_p10 = 0.0
        if "flux" in df_p.columns and "energy" in df_p.columns:
            colors_p = list(pcols_color.values())
            for idx, eng in enumerate(df_p["energy"].unique()):
                sub = df_p[df_p["energy"] == eng].sort_values("time_tag")
                sub_pos = sub[sub["flux"] > 0]
                if sub_pos.empty:
                    continue
                fig_p.add_trace(pgo.Scatter(
                    x=sub_pos["time_tag"], y=sub_pos["flux"],
                    mode="lines", name=str(eng),
                    line=dict(width=1.2, color=colors_p[idx % len(colors_p)]),
                    hovertemplate="%{x|%b %d %H:%M}<br>%{y:.2e} pfu<extra>" + str(eng) + "</extra>",
                ))
                if "10" in str(eng):
                    latest_p10 = float(sub_pos["flux"].iloc[-1])
        else:
            for col, label in pcols_map.items():
                if col not in df_p.columns:
                    continue
                sub = df_p[["time_tag", col]].dropna()
                sub = sub[sub[col] > 0]
                if sub.empty:
                    continue
                fig_p.add_trace(pgo.Scatter(
                    x=sub["time_tag"], y=sub[col],
                    mode="lines", name=label,
                    line=dict(width=1.2, color=pcols_color.get(col, "#888")),
                    hovertemplate="%{x|%b %d %H:%M}<br>%{y:.2e} pfu<extra>" + label + "</extra>",
                ))
                if col == "p3_flux":
                    latest_p10 = float(sub[col].iloc[-1]) if not sub.empty else 0.0

        # S-scale threshold lines
        for threshold, label, col_t in [
            (10, "S1 (10 pfu)", "#ffc107"),
            (100, "S2 (100 pfu)", "#ff9800"),
            (1000, "S3 (1000 pfu)", "#f44336"),
        ]:
            fig_p.add_hline(y=threshold, line_dash="dot", line_color=col_t,
                            annotation_text=label, annotation_font_color=col_t,
                            annotation_position="top right",
                            annotation_font_size=10)

        fig_p.update_layout(
            height=280, margin=dict(l=60, r=20, t=20, b=50),
            paper_bgcolor=paper_bg, plot_bgcolor=plot_bg,
            xaxis=dict(**axis_style, title="Universal Time"),
            yaxis=dict(**axis_style, type="log", title="Flux (pfu = particles cm⁻² s⁻¹ sr⁻¹)",
                       exponentformat="power"),
            legend=dict(bgcolor="rgba(0,0,0,0.3)", font=dict(color=tc, size=10),
                        x=0.01, y=0.99, bordercolor=border, borderwidth=1),
            hovermode="x unified",
        )
        st.plotly_chart(fig_p, use_container_width=True, config={"displayModeBar": False})

        level_p, col_p, impl_p = _spacecraft_implication_goes("proton", latest_p10, None)
        st.markdown(implication_box(level_p, col_p, impl_p), unsafe_allow_html=True)
    else:
        st.info("Proton flux data unavailable — NOAA SWPC API may be temporarily down.")
        st.markdown(implication_box("UNKNOWN", "#888",
            "No live proton data. Assume background levels; maintain standard radiation monitoring."),
            unsafe_allow_html=True)

    st.divider()

    # ════════════════════════════════════════════════════════════════════
    # 3. GOES MAGNETOMETER
    # ════════════════════════════════════════════════════════════════════
    st.markdown("#### GOES Magnetometers")
    st.caption("Magnetic field components (Hp, He, Hn, Ht) · GOES Primary · 3-day · nT")

    if mags:
        import pandas as pd
        df_m = pd.DataFrame(mags)
        df_m["time_tag"] = pd.to_datetime(df_m["time_tag"])

        fig_m = pgo.Figure()
        mag_map = {
            "Hp": ("#2196f3", "Hp — parallel to Earth rotation axis"),
            "He": ("#f44336", "He — Earthward"),
            "Hn": ("#4caf50", "Hn — normal to orbital plane"),
        }
        latest_ht = 90.0  # default nominal ~90 nT at GEO
        for col_key, (col_color, label) in mag_map.items():
            if col_key not in df_m.columns:
                continue
            sub = df_m[["time_tag", col_key]].dropna()
            fig_m.add_trace(pgo.Scatter(
                x=sub["time_tag"], y=sub[col_key],
                mode="lines", name=label,
                line=dict(width=1.5, color=col_color),
                hovertemplate="%{x|%b %d %H:%M}<br>%{y:.1f} nT<extra>" + col_key + "</extra>",
            ))

        if "Ht" in df_m.columns:
            sub_ht = df_m[["time_tag", "Ht"]].dropna()
            if not sub_ht.empty:
                latest_ht = float(sub_ht["Ht"].iloc[-1])

        fig_m.update_layout(
            height=280, margin=dict(l=60, r=20, t=20, b=50),
            paper_bgcolor=paper_bg, plot_bgcolor=plot_bg,
            xaxis=dict(**axis_style, title="Universal Time"),
            yaxis=dict(**axis_style, title="Magnetic Field (nT)"),
            legend=dict(bgcolor="rgba(0,0,0,0.3)", font=dict(color=tc, size=10),
                        x=0.01, y=0.99, bordercolor=border, borderwidth=1),
            hovermode="x unified",
        )
        st.plotly_chart(fig_m, use_container_width=True, config={"displayModeBar": False})

        level_m, col_m, impl_m = _spacecraft_implication_goes("mag", latest_ht, None)
        st.markdown(implication_box(level_m, col_m, impl_m), unsafe_allow_html=True)
    else:
        st.info("Magnetometer data unavailable — NOAA SWPC API may be temporarily down.")
        st.markdown(implication_box("UNKNOWN", "#888",
            "No live magnetometer data. Use onboard magnetometer telemetry for attitude control assessment."),
            unsafe_allow_html=True)

    st.divider()

    # ════════════════════════════════════════════════════════════════════
    # 4. 3-DAY Kp FORECAST TABLE (NOAA style bar chart + table)
    # ════════════════════════════════════════════════════════════════════
    st.markdown("#### 3-Day Kp Index Forecast")
    st.caption("NOAA Geomagnetic Activity Forecast · Issued by SWPC")

    parsed = parse_3day_kp_table(fcst_text)

    if parsed["summary"]:
        st.markdown(
            f'<div style="background:var(--background-color);border:1px solid rgba(128,128,128,0.3);border-radius:8px;'
            f'padding:10px 14px;margin-bottom:12px;font-size:0.85rem;color:var(--text-color);opacity:0.8;">'
            f'📋 {parsed["summary"]}</div>',
            unsafe_allow_html=True)

    if parsed["dates"] and parsed["kp"]:
        import pandas as pd
        import numpy as np

        dates = parsed["dates"]      # e.g. ["Jun 01", "Jun 02", "Jun 03"]
        periods = parsed["periods"]  # e.g. ["00-03UT", ..., "21-00UT"]
        kp_matrix = parsed["kp"]     # 8 rows × 3 cols

        # ── Bar chart (NOAA style — grouped by day) ─────────────────────────
        fig_kp3 = pgo.Figure()
        day_colors = ["#2196f3", "#00bcd4", "#4caf50"]

        for d_idx, (date_label, color) in enumerate(zip(dates, day_colors)):
            kp_day = [row[d_idx] for row in kp_matrix]
            bar_colors = [
                "#4caf50" if k < 4 else "#ff9800" if k < 6 else "#f44336" if k < 8 else "#9c27b0"
                for k in kp_day
            ]
            fig_kp3.add_trace(pgo.Bar(
                name=date_label,
                x=periods,
                y=kp_day,
                marker_color=bar_colors,
                opacity=0.85,
                hovertemplate=f"<b>{date_label}</b><br>%{{x}}<br>Kp: %{{y}}<extra></extra>",
                showlegend=True,
            ))

        fig_kp3.add_hline(y=4, line_dash="dot", line_color="#ff9800",
                          annotation_text="G1 Storm (Kp=4)", annotation_font_color="#ff9800",
                          annotation_position="top left", annotation_font_size=10)
        fig_kp3.add_hline(y=6, line_dash="dot", line_color="#f44336",
                          annotation_text="G2 Storm (Kp=6)", annotation_font_color="#f44336",
                          annotation_position="top left", annotation_font_size=10)

        fig_kp3.update_layout(
            height=300, margin=dict(l=50, r=20, t=30, b=60),
            paper_bgcolor=paper_bg, plot_bgcolor=plot_bg,
            xaxis=dict(**axis_style, title="3-Hour UT Period", tickangle=-35),
            yaxis=dict(**axis_style, range=[0, 9.5], title="Kp Index",
                       tickvals=[0,1,2,3,4,5,6,7,8,9]),
            barmode="group",
            legend=dict(bgcolor="rgba(0,0,0,0.3)", font=dict(color=tc, size=11),
                        orientation="h", x=0, y=1.12, bordercolor=border, borderwidth=1),
            hovermode="x",
        )
        st.plotly_chart(fig_kp3, use_container_width=True, config={"displayModeBar": False})

        # ── Kp table (colour-coded cells) ───────────────────────────────────
        def kp_cell_color(k):
            if k < 4:   return "#4caf50", "#fff"
            if k < 6:   return "#ff9800", "#fff"
            if k < 8:   return "#f44336", "#fff"
            return "#9c27b0", "#fff"

        header_html = "".join(
            f'<th style="padding:6px 12px;text-align:center;font-size:0.78rem;'
            f'color:var(--text-color);opacity:0.7;text-transform:uppercase;letter-spacing:.05em;">{d}</th>'
            for d in dates
        )
        rows_html = ""
        for i, (period, kp_row) in enumerate(zip(periods, kp_matrix)):
            row_bg = f"rgba(255,255,255,0.03)" if i % 2 == 0 else "rgba(0,0,0,0)"
            cells = ""
            for k in kp_row:
                bg_cell, fg_cell = kp_cell_color(k)
                cells += (
                    f'<td style="padding:5px 12px;text-align:center;">'
                    f'<span style="background:{bg_cell};color:{fg_cell};'
                    f'font-weight:700;font-size:0.82rem;padding:2px 9px;'
                    f'border-radius:10px;">{k:.2f}</span></td>'
                )
            rows_html += (
                f'<tr style="background:{row_bg};">'
                f'<td style="padding:5px 12px;font-size:0.82rem;color:var(--text-color);opacity:0.7;'
                f'font-family:monospace;">{period}</td>'
                f'{cells}</tr>'
            )

        hdr_bg = "var(--secondary-background-color)"
        st.markdown(f"""
        <div style="background:var(--background-color);border:1px solid rgba(128,128,128,0.3);border-radius:10px;
             overflow:hidden;margin-bottom:12px;">
          <div style="padding:10px 16px;font-weight:700;font-size:0.9rem;color:var(--text-color);
               border-bottom:1px solid rgba(128,128,128,0.3);background:{hdr_bg};">
               NOAA Kp Index Breakdown — {", ".join(dates)}</div>
          <table style="width:100%;border-collapse:collapse;">
            <thead>
              <tr style="background:{hdr_bg};">
                <th style="padding:6px 12px;text-align:left;font-size:0.78rem;
                    color:var(--text-color);opacity:0.7;text-transform:uppercase;letter-spacing:.05em;">UT Period</th>
                {header_html}
              </tr>
            </thead>
            <tbody>{rows_html}</tbody>
          </table>
        </div>""", unsafe_allow_html=True)

        # ── Geomagnetic storm implications for spacecraft ────────────────────
        max_kp = max(k for row in kp_matrix for k in row)
        level_k, col_k, _ = _spacecraft_implication_goes("proton", 0, None)  # reuse scale logic
        # Derive from max_kp directly
        if max_kp >= 8:
            level_k, col_k = "EXTREME G4-G5", "#9c27b0"
            impl_k = (
                f"Maximum forecast Kp of {max_kp:.1f} indicates a severe geomagnetic storm (G4-G5). "
                "Spacecraft drag will increase dramatically at LEO — revisit orbital decay predictions. "
                "GPS navigation errors up to 10 m expected at affected latitudes. "
                "Protect all sensitive payloads; consider safe-mode operations during peak activity. "
                "High risk of induced currents on long cable harnesses."
            )
        elif max_kp >= 6:
            level_k, col_k = "STRONG G2-G3", "#f44336"
            impl_k = (
                f"Maximum forecast Kp of {max_kp:.1f} indicates a strong storm (G2-G3). "
                "Atmospheric density at LEO will increase, accelerating orbital decay. "
                "GPS positioning degradation expected. "
                "Magnetorquer authority may be insufficient during peak disturbance. "
                "Avoid sensitive instrument calibrations during this window."
            )
        elif max_kp >= 4:
            level_k, col_k = "MODERATE G0-G1", "#ff9800"
            impl_k = (
                f"Maximum forecast Kp of {max_kp:.1f} — minor to moderate activity expected. "
                "Slight increase in atmospheric drag at LEO. Monitor attitude control margins. "
                "GPS accuracy may degrade by 1–3 m at high latitudes. "
                "Normal operations permitted; increase telemetry downlink frequency during peaks."
            )
        else:
            level_k, col_k = "QUIET", "#4caf50"
            impl_k = (
                f"Maximum forecast Kp of {max_kp:.1f} — geomagnetically quiet period. "
                "No significant impact on spacecraft drag, GPS, or attitude control expected. "
                "Optimal window for sensitive instrument calibrations and critical maneuvers."
            )

        if parsed.get("rationale"):
            impl_k += "  \n\n**SWPC Rationale:** " + parsed["rationale"].replace("Rationale:", "").strip()

        st.markdown(implication_box(level_k, col_k, impl_k), unsafe_allow_html=True)

    else:
        st.info("3-day forecast text unavailable from NOAA SWPC.")



def get_current_kp() -> float:
    """
    Get the most recent observed Kp value from NOAA SWPC.
    Uses the 1-min feed when available; falls back to 3-hourly observed data.
    No dependency on internal get_daily_kp().
    """
    rt_rows = fetch_kp_realtime()
    if rt_rows:
        now  = datetime.now(timezone.utc)
        past = [(dt, kp) for dt, kp in rt_rows if dt <= now]
        if past:
            return past[-1][1]
        return rt_rows[-1][1]
    return 0.0


import http.cookiejar as _cookiejar
import urllib.request as _urllib_req
import urllib.parse as _urllib_parse
import ssl as _ssl

try:
    import certifi as _certifi
except ImportError:
    _certifi = None


def _get_spacetrack_creds():
    """Read Space-Track credentials from st.secrets or environment."""
    try:
        return (
            st.secrets["spacetrack"]["username"],
            st.secrets["spacetrack"]["password"],
        )
    except (KeyError, FileNotFoundError):
        import os
        username = os.getenv("SPACETRACK_USERNAME")
        password = os.getenv("SPACETRACK_PASSWORD")
        if username and password:
            return username, password
        return None, None


def _build_opener():
    """Build a urllib opener with cookie support — mirrors the reference script."""
    cj = _cookiejar.CookieJar()
    handlers = [_urllib_req.HTTPCookieProcessor(cj)]
    if _certifi is not None:
        ssl_ctx = _ssl.create_default_context(cafile=_certifi.where())
        handlers.append(_urllib_req.HTTPSHandler(context=ssl_ctx))
    else:
        handlers.append(_urllib_req.HTTPSHandler())
    opener = _urllib_req.build_opener(*handlers)
    opener.addheaders = [
        ("User-Agent", "SSA-Dashboard/1.0 (Python urllib)"),
        ("Accept", "*/*"),
    ]
    return opener


def _spacetrack_login(opener, username, password):
    """POST login to Space-Track. Returns True on success."""
    data = _urllib_parse.urlencode({
        "identity": username,
        "password": password,
    }).encode("utf-8")
    req = _urllib_req.Request(SPACETRACK_LOGIN, data=data)
    try:
        resp = opener.open(req, timeout=30)
        body = resp.read().decode("utf-8", "replace")
        # Space-Track returns "Failed" in the body on bad credentials
        if "Failed" in body:
            return False, "Bad credentials — check username/password in secrets.toml"
        return True, None
    except Exception as e:
        return False, str(e)


@st.cache_data(ttl=600)
def fetch_tle(norad_id: int):
    """
    Fetch the latest TLE for a satellite from Space-Track.org by NORAD ID.
    Uses urllib + cookie jar — same method as the reference fetch script.
    Credentials from .streamlit/secrets.toml [spacetrack] or env vars.
    Returns (line1, line2) or None.
    """
    username, password = _get_spacetrack_creds()
    if not username or not password:
        st.error(
            "Space-Track credentials not found. "
            "Add [spacetrack] username and password to .streamlit/secrets.toml"
        )
        return None

    opener = _build_opener()

    # Step 1 — login
    ok, err = _spacetrack_login(opener, username, password)
    if not ok:
        st.error(f"Space-Track login failed: {err}")
        return None

    # Step 2 — query by NORAD CAT ID, same URL pattern as reference script
    url = (
        "https://www.space-track.org/basicspacedata/query/class/gp"
        f"/NORAD_CAT_ID/{int(norad_id)}"
        "/orderby/NORAD_CAT_ID%20ASC,EPOCH%20DESC"
        "/format/tle"
    )
    try:
        resp     = opener.open(url, timeout=60)
        tle_text = resp.read().decode("utf-8", "replace")
    except Exception as e:
        st.error(f"Space-Track TLE fetch failed: {e}")
        return None

    # Step 3 — parse TLE lines (same parser as reference script)
    lines = [ln.strip() for ln in tle_text.splitlines() if ln.strip()]
    i = 0
    while i + 1 < len(lines):
        l1, l2 = lines[i], lines[i + 1]
        if l1.startswith("1 ") and l2.startswith("2 "):
            return l1, l2
        i += 1

    st.warning(f"No TLE found for NORAD ID {norad_id}. "
               "Verify the ID exists on space-track.org.")
    return None


def propagate_orbit(tle1, tle2, minutes_ahead=100, step_sec=60):
    if not SGP4_OK:
        return []
    try:
        sat = Satrec.twoline2rv(tle1, tle2)
        now = datetime.now(timezone.utc)
        pts = []
        for s in range(0, minutes_ahead * 60, step_sec):
            t = now + timedelta(seconds=s)
            jd, fr = jday(t.year, t.month, t.day, t.hour, t.minute,
                          t.second + t.microsecond / 1e6)
            e, r, v = sat.sgp4(jd, fr)
            if e != 0:
                continue
            x, y, z = r
            alt = math.sqrt(x**2 + y**2 + z**2) - 6371.0
            lat = math.degrees(math.asin(z / math.sqrt(x**2 + y**2 + z**2)))
            t_j2000 = (jd + fr - 2451545.0)
            gmst    = math.fmod(280.46061837 + 360.98564736629 * t_j2000, 360.0)
            lon     = (math.degrees(math.atan2(y, x)) - gmst + 180) % 360 - 180
            pts.append((lat, lon, alt, t))
        return pts
    except Exception:
        return []


def aurora_crossings(orbit_pts, kp):
    crossings, inside, entry_t = [], False, None
    for lat, lon, alt, t in orbit_pts:
        in_oval = AURORA_LAT_MIN <= abs(lat) <= AURORA_LAT_MAX
        if in_oval and not inside:
            inside, entry_t = True, t
        elif not in_oval and inside:
            inside = False
            crossings.append({"entry": entry_t, "exit": t,
                               "duration": (t - entry_t).total_seconds() / 60, "kp": kp})
    if inside and entry_t:
        crossings.append({"entry": entry_t, "exit": orbit_pts[-1][3],
                          "duration": (orbit_pts[-1][3] - entry_t).total_seconds() / 60, "kp": kp})
    return crossings


# # ── UI sections ───────────────────────────────────────────────────────────────
# def render_kp_strip(curr_kp, is_dark):
#     bg, border, tc, sc = _palette(is_dark)
#     risk_lbl, risk_col = kp_risk_label(curr_kp)
#     st.markdown(f"""
#     <div style="display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap;">
#       <div style="background:{bg};border:1px solid {border};border-radius:10px;
#            padding:12px 20px;flex:1;min-width:130px;text-align:center;">
#         <div style="font-size:0.7rem;color:{sc};text-transform:uppercase;
#              letter-spacing:.06em;margin-bottom:4px;">Current Kp</div>
#         <div style="font-size:2rem;font-weight:800;color:{risk_col};">{curr_kp:.1f}</div>
#         <div style="font-size:0.78rem;color:{risk_col};font-weight:600;">{risk_lbl}</div>
#       </div>
#       <div style="background:{bg};border:1px solid {border};border-radius:10px;
#            padding:12px 20px;flex:1;min-width:130px;text-align:center;">
#         <div style="font-size:0.7rem;color:{sc};text-transform:uppercase;
#              letter-spacing:.06em;margin-bottom:4px;">Aurora Oval</div>
#         <div style="font-size:1rem;font-weight:700;color:{tc};margin-top:8px;">60°–75° lat</div>
#         <div style="font-size:0.78rem;color:{sc};">Both hemispheres</div>
#       </div>
#       <div style="background:{bg};border:1px solid {border};border-radius:10px;
#            padding:12px 20px;flex:1;min-width:130px;text-align:center;">
#         <div style="font-size:0.7rem;color:{sc};text-transform:uppercase;
#              letter-spacing:.06em;margin-bottom:4px;">Data Source</div>
#         <div style="font-size:0.85rem;font-weight:600;color:{tc};margin-top:8px;">NOAA SWPC</div>
#         <div style="font-size:0.78rem;color:{sc};">Updated every 5 min</div>
#       </div>
#     </div>""", unsafe_allow_html=True)

# ── UI sections ───────────────────────────────────────────────────────────────
def render_kp_strip(curr_kp, is_dark=True):
    risk_lbl, risk_col = kp_risk_label(curr_kp)
    
    st.markdown("""
    <style>
        .kpi-card {
            background: var(--secondary-background-color);
            border-radius: 15px;
            padding: 22px 15px;
            text-align: center;
            box-shadow: 0 4px 15px rgba(0,0,0,0.15);
            border: 1px solid rgba(128,128,128,0.25);
            height: 168px;
            display: flex;
            flex-direction: column;
            justify-content: center;
            align-items: center;
            transition: all 0.3s ease;
        }
        .kpi-card:hover {
            transform: translateY(-4px);
            box-shadow: 0 8px 20px rgba(0,0,0,0.2);
        }
        .kpi-title {
            font-size: 1.05rem;
            color: var(--text-color);
            opacity: 0.6;
            margin-bottom: 8px;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            font-weight: 500;
        }
        .kpi-value {
            font-size: 2.25rem;
            font-weight: 700;
            margin: 4px 0;
            line-height: 1.1;
            color: var(--text-color);
        }
        .kpi-subtitle {
            font-size: 0.84rem;
            margin-top: 4px;
            opacity: 0.75;
            line-height: 1.2;
            color: var(--text-color);
        }
    </style>
    """, unsafe_allow_html=True)

    # Create three KPI cards with equal sizing
    col1, col2, col3 = st.columns(3)
    
    with col1:
        st.markdown(f"""
        <div class="kpi-card">
            <div class="kpi-title">Current Kp Index</div>
            <div class="kpi-value" style="color:{risk_col};">{curr_kp:.1f}</div>
            <div class="kpi-subtitle" style="color:{risk_col}; font-weight:600;">{risk_lbl}</div>
        </div>
        """, unsafe_allow_html=True)

    with col2:
        st.markdown("""
        <div class="kpi-card">
            <div class="kpi-title">Aurora Oval</div>
            <div class="kpi-value">60°–75°</div>
            <div class="kpi-subtitle">Both Hemispheres</div>
        </div>
        """, unsafe_allow_html=True)

    with col3:
        st.markdown("""
        <div class="kpi-card">
            <div class="kpi-title">Data Source</div>
            <div class="kpi-value">NOAA SWPC</div>
            <div class="kpi-subtitle">Updated every 5 minutes</div>
        </div>
        """, unsafe_allow_html=True)


def render_kp_chart(kp_rows, curr_kp, is_dark):
    if not kp_rows or not PLOTLY_OK:
        return
    _, border, tc, sc = _palette(is_dark)
    now    = datetime.now(timezone.utc)
    future = [(dt, kp) for dt, kp in kp_rows if dt >= now - timedelta(hours=1)][:28]
    if not future:
        return
    dts  = [r[0].strftime("%d %b %H:%M") for r in future]
    kps  = [r[1] for r in future]
    cols = [kp_risk_label(k)[1] for k in kps]
    fig  = go.Figure()
    fig.add_trace(go.Bar(x=dts, y=kps, marker_color=cols,
                         hovertemplate="%{x}<br>Kp: %{y}<extra></extra>"))
    fig.add_hline(y=4, line_dash="dot", line_color="#ff9800",
                  annotation_text="Moderate (4)", annotation_font_color="#ff9800",
                  annotation_position="top left")
    fig.add_hline(y=6, line_dash="dot", line_color="#f44336",
                  annotation_text="High (6)", annotation_font_color="#f44336",
                  annotation_position="top left")
    fig.update_layout(
        title       = dict(text="Kp Index Forecast (next ~24 h)",
                           font=dict(color=tc, size=13)),
        height      = 220,
        margin      = dict(l=40, r=20, t=40, b=70),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor ="rgba(0,0,0,0)",
        yaxis=dict(range=[0, 9], gridcolor=border, color=sc, title="Kp"),
        xaxis=dict(tickangle=-45, color=sc, gridcolor="rgba(0,0,0,0)"),
        showlegend=False,
    )
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


def render_globe(orbit_pts, sat_name, curr_kp, is_dark):
    if not PLOTLY_OK or not orbit_pts:
        return
    _, risk_col = kp_risk_label(curr_kp)
    fig = go.Figure()

    # aurora band fill — northern
    band_lats, band_lons = [], []
    for lon in range(-180, 181, 5):
        band_lats.append(AURORA_LAT_MIN); band_lons.append(lon)
    for lon in range(180, -181, -5):
        band_lats.append(AURORA_LAT_MAX); band_lons.append(lon)
    alpha = min(0.15 + curr_kp * 0.03, 0.5)
    for hem, name in [(1, "Aurora (N)"), (-1, "Aurora (S)")]:
        fig.add_trace(go.Scattergeo(
            lat=[hem * l for l in band_lats], lon=band_lons,
            mode="lines", fill="toself",
            fillcolor=f"rgba(0,220,100,{alpha})",
            line=dict(width=0), name=name))

    # ground track
    lats  = [p[0] for p in orbit_pts]
    lons  = [p[1] for p in orbit_pts]
    times = [p[3].strftime("%H:%M UTC") for p in orbit_pts]
    alts  = [p[2] for p in orbit_pts]
    fig.add_trace(go.Scattergeo(
        lat=lats, lon=lons, mode="lines",
        line=dict(color="#00aaff", width=2),
        name=f"{sat_name} track",
        text=[f"{t}<br>Alt: {a:.0f} km" for t, a in zip(times, alts)],
        hovertemplate="%{text}<extra></extra>"))
    # current position
    fig.add_trace(go.Scattergeo(
        lat=[lats[0]], lon=[lons[0]],
        mode="markers+text",
        marker=dict(size=12, color="#ffeb3b",
                    line=dict(color="#000", width=1)),
        text=[f"  {sat_name}"],
        textfont=dict(color="#ffeb3b", size=11),
        textposition="middle right",
        name="Now",
        hovertemplate=f"Lat: %{{lat:.2f}}°  Lon: %{{lon:.2f}}°<extra></extra>"))

    fig.update_geos(
        projection_type="orthographic",
        showland=True,    landcolor="#1a2a3a",
        showocean=True,   oceancolor="#0a1520",
        showcountries=True, countrycolor="#2a4a6a",
        showcoastlines=True, coastlinecolor="#2a4a6a",
        showframe=False,  bgcolor="#050d14",
        lataxis_showgrid=True, lonaxis_showgrid=True,
        lataxis_gridcolor="rgba(100,150,200,0.12)",
        lonaxis_gridcolor="rgba(100,150,200,0.12)",
    )
    fig.update_layout(
        height=500, margin=dict(l=0, r=0, t=0, b=0),
        paper_bgcolor="#050d14",
        legend=dict(bgcolor="rgba(10,20,35,0.85)",
                    bordercolor="#1e3a50", borderwidth=1,
                    font=dict(color="#c8dce8", size=11),
                    x=0.01, y=0.99),
    )
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


def render_crossing_cards(crossings, curr_kp, is_dark):
    now = datetime.now(timezone.utc)
    for c in crossings[:4]:
        risk, col = kp_risk_label(c["kp"])
        is_active = c["entry"] <= now <= c["exit"]
        status    = "🔴 ACTIVE NOW" if is_active else f"Entry: {c['entry'].strftime('%H:%M UTC')}"
        dur_m     = int(c["duration"])
        dur_s     = int((c["duration"] % 1) * 60)
        warning   = ""
        if c["kp"] >= 6:
            warning = f"""<div style="margin-top:10px;background:rgba(244,67,54,0.12);
                border-left:3px solid #f44336;padding:8px 12px;border-radius:6px;
                font-size:0.8rem;color:#f44336;">
                ⚠️ Kp ≥ 6 — avoid non-critical commands. Increase telemetry monitoring.</div>"""
        st.markdown(f"""
        <div style="background:var(--background-color);border:1px solid {col};border-radius:10px;
             padding:14px 16px;margin-bottom:10px;">
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <span style="font-weight:700;color:var(--text-color);font-size:0.9rem;">Aurora Crossing</span>
            <span style="background:{col};color:#fff;font-size:0.72rem;font-weight:700;
                  padding:2px 9px;border-radius:20px;">{risk}</span>
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px;">
            <div>
              <div style="font-size:0.7rem;color:var(--text-color);opacity:0.6;text-transform:uppercase;
                   letter-spacing:.05em;">Status</div>
              <div style="font-weight:600;color:{col};font-size:0.85rem;">{status}</div>
            </div>
            <div>
              <div style="font-size:0.7rem;color:var(--text-color);opacity:0.6;text-transform:uppercase;
                   letter-spacing:.05em;">Duration</div>
              <div style="font-weight:600;color:var(--text-color);font-size:0.85rem;">
                  {dur_m} min {dur_s:02d} sec</div>
            </div>
            <div>
              <div style="font-size:0.7rem;color:var(--text-color);opacity:0.6;text-transform:uppercase;
                   letter-spacing:.05em;">Kp</div>
              <div style="font-weight:700;color:{col};font-size:1.1rem;">{c['kp']:.1f}</div>
            </div>
            <div>
              <div style="font-size:0.7rem;color:var(--text-color);opacity:0.6;text-transform:uppercase;
                   letter-spacing:.05em;">Exit</div>
              <div style="font-weight:600;color:var(--text-color);font-size:0.85rem;">
                  {c['exit'].strftime('%H:%M UTC')}</div>
            </div>
          </div>
          {warning}
        </div>""", unsafe_allow_html=True)


def render_commanding_rule(curr_kp, in_aurora, is_dark):
    if in_aurora and curr_kp >= 6:
        sbg, sbdr, ico, stxt, scol = ("rgba(244,67,54,0.12)", "#f44336", "🔴",
                                       "RULE ACTIVE — Restrictions apply", "#f44336")
        action = "Avoid non-critical commands. Increase telemetry monitoring frequency."
        cond_col = "#f44336"
    elif in_aurora and curr_kp >= 4:
        sbg, sbdr, ico, stxt, scol = ("rgba(255,152,0,0.12)", "#ff9800", "🟡",
                                       "CAUTION — Satellite in auroral region", "#ff9800")
        action = "Monitor telemetry closely. Defer non-essential operations."
        cond_col = "#ff9800"
    else:
        sbg, sbdr, ico, stxt, scol = ("rgba(76,175,80,0.12)", "#4caf50", "🟢",
                                       "NOMINAL — No restrictions", "#4caf50")
        action = "Normal operations permitted. Continue routine monitoring."
        cond_col = "#4caf50"

    st.markdown(f"""
    <div style="background:var(--background-color);border:1px solid rgba(128,128,128,0.3);border-radius:10px;
         padding:16px 18px;margin-bottom:16px;">
      <div style="font-weight:700;color:var(--text-color);font-size:0.9rem;margin-bottom:12px;">
          Commanding Rule Evaluation</div>
      <div style="font-family:monospace;background:var(--secondary-background-color);border:1px solid rgba(128,128,128,0.3);
           border-radius:8px;padding:12px 14px;font-size:0.8rem;
           color:var(--text-color);margin-bottom:12px;line-height:1.8;">
        <span style="color:var(--text-color);opacity:0.6;font-weight:600;">IF</span>
        <span style="color:{cond_col};font-weight:700;">
            satellite {"inside" if in_aurora else "outside"} auroral oval
        </span>
        <span style="color:var(--text-color);opacity:0.6;"> AND </span>
        <span style="color:{cond_col};font-weight:700;">Kp = {curr_kp:.1f}</span><br>
        <span style="color:var(--text-color);opacity:0.6;font-weight:600;">THEN</span>
        <span style="color:var(--text-color);"> {action}</span>
      </div>
      <div style="background:{sbg};border-left:3px solid {sbdr};
           padding:9px 13px;border-radius:6px;">
        <span style="font-weight:700;color:{scol};">{ico} {stxt}</span>
      </div>
    </div>""", unsafe_allow_html=True)

def render_effects_table(curr_kp, is_dark):
    rows = ""
    for effect, desc in OPERATIONAL_EFFECTS:
        col, sev = effect_severity(effect, curr_kp)
        rows += f"""<tr>
          <td style="padding:8px 12px;color:var(--text-color);font-weight:600;
               font-size:0.82rem;">{effect}</td>
          <td style="padding:8px 12px;color:var(--text-color);opacity:0.7;font-size:0.81rem;">{desc}</td>
          <td style="padding:8px 12px;text-align:center;">
            <span style="background:{col};color:#fff;font-size:0.7rem;font-weight:700;
                  padding:2px 8px;border-radius:12px;">{sev}</span>
          </td></tr>"""
    st.markdown(f"""
    <div style="background:var(--background-color);border:1px solid rgba(128,128,128,0.3);border-radius:10px;
         overflow:hidden;margin-bottom:16px;">
      <div style="padding:12px 16px;font-weight:700;font-size:0.9rem;color:var(--text-color);
           border-bottom:1px solid rgba(128,128,128,0.3);">Operational Effects at Kp {curr_kp:.1f}</div>
      <table style="width:100%;border-collapse:collapse;">
        <thead>
          <tr style="background:var(--secondary-background-color);">
            <th style="padding:7px 12px;text-align:left;font-size:0.72rem;
                 color:var(--text-color);opacity:0.7;text-transform:uppercase;letter-spacing:.05em;">Risk</th>
            <th style="padding:7px 12px;text-align:left;font-size:0.72rem;
                 color:var(--text-color);opacity:0.7;text-transform:uppercase;letter-spacing:.05em;">Effect</th>
            <th style="padding:7px 12px;text-align:center;font-size:0.72rem;
                 color:var(--text-color);opacity:0.7;text-transform:uppercase;letter-spacing:.05em;">Level</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </div>""", unsafe_allow_html=True)


def _palette(is_dark):
    if is_dark:
        return "#0d1b2a", "#1e3a50", "#e6f1f5", "#7a9bb5"
    return "#ffffff", "#c5d8ec", "#0d2137", "#4a6b85"


def render_sw_events_panel():
    """
    Scrollable side panel of Space Weather events (last 7 days), grouped by
    day. Card styling (solid #1a4fa3 background, white text, risk-btn pill
    badges) matches the "Space Weather Events" tab in dashboard.py's Activity
    card, and the scrollable container mirrors dashboard.py's
    st.container(height=..., border=True) pattern.
    """
    st.markdown("""
    <style>
    .risk-btn {
        display: inline-block;
        padding: 4px 12px;
        border-radius: 14px;
        font-weight: 700;
        font-size: 0.72rem;
        text-align: center;
        color: white;
    }
    .risk-btn.risk-critical { background-color: #9c27b0; }
    .risk-btn.risk-high     { background-color: #f44336; }
    .risk-btn.risk-medium   { background-color: #ff9800; }
    .risk-btn.risk-low      { background-color: #4caf50; }
    </style>
    """, unsafe_allow_html=True)

    st.subheader("Events")
    container_events = st.container(border=True, height=600)

    with container_events:
        sw_events = fetch_sw_events_last7days()
        if not sw_events:
            st.success("No significant geomagnetic activity in the last 7 days.")
            return

        from collections import defaultdict as _dd
        by_day = _dd(list)
        for ev in sw_events:
            day_key = ev["time_tag"].strftime("%b %d")
            by_day[day_key].append(ev)

        today_key = datetime.now(timezone.utc).strftime("%b %d")
        for day_key in sorted(
            by_day.keys(),
            key=lambda d: datetime.strptime(f"{d} {datetime.now(timezone.utc).year}", "%b %d %Y"),
            reverse=True,
        ):
            day_evs = by_day[day_key]
            max_ev  = max(day_evs, key=lambda e: e["kp"])
            max_kp  = max_ev["kp"]
            is_today = day_key == today_key
            curr_ev  = next((e for e in day_evs if e.get("is_current")), None)
            curr_str = f"Current: <b>{curr_ev['kp']:.1f}</b> • " if curr_ev and is_today else ""

            lv = max_ev["level"]
            if "Severe" in lv or "G3" in lv:
                badge_class = "risk-critical"
            elif "Storm" in lv or "G1" in lv or "G2" in lv:
                badge_class = "risk-high"
            elif "Active" in lv or "Unsettled" in lv:
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


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def render():
    # ── Remove top padding (matches collision.py) ─────────────────────────────
    st.markdown("""
    <style>
        .block-container {
            padding-top: 4.8rem !important;
        }
    </style>
    """, unsafe_allow_html=True)

    # # ── Header banner (matches collision.py style) ────────────────────────────
    # st.markdown("""
    # <div style="
    #     text-align: center;
    #     font-size: 42px;
    #     font-weight: 700;
    #     padding: 15px 0;
    #     margin: 0;
    #     border-radius: 10px;
    #     background-color: #1a4fa3;
    #     color: #ffffff;
    # ">
    #     Space Weather
    # </div>
    # """, unsafe_allow_html=True)

    # #st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
    # st.space()

    # ── Option menu (matches collision.py style) ──────────────────────────────
    _sw_tabs = ["SW Dashboard", "Aurora Forecast", "Satellite Tracker", "Kp History"]
    _sw_default = _sw_tabs.index(st.session_state.get("sw_selected", "SW Dashboard")) \
                  if st.session_state.get("sw_selected") in _sw_tabs else 0

    selected_option = option_menu(
        menu_title=None,
        options=_sw_tabs,
        icons=["bi bi-bar-chart-fill", "bi bi-globe2", "bi bi-broadcast-pin", "bi bi-graph-up"],
        menu_icon="cast",
        default_index=_sw_default,
        orientation="horizontal",
    )

    if "sw_selected" not in st.session_state:
        st.session_state.sw_selected = "SW Dashboard"
    if selected_option != st.session_state.sw_selected:
        st.session_state.sw_selected = selected_option
        st.rerun()

    # set_page_config is called at module level above — not repeated here

    # ── Theme detection — read Streamlit's active theme from config ──────────
    # config.toml sets base="light"; honour it rather than assuming dark.
    # st.get_option("theme.base") returns "light" or "dark" at runtime.
    try:
        _theme_base = st.get_option("theme.base") or "light"
    except Exception:
        _theme_base = st.session_state.get("theme", "light")
    is_dark = _theme_base.lower() == "dark"
    bg, border, tc, sc = _palette(is_dark)

    # ── Theme-aware CSS — uses CSS variables, no Python detection needed ────────
    st.markdown("""
    <style>
    div[data-baseweb="select"] > div {
        background-color: var(--background-color) !important;
        color: var(--text-color) !important;
        border: 1px solid rgba(128,128,128,0.35) !important;
    }
    div[data-baseweb="select"] > div:focus-within {
        border-color: var(--primary-color) !important;
        box-shadow: 0 0 0 1px var(--primary-color) !important;
    }
    div[data-baseweb="select"] span { color: var(--text-color) !important; }
    div[data-baseweb="popover"] > div,
    div[data-baseweb="menu"] {
        background-color: var(--background-color) !important;
        border: 1px solid rgba(128,128,128,0.35) !important;
    }
    li[role="option"] {
        background-color: var(--background-color) !important;
        color: var(--text-color) !important;
    }
    li[role="option"]:hover { background-color: var(--secondary-background-color) !important; }
    li[aria-selected="true"] {
        background-color: var(--primary-color) !important;
        color: #ffffff !important;
    }
    div[data-testid="stRadio"] label p { color: var(--text-color) !important; }
    div[data-testid="stTextInput"] input {
        background-color: var(--background-color) !important;
        color: var(--text-color) !important;
        border: 1px solid rgba(128,128,128,0.35) !important;
    }
    div[data-testid="stTextInput"] input::placeholder {
        color: rgba(128,128,128,0.6) !important;
    }
    </style>
    """, unsafe_allow_html=True)

    # ── Kp data — fetched once, used across all sections ─────────────────────
    curr_kp = get_current_kp()
    kp_rows = fetch_kp_forecast()

    # ── Missing deps warning ──────────────────────────────────────────────────
    if not SGP4_OK or not PLOTLY_OK:
        missing = []
        if not SGP4_OK:   missing.append("`sgp4`")
        if not PLOTLY_OK: missing.append("`plotly`")
        st.warning(
            f"⚠️ Install {' and '.join(missing)} for orbit tracking and globe: "
            f"`pip install {' '.join(m.strip('`') for m in missing)}`"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION: SW Dashboard
    # ══════════════════════════════════════════════════════════════════════════
    if st.session_state.sw_selected == "SW Dashboard":
        st.subheader("Space Weather Overview")
        st.caption("Live Kp index · Operational risk summary · GOES satellite environment · Data from NOAA SWPC")

        render_kp_strip(curr_kp, is_dark)
        st.divider()

        chart_col, events_col = st.columns([2, 1])

        with chart_col:
            render_effects_table(curr_kp, is_dark)
            render_satellite_environment(curr_kp, is_dark)

        with events_col:
            render_sw_events_panel()

        if st.button("← Back to Home", key="sw_back_home_dash"):
            st.session_state["page"] = "home"
            st.query_params["page"]  = "home"
            st.rerun()

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION: Aurora Forecast
    # ══════════════════════════════════════════════════════════════════════════
    elif st.session_state.sw_selected == "Aurora Forecast":
        st.subheader("Aurora Forecast")
        st.caption("NOAA OVATION real-time aurora imagery · Updated every 5 minutes")

        render_kp_strip(curr_kp, is_dark)

        tab_n, tab_s = st.tabs(["Northern Hemisphere", "Southern Hemisphere"])
        with tab_n:
            st.image(
                NOAA_AURORA_NORTH + f"?cb={int(time.time() // 300)}",
                caption="NOAA OVATION Aurora Forecast — Northern Hemisphere",
                use_container_width=True,
            )
        with tab_s:
            st.image(
                NOAA_AURORA_SOUTH + f"?cb={int(time.time() // 300)}",
                caption="NOAA OVATION Aurora Forecast — Southern Hemisphere",
                use_container_width=True,
            )

        if st.button("← Back to Home", key="sw_back_home_aurora"):
            st.session_state["page"] = "home"
            st.query_params["page"]  = "home"
            st.rerun()

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION: Satellite Tracker
    # ══════════════════════════════════════════════════════════════════════════
    elif st.session_state.sw_selected == "Satellite Tracker":
        st.subheader("Satellite Tracker")
        st.caption("Real-time ground tracks from Space-Track.org (SGP4) · Aurora crossing alerts")

        col_left, col_right = st.columns([1.6, 1], gap="medium")

        # ── LEFT — orbit globe ────────────────────────────────────────────────
        with col_left:
            if PLOTLY_OK and st.session_state.get("orbit_pts"):
                st.markdown(
                    "<div style='font-weight:700;color:var(--text-color);font-size:0.9rem;"
                    "margin:14px 0 6px;'>Satellite Orbit Globe</div>",
                    unsafe_allow_html=True)
                render_globe(
                    st.session_state["orbit_pts"],
                    st.session_state.get("last_sat_label", "Satellite"),
                    curr_kp, is_dark)
            else:
                st.markdown(
                    "<div style='background:var(--background-color);border:1px dashed rgba(128,128,128,0.4);"
                    "border-radius:10px;padding:48px;text-align:center;color:var(--text-color);opacity:0.6;"
                    "font-size:0.88rem;margin-top:20px;'>"
                    "Orbit globe appears here after loading a satellite.</div>",
                    unsafe_allow_html=True)

        # ── RIGHT — selector + crossings + commanding rule ────────────────────
        with col_right:
            st.markdown(
                "<div style='font-weight:700;color:var(--text-color);font-size:0.9rem;"
                "margin-bottom:8px;'>Satellite Selection</div>",
                unsafe_allow_html=True)

            mode = st.radio("mode", ["Quick pick", "Enter NORAD ID"],
                            horizontal=True, label_visibility="collapsed",
                            key="sat_mode")

            norad_id  = None
            sat_label = ""

            if mode == "Quick pick":
                choice    = st.selectbox("sat", list(POPULAR_SATS.keys()),
                                         label_visibility="collapsed", key="sat_quick")
                norad_id, sat_label = POPULAR_SATS[choice]
            else:
                norad_input = st.text_input(
                    "NORAD Catalog ID",
                    placeholder="e.g. 25544 (ISS), 43678 (DIWATA-2)",
                    label_visibility="collapsed",
                    key="sat_norad_input",
                )
                with st.expander("📋 Common NORAD IDs"):
                    rows_html = "".join(
                        f"<tr><td style='padding:4px 10px;color:var(--text-color);font-family:monospace;"
                        f"font-size:0.8rem;'>{nid}</td>"
                        f"<td style='padding:4px 10px;color:var(--text-color);opacity:0.7;font-size:0.8rem;'>{lbl}</td></tr>"
                        for lbl, (nid, _) in POPULAR_SATS.items()
                    )
                    st.markdown(
                        "<table style='width:100%;background:var(--secondary-background-color);"
                        "border-radius:8px;overflow:hidden;'>"
                        "<tr><th style='padding:5px 10px;text-align:left;font-size:0.72rem;"
                        "color:var(--text-color);opacity:0.7;'>NORAD ID</th>"
                        "<th style='padding:5px 10px;text-align:left;font-size:0.72rem;"
                        "color:var(--text-color);opacity:0.7;'>Satellite</th></tr>"
                        f"{rows_html}</table>",
                        unsafe_allow_html=True,
                    )
                if norad_input.strip().isdigit():
                    norad_id  = int(norad_input.strip())
                    sat_label = f"NORAD {norad_id}"
                elif norad_input.strip():
                    st.warning("NORAD ID must be a number (e.g. 25544).")

            mins = st.slider("Ground track (minutes)", 30, 200, 100, 10, key="sat_mins")

            if st.button("Load Satellite", use_container_width=True, key="load_sat"):
                if norad_id is None:
                    st.warning("Select or enter a NORAD ID first.")
                elif not SGP4_OK:
                    st.error("Install sgp4 first: `pip install sgp4`")
                else:
                    with st.spinner(f"Fetching TLE for NORAD {norad_id}…"):
                        tle = fetch_tle(norad_id)
                    if not tle:
                        st.error(f"No TLE returned for NORAD ID {norad_id}.")
                    else:
                        with st.spinner("Propagating orbit…"):
                            pts = propagate_orbit(tle[0], tle[1], mins)
                        if pts:
                            st.session_state["orbit_pts"]      = pts
                            st.session_state["last_sat_label"] = sat_label
                            st.session_state["last_norad_id"]  = norad_id
                            st.success(f"✅ {sat_label} loaded — {len(pts)} track points")
                            st.rerun()
                        else:
                            st.error("Orbit propagation returned no points.")

            st.divider()

            # ── aurora crossings & commanding rule ────────────────────────────
            orbit_pts = st.session_state.get("orbit_pts", [])

            if orbit_pts:
                sat_name  = st.session_state.get("last_sat_label", "Satellite")
                crossings = aurora_crossings(orbit_pts, curr_kp)
                cur_lat   = orbit_pts[0][0]
                in_aurora = AURORA_LAT_MIN <= abs(cur_lat) <= AURORA_LAT_MAX

                st.markdown(
                    f"<div style='font-weight:700;color:var(--text-color);font-size:0.9rem;"
                    f"margin-bottom:8px;'>Aurora Crossings — {sat_name}</div>",
                    unsafe_allow_html=True)

                if crossings:
                    render_crossing_cards(crossings, curr_kp, is_dark)
                else:
                    st.markdown(
                        f"<div style='background:var(--background-color);border:1px solid rgba(128,128,128,0.3);"
                        f"border-radius:10px;padding:12px 14px;color:var(--text-color);opacity:0.7;font-size:0.83rem;'>"
                        f"✅ No aurora crossings in the next {mins} min.</div>",
                        unsafe_allow_html=True)

                render_commanding_rule(curr_kp, in_aurora, is_dark)
            else:
                st.markdown(
                    "<div style='background:var(--background-color);border:1px dashed rgba(128,128,128,0.4);"
                    "border-radius:10px;padding:24px;text-align:center;color:var(--text-color);opacity:0.6;"
                    "font-size:0.85rem;'>Select a satellite and click<br>"
                    "<b style='color:var(--text-color);'>Load Satellite</b><br>"
                    "to see aurora crossings,<br>orbit globe, and commanding rules.</div>",
                    unsafe_allow_html=True)

        if st.button("← Back to Home", key="sw_back_home_tracker"):
            st.session_state["page"] = "home"
            st.query_params["page"]  = "home"
            st.rerun()

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION: Kp History
    # ══════════════════════════════════════════════════════════════════════════
    elif st.session_state.sw_selected == "Kp History":
        st.subheader("Kp Index History & Forecast")
        st.caption("30-day observed Kp history · 24-hour forecast · NOAA SWPC · DGD & Planetary K-index feeds")

        render_kp_strip(curr_kp, is_dark)

        if not PLOTLY_OK:
            st.info("Install `plotly` to view Kp charts.")
        else:
            import plotly.graph_objects as _pgo
            _, border, tc, sc = _palette(is_dark)
            paper_bg = "rgba(0,0,0,0)"
            plot_bg  = "rgba(0,0,0,0)"
            grid_col = border

            # ── fetch history ─────────────────────────────────────────────────
            with st.spinner("Loading 30-day Kp history from NOAA SWPC…"):
                kp_history = fetch_kp_history()

            # ── 1. 30-DAY DAILY HISTORY BAR CHART ────────────────────────────
            st.markdown("#### 30-Day Observed Kp History")
            st.caption("Daily maximum Kp · Source: NOAA SWPC Daily Geomagnetic Data")

            if kp_history:
                import pandas as pd

                periods_label = ["00–03", "03–06", "06–09", "09–12",
                                 "12–15", "15–18", "18–21", "21–24"]

                # Build a flat per-3h record for the detailed view
                flat_rows = []
                for rec in kp_history:
                    base = rec["date"]
                    for i, kv in enumerate(rec["kp_values"]):
                        if kv < 0:
                            continue
                        ts = base + timedelta(hours=i * 3)
                        flat_rows.append({"time": ts, "Kp": kv, "period": periods_label[i]})

                df_hist = pd.DataFrame(flat_rows)
                df_hist["color"] = df_hist["Kp"].apply(
                    lambda k: "#4caf50" if k < 4 else "#ff9800" if k < 6
                    else "#f44336" if k < 8 else "#9c27b0"
                )

                fig_hist = _pgo.Figure()
                fig_hist.add_trace(_pgo.Bar(
                    x=df_hist["time"],
                    y=df_hist["Kp"],
                    marker_color=df_hist["color"],
                    hovertemplate="<b>%{x|%b %d %H:%M} UTC</b><br>Kp: %{y:.2f}<extra></extra>",
                    name="Observed Kp",
                ))

                # G-level threshold lines
                for thresh, label, col_t in [
                    (4, "G1 (Kp≥4)", "#ff9800"),
                    (5, "G2 (Kp≥5)", "#ff5722"),
                    (6, "G3 (Kp≥6)", "#f44336"),
                    (7, "G4 (Kp≥7)", "#9c27b0"),
                ]:
                    fig_hist.add_hline(
                        y=thresh, line_dash="dot", line_color=col_t, line_width=1,
                        annotation_text=label, annotation_font_color=col_t,
                        annotation_position="top right", annotation_font_size=10,
                    )

                # Mark current Kp
                now_utc = datetime.now(timezone.utc)
                fig_hist.add_vline(
                    x=now_utc.timestamp() * 1000,
                    line_dash="dash", line_color="#00bcd4", line_width=1.5,
                    annotation_text="Now", annotation_font_color="#00bcd4",
                    annotation_position="top right",
                )

                fig_hist.update_layout(
                    height=320,
                    margin=dict(l=50, r=20, t=30, b=60),
                    paper_bgcolor=paper_bg, plot_bgcolor=plot_bg,
                    xaxis=dict(
                        color=sc, gridcolor=grid_col, showgrid=True,
                        title="Date (UTC)", tickformat="%b %d",
                        tickangle=-35, tickfont=dict(size=10),
                    ),
                    yaxis=dict(
                        color=sc, gridcolor=grid_col, showgrid=True,
                        range=[0, 9.5], title="Kp Index",
                        tickvals=[0,1,2,3,4,5,6,7,8,9],
                    ),
                    showlegend=False,
                    bargap=0.05,
                )
                st.plotly_chart(fig_hist, use_container_width=True,
                                config={"displayModeBar": False})

                # ── summary stats ─────────────────────────────────────────────
                max_30d = max(r["kp_max"] for r in kp_history)
                mean_30d = sum(r["kp_mean"] for r in kp_history) / len(kp_history)
                storm_days = sum(1 for r in kp_history if r["kp_max"] >= 5)
                max_level, max_col = kp_risk_label(max_30d)

                st.markdown(f"""
                <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px;">
                  <div style="background:var(--background-color);border:1px solid rgba(128,128,128,0.3);border-radius:10px;
                       padding:14px 20px;flex:1;min-width:120px;text-align:center;">
                    <div style="font-size:0.72rem;color:var(--text-color);opacity:0.6;text-transform:uppercase;
                         letter-spacing:.06em;margin-bottom:6px;">30-Day Max Kp</div>
                    <div style="font-size:2rem;font-weight:700;color:{max_col};">{max_30d:.1f}</div>
                    <div style="font-size:0.78rem;color:{max_col};font-weight:600;">{max_level}</div>
                  </div>
                  <div style="background:var(--background-color);border:1px solid rgba(128,128,128,0.3);border-radius:10px;
                       padding:14px 20px;flex:1;min-width:120px;text-align:center;">
                    <div style="font-size:0.72rem;color:var(--text-color);opacity:0.6;text-transform:uppercase;
                         letter-spacing:.06em;margin-bottom:6px;">30-Day Mean Kp</div>
                    <div style="font-size:2rem;font-weight:700;color:var(--text-color);">{mean_30d:.2f}</div>
                  </div>
                  <div style="background:var(--background-color);border:1px solid rgba(128,128,128,0.3);border-radius:10px;
                       padding:14px 20px;flex:1;min-width:120px;text-align:center;">
                    <div style="font-size:0.72rem;color:var(--text-color);opacity:0.6;text-transform:uppercase;
                         letter-spacing:.06em;margin-bottom:6px;">Storm Days (Kp≥5)</div>
                    <div style="font-size:2rem;font-weight:700;
                         color:{"#f44336" if storm_days > 0 else "#4caf50"};">{storm_days}</div>
                  </div>
                  <div style="background:var(--background-color);border:1px solid rgba(128,128,128,0.3);border-radius:10px;
                       padding:14px 20px;flex:1;min-width:120px;text-align:center;">
                    <div style="font-size:0.72rem;color:var(--text-color);opacity:0.6;text-transform:uppercase;
                         letter-spacing:.06em;margin-bottom:6px;">Current Kp</div>
                    <div style="font-size:2rem;font-weight:700;
                         color:{kp_risk_label(curr_kp)[1]};">{curr_kp:.1f}</div>
                    <div style="font-size:0.78rem;color:{kp_risk_label(curr_kp)[1]};
                         font-weight:600;">{kp_risk_label(curr_kp)[0]}</div>
                  </div>
                </div>
                """, unsafe_allow_html=True)

            else:
                st.info("30-day Kp history unavailable from NOAA SWPC.")

            st.divider()

            # ── 2. 24-HOUR FORECAST CHART ─────────────────────────────────────
            st.markdown("#### 24-Hour Kp Forecast")
            st.caption("Source: NOAA SWPC Planetary K-index forecast feed")
            render_kp_chart(kp_rows, curr_kp, is_dark)

        if st.button("← Back to Home", key="sw_back_home_kp"):
            st.session_state["page"] = "home"
            st.query_params["page"]  = "home"
            st.rerun()

    st.caption("Compiled by the Space Mission Control and Operations Division (SMCOD) of the Philippine Space Agency (PhilSA)")


render()