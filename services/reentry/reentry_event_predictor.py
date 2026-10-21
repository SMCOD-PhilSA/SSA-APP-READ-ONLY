from __future__ import annotations

import os
import csv
import json
import time
import math
import random
import re
import hashlib
import datetime as dt
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict, Any

import numpy as np
import requests
from dotenv import load_dotenv

# ══════════════════════════════════════════════════════════════════════════
# DUMMY DISPLAY MODE (optional, for demos/screenshots)
#
# When enabled, the object name and NORAD ID shown in the UI (fetch success
# message, current-state header, report title) are swapped for deterministic
# dummy values. All underlying Space-Track fetch calls, TLE propagation, and
# report/KML file logic still use the REAL NORAD ID -- only what gets
# *displayed* changes.
#
# To disable completely: leave/set DEMO_MODE = False. Nothing else needs to
# change -- _demo_display() becomes a no-op passthrough and every call site
# behaves exactly as before.
# ══════════════════════════════════════════════════════════════════════════
DEMO_MODE = False
# DEMO_MODE = True   # uncomment this line to enable dummy names/NORAD IDs

_DEMO_NAME_POOL = [
    "OBJECT A", "OBJECT B", "OBJECT C", "OBJECT D", "OBJECT E",
    "OBJECT F", "OBJECT G", "OBJECT H", "OBJECT I", "OBJECT J",
]

def _demo_display(real_norad, real_name=None):
    """
    Returns (display_norad, display_name) for UI labels only.
    The same real_norad always maps to the same dummy values within a run.
    Does NOT affect any fetch/propagation/report logic -- keep passing the
    REAL norad_id into every function call as usual; only swap in the
    returned values where text is rendered on screen.
    """
    if not DEMO_MODE:
        return real_norad, (real_name if real_name is not None else str(real_norad))
    seed = int(hashlib.sha1(str(real_norad).encode()).hexdigest(), 16)
    dummy_name  = _DEMO_NAME_POOL[seed % len(_DEMO_NAME_POOL)]
    dummy_norad = 10000 + (seed % 90000)
    return dummy_norad, dummy_name

import matplotlib
matplotlib.use("Agg")   # non-interactive backend — no Tkinter, safe for Streamlit threads
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

import cartopy.crs as ccrs
import cartopy.feature as cfeature

try:
    import geopandas as gpd
except Exception:
    gpd = None

try:
    from shapely.geometry import Point as ShapelyPoint
except Exception:
    ShapelyPoint = None

from skyfield.api import EarthSatellite, load as sf_load

try:
    import simplekml
except Exception:
    simplekml = None

try:
    from pymsis import msis as pymsis_msis
except Exception:
    pymsis_msis = None


# -----------------------------
# Load .env early
# -----------------------------
load_dotenv()

LOGIN_URL = "https://www.space-track.org/ajaxauth/login"
DEFAULT_TIP_LIMIT = int(os.getenv("TIP_LIMIT", "200"))

PH_TZ = dt.timezone(dt.timedelta(hours=8))
EARTH_RADIUS_KM = 6371.0088



# -----------------------------
# Models
# -----------------------------
@dataclass
class TipSolution:
    msg_epoch: str
    decay_epoch: str
    rev: Optional[int]
    direction: Optional[str]
    lat: Optional[float]
    lon: Optional[float]
    incl: Optional[float]
    high_interest: Optional[str]
    raw: dict


@dataclass
class TLEPoint:
    epoch_utc: dt.datetime
    name: str
    l1: str
    l2: str
    bstar: float


# -----------------------------
# Helpers: time parsing
# -----------------------------
def parse_any_datetime_utc(s: str) -> dt.datetime:
    if not s:
        raise ValueError("Empty datetime string")
    txt = s.strip()

    if txt.endswith("Z"):
        txt2 = txt[:-1] + "+00:00"
    else:
        txt2 = txt

    try:
        d = dt.datetime.fromisoformat(txt2)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.astimezone(dt.timezone.utc)
    except Exception:
        pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            d = dt.datetime.strptime(txt, fmt).replace(tzinfo=dt.timezone.utc)
            return d
        except Exception:
            continue

    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            d = dt.datetime.strptime(txt, fmt).replace(tzinfo=dt.timezone.utc)
            return d
        except Exception:
            continue

    raise ValueError(f"Unrecognized datetime format: {s!r}")


def dt_to_iso_z(t: dt.datetime) -> str:
    t = t.astimezone(dt.timezone.utc)
    return t.strftime("%Y-%m-%d %H:%M:%SZ")


def dt_to_iso_ph(t: dt.datetime) -> str:
    t = t.astimezone(PH_TZ)
    return t.strftime("%Y-%m-%d %H:%M:%S (PH)")


def fmt_timedelta(td: dt.timedelta) -> str:
    total = int(abs(td.total_seconds()))
    sign = "-" if td.total_seconds() < 0 else ""
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h > 0:
        return f"{sign}{h}h {m}m {s}s"
    if m > 0:
        return f"{sign}{m}m {s}s"
    return f"{sign}{s}s"


# -----------------------------------------------------------------------------
# TLE / OMM fallback for objects without TIP messages
# -----------------------------------------------------------------------------

def _orbital_epoch(sat):
    return sat.epoch.utc_datetime().replace(tzinfo=dt.timezone.utc)


def _satellite_from_gp_row(row):
    ts = sf_load.timescale()
    name = str(row.get("OBJECT_NAME") or row.get("SATNAME") or "NORAD object")
    if row.get("TLE_LINE1") and row.get("TLE_LINE2"):
        return EarthSatellite(row["TLE_LINE1"], row["TLE_LINE2"], name, ts)
    defaults = {
        "OBJECT_NAME": name, "OBJECT_ID": row.get("OBJECT_ID") or "UNKNOWN",
        "CENTER_NAME": "EARTH", "REF_FRAME": "TEME", "TIME_SYSTEM": "UTC",
        "MEAN_ELEMENT_THEORY": "SGP4", "EPHEMERIS_TYPE": "0",
        "CLASSIFICATION_TYPE": "U", "ELEMENT_SET_NO": "0", "REV_AT_EPOCH": "0",
        "MEAN_MOTION_DOT": "0", "MEAN_MOTION_DDOT": "0",
    }
    defaults.update({k:v for k,v in row.items() if v is not None and v != ""})
    return EarthSatellite.from_omm(ts, defaults)


def _fetch_orbital_candidates(session, norad_id, days=7):
    base = "https://www.space-track.org/basicspacedata/query"
    rows = []
    messages = []
    for cls, url in (
        ("gp", f"{base}/class/gp/NORAD_CAT_ID/{norad_id}/orderby/EPOCH%20desc/limit/20/format/json"),
        ("gp_history", f"{base}/class/gp_history/NORAD_CAT_ID/{norad_id}/orderby/EPOCH%20desc/limit/120/format/json"),
    ):
        try:
            payload = retry_get(session, url, tries=3, timeout=50).json()
            if isinstance(payload, list):
                for item in payload:
                    item = dict(item)
                    item["_fetch_class"] = cls
                    rows.append(item)
        except Exception as exc:
            messages.append(f"{cls}: {exc}")
    return rows, messages


def _parse_omm_xml(xml_bytes):
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml_bytes)
    fields = {}
    for elem in root.iter():
        name = elem.tag.split("}")[-1]
        if name == "USER_DEFINED":
            key = elem.attrib.get("parameter")
        else:
            key = name
        if key and elem.text and elem.text.strip():
            fields[key] = elem.text.strip()
    for required in ("NORAD_CAT_ID", "EPOCH", "MEAN_MOTION", "ECCENTRICITY", "INCLINATION", "RA_OF_ASC_NODE", "ARG_OF_PERICENTER", "MEAN_ANOMALY", "BSTAR"):
        if not fields.get(required):
            raise ValueError(f"OMM XML missing {required}")
    fields["_fetch_class"] = "uploaded_omm"
    return fields


def _select_orbital_candidate(rows, target_epoch=None):
    options = []
    for row in rows:
        try:
            epoch = parse_any_datetime_utc(str(row["EPOCH"]))
            sat = _satellite_from_gp_row(row)
            options.append((epoch, sat, row))
        except Exception:
            continue
    if not options:
        raise RuntimeError("No SGP4-compatible GP, GP_HISTORY, or uploaded OMM element set")
    if target_epoch is None:
        return max(options, key=lambda x:x[0])
    before = [x for x in options if x[0] <= target_epoch]
    return (max(before, key=lambda x:x[0]) if before else min(options,key=lambda x: abs((x[0]-target_epoch).total_seconds())))


def _height_km(sat, times):
    ts = sf_load.timescale()
    return np.asarray(sat.at(ts.from_datetimes(times)).subpoint().elevation.km, dtype=float)


def _assumed_decay_window(sat, half_minutes, scan_hours=6, threshold_km=120):
    """Heuristic scenario time; NOT a measured decay epoch or probabilistic forecast."""
    origin = _orbital_epoch(sat)
    scan_hours = max(0.5, min(12., float(scan_hours)))
    times = [origin + dt.timedelta(minutes=2*i) for i in range(int(scan_hours*30)+1)]
    altitudes = _height_km(sat, times)
    good = np.where(np.isfinite(altitudes) & (altitudes <= threshold_km) & (altitudes >= 0))[0]
    if len(good):
        central = times[int(good[0])]
        basis = "first modeled subpoint altitude at or below 120 km"
    else:
        idx = int(np.nanargmin(altitudes)) if np.any(np.isfinite(altitudes)) else 0
        central = times[idx]
        basis = "lowest modeled altitude in scan (no 120 km crossing)"
    half = dt.timedelta(minutes=float(half_minutes))
    return central-half, central+half, central, basis


def _altitude_band_plot(sat, start, end, fir_segments=None, step_seconds=45):
    """Altitude-coded SGP4 subpoint track (not an impact footprint)."""
    import matplotlib.pyplot as plt
    import matplotlib.colors as mcolors
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    from matplotlib.lines import Line2D
    import matplotlib.collections as mc
    step_seconds = max(15, int(step_seconds))
    n = max(2, int((end-start).total_seconds()/step_seconds)+1)
    n = min(2500, n)
    times = [start+dt.timedelta(seconds=(end-start).total_seconds()*i/(n-1)) for i in range(n)]
    sub = sat.at(sf_load.timescale().from_datetimes(times)).subpoint()
    lats = np.asarray(sub.latitude.degrees, dtype=float)
    lons = np.asarray(sub.longitude.degrees, dtype=float)
    heights = np.asarray(sub.elevation.km, dtype=float)
    palette = [(200., "#e8e8e8"), (160., "#45cf52"), (140., "#ffda48"), (120., "#fa493e"), (-1e9, "#161616")]
    def color_for(alt):
        for lower, col in palette:
            if alt > lower:
                return col
        return palette[-1][1]
    fig=plt.figure(figsize=(13,6),dpi=120,facecolor="#0c1420")
    ax=fig.add_subplot(111,projection=ccrs.PlateCarree())
    ax.set_global(); ax.set_facecolor("#092036")
    ax.add_feature(cfeature.LAND, facecolor="#737476", zorder=0)
    ax.add_feature(cfeature.OCEAN, facecolor="#082542", zorder=0)
    ax.add_feature(cfeature.BORDERS, edgecolor="#3b4045",linewidth=.35,zorder=1)
    ax.coastlines(color="#37424b",linewidth=.45,zorder=1)
    for i in range(n-1):
        if not np.isfinite(heights[i]) or not np.isfinite(heights[i+1]):
            continue
        if abs(lons[i+1]-lons[i]) > 180:
            continue
        ax.plot([lons[i],lons[i+1]],[lats[i],lats[i+1]],color=color_for((heights[i]+heights[i+1])/2),lw=2.0,transform=ccrs.PlateCarree(),zorder=3)
    if fir_segments:
        for slats,slons in fir_segments:
            ax.plot(slons,slats,lw=1.2,color="#00ecff",alpha=.85,transform=ccrs.PlateCarree(),zorder=4)
    labels=[("> 200 km","#e8e8e8"),("160–200 km","#45cf52"),("140–160 km","#ffda48"),("120–140 km","#fa493e"),("≤ 120 km","#161616"),("Manila FIR","#00ecff")]
    ax.legend([Line2D([0],[0],color=color,lw=3) for _,color in labels],[lab for lab,_ in labels],loc="lower left",fontsize=8,facecolor="#172331",labelcolor="white",framealpha=.93)
    ax.gridlines(draw_labels=False,alpha=.18,color="white")
    fig.suptitle(f"{sat.name} — Altitude-coded SGP4 ground track",color="white",fontsize=12)
    ax.set_title(f"{dt_to_iso_z(start)} → {dt_to_iso_z(end)} | model is not a re-entry footprint",color="#e1e1e1",fontsize=9)
    return fig, {"minimum_altitude_km": float(np.nanmin(heights)), "maximum_altitude_km":float(np.nanmax(heights))}




def _plot_envelope_altitude_track(ax, sat, t_center, before_min, after_min,
                                  step_seconds, linewidth=1.8, alpha=0.9,
                                  linestyle="-", zorder=3):
    """Draw the ORIGINAL envelope-map tracks with segment colors from SGP4 altitude.

    Each segment uses its midpoint altitude. Dateline jumps and invalid points
    are skipped to avoid long erroneous map-spanning connections.
    """
    from matplotlib.collections import LineCollection
    import cartopy.crs as ccrs

    lats, lons, times = groundtrack_corridor(
        sat, t_center, int(before_min), int(after_min), max(10, int(step_seconds))
    )
    sub = sat.at(sf_load.timescale().from_datetimes(times)).subpoint()
    alts = np.asarray(sub.elevation.km, dtype=float)
    coords = np.column_stack((np.asarray(lons, dtype=float), np.asarray(lats, dtype=float)))
    palette = ((200.0, "#e8e8e8"), (160.0, "#45cf52"),
               (140.0, "#ffda48"), (120.0, "#fa493e"),
               (-float("inf"), "#161616"))

    def band_color(alt):
        return next(color for threshold, color in palette if alt > threshold)

    groups = {}
    for i in range(len(coords)-1):
        if (not np.all(np.isfinite(coords[i:i+2]))
                or not np.all(np.isfinite(alts[i:i+2]))
                or abs(coords[i+1, 0]-coords[i, 0]) > 180):
            continue
        color = band_color(float((alts[i]+alts[i+1])/2.0))
        groups.setdefault(color, []).append(coords[i:i+2])
    for color, segments in groups.items():
        coll = LineCollection(segments, colors=color,
                              linewidths=linewidth, alpha=alpha,
                              linestyles=linestyle, zorder=zorder,
                              transform=ccrs.PlateCarree())
        ax.add_collection(coll)
    return len(groups)

def _fallback_prediction(sat, wmin, wmax, n_mc, norad, mode, basis):
    """P10/P50/P90 quantiles conditional on explicitly assumed uniform time window."""
    generator = np.random.default_rng(101070 + int(norad))
    samples = generator.uniform(wmin.timestamp(),wmax.timestamp(),size=max(200,int(n_mc)))
    times = {q:dt.datetime.fromtimestamp(float(np.percentile(samples,p)),tz=dt.timezone.utc) for q,p in (("p10",10),("p50",50),("p90",90))}
    impacts = {}
    for q,t in times.items():
        la,lo = subpoint_at_time(sat,t)
        alt = float(_height_km(sat,[t])[0])
        impacts[q] = {"lat":la,"lon":lo,"meta":{"altitude_km":alt,"location_kind":"SGP4 subpoint, not predicted debris impact"}}
    p10,p50,p90 = (times[q] for q in ("p10","p50","p90"))
    return {
        "created_utc":dt_to_iso_z(dt.datetime.now(dt.timezone.utc)),
        "norad_id":int(norad),
        "analysis_mode":"OMM_TLE_ASSUMPTION_BASED_SCENARIO",
        "prediction_disclaimer":"P10/P50/P90 are percentiles of an ASSUMED uniform time window centered on an SGP4 altitude event; they are not calibrated decay probabilities. Locations are orbital subpoints, NOT predicted impacts.",
        "scenario_basis":basis,
        "tip_window":{"start_utc":dt_to_iso_z(wmin),"end_utc":dt_to_iso_z(wmax),"width_sec":int((wmax-wmin).total_seconds()),"mode":mode},
        "noaa_kp_mean_1m_near_mid":None,
        "monte_carlo":{"n":int(max(200,n_mc)),**{f"{q}_utc":dt_to_iso_z(t) for q,t in times.items()}},
        "impact_proxy":impacts,
        "uncertainty":{"p10_utc":dt_to_iso_z(p10),"p50_utc":dt_to_iso_z(p50),"p90_utc":dt_to_iso_z(p90),"interval_80pct_min":round((p90-p10).total_seconds()/60,1),"half_width_min":round((p90-p10).total_seconds()/120,1),"tip_window_total_min":round((wmax-wmin).total_seconds()/60,1)}
    }


# -----------------------------
# TIP uncertainty parsing (best-effort)
# -----------------------------
def parse_uncertainty_seconds(val: Any) -> Optional[float]:
    """
    Tries to parse uncertainty fields like:
    - "0.8" (assume hours if small) or numeric minutes/seconds (unknown)
    - "~±0h 48m", "±0h 12m", "0h 48m", "48m", "2880s"
    Returns seconds or None.
    """
    if val is None:
        return None

    # direct numeric
    if isinstance(val, (int, float)):
        x = float(val)
        # heuristic: if <= 10 treat as hours, else if <= 600 treat as minutes, else seconds
        if x <= 10:
            return x * 3600.0
        if x <= 600:
            return x * 60.0
        return x

    s = str(val).strip().lower()
    if not s:
        return None

    # remove weird chars
    s = s.replace("~", "").replace("≈", "").replace("about", "").replace("+/-", "±").replace("±", "")
    s = s.strip()

    total = 0.0
    found = False

    m = re.search(r"(\d+(?:\.\d+)?)\s*h", s)
    if m:
        total += float(m.group(1)) * 3600.0
        found = True

    m = re.search(r"(\d+(?:\.\d+)?)\s*(m|min|mins|minute|minutes)\b", s)
    if m:
        total += float(m.group(1)) * 60.0
        found = True

    m = re.search(r"(\d+(?:\.\d+)?)\s*(s|sec|secs|second|seconds)\b", s)
    if m:
        total += float(m.group(1))
        found = True

    if found and total > 0:
        return total

    m = re.search(r"(\d+(?:\.\d+)?)", s)
    if m:
        x = float(m.group(1))
        if x <= 10:
            return x * 3600.0
        if x <= 600:
            return x * 60.0
        return x

    return None


def tip_batch_uncertainty_seconds(latest_batch: List[TipSolution]) -> Optional[float]:
    candidate_keys = [
        "EPOCH_UNCERTAINTY",
        "EPOCH_UNC",
        "DECAY_UNCERTAINTY",
        "DECAY_UNC",
        "DECAY_EPOCH_UNCERTAINTY",
        "DECAY_EPOCH_UNC",
        "WINDOW",
        "WINDOW_WIDTH",
        "WINDOW_MINUTES",
    ]
    for s in latest_batch:
        raw = s.raw or {}
        for k in candidate_keys:
            if k in raw and raw[k] not in (None, "", "N/A"):
                sec = parse_uncertainty_seconds(raw[k])
                if sec and sec > 0:
                    return sec
    return None


# -----------------------------
# HTTP helpers
# -----------------------------
def retry_get(session: requests.Session, url: str, tries: int = 6, timeout: int = 30) -> requests.Response:
    last_exc = None
    for i in range(tries):
        try:
            r = session.get(url, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep((2 ** i) + random.random())
                continue
            r.raise_for_status()
            return r
        except Exception as e:
            last_exc = e
            time.sleep((2 ** i) + random.random())
    raise RuntimeError(f"GET failed after retries: {url}") from last_exc


def spacetrack_login(username: str, password: str) -> requests.Session:
    if not username or not password:
        raise RuntimeError("Missing SPACE_TRACK_USERNAME / SPACE_TRACK_PASSWORD (or GUI input).")
    s = requests.Session()
    r = s.post(LOGIN_URL, data={"identity": username, "password": password}, timeout=30)
    r.raise_for_status()
    return s


# -----------------------------
# TIP + TLE
# -----------------------------
def parse_tip_solutions(tip_json: list) -> List[TipSolution]:
    sols: List[TipSolution] = []
    for row in tip_json:
        sols.append(
            TipSolution(
                msg_epoch=(row.get("MSG_EPOCH") or row.get("MSG_EPOCH ", "") or "").strip(),
                decay_epoch=(row.get("DECAY_EPOCH") or "").strip(),
                rev=int(row["REV"]) if str(row.get("REV", "")).isdigit() else None,
                direction=row.get("DIRECTION"),
                lat=float(row["LAT"]) if row.get("LAT") not in (None, "") else None,
                lon=float(row["LON"]) if row.get("LON") not in (None, "") else None,
                incl=float(row["INCL"]) if row.get("INCL") not in (None, "") else None,
                high_interest=row.get("HIGH_INTEREST"),
                raw=row,
            )
        )

    def key(sol: TipSolution):
        try:
            return parse_any_datetime_utc(sol.msg_epoch).timestamp()
        except Exception:
            return 0.0

    sols.sort(key=key, reverse=True)
    return sols


def select_latest_tip_batch(solutions: List[TipSolution]) -> List[TipSolution]:
    if not solutions:
        return []
    newest_msg = solutions[0].msg_epoch
    if not newest_msg:
        return solutions[:1]
    return [s for s in solutions if s.msg_epoch == newest_msg]


def fetch_tip(session: requests.Session, norad_id: int, tip_url_override: str = "", limit: int = DEFAULT_TIP_LIMIT) -> list:
    if tip_url_override.strip():
        url = tip_url_override.strip()
    else:
        url = (
            f"https://www.space-track.org/basicspacedata/query/class/tip/"
            f"NORAD_CAT_ID/{norad_id}/orderby/MSG_EPOCH%20desc/limit/{limit}/format/json"
        )
    r = retry_get(session, url)
    txt = r.text.strip()
    return r.json() if txt.startswith("[") else json.loads(txt)


def fetch_latest_tle(session: requests.Session, norad_id: int) -> Tuple[str, str, str]:
    tle_url = (
        f"https://www.space-track.org/basicspacedata/query/class/gp/"
        f"NORAD_CAT_ID/{norad_id}/orderby/EPOCH%20desc/limit/1/format/tle"
    )
    r = retry_get(session, tle_url)
    lines = [ln.strip() for ln in r.text.splitlines() if ln.strip()]
    if len(lines) < 2:
        raise RuntimeError("TLE fetch returned insufficient lines.")
    if lines[0].startswith("1 ") and lines[1].startswith("2 "):
        name = f"NORAD {norad_id}"
        l1, l2 = lines[0], lines[1]
    else:
        if len(lines) < 3:
            raise RuntimeError("TLE fetch returned insufficient lines (expected name+2 lines).")
        name = lines[0]
        l1, l2 = lines[1], lines[2]
    return name, l1, l2


def parse_bstar_from_tle_line1(l1: str) -> float:
    try:
        s = l1[53:61].strip()
        if len(s) < 7:
            return float("nan")
        mant = s[:-2]
        exp = s[-2:]

        sign = 1.0
        if mant[0] == "-":
            sign = -1.0
            mant = mant[1:]
        elif mant[0] == "+":
            mant = mant[1:]

        mantissa = sign * float(f"0.{mant}")
        exponent = int(exp)
        return mantissa * (10 ** exponent)
    except Exception:
        return float("nan")


def fetch_tle_history(session: requests.Session, norad_id: int, limit: int = 25) -> List["TLEPoint"]:
    url = (
        f"https://www.space-track.org/basicspacedata/query/class/gp/"
        f"NORAD_CAT_ID/{norad_id}/orderby/EPOCH%20desc/limit/{int(limit)}/format/json"
    )
    r = retry_get(session, url)
    data = r.json()
    if not isinstance(data, list) or not data:
        return []

    out: List[TLEPoint] = []
    for row in data:
        name = row.get("OBJECT_NAME") or f"NORAD {norad_id}"
        l1 = row.get("TLE_LINE1")
        l2 = row.get("TLE_LINE2")
        epoch = row.get("EPOCH")
        if not (l1 and l2 and epoch):
            continue
        epoch_dt = parse_any_datetime_utc(epoch)
        bstar = parse_bstar_from_tle_line1(l1)

        out.append(TLEPoint(epoch_utc=epoch_dt, name=name, l1=l1.strip(), l2=l2.strip(), bstar=bstar))

    out.sort(key=lambda x: x.epoch_utc)
    return out


def robust_bstar_stats(tles: List[TLEPoint]) -> Dict[str, float]:
    vals = np.array([x.bstar for x in tles if np.isfinite(x.bstar)], dtype=float)
    if len(vals) < 5:
        return {"bstar_med": float("nan"), "bstar_mad": float("nan"), "bstar_trend_per_day": float("nan")}

    med = float(np.median(vals))
    mad = float(np.median(np.abs(vals - med))) + 1e-30

    n = min(15, len(tles))
    xs = np.array([(tles[-n+i].epoch_utc - tles[-n].epoch_utc).total_seconds() / 86400 for i in range(n)], dtype=float)
    ys = np.array([tles[-n+i].bstar for i in range(n)], dtype=float)
    try:
        m = float(np.polyfit(xs, ys, 1)[0])
    except Exception:
        m = float("nan")

    return {"bstar_med": med, "bstar_mad": mad, "bstar_trend_per_day": m}


def compute_tip_window_from_latest_batch(
    solutions_latest_batch: List[TipSolution],
    fallback_uncert_minutes: float
) -> Tuple[Optional[dt.datetime], Optional[dt.datetime], List[dt.datetime], str]:
    decays: List[dt.datetime] = []
    for s in solutions_latest_batch:
        if s.decay_epoch:
            try:
                decays.append(parse_any_datetime_utc(s.decay_epoch))
            except Exception:
                pass

    if not decays:
        return None, None, [], "none"

    wmin = min(decays)
    wmax = max(decays)

    if (wmax - wmin).total_seconds() > 0:
        return wmin, wmax, decays, "tip_spread"

    tip_unc_sec = tip_batch_uncertainty_seconds(solutions_latest_batch)
    if tip_unc_sec and tip_unc_sec > 0:
        half = dt.timedelta(seconds=float(tip_unc_sec))
        return wmin - half, wmax + half, decays, "tip_uncertainty"

    half = dt.timedelta(minutes=float(fallback_uncert_minutes))
    return wmin - half, wmax + half, decays, "fallback_uncertainty"


# -----------------------------
# Ground track / subpoint
# -----------------------------
def groundtrack_corridor(
    sat: EarthSatellite,
    t_center: dt.datetime,
    minutes_before: int,
    minutes_after: int,
    step_seconds: int
) -> Tuple[List[float], List[float], List[dt.datetime]]:
    ts = sf_load.timescale()
    start = t_center - dt.timedelta(minutes=minutes_before)
    end = t_center + dt.timedelta(minutes=minutes_after)

    times_dt: List[dt.datetime] = []
    cur = start
    while cur <= end:
        times_dt.append(cur)
        cur += dt.timedelta(seconds=step_seconds)

    t_sf = ts.from_datetimes(times_dt)
    geoc = sat.at(t_sf)
    sub = geoc.subpoint()

    lats = list(sub.latitude.degrees)
    lons_raw = list(sub.longitude.degrees)
    lons = [((x + 180) % 360) - 180 for x in lons_raw]
    return lats, lons, times_dt


def subpoint_at_time(sat: EarthSatellite, t_utc: dt.datetime) -> Tuple[float, float]:
    ts = sf_load.timescale()
    t_sf = ts.from_datetime(t_utc)
    sub = sat.at(t_sf).subpoint()
    lat = float(sub.latitude.degrees)
    lon = float(sub.longitude.degrees)
    lon = ((lon + 180) % 360) - 180
    return lat, lon


def split_dateline_segments(lats: List[float], lons: List[float], jump_deg: float = 180.0):
    if not lats or not lons or len(lats) != len(lons):
        return []
    segs = []
    cur_lat = [lats[0]]
    cur_lon = [lons[0]]
    for i in range(1, len(lats)):
        if abs(lons[i] - lons[i-1]) > jump_deg:
            segs.append((cur_lat, cur_lon))
            cur_lat = [lats[i]]
            cur_lon = [lons[i]]
        else:
            cur_lat.append(lats[i])
            cur_lon.append(lons[i])
    segs.append((cur_lat, cur_lon))
    return segs


# -----------------------------
# NOAA Kp fetch (1-minute)
# -----------------------------
def fetch_noaa_kp_1m_mean_near(t_utc: dt.datetime, hours_window: int = 12) -> Optional[float]:
    url = "https://services.swpc.noaa.gov/json/planetary_k_index_1m.json"
    try:
        r = requests.get(url, timeout=25)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            return None

        t0 = t_utc - dt.timedelta(hours=hours_window)
        t1 = t_utc + dt.timedelta(hours=hours_window)

        vals = []
        for row in data:
            tt = row.get("time_tag")
            kp = row.get("kp")
            if tt is None or kp is None:
                continue
            try:
                dtt = parse_any_datetime_utc(tt)
            except Exception:
                continue
            if t0 <= dtt <= t1:
                try:
                    vals.append(float(kp))
                except Exception:
                    pass

        if not vals:
            return None
        return float(np.mean(vals))
    except Exception:
        return None


def kp_to_ap_proxy(kp: float) -> float:
    kp = float(np.clip(kp, 0.0, 9.0))
    if kp <= 5:
        return 3 + 6 * kp
    return 33 + 20 * (kp - 5)


# -----------------------------
# pymsis density helper
# -----------------------------
def pymsis_density_kg_m3(
    time_utc: dt.datetime,
    alt_km: float,
    lat_deg: float,
    lon_deg: float,
    f107: float,
    f107a: float,
    ap_scalar: float,
) -> float:
    if pymsis_msis is None:
        raise RuntimeError("pymsis not installed. pip install pymsis")

    t64 = np.array([np.datetime64(time_utc.astimezone(dt.timezone.utc).replace(tzinfo=None))])

    lons = np.array([float(lon_deg)], dtype=float)
    lats = np.array([float(lat_deg)], dtype=float)
    alts = np.array([float(alt_km)], dtype=float)

    f107s = np.array([float(f107)], dtype=float)
    f107as = np.array([float(f107a)], dtype=float)

    aps = np.array([[float(ap_scalar)] * 7], dtype=float)

    out = pymsis_msis.run(
        t64, lons, lats, alts,
        f107s=f107s,
        f107as=f107as,
        aps=aps,
        version=0,                 # <-- KEY FIX (MSISE-00)
        geomagnetic_activity=1,     # daily Ap mode
    )

    arr = np.asarray(out)
    if arr.ndim == 2:
        rho = arr[0, 0]
    else:
        rho = arr[0, 0, 0, 0, 0]
    return float(rho)


# -----------------------------
# Proxy report generator (PNG + optional PDF)
# -----------------------------
def generate_proxy_report(
    out_path_png: str,
    *,
    norad_id: int,
    object_name: str,
    created_utc: str,
    reentry_epoch_utc: str,
    epoch_uncertainty_text: str,
    window_start_utc: str,
    window_end_utc: str,
    p50_latlon_text: str,
    kp_text: str,
    footer_text: str = "This report is an operational proxy. For close agreement, load EU-SST PDF to anchor epoch/time.",
    out_path_pdf: Optional[str] = None,
) -> None:
    fig = plt.figure(figsize=(8.6, 9.2), dpi=160)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.axis("off")

    ax.text(0.5, 0.94, "Re-entry Analysis Report (Proxy)", ha="center", va="center",
            fontsize=20, fontweight="bold")
    ax.text(0.5, 0.905, f"NORAD {norad_id}  |  {object_name}", ha="center", va="center",
            fontsize=12)

    left, right = 0.10, 0.90
    top, bottom = 0.82, 0.27
    width, height = (right - left), (top - bottom)

    rows = [
        ("Creation Date (UTC)", created_utc),
        ("Re-entry Epoch (UTC)", reentry_epoch_utc),
        ("Epoch Uncertainty", epoch_uncertainty_text),
        ("Window Start (UTC)", window_start_utc),
        ("Window End (UTC)", window_end_utc),
        ("Predicted P50 Lat/Lon", p50_latlon_text),
        ("NOAA Kp daily mean", kp_text),
    ]
    n = len(rows)
    row_h = height / n

    ax.add_patch(Rectangle((left, bottom), width, height, fill=False, linewidth=1.5))

    sep_colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b", "#e377c2"]
    for i in range(1, n):
        y = top - i * row_h
        ax.plot([left, right], [y, y], linewidth=1.0, color=sep_colors[(i - 1) % len(sep_colors)])

    split = left + 0.46 * width

    for i, (label, value) in enumerate(rows):
        y_center = top - (i + 0.5) * row_h
        ax.text(left + 0.02 * width, y_center, label, ha="left", va="center",
                fontsize=13, fontweight="bold")
        ax.text(split + 0.02 * width, y_center, str(value), ha="left", va="center",
                fontsize=13)

    ax.text(0.5, 0.09, footer_text, ha="center", va="center", fontsize=10)

    fig.savefig(out_path_png, bbox_inches="tight", dpi=200)
    if out_path_pdf:
        fig.savefig(out_path_pdf, bbox_inches="tight")
    plt.close(fig)


# -----------------------------
# Monte Carlo prediction
# -----------------------------
def mc_sample_times_with_bias(
    wmin: dt.datetime,
    wmax: dt.datetime,
    bstar_stats: Dict[str, float],
    kp_mean: Optional[float],
    n: int,
    seed: Optional[int] = None
) -> np.ndarray:
    if seed is not None:
        np.random.seed(seed)

    width_s = max(1.0, (wmax - wmin).total_seconds())

    med = bstar_stats.get("bstar_med", float("nan"))
    trend = bstar_stats.get("bstar_trend_per_day", float("nan"))

    bias = 0.0
    if np.isfinite(med):
        bias += -0.10 * np.tanh(math.log10(abs(med) + 1e-12) + 7.0)
    if np.isfinite(trend):
        bias += -0.12 * np.tanh(trend * 5e4)
    if kp_mean is not None and np.isfinite(kp_mean):
        bias += -0.08 * np.tanh((kp_mean - 2.0) / 2.0)

    # Calibrated uncertainty: keep bias small, use low k so P10-P90
    # spans ~60% of the TIP window rather than squeezing to ~30%.
    # This keeps our uncertainty comparable to EU SST / Space-Track
    # rather than misleadingly narrow.
    bias = float(np.clip(bias, -0.20, +0.15))

    mean = float(np.clip(0.5 + bias, 0.15, 0.85))
    # k=4 → Beta(2,2)-like spread; std ≈ 0.17*window vs k=10 which was ~0.09*window
    k = 4.0
    a = mean * k
    b = (1.0 - mean) * k

    u = np.random.beta(a, b, size=int(n))
    ts = np.array([wmin.timestamp() + float(x) * width_s for x in u], dtype=float)
    return ts


def approx_descent_time_seconds(rho_kg_m3: float, bstar_med: float) -> float:
    rho = max(1e-14, min(1e-6, float(rho_kg_m3)))
    bmag = abs(bstar_med) if np.isfinite(bstar_med) else 1e-5
    bmag = max(1e-12, min(1e-2, bmag))

    idx = (math.log10(rho) + 12.0) + 0.7 * (math.log10(bmag) + 7.0)
    t = 900.0 - 250.0 * math.tanh(idx)
    return float(np.clip(t, 360.0, 1500.0))


def bearing_deg(lat1, lon1, lat2, lon2) -> float:
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dl)
    br = math.degrees(math.atan2(y, x))
    return (br + 360.0) % 360.0


def move_along_bearing(lat, lon, bearing, distance_km) -> Tuple[float, float]:
    ang = distance_km / EARTH_RADIUS_KM
    br = math.radians(bearing)
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)

    lat2 = math.asin(math.sin(lat1) * math.cos(ang) + math.cos(lat1) * math.sin(ang) * math.cos(br))
    lon2 = lon1 + math.atan2(math.sin(br) * math.sin(ang) * math.cos(lat1),
                             math.cos(ang) - math.sin(lat1) * math.sin(lat2))
    lat2d = math.degrees(lat2)
    lon2d = ((math.degrees(lon2) + 180) % 360) - 180
    return lat2d, lon2d


def earth_rotation_lon_shift_deg(seconds: float) -> float:
    omega = 360.0 / 86164.0905
    return omega * float(seconds)


def impact_proxy_from_time(
    sat: EarthSatellite,
    t_utc: dt.datetime,
    f107: float,
    f107a: float,
    ap_scalar: float,
    bstar_med: float
) -> Tuple[float, float, Dict[str, Any]]:
    lat0, lon0 = subpoint_at_time(sat, t_utc)

    lat_a, lon_a = subpoint_at_time(sat, t_utc - dt.timedelta(seconds=30))
    lat_b, lon_b = subpoint_at_time(sat, t_utc + dt.timedelta(seconds=30))
    br = bearing_deg(lat_a, lon_a, lat_b, lon_b)

    rho = pymsis_density_kg_m3(t_utc, 120.0, lat0, lon0, f107, f107a, ap_scalar)
    t_desc = approx_descent_time_seconds(rho, bstar_med)

    dens_idx = np.clip((math.log10(max(rho, 1e-14)) + 11.0) / 4.0, -1.0, +1.0)
    v_avg = 2600.0 - 600.0 * float(dens_idx)
    v_avg = float(np.clip(v_avg, 1400.0, 3200.0))
    downrange_km = (v_avg * t_desc) / 1000.0

    lat1, lon1 = move_along_bearing(lat0, lon0, br, downrange_km)
    dlon = earth_rotation_lon_shift_deg(t_desc)
    lon2 = ((lon1 + dlon + 180) % 360) - 180

    meta = {
        "subpoint_lat": lat0,
        "subpoint_lon": lon0,
        "bearing_deg": br,
        "rho_120km_kgm3": rho,
        "descent_time_s": t_desc,
        "v_avg_ms": v_avg,
        "downrange_km": downrange_km,
        "earth_rot_dlon_deg": dlon,
    }
    return lat1, lon2, meta


# -----------------------------
# Outputs: history CSV
# -----------------------------
def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def history_path(out_dir: str, norad_id: int) -> str:
    return os.path.join(out_dir, f"tip_history_{norad_id}.csv")


def append_history_row(csv_path: str, row: dict) -> None:
    file_exists = os.path.exists(csv_path)
    fieldnames = list(row.keys())
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            w.writeheader()
        w.writerow(row)


# -----------------------------
# KML exporters
# -----------------------------
def _split_track_at_dateline(
    lats: List[float], lons: List[float], jump_deg: float = 180.0
) -> List[List[Tuple[float, float]]]:
    """
    Split a (lat, lon) track into segments wherever the longitude jumps by
    more than jump_deg degrees — i.e. at the antimeridian — and return each
    segment as a list of (lon, lat) tuples ready for KML coords.
    """
    if not lats:
        return []
    segments: List[List[Tuple[float, float]]] = []
    cur: List[Tuple[float, float]] = [(lons[0], lats[0])]
    for i in range(1, len(lats)):
        if abs(lons[i] - lons[i - 1]) > jump_deg:
            if len(cur) > 1:
                segments.append(cur)
            cur = [(lons[i], lats[i])]
        else:
            cur.append((lons[i], lats[i]))
    if len(cur) > 1:
        segments.append(cur)
    return segments


def export_kml_tracks(path: str, tracks: List[Dict[str, Any]]) -> None:
    """
    Export ground tracks to KML, splitting each track at the antimeridian
    so QGIS / Google Earth never draws the horizontal artifact lines.
    Each segment becomes a separate LineString placemark named
    "<track_name> seg N".
    """
    if simplekml is None:
        raise RuntimeError("simplekml not installed. pip install simplekml")
    kml = simplekml.Kml()
    for tr in tracks:
        segments = _split_track_at_dateline(tr["lats"], tr["lons"])
        if not segments:
            continue
        if len(segments) == 1:
            ls = kml.newlinestring(name=tr["name"])
            ls.coords = segments[0]
            ls.altitudemode = simplekml.AltitudeMode.clamptoground
            ls.extrude = 0
        else:
            for seg_idx, seg_coords in enumerate(segments, 1):
                ls = kml.newlinestring(name=f"{tr['name']} seg {seg_idx}")
                ls.coords = seg_coords
                ls.altitudemode = simplekml.AltitudeMode.clamptoground
                ls.extrude = 0
    kml.save(path)


def export_kml_points(path: str, points: List[Dict[str, Any]]) -> None:
    if simplekml is None:
        raise RuntimeError("simplekml not installed. pip install simplekml")
    kml = simplekml.Kml()
    for p in points:
        pt = kml.newpoint(name=p["name"], coords=[(p["lon"], p["lat"])])
        pt.description = p.get("desc", "")
    kml.save(path)


def export_kml_swath_polygon(path: str, center_track_lats: List[float], center_track_lons: List[float], half_width_km: float) -> None:
    """
    Export the swath as a series of MultiGeometry polygon segments, splitting
    at the antimeridian to avoid the horizontal line artifact in QGIS / Google Earth.
    Each antimeridian-safe sub-track becomes its own closed polygon.
    """
    if simplekml is None:
        raise RuntimeError("simplekml not installed. pip install simplekml")
    if not center_track_lats:
        raise RuntimeError("Empty track for swath")

    # Build offset edge points
    left: List[Tuple[float, float]] = []
    right: List[Tuple[float, float]] = []
    for lat, lon in zip(center_track_lats, center_track_lons):
        dlon = half_width_km / (111.0 * max(0.2, math.cos(math.radians(lat))))
        left.append((((lon - dlon + 180) % 360) - 180, lat))
        right.append((((lon + dlon + 180) % 360) - 180, lat))

    # Split the center track at dateline jumps; use the same segment boundaries
    # for left and right edges so each polygon segment is self-consistent.
    jump = 180.0
    seg_indices: List[Tuple[int, int]] = []  # (start, end) inclusive
    seg_start = 0
    for i in range(1, len(center_track_lons)):
        if abs(center_track_lons[i] - center_track_lons[i - 1]) > jump:
            seg_indices.append((seg_start, i - 1))
            seg_start = i
    seg_indices.append((seg_start, len(center_track_lons) - 1))

    kml = simplekml.Kml()
    for seg_num, (i0, i1) in enumerate(seg_indices, 1):
        if i1 <= i0:
            continue
        seg_left  = left[i0 : i1 + 1]
        seg_right = right[i0 : i1 + 1]
        poly_ring = seg_left + seg_right[::-1] + [seg_left[0]]
        pol = kml.newpolygon(name=f"Swath ±{half_width_km:.0f} km seg {seg_num}")
        pol.outerboundaryis = poly_ring
        pol.style.polystyle.fill = 0
        pol.style.linestyle.width = 2
    kml.save(path)



# =============================================================================
# STREAMLIT RENDER
# Replaces the Tkinter ReentryGUI class.
# All pure functions above are unchanged.
# Credentials come from st.secrets["spacetrack"] (same as reentry_event_analyzer).
# State (sat, window, pred, etc.) lives in st.session_state under "re_pred_*" keys.
# =============================================================================

import streamlit as st
import plotly.graph_objects as go
import io
import tempfile


def _get_spacetrack_creds():
    """Read Space-Track credentials — st.secrets first, .env fallback."""
    try:
        return (
            st.secrets["spacetrack"]["username"],
            st.secrets["spacetrack"]["password"],
        )
    except Exception:
        import os
        return os.getenv("SPACE_TRACK_USERNAME", ""), os.getenv("SPACE_TRACK_PASSWORD", "")


def _ss(key, default=None):
    """Shorthand for st.session_state with a 're_pred_' namespace."""
    full = f"re_pred_{key}"
    if full not in st.session_state:
        st.session_state[full] = default
    return st.session_state[full]


def _set(key, val):
    st.session_state[f"re_pred_{key}"] = val



# ─────────────────────────────────────────────────────────────────────────────
# ANIMATED RE-ENTRY GLOBE
# Adapted from collision.py's render_globe_animated() for a single re-entry
# object. The animation spans the P10–P90 prediction interval, follows the
# nominal TLE ground track, and highlights the P10/P50/P90 proxy locations.
# ─────────────────────────────────────────────────────────────────────────────

def _reentry_animation_points(
    sat: EarthSatellite,
    start_time: dt.datetime,
    end_time: dt.datetime,
    max_frames: int = 121,
) -> List[Tuple[float, float, float, dt.datetime]]:
    """Return evenly sampled (lat, lon, alt_km, time_utc) points for animation."""
    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=dt.timezone.utc)
    else:
        start_time = start_time.astimezone(dt.timezone.utc)
    if end_time.tzinfo is None:
        end_time = end_time.replace(tzinfo=dt.timezone.utc)
    else:
        end_time = end_time.astimezone(dt.timezone.utc)

    total_s = max(1.0, (end_time - start_time).total_seconds())
    # About one frame every 30 s, capped to keep the Plotly payload responsive.
    n_frames = int(np.clip(math.ceil(total_s / 30.0) + 1, 2, max_frames))
    times = [
        start_time + dt.timedelta(seconds=total_s * i / (n_frames - 1))
        for i in range(n_frames)
    ]

    ts = sf_load.timescale()
    t_sf = ts.from_datetimes(times)
    geoc = sat.at(t_sf)
    sub = geoc.subpoint()

    lats = np.asarray(sub.latitude.degrees, dtype=float)
    lons = np.asarray(sub.longitude.degrees, dtype=float)
    lons = ((lons + 180.0) % 360.0) - 180.0
    try:
        alts = np.asarray(sub.elevation.km, dtype=float)
    except Exception:
        alts = np.full(len(times), np.nan, dtype=float)

    return [
        (float(la), float(lo), float(alt), t)
        for la, lo, alt, t in zip(lats, lons, alts, times)
    ]


def render_reentry_globe_animated(
    sat: EarthSatellite,
    pred: Dict[str, Any],
    object_name: str,
) -> None:
    """
    Animated orthographic globe for the re-entry predictor.

    The satellite marker follows the nominal TLE ground track from P10 to P90.
    P10/P50/P90 proxy re-entry locations remain visible on the globe and pulse
    when the animation reaches their corresponding prediction time.
    """
    if sat is None or not pred:
        return

    mc = pred.get("monte_carlo", {})
    imp = pred.get("impact_proxy", {})

    try:
        p10_t = parse_any_datetime_utc(mc["p10_utc"])
        p50_t = parse_any_datetime_utc(mc["p50_utc"])
        p90_t = parse_any_datetime_utc(mc["p90_utc"])
    except Exception as exc:
        st.warning(f"Could not build re-entry animation: invalid P10/P50/P90 time ({exc}).")
        return

    if p90_t <= p10_t:
        st.warning("Could not build re-entry animation: P90 must occur after P10.")
        return

    impact_defs = [
        ("p10", "P10", "EARLY", "#29b6f6"),
        ("p50", "P50", "BEST",  "#f44336"),
        ("p90", "P90", "LATE",  "#66bb6a"),
    ]

    impact_points: Dict[str, Dict[str, Any]] = {}
    for key, short_label, long_label, color in impact_defs:
        p = imp.get(key, {})
        if "lat" not in p or "lon" not in p:
            st.warning(f"Could not build re-entry animation: {short_label} proxy coordinates are missing.")
            return
        impact_points[key] = {
            "lat": float(p["lat"]),
            "lon": float(p["lon"]),
            "short": short_label,
            "long": long_label,
            "color": color,
        }

    pts = _reentry_animation_points(sat, p10_t, p90_t)
    if len(pts) < 2:
        st.warning("Not enough propagated points for re-entry animation.")
        return

    event_times = {"p10": p10_t, "p50": p50_t, "p90": p90_t}
    event_frames = {
        key: int(np.argmin([abs((p[3] - t_evt).total_seconds()) for p in pts]))
        for key, t_evt in event_times.items()
    }

    # P50 is visually central to the estimate, but the animation starts at P10
    # so Play shows the full early→best→late progression.
    p50_frame = event_frames["p50"]

    full_lats = [p[0] for p in pts]
    full_lons = [p[1] for p in pts]

    def _active_event(i: int) -> Optional[str]:
        # Keep the pulse visible for a few frames rather than a single instant.
        candidates = [
            (abs(i - frame_idx), key)
            for key, frame_idx in event_frames.items()
            if abs(i - frame_idx) <= 1
        ]
        return min(candidates)[1] if candidates else None

    def _make_frame(i: int) -> go.Frame:
        lat, lon, alt_km, t_utc = pts[i]
        trail_start = max(0, i - 20)
        trail_lats = [p[0] for p in pts[trail_start:i + 1]]
        trail_lons = [p[1] for p in pts[trail_start:i + 1]]

        active_key = _active_event(i)
        if active_key:
            active = impact_points[active_key]
            pulse_lat = [active["lat"]]
            pulse_lon = [active["lon"]]
            pulse_size = 24
            annotation_text = (
                f"{active['short']} {active['long']} predicted re-entry · "
                f"{dt_to_iso_z(event_times[active_key])}"
            )
            annotation_color = active["color"]
        else:
            pulse_lat, pulse_lon, pulse_size = [], [], 0
            annotation_text = "P10–P90 predicted re-entry interval"
            annotation_color = "#e0e0e0"

        hover_alt = "N/A" if not np.isfinite(alt_km) else f"{alt_km:.1f} km"

        traces = [
            # Full P10→P90 nominal ground track for context.
            go.Scattergeo(
                lat=full_lats,
                lon=full_lons,
                mode="lines",
                name="P10–P90 nominal track",
                line=dict(color="rgba(180,200,220,0.35)", width=1.0, dash="dot"),
                hoverinfo="skip",
                showlegend=False,
            ),
            # Recent trail behind the moving object.
            go.Scattergeo(
                lat=trail_lats,
                lon=trail_lons,
                mode="lines",
                name="Recent ground track",
                line=dict(color="#FFD700", width=2.0),
                hoverinfo="skip",
                showlegend=False,
            ),
            # Moving satellite marker.
            go.Scattergeo(
                lat=[lat],
                lon=[lon],
                mode="markers+text",
                name=object_name,
                marker=dict(
                    size=11,
                    color="#FFD700",
                    symbol="circle",
                    line=dict(color="white", width=1),
                ),
                text=[object_name],
                textposition="top right",
                textfont=dict(color="#FFD700", size=10),
                customdata=[[dt_to_iso_z(t_utc), hover_alt]],
                hovertemplate=(
                    "%{text}<br>Time: %{customdata[0]}<br>"
                    "Subpoint: %{lat:.2f}°, %{lon:.2f}°<br>"
                    "TLE altitude: %{customdata[1]}<extra></extra>"
                ),
                showlegend=(i == 0),
            ),
        ]

        # Fixed P10/P50/P90 proxy markers.
        for key, short_label, long_label, color in impact_defs:
            p = impact_points[key]
            evt = event_times[key]
            traces.append(
                go.Scattergeo(
                    lat=[p["lat"]],
                    lon=[p["lon"]],
                    mode="markers+text",
                    name=f"{short_label} {long_label}",
                    marker=dict(
                        size=10 if key == "p50" else 8,
                        color=color,
                        symbol="circle",
                        line=dict(color="white", width=1),
                        opacity=0.95,
                    ),
                    text=[short_label],
                    textposition="top right",
                    textfont=dict(color=color, size=10),
                    customdata=[[dt_to_iso_z(evt), long_label]],
                    hovertemplate=(
                        f"{short_label} {long_label}<br>"
                        "Time: %{customdata[0]}<br>"
                        "Proxy location: %{lat:.3f}°, %{lon:.3f}°<extra></extra>"
                    ),
                    showlegend=(i == 0),
                )
            )

        # Pulse at the active percentile location.
        traces.append(
            go.Scattergeo(
                lat=pulse_lat,
                lon=pulse_lon,
                mode="markers",
                name="Predicted re-entry event",
                marker=dict(
                    size=pulse_size,
                    color="rgba(255,255,255,0.08)",
                    symbol="circle",
                    line=dict(color=(impact_points[active_key]["color"] if active_key else "white"), width=3),
                ),
                hoverinfo="skip",
                showlegend=False,
            )
        )

        return go.Frame(
            data=traces,
            name=t_utc.strftime("%Y-%m-%dT%H:%M:%S"),
            layout=go.Layout(
                geo=dict(projection_rotation=dict(lon=lon, lat=lat)),
                annotations=[dict(
                    text=annotation_text,
                    xref="paper", yref="paper",
                    x=0.5, y=1.04,
                    showarrow=False,
                    font=dict(color=annotation_color, size=11),
                )],
            ),
        )

    frames = [_make_frame(i) for i in range(len(pts))]
    slider_steps = [
        dict(
            args=[[f.name], dict(
                frame=dict(duration=0, redraw=True),
                mode="immediate",
                transition=dict(duration=0),
            )],
            label=f.name[11:16],
            method="animate",
        )
        for f in frames
    ]

    _land_col    = "#2a4a2a"
    _ocean_col   = "#0d1b2a"
    _coast_col   = "#4a8aaa"
    _country_col = "#3a6080"
    _globe_bg    = "rgba(0,0,0,0)"
    _txt         = "#e0e0e0"
    _ctrl_bg     = "rgba(14,34,75,0.88)"
    _ctrl_bdr    = "#1a4fa3"
    _leg_bg      = "rgba(10,20,50,0.88)"

    init_frame = frames[0]
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
                projection_rotation=dict(lon=pts[0][1], lat=pts[0][0]),
                showland=True,       landcolor=_land_col,
                showocean=True,      oceancolor=_ocean_col,
                showcoastlines=True, coastlinecolor=_coast_col,
                showcountries=True,  countrycolor=_country_col,
                bgcolor=_globe_bg,
                showframe=False,
                showlakes=True,      lakecolor=_ocean_col,
            ),
            legend=dict(
                font=dict(color=_txt),
                bgcolor=_leg_bg,
                bordercolor=_ctrl_bdr,
                x=0.01, y=0.99,
            ),
            margin=dict(l=0, r=0, t=50, b=120),
            height=700,
            updatemenus=[dict(
                type="buttons",
                showactive=False,
                x=0.5, xanchor="center",
                y=-0.16, yanchor="top",
                buttons=[
                    dict(
                        label="▶  Play",
                        method="animate",
                        args=[None, dict(
                            frame=dict(duration=120, redraw=True),
                            fromcurrent=True,
                            transition=dict(duration=0),
                        )],
                    ),
                    dict(
                        label="⏸  Pause",
                        method="animate",
                        args=[[None], dict(
                            frame=dict(duration=0, redraw=False),
                            mode="immediate",
                            transition=dict(duration=0),
                        )],
                    ),
                ],
                font=dict(color=_txt),
                bgcolor=_ctrl_bg,
                bordercolor=_ctrl_bdr,
            )],
            sliders=[dict(
                active=0,
                currentvalue=dict(
                    prefix="Time: ",
                    font=dict(color=_txt, size=12),
                    xanchor="center",
                ),
                pad=dict(t=50, b=10),
                steps=slider_steps,
                font=dict(color=_txt),
                bgcolor=_ctrl_bg,
                bordercolor=_ctrl_bdr,
                tickcolor=_txt,
            )],
            annotations=[dict(
                text="P10 EARLY predicted re-entry",
                xref="paper", yref="paper",
                x=0.5, y=1.04,
                showarrow=False,
                font=dict(color=impact_points["p10"]["color"], size=11),
            )],
        ),
    )

    st.markdown(
        "<div style='font-weight:600;color:var(--text-color);font-size:0.9rem;"
        "margin:14px 0 0;'>Animated Re-entry Ground Track</div>",
        unsafe_allow_html=True,
    )
    st.plotly_chart(fig, use_container_width=True, key="reentry_globe_animation")

    # Recolor Plotly control/slider text to follow the live Streamlit theme,
    # mirroring the collision animation's theme handling.
    import streamlit.components.v1 as components
    components.html("""
    <script>
    (function recolorReentryPlotlyText() {
        function getThemeColor() {
            var bg = window.parent.getComputedStyle(window.parent.document.body).backgroundColor;
            var rgb = bg.match(/\d+/g);
            if (rgb) {
                var lum = 0.299*parseInt(rgb[0]) + 0.587*parseInt(rgb[1]) + 0.114*parseInt(rgb[2]);
                return lum > 128 ? '#0e224b' : '#e0e0e0';
            }
            return '#0e224b';
        }

        var preserveSet = new Set([
            '#ffd700','#29b6f6','#f44336','#66bb6a',
            'rgb(255,215,0)','rgb(41,182,246)','rgb(244,67,54)','rgb(102,187,106)'
        ]);

        function shouldPreserve(color) {
            if (!color) return false;
            return preserveSet.has(color.toLowerCase().replace(/\s/g,''));
        }

        function recolor(root, textColor) {
            var texts = root.querySelectorAll('.js-plotly-plot text, .js-plotly-plot tspan');
            texts.forEach(function(el) {
                var fill = (el.getAttribute('fill') || '').toLowerCase().replace(/\s/g,'');
                var styleFill = (el.style.fill || '').toLowerCase().replace(/\s/g,'');
                if (shouldPreserve(fill) || shouldPreserve(styleFill)) return;
                el.setAttribute('fill', textColor);
                el.style.fill = textColor;
            });
        }

        function run() {
            var color = getThemeColor();
            var iframes = window.parent.document.querySelectorAll('iframe');
            iframes.forEach(function(iframe) {
                try {
                    var idoc = iframe.contentDocument || iframe.contentWindow.document;
                    recolor(idoc, color);
                } catch(e) {}
            });
            recolor(window.parent.document, color);
        }

        run();
        setTimeout(run, 200);
        setTimeout(run, 600);
        setTimeout(run, 1200);
        setTimeout(run, 2500);
    })();
    </script>
    """, height=0)



def _is_over_ph(lat: float, lon: float) -> bool:
    """Return True only when the point is inside the Manila FIR polygon.

    No rectangular Philippines fallback is used. If the FIR shapefile cannot
    be loaded, this returns False and the UI surfaces that FIR calculations
    are unavailable.
    """
    geom = _load_manila_fir_geometry()
    if geom is None or ShapelyPoint is None:
        return False
    try:
        pt = ShapelyPoint(float(lon), float(lat))
        return bool(geom.contains(pt) or geom.touches(pt))
    except Exception:
        return False


def _merge_geo_segments(segments: List[Tuple[List[float], List[float]]]) -> Tuple[List[Any], List[Any]]:
    """Merge dateline-safe segments into one Plotly trace using None gaps."""
    out_lat, out_lon = [], []
    for seg_lat, seg_lon in segments:
        if not seg_lat or not seg_lon:
            continue
        if out_lat:
            out_lat.append(None)
            out_lon.append(None)
        out_lat.extend([float(x) for x in seg_lat])
        out_lon.extend([float(x) for x in seg_lon])
    return out_lat, out_lon


def _manila_fir_trace(showlegend: bool = True):
    segments = _load_manila_fir_segments()
    lat, lon = _merge_geo_segments(segments)
    return go.Scattergeo(
        lat=lat, lon=lon,
        mode="lines",
        name="Manila FIR",
        line=dict(color="#FFA500", width=2.0),
        hoverinfo="skip",
        showlegend=showlegend,
    )


def _track_over_window(sat: EarthSatellite, start_utc: dt.datetime, end_utc: dt.datetime, step_seconds: int = 30):
    total_s = max(1.0, (end_utc - start_utc).total_seconds())
    center = start_utc + dt.timedelta(seconds=total_s / 2.0)
    half_min = max(1, int(math.ceil(total_s / 120.0)))
    lats, lons, times = groundtrack_corridor(sat, center, half_min, half_min, int(step_seconds))
    return lats, lons, times


def _summarize_ph_crossings_from_track(lats, lons, times):
    crossings = []
    active = []
    for lat, lon, t in zip(lats, lons, times):
        if _is_over_ph(lat, lon):
            active.append((t, lat, lon))
        elif active:
            crossings.append(active)
            active = []
    if active:
        crossings.append(active)

    out = []
    for idx, pas in enumerate(crossings, 1):
        entry_t, entry_lat, entry_lon = pas[0]
        exit_t, exit_lat, exit_lon = pas[-1]
        out.append({
            "pass": idx,
            "entry_utc": dt_to_iso_z(entry_t),
            "entry_lat": round(float(entry_lat), 4),
            "entry_lon": round(float(entry_lon), 4),
            "exit_utc": dt_to_iso_z(exit_t),
            "exit_lat": round(float(exit_lat), 4),
            "exit_lon": round(float(exit_lon), 4),
            "samples": len(pas),
        })
    return out


def _stable_seed_from_text(text_seed: str) -> int:
    return int(hashlib.sha1(str(text_seed).encode()).hexdigest()[:8], 16)


_MANILA_FIR_SEGMENTS_CACHE = None
_MANILA_FIR_GEOM_CACHE = None
_MANILA_FIR_LOAD_ATTEMPTED = False


def _discover_manila_fir_shapefile() -> Optional[str]:
    """Locate the same Manila_FIR_boundary.shp used by rocket.py."""
    env_file = os.getenv("REENTRY_MANILA_FIR_SHP", "").strip()
    if env_file and os.path.exists(env_file):
        return env_file

    here = Path(__file__).resolve().parent

    # rocket.py uses: parent_dir / utils / shapefiles / Manila_FIR_boundary.shp
    # Search that exact layout first, then common module layouts.
    candidate_dirs = []
    env_dir = os.getenv("REENTRY_SHAPE_DIR", "").strip()
    if env_dir:
        candidate_dirs.append(Path(env_dir))
    candidate_dirs.extend([
        here.parent / "utils" / "shapefiles",
        here / "utils" / "shapefiles",
        here.parent.parent / "utils" / "shapefiles",
        here / "shapefiles",
        here.parent / "shapefiles",
    ])

    for base in candidate_dirs:
        shp = base / "Manila_FIR_boundary.shp"
        if shp.exists():
            return str(shp)

    # Last-resort repo-local search.
    for root in (here, here.parent, here.parent.parent):
        try:
            for shp in root.rglob("Manila_FIR_boundary.shp"):
                return str(shp)
        except Exception:
            pass
    return None


def _load_manila_fir_geometry():
    global _MANILA_FIR_GEOM_CACHE, _MANILA_FIR_LOAD_ATTEMPTED
    if _MANILA_FIR_LOAD_ATTEMPTED:
        return _MANILA_FIR_GEOM_CACHE
    _MANILA_FIR_LOAD_ATTEMPTED = True

    shp_path = _discover_manila_fir_shapefile()
    if gpd is None or not shp_path or not os.path.exists(shp_path):
        _MANILA_FIR_GEOM_CACHE = None
        return None

    try:
        gdf = gpd.read_file(shp_path)
        if gdf.empty:
            return None
        # Ensure lat/lon coordinates before point-in-polygon checks.
        if gdf.crs is not None and str(gdf.crs).upper() not in ("EPSG:4326", "WGS84"):
            gdf = gdf.to_crs(epsg=4326)
        try:
            geom = gdf.geometry.union_all()
        except Exception:
            geom = gdf.geometry.unary_union
        _MANILA_FIR_GEOM_CACHE = geom
        return geom
    except Exception:
        _MANILA_FIR_GEOM_CACHE = None
        return None


def _load_manila_fir_segments() -> List[Tuple[List[float], List[float]]]:
    global _MANILA_FIR_SEGMENTS_CACHE
    if _MANILA_FIR_SEGMENTS_CACHE is not None:
        return _MANILA_FIR_SEGMENTS_CACHE

    segments: List[Tuple[List[float], List[float]]] = []
    geom = _load_manila_fir_geometry()
    if geom is None:
        _MANILA_FIR_SEGMENTS_CACHE = segments
        return segments

    try:
        geoms = [geom] if geom.geom_type == "Polygon" else list(geom.geoms)
        for poly in geoms:
            if getattr(poly, "geom_type", "") != "Polygon":
                continue
            x, y = poly.exterior.xy
            lats = [float(v) for v in y]
            lons = [float(v) for v in x]
            for seg_lat, seg_lon in split_dateline_segments(lats, lons):
                if len(seg_lat) >= 2:
                    segments.append((seg_lat, seg_lon))
    except Exception:
        segments = []

    _MANILA_FIR_SEGMENTS_CACHE = segments
    return segments


def _shortest_lon_delta(lon_a: float, lon_b: float) -> float:
    delta = float(lon_b) - float(lon_a)
    while delta > 180.0:
        delta -= 360.0
    while delta < -180.0:
        delta += 360.0
    return delta


def _interp_lon(lon_a: float, lon_b: float, alpha: float) -> float:
    out = float(lon_a) + _shortest_lon_delta(lon_a, lon_b) * float(alpha)
    return ((out + 180.0) % 360.0) - 180.0


def _interp_latlon(lat_a: float, lon_a: float, lat_b: float, lon_b: float, alpha: float) -> Tuple[float, float]:
    lat = float(lat_a) + (float(lat_b) - float(lat_a)) * float(alpha)
    lon = _interp_lon(float(lon_a), float(lon_b), float(alpha))
    return lat, lon


def _resample_track(track_lats: List[float], track_lons: List[float], n_points: int = 24) -> Tuple[List[float], List[float]]:
    if not track_lats or not track_lons or len(track_lats) != len(track_lons):
        return [], []
    if len(track_lats) == 1:
        return [float(track_lats[0])] * n_points, [float(track_lons[0])] * n_points

    base = np.arange(len(track_lats), dtype=float)
    xi = np.linspace(0.0, float(len(track_lats) - 1), int(max(2, n_points)))
    lats = np.interp(xi, base, np.asarray(track_lats, dtype=float))

    lons_raw = np.asarray(track_lons, dtype=float)
    lons_unwrapped = [float(lons_raw[0])]
    for lon in lons_raw[1:]:
        prev = lons_unwrapped[-1]
        cur = float(lon)
        while cur - prev > 180.0:
            cur -= 360.0
        while cur - prev < -180.0:
            cur += 360.0
        lons_unwrapped.append(cur)
    lons = np.interp(xi, base, np.asarray(lons_unwrapped, dtype=float))
    lons = [((float(lo) + 180.0) % 360.0) - 180.0 for lo in lons]
    return [float(x) for x in lats], [float(x) for x in lons]


def _predict_single_tip_batch(
    sat: EarthSatellite,
    batch: List[TipSolution],
    fallback_unc_min: float,
    bstar_stats: Dict[str, float],
    f107: float,
    f107a: float,
    use_bstar: bool,
    mc_samples: int,
    step_seconds: int = 30,
) -> Optional[Dict[str, Any]]:
    if not batch:
        return None

    wmin, wmax, _, wmode = compute_tip_window_from_latest_batch(batch, float(fallback_unc_min))
    if not (wmin and wmax):
        return None

    width_s = (wmax - wmin).total_seconds()
    if width_s <= 0:
        return None

    wmid_t = wmin + dt.timedelta(seconds=width_s / 2.0)
    kp_mean = fetch_noaa_kp_1m_mean_near(wmid_t, hours_window=12)
    ap = kp_to_ap_proxy(kp_mean) if kp_mean is not None else 10.0

    bstar_med = bstar_stats.get("bstar_med", float("nan")) if use_bstar else float("nan")
    bstats = bstar_stats if use_bstar else {}
    seed = _stable_seed_from_text(batch[0].msg_epoch or dt_to_iso_z(wmid_t))
    n_mc = max(200, min(50000, int(mc_samples)))
    ts_samples = mc_sample_times_with_bias(wmin, wmax, bstats, kp_mean, n=n_mc, seed=seed)

    p10 = dt.datetime.fromtimestamp(float(np.percentile(ts_samples, 10)), tz=dt.timezone.utc)
    p50 = dt.datetime.fromtimestamp(float(np.percentile(ts_samples, 50)), tz=dt.timezone.utc)
    p90 = dt.datetime.fromtimestamp(float(np.percentile(ts_samples, 90)), tz=dt.timezone.utc)

    lat10, lon10, meta10 = impact_proxy_from_time(sat, p10, f107, f107a, ap, bstar_med)
    lat50, lon50, meta50 = impact_proxy_from_time(sat, p50, f107, f107a, ap, bstar_med)
    lat90, lon90, meta90 = impact_proxy_from_time(sat, p90, f107, f107a, ap, bstar_med)

    track_lats, track_lons, track_times = _track_over_window(sat, wmin, wmax, step_seconds=step_seconds)
    fir_available = _load_manila_fir_geometry() is not None
    if fir_available:
        ph_crossings = _summarize_ph_crossings_from_track(track_lats, track_lons, track_times)
        track_crosses_ph = len(ph_crossings) > 0
        proxy_crosses_ph = any(
            _is_over_ph(lat, lon)
            for lat, lon in ((lat10, lon10), (lat50, lon50), (lat90, lon90))
        )
    else:
        ph_crossings = []
        track_crosses_ph = False
        proxy_crosses_ph = False

    return {
        "msg_epoch_utc": batch[0].msg_epoch,
        "n_batch_rows": len(batch),
        "tip_window": {
            "start_utc": dt_to_iso_z(wmin),
            "end_utc": dt_to_iso_z(wmax),
            "width_sec": int(width_s),
            "mode": wmode,
        },
        "monte_carlo": {
            "n": n_mc,
            "seed": seed,
            "p10_utc": dt_to_iso_z(p10),
            "p50_utc": dt_to_iso_z(p50),
            "p90_utc": dt_to_iso_z(p90),
        },
        "impact_proxy": {
            "p10": {"lat": lat10, "lon": lon10, "meta": meta10},
            "p50": {"lat": lat50, "lon": lon50, "meta": meta50},
            "p90": {"lat": lat90, "lon": lon90, "meta": meta90},
        },
        "noaa_kp_mean_1m_near_mid": kp_mean,
        "ap_proxy_used": ap,
        "ph_crossings": ph_crossings,
        "ph_alert": {
            "available": fir_available,
            "track_crosses_ph": track_crosses_ph,
            "proxy_crosses_ph": proxy_crosses_ph,
            "monitor_closely": bool(fir_available and (track_crosses_ph or proxy_crosses_ph)),
            "reason": (
                "Manila FIR boundary unavailable" if not fir_available else
                ("Ground track crosses the Manila FIR during this TIP window" if track_crosses_ph else
                 ("One or more proxy re-entry locations falls inside the Manila FIR" if proxy_crosses_ph else
                  "No Manila FIR crossing detected in this TIP window"))
            ),
        },
        "track_preview": {
            "lats": [float(x) for x in track_lats],
            "lons": [float(x) for x in track_lons],
        },
    }


def _build_development_history(
    sat: EarthSatellite,
    solutions_all: List[TipSolution],
    fallback_unc_min: float,
    bstar_stats: Dict[str, float],
    f107: float,
    f107a: float,
    use_bstar: bool,
    mc_samples: int,
    max_batches: int = 12,
) -> List[Dict[str, Any]]:
    grouped = {}
    for sol in solutions_all:
        if sol.msg_epoch:
            grouped.setdefault(sol.msg_epoch, []).append(sol)
    if not grouped:
        return []

    def _ep_key(ep: str):
        try:
            return parse_any_datetime_utc(ep)
        except Exception:
            return dt.datetime.min.replace(tzinfo=dt.timezone.utc)

    ordered_epochs = sorted(grouped.keys(), key=_ep_key)
    if max_batches and len(ordered_epochs) > max_batches:
        ordered_epochs = ordered_epochs[-max_batches:]

    out = []
    for ep in ordered_epochs:
        try:
            pred = _predict_single_tip_batch(
                sat=sat,
                batch=grouped[ep],
                fallback_unc_min=fallback_unc_min,
                bstar_stats=bstar_stats,
                f107=f107,
                f107a=f107a,
                use_bstar=use_bstar,
                mc_samples=mc_samples,
                step_seconds=30,
            )
            if pred:
                out.append(pred)
        except Exception:
            continue
    return out


def render_reentry_development_animation(history: List[Dict[str, Any]], object_name: str) -> None:
    """Smooth presentation animation with fixed trace topology.

    Important design rules:
      1) exactly ONE real ground track is displayed per frame;
      2) the ground track is never geometrically interpolated between TIP updates;
      3) every frame always contains the object + P10/P50/P90 markers;
      4) every frame has the same number/order of Plotly traces, preventing
         stale traces from accumulating during redraw=True animation;
      5) Manila FIR is the PH monitoring boundary.
    """
    if not history:
        return

    fir_segments = _load_manila_fir_segments()
    if not fir_segments:
        st.warning(
            "Manila FIR boundary could not be loaded. The animation will not use a rectangular PH fallback. "
            "Place `Manila_FIR_boundary.shp` in the same `utils/shapefiles` directory used by rocket.py, "
            "or set `REENTRY_MANILA_FIR_SHP`."
        )
        return

    # More samples = smoother motion along each real TIP ground track.
    object_frames_per_tip = 42
    proxy_ease_frames = 12

    prepared = []
    for item in history:
        imp = item.get("impact_proxy", {})
        if not all(k in imp and "lat" in imp[k] and "lon" in imp[k] for k in ("p10", "p50", "p90")):
            continue
        track = item.get("track_preview", {})
        track_lats, track_lons = _resample_track(
            track.get("lats", []), track.get("lons", []), n_points=object_frames_per_tip
        )
        if not track_lats or not track_lons:
            continue
        prepared.append({
            "msg_epoch": item.get("msg_epoch_utc", ""),
            "tip_window": item.get("tip_window", {}),
            "mc": item.get("monte_carlo", {}),
            "imp": imp,
            "track_lats": track_lats,
            "track_lons": track_lons,
            "ph_alert": item.get("ph_alert", {}),
        })

    if not prepared:
        return

    fir_lat, fir_lon = _merge_geo_segments(fir_segments)

    def _single_track_arrays(lats, lons):
        # One trace only, with None separators at the antimeridian.
        return _merge_geo_segments(split_dateline_segments(lats, lons))

    def _proxy_ll(prep, key):
        p = prep["imp"][key]
        return float(p["lat"]), float(p["lon"])

    def _ease(a: float) -> float:
        # Smoothstep: zero slope at the start/end of the proxy transition.
        a = float(np.clip(a, 0.0, 1.0))
        return a * a * (3.0 - 2.0 * a)

    def _make_frame_traces(prep, obj_lat, obj_lon, p10_ll, p50_ll, p90_ll, pulse=False):
        tr_lat, tr_lon = _single_track_arrays(prep["track_lats"], prep["track_lons"])
        mc = prep["mc"]

        # FIXED trace order in every frame:
        # 0 FIR, 1 ground track, 2 object, 3 P10, 4 P50, 5 P90, 6 pulse
        return [
            go.Scattergeo(
                lat=fir_lat, lon=fir_lon, mode="lines", name="Manila FIR",
                line=dict(color="#FFA500", width=2.0), hoverinfo="skip", showlegend=True,
            ),
            go.Scattergeo(
                lat=tr_lat, lon=tr_lon, mode="lines", name="Active nominal ground track",
                line=dict(color="#FFD700", width=2.5), hoverinfo="skip", showlegend=True,
            ),
            go.Scattergeo(
                lat=[obj_lat], lon=[obj_lon], mode="markers", name=object_name,
                marker=dict(size=12, color="white", line=dict(color="#FFD700", width=2.2)),
                hovertemplate=(f"{object_name}<br>Lat: %{{lat:.3f}}°<br>Lon: %{{lon:.3f}}°<extra></extra>"),
                showlegend=True,
            ),
            go.Scattergeo(
                lat=[p10_ll[0]], lon=[p10_ll[1]], mode="markers+text", name="P10 Early",
                marker=dict(size=8, color="#29b6f6", line=dict(color="white", width=1.1)),
                text=["P10"], textposition="top right", textfont=dict(color="#29b6f6", size=10),
                customdata=[[mc.get("p10_utc", "")]],
                hovertemplate="P10 Early<br>Predicted time: %{customdata[0]}<br>Lat: %{lat:.3f}°<br>Lon: %{lon:.3f}°<extra></extra>",
                showlegend=True,
            ),
            go.Scattergeo(
                lat=[p50_ll[0]], lon=[p50_ll[1]], mode="markers+text", name="P50 Best",
                marker=dict(size=11, color="#f44336", line=dict(color="white", width=1.2)),
                text=["P50"], textposition="top right", textfont=dict(color="#f44336", size=10),
                customdata=[[mc.get("p50_utc", "")]],
                hovertemplate="P50 Best<br>Predicted time: %{customdata[0]}<br>Lat: %{lat:.3f}°<br>Lon: %{lon:.3f}°<extra></extra>",
                showlegend=True,
            ),
            go.Scattergeo(
                lat=[p90_ll[0]], lon=[p90_ll[1]], mode="markers+text", name="P90 Late",
                marker=dict(size=8, color="#66bb6a", line=dict(color="white", width=1.1)),
                text=["P90"], textposition="top right", textfont=dict(color="#66bb6a", size=10),
                customdata=[[mc.get("p90_utc", "")]],
                hovertemplate="P90 Late<br>Predicted time: %{customdata[0]}<br>Lat: %{lat:.3f}°<br>Lon: %{lon:.3f}°<extra></extra>",
                showlegend=True,
            ),
            go.Scattergeo(
                lat=[p50_ll[0]], lon=[p50_ll[1]], mode="markers", name="P50 focus",
                marker=dict(
                    size=(22 if pulse else 0), color="rgba(255,255,255,0.04)",
                    line=dict(color="#f44336", width=3)
                ),
                hoverinfo="skip", showlegend=False,
            ),
        ]

    frames = []
    slider_steps = []
    frame_index = 0

    for idx, prep in enumerate(prepared):
        cur10 = _proxy_ll(prep, "p10")
        cur50 = _proxy_ll(prep, "p50")
        cur90 = _proxy_ll(prep, "p90")

        if idx == 0:
            prev10, prev50, prev90 = cur10, cur50, cur90
        else:
            prev = prepared[idx - 1]
            prev10 = _proxy_ll(prev, "p10")
            prev50 = _proxy_ll(prev, "p50")
            prev90 = _proxy_ll(prev, "p90")

        ph_alert = prep.get("ph_alert", {})
        fir_available = ph_alert.get("available", True)
        alert_on = bool(ph_alert.get("monitor_closely"))
        alert_color = "#ff5c5c" if alert_on else ("#ffb74d" if not fir_available else "#d8dde6")
        alert_label = (
            "Manila FIR unavailable" if not fir_available else
            ("MONITOR CLOSELY" if alert_on else "No Manila FIR alert")
        )
        ann_text = (
            f"TIP MSG_EPOCH: {prep['msg_epoch']}<br>"
            f"Window: {prep['tip_window'].get('start_utc','')} → {prep['tip_window'].get('end_utc','')}<br>"
            f"Status: {alert_label} — {ph_alert.get('reason','')}"
        )

        # The ground track is always the ACTUAL track for this TIP update.
        # Only the proxy markers ease from the previous solution into the new solution.
        for j, (obj_lat, obj_lon) in enumerate(zip(prep["track_lats"], prep["track_lons"])):
            if idx > 0 and j < proxy_ease_frames:
                a = _ease((j + 1) / float(proxy_ease_frames))
                p10_ll = _interp_latlon(prev10[0], prev10[1], cur10[0], cur10[1], a)
                p50_ll = _interp_latlon(prev50[0], prev50[1], cur50[0], cur50[1], a)
                p90_ll = _interp_latlon(prev90[0], prev90[1], cur90[0], cur90[1], a)
            else:
                p10_ll, p50_ll, p90_ll = cur10, cur50, cur90

            traces = _make_frame_traces(
                prep, obj_lat, obj_lon, p10_ll, p50_ll, p90_ll,
                pulse=(j in (proxy_ease_frames - 1, len(prep["track_lats"]) // 2)),
            )
            fname = f"tipframe_{frame_index:04d}"
            frames.append(go.Frame(
                data=traces,
                traces=[0, 1, 2, 3, 4, 5, 6],
                name=fname,
                layout=go.Layout(
                    annotations=[dict(
                        text=ann_text, xref="paper", yref="paper", x=0.5, y=1.065,
                        showarrow=False, align="center", font=dict(color=alert_color, size=11),
                    )]
                ),
            ))

            if j == 0:
                slider_steps.append(dict(
                    args=[[fname], dict(
                        frame=dict(duration=0, redraw=True),
                        mode="immediate", transition=dict(duration=0),
                    )],
                    label=(prep["msg_epoch"][5:16] if len(prep["msg_epoch"]) >= 16 else prep["msg_epoch"]),
                    method="animate",
                ))
            frame_index += 1

    if not frames:
        return

    first = frames[0]
    fig = go.Figure(
        data=first.data,
        frames=frames,
        layout=go.Layout(
            title=None,
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color="#e0e0e0"),
            geo=dict(
                projection_type="equirectangular",
                showland=True, landcolor="#dde6d8",
                showocean=True, oceancolor="#dce8f3",
                showcoastlines=True, coastlinecolor="#7a8ea3",
                showcountries=True, countrycolor="#b0b9c2",
                showframe=False, bgcolor="rgba(0,0,0,0)",
                lataxis=dict(showgrid=True, gridcolor="rgba(120,120,120,0.18)"),
                lonaxis=dict(showgrid=True, gridcolor="rgba(120,120,120,0.18)"),
            ),
            legend=dict(
                bgcolor="rgba(10,20,50,0.88)", bordercolor="#1a4fa3",
                font=dict(color="#e0e0e0"), x=0.01, y=0.99,
            ),
            margin=dict(l=0, r=0, t=82, b=120), height=650,
            updatemenus=[dict(
                type="buttons", showactive=False, x=0.5, xanchor="center", y=-0.16, yanchor="top",
                buttons=[
                    dict(label="▶  Play", method="animate", args=[None, dict(
                        frame=dict(duration=85, redraw=True), fromcurrent=True,
                        transition=dict(duration=0), mode="immediate",
                    )]),
                    dict(label="⏸  Pause", method="animate", args=[[None], dict(
                        frame=dict(duration=0, redraw=False), mode="immediate", transition=dict(duration=0),
                    )]),
                ],
                font=dict(color="#e0e0e0"), bgcolor="rgba(14,34,75,0.88)", bordercolor="#1a4fa3",
            )],
            sliders=[dict(
                active=0,
                currentvalue=dict(prefix="TIP update: ", font=dict(color="#e0e0e0", size=12), xanchor="center"),
                pad=dict(t=50, b=10), steps=slider_steps, font=dict(color="#e0e0e0"),
                bgcolor="rgba(14,34,75,0.88)", bordercolor="#1a4fa3", tickcolor="#e0e0e0",
            )],
            annotations=first.layout.annotations,
        ),
    )

    st.markdown(
        "<div style='font-weight:600;color:var(--text-color);font-size:0.9rem;margin:16px 0 0;'>"
        "Animated Development of Predicted Proxy Re-entry Locations</div>", unsafe_allow_html=True,
    )

    st.plotly_chart(fig, use_container_width=True, key="reentry_development_animation")


def render_development_summary(history: List[Dict[str, Any]]) -> None:
    if not history:
        return
    first = history[0]
    latest = history[-1]
    first_alert_info = first.get("ph_alert") or {}
    latest_alert_info = latest.get("ph_alert") or {}
    first_alert = first_alert_info.get("monitor_closely")
    latest_alert = latest_alert_info.get("monitor_closely")
    first_fir_available = first_alert_info.get("available", True)
    latest_fir_available = latest_alert_info.get("available", True)
    first_reason = (first.get("ph_alert") or {}).get("reason", "N/A")
    latest_reason = (latest.get("ph_alert") or {}).get("reason", "N/A")
    latest_p50 = (latest.get("impact_proxy") or {}).get("p50", {})

    st.markdown(
        "<div style='font-weight:600;color:var(--text-color);font-size:0.9rem;margin:14px 0 6px;'>"
        "Prediction Development Summary</div>",
        unsafe_allow_html=True,
    )
    st.markdown(f"""
    <div style='background:var(--background-color);border:1px solid rgba(128,128,128,0.3);border-radius:10px;
                padding:12px 16px;font-size:0.85rem;line-height:1.9;'>
        <b style='color:var(--text-color);opacity:0.6;'>First TIP update:</b>
        <span style='color:var(--text-color);'>{first.get('msg_epoch_utc','N/A')}</span><br>
        <b style='color:var(--text-color);opacity:0.6;'>Initial Manila FIR status:</b>
        <span style='color:{"#f44336" if first_alert else "var(--text-color)"};font-weight:{"700" if first_alert else "400"};'>
            {"Manila FIR unavailable" if not first_fir_available else ("Monitor closely" if first_alert else "No Manila FIR alert")}
        </span>
        <span style='color:var(--text-color);opacity:0.7;'> — {first_reason}</span><br>
        <b style='color:var(--text-color);opacity:0.6;'>Latest TIP update:</b>
        <span style='color:var(--text-color);'>{latest.get('msg_epoch_utc','N/A')}</span><br>
        <b style='color:var(--text-color);opacity:0.6;'>Latest Manila FIR status:</b>
        <span style='color:{"#f44336" if latest_alert else "var(--text-color)"};font-weight:{"700" if latest_alert else "400"};'>
            {"Manila FIR unavailable" if not latest_fir_available else ("Monitor closely" if latest_alert else "No Manila FIR alert")}
        </span>
        <span style='color:var(--text-color);opacity:0.7;'> — {latest_reason}</span><br>
        <b style='color:var(--text-color);opacity:0.6;'>Latest P50 proxy:</b>
        <span style='color:var(--text-color);'>
            lat {round(float(latest_p50.get('lat', 0.0)), 3) if 'lat' in latest_p50 else 'N/A'}°,
            lon {round(float(latest_p50.get('lon', 0.0)), 3) if 'lon' in latest_p50 else 'N/A'}°
        </span>
    </div>""", unsafe_allow_html=True)


def render():


    # ── initialise session state ──────────────────────────────────────────────
    _ss("sat",          None)
    _ss("window_min",   None)
    _ss("window_max",   None)
    _ss("window_mode",  "none")
    _ss("solutions_all",    [])
    _ss("solutions_latest", [])
    _ss("tle",          None)
    _ss("tle_hist",     [])
    _ss("bstar_stats",  {})
    _ss("pred",         {})
    _ss("log",          [])
    _ss("tip_raw",      None)
    _ss("orbital_fallback", False)
    _ss("orbital_basis", "")
    _ss("orbital_epoch", None)

    def log(msg):
        ts = dt.datetime.now().strftime("%H:%M:%S")
        st.session_state["re_pred_log"].append(f"[{ts}] {msg}")

    # ═══════════════════════════════════════════════
    # INPUTS
    # ═══════════════════════════════════════════════
    with st.expander("⚙️ Parameters", expanded=True):
        c1, c2, c3 = st.columns(3)
        with c1:
            norad_input = st.text_input(
                "NORAD CAT ID",
                value=st.session_state.get("re_pred_norad_input", "66877"),
                key="re_pred_norad_input",
                placeholder="e.g. 66877",
            )
            tip_url_override = st.text_input(
                "TIP URL override (optional)",
                value=st.session_state.get("re_pred_tip_url", ""),
                key="re_pred_tip_url",
                placeholder="Leave blank for default Space-Track query",
            )
        with c2:
            before_min  = st.number_input("Window Before (min)", value=90,  min_value=1,   max_value=360, key="re_pred_before")
            after_min   = st.number_input("After (min)",          value=90,  min_value=1,   max_value=360, key="re_pred_after")
            step_s      = st.number_input("Step (sec)",           value=30,  min_value=5,   max_value=120, key="re_pred_step")
            mid_tracks  = st.number_input("Intermediate tracks",  value=5,   min_value=0,   max_value=20,  key="re_pred_mid")
        with c3:
            mc_samples       = st.number_input("MC samples",              value=2000, min_value=200,  max_value=50000, key="re_pred_mc")
            swath_km         = st.number_input("Swath ±km (KML)",         value=100,  min_value=10,   max_value=500,   key="re_pred_swath")
            fallback_unc_min = st.number_input("Fallback uncertainty (min)", value=48, min_value=1,   max_value=360,   key="re_pred_fallback")
            f107  = st.number_input("F10.7",  value=150.0, min_value=60.0, max_value=300.0, step=1.0, key="re_pred_f107")
            f107a = st.number_input("F10.7a", value=150.0, min_value=60.0, max_value=300.0, step=1.0, key="re_pred_f107a")

        uploaded_omm = st.file_uploader("OMM XML fallback (optional)", type=["xml"], key="re_pred_omm_xml")
        fallback_scan_hours = st.number_input("No-TIP altitude scan (hours)", value=6.0, min_value=0.5, max_value=12.0, step=0.5, key="re_pred_scan_hours")
        st.caption("No TIP: P10/P50/P90 are scenario quantiles based on an assumed time window, not validated decay probabilities.")
        use_bstar   = st.checkbox("Use sequential TLE B* bias", value=True, key="re_pred_bstar")
        ph_focus    = st.checkbox("Philippines focus (map zoom)", value=False, key="re_pred_ph_focus")

    # ── Tip selector (shown after fetch) ─────────────────────────────────────
    solutions_all = st.session_state["re_pred_solutions_all"]
    if solutions_all:
        epochs_seen = []
        seen_set = set()
        for s in solutions_all:
            ep = s.msg_epoch
            if ep and ep not in seen_set:
                seen_set.add(ep)
                batch = [x for x in solutions_all if x.msg_epoch == ep]
                decays = []
                for b in batch:
                    try:
                        decays.append(parse_any_datetime_utc(b.decay_epoch))
                    except Exception:
                        pass
                if decays:
                    w_field = batch[0].raw.get("WINDOW", "?")
                    decay_str = dt_to_iso_z(min(decays) + (max(decays) - min(decays)) / 2)
                    epochs_seen.append(f"{ep}  →  decay~{decay_str}  W={w_field}min")
                else:
                    epochs_seen.append(ep)

        tip_options = ["(latest)"] + epochs_seen
        selected_tip = st.selectbox(
            "Predict using TIP MSG_EPOCH",
            tip_options,
            index=0,
            key="re_pred_tip_selector",
        )
        if selected_tip != "(latest)":
            ep_key = selected_tip.split("  ")[0].strip()
            batch = [s for s in solutions_all if s.msg_epoch == ep_key]
            if batch:
                wmin, wmax, _, mode = compute_tip_window_from_latest_batch(batch, float(fallback_unc_min))
                _set("window_min",  wmin)
                _set("window_max",  wmax)
                _set("window_mode", mode)

    # ═══════════════════════════════════════════════
    # ACTION BUTTONS
    # ═══════════════════════════════════════════════
    btn_col1, btn_col2, btn_col3, btn_col4, btn_col5 = st.columns(5)

    fetch_clicked      = btn_col1.button("Fetch TIP + TLE",          use_container_width=True, key="re_btn_fetch")
    plot_clicked       = btn_col2.button("Plot Envelope",             use_container_width=True, key="re_btn_plot")
    predict_clicked    = btn_col3.button("Run Prediction",            use_container_width=True, key="re_btn_predict")
    report_clicked     = btn_col4.button("Generate Report",           use_container_width=True, key="re_btn_report")
    kml_clicked        = btn_col5.button("Export KML",                use_container_width=True, key="re_btn_kml")

    # ═══════════════════════════════════════════════
    # FETCH
    # ═══════════════════════════════════════════════
    if fetch_clicked:
        try:
            norad_id = int(norad_input.strip())
        except ValueError:
            st.error("NORAD CAT ID must be a number.")
            st.stop()

        user, pw = _get_spacetrack_creds()
        with st.spinner("Logging in to Space-Track…"):
            try:
                session = spacetrack_login(user, pw)
            except Exception as e:
                st.error(f"Login failed: {e}")
                st.stop()

        log("Logged in to Space-Track.")

        tip_raw = []
        try:
            with st.spinner(f"Fetching TIP for NORAD {norad_id}…"):
                tip_raw = fetch_tip(session, norad_id, tip_url_override.strip(), limit=DEFAULT_TIP_LIMIT)
        except Exception as exc:
            log(f"TIP unavailable: {exc}; attempting OMM/TLE fallback")
        solutions_all = parse_tip_solutions(tip_raw)
        solutions_latest = select_latest_tip_batch(solutions_all)
        valid_tip = any(s.decay_epoch for s in solutions_latest)
        log(f"TIP records: {len(solutions_all)}, valid decay time: {valid_tip}")
        candidate_rows, fetch_errors = _fetch_orbital_candidates(session,norad_id)
        for issue in fetch_errors:
            log(f"Orbital source warning: {issue}")
        if uploaded_omm:
            try:
                candidate_rows.append(_parse_omm_xml(uploaded_omm.getvalue()))
            except Exception as exc:
                st.error(f"OMM XML invalid: {exc}")
                st.stop()
        target = None
        if valid_tip:
            try:
                target=parse_any_datetime_utc(solutions_latest[0].decay_epoch)
            except Exception:
                pass
        if not candidate_rows:
            st.error("No orbital data found. Upload a valid OMM XML or verify Space-Track GP_HISTORY access.")
            st.stop()
        try:
            orbit_epoch,sat,orbit_row = _select_orbital_candidate(candidate_rows,target)
        except Exception as exc:
            st.error(f"No usable SGP4 orbital elements: {exc}")
            st.stop()
        name=sat.name
        l1 = orbit_row.get("TLE_LINE1", "")
        l2 = orbit_row.get("TLE_LINE2", "")
        log(f"Loaded {orbit_row.get('_fetch_class')} orbital state, epoch {dt_to_iso_z(orbit_epoch)}")
        bstar_stats = {"bstar_med": float("nan"), "bstar_mad": float("nan"), "bstar_trend_per_day": float("nan")}
        tle_hist = []
        if use_bstar:
            with st.spinner("Fetching TLE history for B* bias…"):
                try:
                    tle_hist = fetch_tle_history(session, norad_id, limit=25)
                    if tle_hist:
                        bstar_stats = robust_bstar_stats(tle_hist)
                        log(
                            f"B* stats: median={bstar_stats.get('bstar_med', float('nan')):.3e} "
                            f"MAD={bstar_stats.get('bstar_mad', float('nan')):.3e} "
                            f"trend/day={bstar_stats.get('bstar_trend_per_day', float('nan')):.3e}"
                        )
                except Exception as e:
                    log(f"TLE history fetch failed (non-fatal): {e}")

        wmin, wmax, _, mode = compute_tip_window_from_latest_batch(
            solutions_latest, float(fallback_unc_min)
        )
        fallback = not (wmin and wmax)
        basis = ""
        if fallback:
            try:
                wmin,wmax,nominal,basis=_assumed_decay_window(sat,float(fallback_unc_min),float(fallback_scan_hours))
                mode="ommtle_assumed_uniform_scenario"
                log(f"No TIP decay epoch; scenario centered on {basis} at {dt_to_iso_z(nominal)}")
            except Exception as exc:
                st.error(f"OMM/TLE fallback failed: {exc}")
                st.stop()
        latest_epoch = solutions_latest[0].msg_epoch if solutions_latest else "No TIP"
        if wmin and wmax:
            width = wmax - wmin
            log(f"Latest TIP MSG_EPOCH: {latest_epoch} (batch rows: {len(solutions_latest)})")
            log(f"Decay window: {dt_to_iso_z(wmin)} → {dt_to_iso_z(wmax)} (width {fmt_timedelta(width)})")
            log(f"Window mode: {mode}")
        else:
            log(f"Latest TIP MSG_EPOCH: {latest_epoch} — no valid DECAY_EPOCH in latest batch.")

        _set("tip_raw",          tip_raw)
        _set("solutions_all",    solutions_all)
        _set("solutions_latest", solutions_latest)
        _set("sat",              sat)
        _set("tle",              (name, l1, l2))
        _set("tle_hist",         tle_hist)
        _set("bstar_stats",      bstar_stats)
        _set("window_min",       wmin)
        _set("window_max",       wmax)
        _set("window_mode",      mode)
        _set("pred",             {})
        _set("orbital_fallback", fallback)
        _set("orbital_basis", basis)
        _set("orbital_epoch", orbit_epoch)
        disp_norad, disp_name = _demo_display(norad_id, name)   # display-only, real norad_id/name used above
        st.success(f"Orbital data ready for NORAD {disp_norad} — {disp_name}")
        if fallback:
            st.warning("No usable TIP decay epoch. Running OMM/TLE altitude-based SCENARIO only; times/locations are not validated re-entry predictions.")

    # ═══════════════════════════════════════════════
    # CURRENT STATE DISPLAY
    # ═══════════════════════════════════════════════
    sat        = st.session_state["re_pred_sat"]
    wmin       = st.session_state["re_pred_window_min"]
    wmax       = st.session_state["re_pred_window_max"]
    wmode      = st.session_state["re_pred_window_mode"]
    pred       = st.session_state["re_pred_pred"]
    bstar_stats = st.session_state["re_pred_bstar_stats"]

    if sat and wmin and wmax:
        width = wmax - wmin
        _disp_norad, _disp_sat_name = _demo_display(
            st.session_state.get("re_pred_norad_input", ""), sat.name
        )
        st.markdown(f"""
        <div style='background:var(--background-color);border:1px solid rgba(128,128,128,0.3);border-radius:10px;
                    padding:14px 18px;margin:10px 0;font-size:0.87rem;'>
            <b style='color:var(--text-color);'>Satellite:</b>
            <span style='color:var(--text-color);opacity:0.7;'>{_disp_sat_name}</span> &nbsp;|&nbsp;
            <b style='color:var(--text-color);'>Window:</b>
            <span style='color:var(--text-color);opacity:0.7;'>{dt_to_iso_z(wmin)}</span> →
            <span style='color:var(--text-color);opacity:0.7;'>{dt_to_iso_z(wmax)}</span>
            &nbsp;(<span style='color:var(--text-color);opacity:0.7;'>{fmt_timedelta(width)}</span>)
            &nbsp;|&nbsp;<b style='color:var(--text-color);'>Mode:</b>
            <span style='color:var(--text-color);opacity:0.7;'>{wmode}</span>
        </div>
        """, unsafe_allow_html=True)

    # ═══════════════════════════════════════════════
    # PLOT ENVELOPE
    # ═══════════════════════════════════════════════
    if plot_clicked:
        if not (sat and wmin and wmax):
            st.warning("Fetch TIP + TLE first.")
        else:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import cartopy.crs as ccrs
            import cartopy.feature as cfeature

            width_td = wmax - wmin
            wmid = wmin + (width_td / 2)

            # Theme colours for the cartopy map — must be real hex since
            # matplotlib renders to a PNG image (no CSS variables here).
            try:
                _base = st.get_option("theme.base") or "light"
            except Exception:
                _base = "light"
            _dark = _base.lower() == "dark"
            _map_bg     = "#0a0f1c" if _dark else "#e8eef4"
            _ocean_col  = "#0d1b2a" if _dark else "#c8daea"
            _land_col   = "#1e2a44" if _dark else "#dde8d0"
            _border_col = "#3a5070" if _dark else "#aaaaaa"
            _coast_col  = "#5a7a9a" if _dark else "#555555"
            _grid_col   = "#2a4060" if _dark else "#cccccc"
            _title_col  = "#e0e0e0" if _dark else "#0e224b"

            fig = plt.Figure(figsize=(12, 5.5), dpi=110, facecolor=_map_bg)
            ax  = fig.add_subplot(1, 1, 1, projection=ccrs.PlateCarree())
            ax.set_facecolor(_map_bg)
            ax.coastlines(linewidth=0.8, color=_coast_col)
            ax.add_feature(cfeature.BORDERS, linewidth=0.4, edgecolor=_border_col)
            ax.add_feature(cfeature.LAND,  facecolor=_land_col,  alpha=0.6)
            ax.add_feature(cfeature.OCEAN, facecolor=_ocean_col, alpha=0.6)
            ax.gridlines(draw_labels=False, linewidth=0.4, color=_grid_col, alpha=0.6)
            if ph_focus:
                ax.set_extent([115, 130, 4, 22], crs=ccrs.PlateCarree())
            else:
                ax.set_global()

            # Preserve the PREVIOUS envelope map and its overlay structure.
            # Only change the ground-track line colors to altitude bands.
            n_mid = int(mid_tracks)
            if n_mid > 0 and width_td.total_seconds() > 0:
                for i in range(1, n_mid + 1):
                    frac = i / (n_mid + 1)
                    t_i = wmin + dt.timedelta(seconds=width_td.total_seconds() * frac)
                    _plot_envelope_altitude_track(
                        ax, sat, t_i, before_min, after_min, step_s,
                        linewidth=0.9, alpha=0.35, linestyle="--", zorder=2
                    )

            for t_edge in (wmin, wmax):
                _plot_envelope_altitude_track(
                    ax, sat, t_edge, before_min, after_min, step_s,
                    linewidth=1.8, alpha=0.95, zorder=3
                )

            _plot_envelope_altitude_track(
                ax, sat, wmid, before_min, after_min, step_s,
                linewidth=2.2, alpha=1.0, linestyle=":", zorder=4
            )

            # Exact existing FIR outline, not a Philippines bounding box.
            fir_segs = _load_manila_fir_segments()
            if fir_segs:
                for fir_lats, fir_lons in fir_segs:
                    ax.plot(fir_lons, fir_lats, color="#00cfe8", linewidth=1.5,
                            alpha=0.9, transform=ccrs.PlateCarree(), zorder=5)

            from matplotlib.lines import Line2D
            altitude_bands = [
                ("> 200 km", "#e8e8e8"),
                ("160–200 km", "#45cf52"),
                ("140–160 km", "#ffda48"),
                ("120–140 km", "#fa493e"),
                ("≤ 120 km", "#161616"),
            ]
            legend_handles = [Line2D([0], [0], color=color, lw=3, label=label)
                              for label, color in altitude_bands]
            if fir_segs:
                legend_handles.append(Line2D([0], [0], color="#00cfe8", lw=2,
                                             label="Manila FIR"))
            legend_handles += [
                Line2D([0], [0], color="#89919a", lw=1.8, ls="-", label="Window edges"),
                Line2D([0], [0], color="#89919a", lw=2.2, ls=":", label="Window midpoint"),
                Line2D([0], [0], color="#89919a", lw=0.9, ls="--", label="Intermediate tracks"),
            ]
            legend = ax.legend(handles=legend_handles, loc="lower left", fontsize=7,
                               framealpha=0.92, ncol=2)
            legend.get_frame().set_facecolor("#f7f7f7")

            # overlay prediction points if available
            if pred:
                imp = pred.get("impact_proxy", {})
                mc  = pred.get("monte_carlo", {})
                for pct, color, label in (("p10","#29b6f6","P10"), ("p50","#f44336","P50"), ("p90","#66bb6a","P90")):
                    p = imp.get(pct, {})
                    if "lat" in p and "lon" in p:
                        ax.plot([float(p["lon"])], [float(p["lat"])], marker="o",
                                markersize=8 if pct=="p50" else 6,
                                color=color, transform=ccrs.PlateCarree(), zorder=5)
                        ax.text(float(p["lon"]) + 2, float(p["lat"]) + 2, label,
                                transform=ccrs.PlateCarree(), fontsize=9, color=color, zorder=6)

            latest_epoch = st.session_state["re_pred_solutions_latest"][0].msg_epoch \
                           if st.session_state["re_pred_solutions_latest"] else ""
            ax.set_title(
                f"{sat.name} — TIP MSG_EPOCH: {latest_epoch}\n"
                f"Decay window: {dt_to_iso_z(wmin)} → {dt_to_iso_z(wmax)} "
                f"(width {fmt_timedelta(wmax - wmin)}) | mode={wmode}",
                fontsize=9, color=_title_col,
            )

            buf = io.BytesIO()
            fig.savefig(buf, format="png", bbox_inches="tight", dpi=130,
                        facecolor=_map_bg)
            buf.seek(0)
            _set("envelope_png", buf.getvalue())
            st.image(buf, use_container_width=True)
            plt.close(fig)
            log("Envelope plotted with altitude-colored tracks.")

    if not plot_clicked and st.session_state.get("re_pred_envelope_png"):
        st.image(st.session_state["re_pred_envelope_png"], use_container_width=True)

    # ═══════════════════════════════════════════════
    # RUN PREDICTION
    # ═══════════════════════════════════════════════
    if predict_clicked:
        if not (sat and wmin and wmax):
            st.warning("Fetch TIP + TLE first.")
        elif pymsis_msis is None and not st.session_state.get("re_pred_orbital_fallback", False):
            st.error("pymsis is required. Install: `pip install pymsis`")
        elif st.session_state.get("re_pred_orbital_fallback", False):
            try:
                new_pred = _fallback_prediction(
                    sat,wmin,wmax,int(mc_samples),int(norad_input.strip()),wmode,
                    st.session_state.get("re_pred_orbital_basis", "unknown")
                )
                fir_geom=_load_manila_fir_geometry()
                if fir_geom is not None:
                    try:
                        fl,fo,ft=_track_over_window(sat,wmin,wmax,step_seconds=15)
                        new_pred["ph_crossings"]=_summarize_ph_crossings_from_track(fl,fo,ft)
                    except Exception as exc:
                        log(f"FIR screening failed: {exc}")
                _set("pred",new_pred)
                st.warning("Scenario P10/P50/P90 computed from an assumed uniform decay-time window; displayed locations are orbital subpoints, NOT debris impacts.")
                log("OMM/TLE scenario outputs calculated")
            except Exception as exc:
                st.error(f"OMM/TLE scenario failed: {exc}")
        else:
            width_s = (wmax - wmin).total_seconds()
            if width_s <= 0:
                st.error("Zero-width window — increase 'Fallback uncertainty (min)' and Fetch again.")
            else:
                with st.spinner("Running Monte Carlo prediction…"):
                    wmid_t = wmin + dt.timedelta(seconds=width_s / 2)
                    kp_mean = fetch_noaa_kp_1m_mean_near(wmid_t, hours_window=12)
                    ap = kp_to_ap_proxy(kp_mean) if kp_mean is not None else 10.0

                    bstar_med = bstar_stats.get("bstar_med", float("nan")) if use_bstar else float("nan")
                    bstats    = bstar_stats if use_bstar else {}

                    n_mc = max(200, min(50000, int(mc_samples)))
                    ts_samples = mc_sample_times_with_bias(wmin, wmax, bstats, kp_mean, n=n_mc)

                    p10 = dt.datetime.fromtimestamp(float(np.percentile(ts_samples, 10)), tz=dt.timezone.utc)
                    p50 = dt.datetime.fromtimestamp(float(np.percentile(ts_samples, 50)), tz=dt.timezone.utc)
                    p90 = dt.datetime.fromtimestamp(float(np.percentile(ts_samples, 90)), tz=dt.timezone.utc)

                    lat10, lon10, meta10 = impact_proxy_from_time(sat, p10, f107, f107a, ap, bstar_med)
                    lat50, lon50, meta50 = impact_proxy_from_time(sat, p50, f107, f107a, ap, bstar_med)
                    lat90, lon90, meta90 = impact_proxy_from_time(sat, p90, f107, f107a, ap, bstar_med)

                    new_pred = {
                        "created_utc":  dt_to_iso_z(dt.datetime.now(dt.timezone.utc)),
                        "norad_id":     int(norad_input.strip()) if norad_input.strip().isdigit() else None,
                        "tip_window":   {"start_utc": dt_to_iso_z(wmin), "end_utc": dt_to_iso_z(wmax),
                                         "width_sec": int(width_s), "mode": wmode},
                        "noaa_kp_mean_1m_near_mid": kp_mean,
                        "ap_proxy_used":   ap,
                        "f107_used":       f107,
                        "f107a_used":      f107a,
                        "bstar_used_median": bstar_med if np.isfinite(bstar_med) else None,
                        "monte_carlo":  {"n": n_mc, "p10_utc": dt_to_iso_z(p10),
                                         "p50_utc": dt_to_iso_z(p50), "p90_utc": dt_to_iso_z(p90)},
                        "impact_proxy": {
                            "p10": {"lat": lat10, "lon": lon10, "meta": meta10},
                            "p50": {"lat": lat50, "lon": lon50, "meta": meta50},
                            "p90": {"lat": lat90, "lon": lon90, "meta": meta90},
                        },
                    }

                    # Uncertainty summary
                    uncert_half_s = (p90 - p10).total_seconds() / 2.0
                    new_pred["uncertainty"] = {
                        "p10_utc": dt_to_iso_z(p10), "p50_utc": dt_to_iso_z(p50),
                        "p90_utc": dt_to_iso_z(p90),
                        "interval_80pct_min": round((p90 - p10).total_seconds() / 60.0, 1),
                        "half_width_min": round(uncert_half_s / 60.0, 1),
                        "half_width_hr":  round(uncert_half_s / 3600.0, 3),
                        "tip_window_total_min": round(width_s / 60.0, 1),
                    }

                    # Manila FIR crossings + overpass probability
                    fir_geom = _load_manila_fir_geometry()
                    if fir_geom is None:
                        log("Manila FIR shapefile unavailable — FIR crossing/probability calculations skipped.")
                    else:
                        try:
                            fir_lats, fir_lons, fir_times = _track_over_window(
                                sat, wmin, wmax, step_seconds=15
                            )
                            new_pred["ph_crossings"] = _summarize_ph_crossings_from_track(
                                fir_lats, fir_lons, fir_times
                            )

                            orbital_period_min = 2 * math.pi / (sat.model.no_kozai) * (1 / 60.0)
                            now_utc = dt.datetime.now(dt.timezone.utc)
                            scan_start = max(now_utc, wmin)
                            total_scan_s = max(1.0, (wmax - scan_start).total_seconds())
                            scan_lats, scan_lons, _ = groundtrack_corridor(
                                sat, scan_start + dt.timedelta(seconds=total_scan_s / 2),
                                int(total_scan_s / 120), int(total_scan_s / 120), 15
                            )
                            total_pts = len(scan_lats)
                            fir_pts = sum(1 for la, lo in zip(scan_lats, scan_lons) if _is_over_ph(la, lo))
                            fir_fraction = fir_pts / max(1, total_pts)
                            new_pred["ph_overpass_probability"] = {
                                "boundary": "Manila FIR",
                                "orbital_period_min": round(orbital_period_min, 2),
                                "remaining_window_min": round(total_scan_s / 60.0, 1),
                                "n_orbits_remaining": round(total_scan_s / (orbital_period_min * 60.0), 2),
                                "ph_pts": fir_pts, "total_pts": total_pts,
                                "ph_fraction_pct": round(fir_fraction * 100, 3),
                                "ph_time_per_orbit_s": round(fir_fraction * orbital_period_min * 60.0, 1),
                                "ph_total_time_in_window_s": round(fir_fraction * total_scan_s, 1),
                            }
                        except Exception as ph_err:
                            log(f"Manila FIR probability calc skipped: {ph_err}")

                    # Development history across multiple TIP MSG_EPOCH updates
                    try:
                        dev_history = _build_development_history(
                            sat=sat,
                            solutions_all=st.session_state.get("re_pred_solutions_all", []),
                            fallback_unc_min=float(fallback_unc_min),
                            bstar_stats=bstar_stats,
                            f107=float(f107),
                            f107a=float(f107a),
                            use_bstar=bool(use_bstar),
                            mc_samples=int(mc_samples),
                            max_batches=12,
                        )
                        new_pred["development_history"] = dev_history
                        if dev_history:
                            log(f"Development timeline built from {len(dev_history)} TIP update(s).")
                    except Exception as dev_err:
                        log(f"Development timeline build skipped: {dev_err}")

                    _set("pred", new_pred)
                    log(f"Prediction complete. P50={dt_to_iso_z(p50)} | lat={lat50:.3f}, lon={lon50:.3f}")
                    st.rerun()

    # ═══════════════════════════════════════════════
    # RESULTS DISPLAY
    # ═══════════════════════════════════════════════
    pred = st.session_state["re_pred_pred"]
    if pred:
        mc   = pred.get("monte_carlo", {})
        imp  = pred.get("impact_proxy", {})
        unc  = pred.get("uncertainty", {})
        ph_p = pred.get("ph_overpass_probability", {})

        st.markdown(
            "<div style='font-weight:700;color:var(--text-color);font-size:1rem;margin:16px 0 8px;'>"
            "Prediction Results</div>",
            unsafe_allow_html=True)

        if pred.get("prediction_disclaimer"):
            st.warning(pred["prediction_disclaimer"])
        # Timing
        mc_c1, mc_c2, mc_c3 = st.columns(3)
        for col, pct, label, color in (
            (mc_c1, "p10", "P10 (Early)", "#29b6f6"),
            (mc_c2, "p50", "P50 (Best)",  "#f44336"),
            (mc_c3, "p90", "P90 (Late)",  "#66bb6a"),
        ):
            utc_str = mc.get(f"{pct}_utc", "N/A")
            p_obj = imp.get(pct, {})
            lat_s = f"{float(p_obj['lat']):.3f}°" if "lat" in p_obj else "N/A"
            lon_s = f"{float(p_obj['lon']):.3f}°" if "lon" in p_obj else "N/A"
            try:
                ph_str = dt_to_iso_ph(parse_any_datetime_utc(utc_str)) if utc_str != "N/A" else "N/A"
            except Exception:
                ph_str = "N/A"
            col.markdown(f"""
            <div style='background:var(--background-color);border:1px solid rgba(128,128,128,0.3);border-radius:10px;
                        padding:14px;text-align:center;'>
                <div style='font-size:0.75rem;color:var(--text-color);opacity:0.6;text-transform:uppercase;
                            letter-spacing:.06em;margin-bottom:6px;'>{label}</div>
                <div style='font-size:1.05rem;font-weight:700;color:{color};'>{utc_str}</div>
                <div style='font-size:0.78rem;color:var(--text-color);opacity:0.6;margin-top:2px;'>{ph_str}</div>
                <div style='font-size:0.82rem;color:var(--text-color);margin-top:8px;'>
                    Lat {lat_s} · Lon {lon_s}
                </div>
            </div>""", unsafe_allow_html=True)

        # Animated globe — adapted from collision.py's conjunction animation.
        # It uses the same Play/Pause + slider interaction, but follows one object
        # across the P10→P90 interval and highlights the three proxy locations.
        if sat is not None:
            _disp_norad_anim, _disp_name_anim = _demo_display(
                pred.get("norad_id") or st.session_state.get("re_pred_norad_input", ""),
                sat.name,
            )
            render_reentry_globe_animated(sat, pred, _disp_name_anim)

        dev_history = pred.get("development_history", [])
        if dev_history:
            render_development_summary(dev_history)
            fir_path = _discover_manila_fir_shapefile()
            if fir_path:
                st.caption(f"Manila FIR boundary: `{fir_path}`")
            render_reentry_development_animation(dev_history, _disp_name_anim if sat is not None else "Object")

        # Uncertainty + PH stats
        st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)
        u_col, ph_col = st.columns(2)

        with u_col:
            st.markdown(
                "<div style='font-weight:600;color:var(--text-color);font-size:0.9rem;margin-bottom:6px;'>"
                "Uncertainty</div>", unsafe_allow_html=True)
            st.markdown(f"""
            <div style='background:var(--background-color);border:1px solid rgba(128,128,128,0.3);border-radius:10px;
                        padding:12px 16px;font-size:0.85rem;line-height:1.9;'>
                <b style='color:var(--text-color);opacity:0.6;'>TIP window (total):</b>
                <span style='color:var(--text-color);'>{unc.get('tip_window_total_min','N/A')} min</span><br>
                <b style='color:var(--text-color);opacity:0.6;'>80% interval width:</b>
                <span style='color:var(--text-color);'>{unc.get('interval_80pct_min','N/A')} min</span><br>
                <b style='color:var(--text-color);opacity:0.6;'>±½ width (80%):</b>
                <span style='color:var(--text-color);'>
                    ±{unc.get('half_width_min','N/A')} min
                    (±{unc.get('half_width_hr','N/A')} h)
                </span><br>
                <b style='color:var(--text-color);opacity:0.6;'>NOAA Kp mean:</b>
                <span style='color:var(--text-color);'>{pred.get('noaa_kp_mean_1m_near_mid','N/A')}</span><br>
                <b style='color:var(--text-color);opacity:0.6;'>Ap proxy:</b>
                <span style='color:var(--text-color);'>{pred.get('ap_proxy_used','N/A')}</span>
            </div>""", unsafe_allow_html=True)

        with ph_col:
            st.markdown(
                "<div style='font-weight:600;color:var(--text-color);font-size:0.9rem;margin-bottom:6px;'>"
                "Manila FIR Overpass Probability</div>", unsafe_allow_html=True)
            if ph_p:
                st.markdown(f"""
                <div style='background:var(--background-color);border:1px solid rgba(128,128,128,0.3);border-radius:10px;
                            padding:12px 16px;font-size:0.85rem;line-height:1.9;'>
                    <b style='color:var(--text-color);opacity:0.6;'>Orbital period:</b>
                    <span style='color:var(--text-color);'>{ph_p.get('orbital_period_min','N/A')} min</span><br>
                    <b style='color:var(--text-color);opacity:0.6;'>Remaining window:</b>
                    <span style='color:var(--text-color);'>{ph_p.get('remaining_window_min','N/A')} min
                    ({ph_p.get('n_orbits_remaining','N/A')} orbits)</span><br>
                    <b style='color:var(--text-color);opacity:0.6;'>FIR track fraction:</b>
                    <span style='color:var(--text-color);'>{ph_p.get('ph_fraction_pct','N/A')}%</span><br>
                    <b style='color:var(--text-color);opacity:0.6;'>Time inside FIR / orbit:</b>
                    <span style='color:var(--text-color);'>{ph_p.get('ph_time_per_orbit_s','N/A')} s</span><br>
                    <b style='color:var(--text-color);opacity:0.6;'>Rough P(reentry within FIR):</b>
                    <span style='color:#f44336;font-weight:700;'>
                        {ph_p.get('ph_fraction_pct','N/A')}%
                    </span>
                </div>""", unsafe_allow_html=True)
            else:
                st.info("No Manila FIR overpass probability data.")

        # Philippines crossings
        ph_cx = pred.get("ph_crossings", [])
        if ph_cx:
            st.markdown(
                "<div style='font-weight:600;color:#f44336;font-size:0.9rem;margin:12px 0 6px;'>"
                "Manila FIR Crossings Detected</div>", unsafe_allow_html=True)
            for cx in ph_cx:
                st.markdown(f"""
                <div style='background:var(--background-color);border:1px solid #f44336;border-radius:8px;
                            padding:10px 14px;margin-bottom:6px;font-size:0.83rem;'>
                    <b style='color:var(--text-color);'>Pass #{cx['pass']}</b><br>
                    Entry: <span style='color:var(--text-color);opacity:0.7;'>{cx['entry_utc']}</span>
                    (lat={cx['entry_lat']}, lon={cx['entry_lon']})<br>
                    Exit: &nbsp;<span style='color:var(--text-color);opacity:0.7;'>{cx['exit_utc']}</span>
                    (lat={cx['exit_lat']}, lon={cx['exit_lon']})
                </div>""", unsafe_allow_html=True)

        # Download prediction JSON
        import json as _json
        st.download_button(
            "⬇️ Download Prediction JSON",
            data=_json.dumps(pred, indent=2, default=str),
            file_name=f"prediction_{pred.get('norad_id','unknown')}.json",
            mime="application/json",
            key="re_dl_pred_json",
        )

    # ═══════════════════════════════════════════════
    # GENERATE REPORT
    # ═══════════════════════════════════════════════
    if report_clicked:
        pred = st.session_state["re_pred_pred"]
        if not pred:
            st.warning("Run prediction first.")
        else:
            try:
                norad_id = pred.get("norad_id") or 0
                obj_name = sat.name if sat else f"NORAD {norad_id}"
                created_utc = pred.get("created_utc", dt_to_iso_z(dt.datetime.now(dt.timezone.utc)))
                mc = pred.get("monte_carlo", {})
                reentry_epoch_utc = mc.get("p50_utc", "N/A")

                epoch_uncertainty_text = "N/A"
                p10s = mc.get("p10_utc"); p90s = mc.get("p90_utc")
                if p10s and p90s:
                    try:
                        half = (parse_any_datetime_utc(p90s) - parse_any_datetime_utc(p10s)) / 2
                        mins = int(round(half.total_seconds() / 60))
                        epoch_uncertainty_text = f"~±{mins//60}h {mins%60:02d}m (P10–P90/2)"
                    except Exception:
                        pass

                tw = pred.get("tip_window", {})
                imp50 = pred.get("impact_proxy", {}).get("p50", {})
                p50_latlon_text = (f"{float(imp50['lat']):.4f}°, {float(imp50['lon']):.4f}°"
                                   if "lat" in imp50 else "N/A")
                kp_val = pred.get("noaa_kp_mean_1m_near_mid")
                kp_text = "N/A" if kp_val is None else f"{float(kp_val):.2f}"

                with tempfile.TemporaryDirectory() as tmpdir:
                    ts_str = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
                    png_path = os.path.join(tmpdir, f"report_{norad_id}_{ts_str}.png")
                    pdf_path = os.path.join(tmpdir, f"report_{norad_id}_{ts_str}.pdf")
                    _disp_norad, _disp_obj_name = _demo_display(norad_id, obj_name)
                    generate_proxy_report(
                        png_path, out_path_pdf=pdf_path,
                        norad_id=_disp_norad, object_name=_disp_obj_name,
                        created_utc=created_utc, reentry_epoch_utc=reentry_epoch_utc,
                        epoch_uncertainty_text=epoch_uncertainty_text,
                        window_start_utc=tw.get("start_utc", "N/A"),
                        window_end_utc=tw.get("end_utc",   "N/A"),
                        p50_latlon_text=p50_latlon_text,
                        kp_text=kp_text,
                        footer_text=("OMM/TLE scenario: P10/P50/P90 not calibrated; coordinates are orbital subpoints, NOT impact forecasts." if pred.get("analysis_mode", "").startswith("OMM_TLE") else "TIP-based operational proxy, not a confirmed impact forecast."),
                    )
                    with open(png_path, "rb") as f:
                        png_bytes = f.read()
                    with open(pdf_path, "rb") as f:
                        pdf_bytes = f.read()

                st.image(png_bytes, caption="Proxy Re-entry Report", use_container_width=True)
                dl_c1, dl_c2 = st.columns(2)
                dl_c1.download_button("⬇Download PNG", data=png_bytes,
                                      file_name=f"report_{norad_id}_{ts_str}.png",
                                      mime="image/png", key="re_dl_png")
                dl_c2.download_button("⬇Download PDF", data=pdf_bytes,
                                      file_name=f"report_{norad_id}_{ts_str}.pdf",
                                      mime="application/pdf", key="re_dl_pdf")
                log("Report generated.")
            except Exception as e:
                st.error(f"Report error: {e}")
                log(f"ERROR: {e}")

    # ═══════════════════════════════════════════════
    # EXPORT KML
    # ═══════════════════════════════════════════════
    if kml_clicked:
        if not (sat and wmin and wmax):
            st.warning("Fetch TIP + TLE first.")
        elif simplekml is None:
            st.error("simplekml not installed. Run: `pip install simplekml`")
        else:
            try:
                width_td = wmax - wmin
                wmid = wmin + (width_td / 2)
                lats_min, lons_min, _ = groundtrack_corridor(sat, wmin, int(before_min), int(after_min), int(step_s))
                lats_mid, lons_mid, _ = groundtrack_corridor(sat, wmid, int(before_min), int(after_min), int(step_s))
                lats_max, lons_max, _ = groundtrack_corridor(sat, wmax, int(before_min), int(after_min), int(step_s))

                with tempfile.TemporaryDirectory() as tmpdir:
                    ts_str  = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
                    norad_s = str(norad_input.strip())

                    corridor_path = os.path.join(tmpdir, f"corridor_{norad_s}_{ts_str}.kml")
                    export_kml_tracks(corridor_path, [
                        {"name": f"Track_wmin ({dt_to_iso_z(wmin)})", "lats": lats_min, "lons": lons_min},
                        {"name": f"Track_wmid ({dt_to_iso_z(wmid)})", "lats": lats_mid, "lons": lons_mid},
                        {"name": f"Track_wmax ({dt_to_iso_z(wmax)})", "lats": lats_max, "lons": lons_max},
                    ])

                    swath_path = os.path.join(tmpdir, f"swath_{norad_s}_{ts_str}.kml")
                    export_kml_swath_polygon(swath_path, lats_mid, lons_mid, float(swath_km))

                    with open(corridor_path, "rb") as f: corridor_bytes = f.read()
                    with open(swath_path,   "rb") as f: swath_bytes    = f.read()

                    pts_bytes = None
                    pred_now = st.session_state["re_pred_pred"]
                    if pred_now:
                        imp = pred_now.get("impact_proxy", {})
                        mc  = pred_now.get("monte_carlo", {})
                        pts = []
                        for pct in ("p10", "p50", "p90"):
                            p = imp.get(pct, {})
                            if "lat" in p and "lon" in p:
                                m = p.get("meta", {})
                                pts.append({
                                    "name": f"{('Orbital scenario' if pred_now.get('analysis_mode', '').startswith('OMM_TLE') else 'Impact proxy')} {pct.upper()} ({mc.get(pct+'_utc','')})",
                                    "lat": float(p["lat"]), "lon": float(p["lon"]),
                                    "desc": (
                                        f"Time UTC: {mc.get(pct+'_utc','N/A')}\n"
                                        f"Downrange: {m.get('downrange_km',0):.0f} km\n"
                                        f"Descent time: {m.get('descent_time_s',0):.0f} s"
                                    ),
                                })
                        if pts:
                            pts_path = os.path.join(tmpdir, f"impact_points_{norad_s}_{ts_str}.kml")
                            export_kml_points(pts_path, pts)
                            with open(pts_path, "rb") as f: pts_bytes = f.read()

                kml_c1, kml_c2, kml_c3 = st.columns(3)
                kml_c1.download_button("⬇Corridor KML", data=corridor_bytes,
                                       file_name=f"corridor_{norad_s}_{ts_str}.kml",
                                       mime="application/vnd.google-earth.kml+xml",
                                       key="re_dl_corridor")
                kml_c2.download_button("⬇Swath KML", data=swath_bytes,
                                       file_name=f"swath_{norad_s}_{ts_str}.kml",
                                       mime="application/vnd.google-earth.kml+xml",
                                       key="re_dl_swath")
                if pts_bytes:
                    kml_c3.download_button("⬇Impact Points KML", data=pts_bytes,
                                           file_name=f"impact_points_{norad_s}_{ts_str}.kml",
                                           mime="application/vnd.google-earth.kml+xml",
                                           key="re_dl_pts")
                log("KML exported.")
                st.success("KML files ready for download.")
            except Exception as e:
                st.error(f"KML export error: {e}")
                log(f"ERROR: {e}")

    # ═══════════════════════════════════════════════
    # LOG
    # ═══════════════════════════════════════════════
    log_entries = st.session_state["re_pred_log"]
    if log_entries:
        with st.expander("Activity Log", expanded=False):
            st.code("\n".join(log_entries[-60:]), language=None)