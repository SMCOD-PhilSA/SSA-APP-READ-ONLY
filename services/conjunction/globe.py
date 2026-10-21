# import streamlit as st
# import requests
# import plotly.graph_objects as go
# from sgp4.api import Satrec, jday
# from datetime import datetime, timedelta, timezone
# import numpy as np
# import math
# import http.cookiejar as _cookiejar
# import urllib.request as _urllib_req
# import urllib.parse as _urllib_parse
# import ssl as _ssl
# import pandas as pd

# try:
#     import certifi as _certifi
# except ImportError:
#     _certifi = None

# SPACETRACK_LOGIN = "https://www.space-track.org/ajaxauth/login"

# # ====================== SPACE-TRACK HELPERS ======================

# def _get_spacetrack_creds():
#     try:
#         return (
#             st.secrets["spacetrack"]["username"],
#             st.secrets["spacetrack"]["password"],
#         )
#     except:
#         import os
#         username = os.getenv("SPACETRACK_USERNAME")
#         password = os.getenv("SPACETRACK_PASSWORD")
#         if username and password:
#             return username, password
#         return None, None


# def _build_opener():
#     cj = _cookiejar.CookieJar()
#     handlers = [_urllib_req.HTTPCookieProcessor(cj)]
#     if _certifi:
#         ssl_ctx = _ssl.create_default_context(cafile=_certifi.where())
#         handlers.append(_urllib_req.HTTPSHandler(context=ssl_ctx))
#     else:
#         handlers.append(_urllib_req.HTTPSHandler())
    
#     opener = _urllib_req.build_opener(*handlers)
#     opener.addheaders = [("User-Agent", "SSA-Dashboard/1.0")]
#     return opener


# def _spacetrack_login(opener, username, password):
#     data = _urllib_parse.urlencode({
#         "identity": username,
#         "password": password,
#     }).encode("utf-8")
#     req = _urllib_req.Request(SPACETRACK_LOGIN, data=data)
#     try:
#         resp = opener.open(req, timeout=30)
#         body = resp.read().decode("utf-8", "replace")
#         return "Failed" not in body, None
#     except Exception as e:
#         return False, str(e)


# @st.cache_data(ttl=1800)
# def fetch_tle(norad_id: int):
#     """Fetch TLE from Space-Track"""
#     creds = _get_spacetrack_creds()
#     if not creds[0] or not creds[1]:
#         st.error("Space-Track credentials not configured in secrets.toml")
#         return None, None

#     username, password = creds
#     opener = _build_opener()

#     # Login
#     success, err = _spacetrack_login(opener, username, password)
#     if not success:
#         st.error(f"Space-Track Login Failed: {err}")
#         return None, None

#     # Query TLE
#     url = f"https://www.space-track.org/basicspacedata/query/class/gp/NORAD_CAT_ID/{int(norad_id)}/orderby/EPOCH%20DESC/format/tle"
#     try:
#         resp = opener.open(url, timeout=40)
#         text = resp.read().decode("utf-8", "replace").strip()
#         lines = [line.strip() for line in text.splitlines() if line.strip()]

#         for i in range(len(lines) - 1):
#             if lines[i].startswith("1 ") and lines[i+1].startswith("2 "):
#                 return lines[i], lines[i+1]
#         st.warning(f"No valid TLE found for NORAD {norad_id}")
#         return None, None
#     except Exception as e:
#         st.error(f"Space-Track fetch failed: {e}")
#         return None, None


# def eci_to_llh(x, y, z):
#     """Simple ECI to Lat/Lon/Alt"""
#     a = 6378.137
#     b = 6356.7523142
#     e2 = 1 - (b**2 / a**2)
    
#     lon = math.degrees(math.atan2(y, x))
#     r = math.sqrt(x*x + y*y)
#     lat = math.degrees(math.atan2(z, r))
    
#     for _ in range(5):
#         N = a / math.sqrt(1 - e2 * math.sin(math.radians(lat))**2)
#         h = r / math.cos(math.radians(lat)) - N
#         lat = math.degrees(math.atan2(z, r * (1 - e2 * N / (N + h))))
    
#     return lat, lon, max(h, 0)


# def get_orbit_points_for_event(df, event_id, object_num=1, num_points=60):
#     event_df = df[df["EVENT_ID"] == event_id]
#     if event_df.empty:
#         return []

#     norad_col = "Primary_SCC" if object_num == 1 else "Secondary_SCC"
#     name_col = "Primary_Object" if object_num == 1 else "Secondary_Object"

#     try:
#         norad = int(float(event_df[norad_col].iloc[0]))
#     except:
#         return []

#     sat_name = str(event_df.get(name_col, pd.Series([f"NORAD {norad}"])).iloc[0]).strip()
#     if not sat_name or sat_name.lower() in ["nan", "n/a"]:
#         sat_name = f"NORAD {norad}"

#     tle1, tle2 = fetch_tle(norad)
#     if not tle1 or not tle2:
#         st.warning(f"⚠️ Failed to get TLE for {sat_name} (NORAD {norad})")
#         return []

#     return propagate_orbit(tle1, tle2, minutes_ahead=240, step_sec=240)  # 4 hours, 4-min steps


# def propagate_orbit(tle1, tle2, minutes_ahead=240, step_sec=240):
#     try:
#         sat = Satrec.twoline2rv(tle1, tle2)
#         now = datetime.now(timezone.utc)
#         pts = []
#         for s in range(0, minutes_ahead * 60 + 1, step_sec):
#             t = now + timedelta(seconds=s)
#             jd, fr = jday(t.year, t.month, t.day, t.hour, t.minute, t.second + t.microsecond/1e6)
#             e, r, v = sat.sgp4(jd, fr)
#             if e != 0:
#                 continue
#             x, y, z = r
#             alt = math.sqrt(x**2 + y**2 + z**2) - 6371.0
#             lat = math.degrees(math.asin(z / math.sqrt(x**2 + y**2 + z**2)))
#             # Simple lon calculation
#             lon = math.degrees(math.atan2(y, x))
#             pts.append((lat, lon, alt, t))
#         return pts
#     except Exception as e:
#         st.error(f"Propagation error: {e}")
#         return []


# def render_globe_dual(orbit_pts1, sat_name1, orbit_pts2, sat_name2):
#     if not orbit_pts1 or not orbit_pts2:
#         st.warning("No orbit data available.")
#         return

#     fig = go.Figure()

#     # Sat 1 - Diwata-2 (Always Yellow)
#     lats1 = [p[0] for p in orbit_pts1]
#     lons1 = [p[1] for p in orbit_pts1]
#     fig.add_trace(go.Scattergeo(
#         lat=lats1, 
#         lon=lons1, 
#         mode="lines",
#         line=dict(color="#FFD700", width=3.5),
#         name=sat_name1,
#         showlegend=True
#     ))
#     fig.add_trace(go.Scattergeo(
#         lat=[lats1[0]], 
#         lon=[lons1[0]], 
#         mode="markers+text",
#         marker=dict(size=14, color="#FFD700", symbol="star"),
#         text=[f" {sat_name1}"],
#         textposition="middle right",
#         showlegend=False
#     ))

#     # Sat 2
#     lats2 = [p[0] for p in orbit_pts2]
#     lons2 = [p[1] for p in orbit_pts2]
#     fig.add_trace(go.Scattergeo(
#         lat=lats2, 
#         lon=lons2, 
#         mode="lines",
#         line=dict(color="#ff4081", width=3.5),
#         name=sat_name2,
#         showlegend=True
#     ))
#     fig.add_trace(go.Scattergeo(
#         lat=[lats2[0]], 
#         lon=[lons2[0]], 
#         mode="markers+text",
#         marker=dict(size=14, color="#ff4081"),
#         text=[f" {sat_name2}"],
#         textposition="middle right",
#         showlegend=False
#     ))

#     # === TRANSPARENT GLOBE BACKGROUND ===
#     fig.update_geos(
#         projection_type="orthographic",
#         showland=True,
#         landcolor="#1e3a5f",           # subtle land
#         showocean=True,
#         oceancolor="#0a1f3d",          # subtle ocean
#         showcountries=False,
#         #countrycolor="rgba(255,255,255,0.15)",
#         coastlinecolor="rgba(255,255,255,0.55)",
#         bgcolor="rgba(0,0,0,0)",       # Fully transparent
#     )

#     fig.update_layout(
#         height=650,
#         title=f"{sat_name1} — {sat_name2}",
#         title_x=0.5,
#         paper_bgcolor="rgba(0,0,0,0)",   # Transparent
#         plot_bgcolor="rgba(0,0,0,0)",    # Transparent
#         margin=dict(l=0, r=0, t=40, b=0),
#         legend=dict(
#             bgcolor="rgba(255,255,255,0.85)",
#             bordercolor="rgba(0,0,0,0.4)",
#             borderwidth=1,
#             font=dict(color="black", size=13),
#             yanchor="top",
#             y=0.95,
#             xanchor="left",
#             x=0.02,
#             title=dict(
#                 text="<b>Legend</b>", 
#                 font=dict(size=14, color="black")
#             )
#         )
#     )

#     st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

import streamlit as st
import requests
import plotly.graph_objects as go
from sgp4.api import Satrec, jday
from datetime import datetime, timedelta, timezone
import numpy as np
import math
import http.cookiejar as _cookiejar
import urllib.request as _urllib_req
import urllib.parse as _urllib_parse
import ssl as _ssl
import pandas as pd
import gspread

try:
    import certifi as _certifi
except ImportError:
    _certifi = None

SPACETRACK_LOGIN = "https://www.space-track.org/ajaxauth/login"

# ====================== CONFIG ======================
TLE_CACHE_SHEET_NAME = "TLE_CDM"
MAX_TLE_CACHE_HOURS = 12  # Adjust based on your accuracy needs

# ====================== SPACE-TRACK HELPERS ======================
def _get_spacetrack_creds():
    try:
        return (
            st.secrets["spacetrack"]["username"],
            st.secrets["spacetrack"]["password"],
        )
    except:
        import os
        username = os.getenv("SPACETRACK_USERNAME")
        password = os.getenv("SPACETRACK_PASSWORD")
        if username and password:
            return username, password
        return None, None

def _build_opener():
    cj = _cookiejar.CookieJar()
    handlers = [_urllib_req.HTTPCookieProcessor(cj)]
    if _certifi:
        ssl_ctx = _ssl.create_default_context(cafile=_certifi.where())
        handlers.append(_urllib_req.HTTPSHandler(context=ssl_ctx))
    else:
        handlers.append(_urllib_req.HTTPSHandler())
   
    opener = _urllib_req.build_opener(*handlers)
    opener.addheaders = [("User-Agent", "SSA-Dashboard/1.0")]
    return opener

def _spacetrack_login(opener, username, password):
    data = _urllib_parse.urlencode({
        "identity": username,
        "password": password,
    }).encode("utf-8")
    req = _urllib_req.Request(SPACETRACK_LOGIN, data=data)
    try:
        resp = opener.open(req, timeout=30)
        body = resp.read().decode("utf-8", "replace")
        return "Failed" not in body, None
    except Exception as e:
        return False, str(e)

# ====================== GOOGLE SHEETS TLE CACHE ======================
def _get_gspread_client():
    try:
        return gspread.service_account()
    except:
        creds = st.secrets["gcp_service_account_odr"]
        return gspread.service_account_from_dict(creds)

def _get_or_create_worksheet(sh, title, rows=200, cols=10, header=None):
    try:
        return sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=rows, cols=cols)
        if header:
            ws.append_row(header)
        return ws

def _get_tle_cache_ws():
    client = _get_gspread_client()
    sh = client.open_by_key(st.secrets["google"]["sheet_id"])
    return _get_or_create_worksheet(
        sh, TLE_CACHE_SHEET_NAME,
        rows=200, cols=10,
        header=["updated_at_utc", "norad_cat_id", "object_name", "epoch",
                "tle_line1", "tle_line2"]
    )

def _read_tle_cache(norad_id):
    try:
        ws = _get_tle_cache_ws()
        rows = ws.get_all_records()
        if not rows:
            return None
        matching = [r for r in rows if str(r.get("norad_cat_id")) == str(norad_id)]
        if not matching:
            return None
        latest = matching[-1]
        updated_at = pd.to_datetime(latest["updated_at_utc"], utc=True)
        age_hours = (pd.Timestamp.utcnow() - updated_at).total_seconds() / 3600
        
        return {
            "age_hours": age_hours,
            "rec": {
                "TLE_LINE1": str(latest["tle_line1"]),
                "TLE_LINE2": str(latest["tle_line2"]),
            }
        }
    except Exception:
        return None

def _write_tle_cache(norad_id, tle1, tle2, object_name=""):
    try:
        ws = _get_tle_cache_ws()
        ws.append_row([
            datetime.now(timezone.utc).isoformat(),
            str(norad_id),
            object_name,
            "",
            tle1,
            tle2,
        ])
    except Exception as e:
        st.warning(f"Failed to cache TLE: {e}")

# ====================== CACHED TLE FETCH ======================
@st.cache_data(ttl=900)  # 15 minutes
def fetch_tle_cached(norad_id: int):
    """Fetch TLE with Google Sheets caching"""
    cache = _read_tle_cache(norad_id)
    
    should_refresh = (
        cache is None
        or cache["age_hours"] >= MAX_TLE_CACHE_HOURS
    )
    
    if not should_refresh:
        return cache["rec"]["TLE_LINE1"], cache["rec"]["TLE_LINE2"], f"✅ Cached ({cache['age_hours']:.1f}h old)"
    
    # Live fetch
    tle1, tle2 = fetch_tle(norad_id)  # Original function below
    
    if tle1 and tle2:
        _write_tle_cache(norad_id, tle1, tle2)
        return tle1, tle2, "✅ Fresh from Space-Track"
    else:
        if cache:
            return (cache["rec"]["TLE_LINE1"], 
                   cache["rec"]["TLE_LINE2"], 
                   f"⚠️ Using stale cache ({cache['age_hours']:.1f}h old)")
        return None, None, "❌ Failed to fetch TLE"

@st.cache_data(ttl=1800)
def fetch_tle(norad_id: int):
    """Original Space-Track fetch (kept for backward compatibility)"""
    creds = _get_spacetrack_creds()
    if not creds[0] or not creds[1]:
        st.error("Space-Track credentials not configured in secrets.toml")
        return None, None
    username, password = creds
    opener = _build_opener()
    success, err = _spacetrack_login(opener, username, password)
    if not success:
        st.error(f"Space-Track Login Failed: {err}")
        return None, None
    url = f"https://www.space-track.org/basicspacedata/query/class/gp/NORAD_CAT_ID/{int(norad_id)}/orderby/EPOCH%20DESC/format/tle"
    try:
        resp = opener.open(url, timeout=40)
        text = resp.read().decode("utf-8", "replace").strip()
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for i in range(len(lines) - 1):
            if lines[i].startswith("1 ") and lines[i+1].startswith("2 "):
                return lines[i], lines[i+1]
        st.warning(f"No valid TLE found for NORAD {norad_id}")
        return None, None
    except Exception as e:
        st.error(f"Space-Track fetch failed: {e}")
        return None, None

# ====================== REST OF YOUR CODE ======================
def eci_to_llh(x, y, z):
    """Simple ECI to Lat/Lon/Alt"""
    a = 6378.137
    b = 6356.7523142
    e2 = 1 - (b**2 / a**2)
   
    lon = math.degrees(math.atan2(y, x))
    r = math.sqrt(x*x + y*y)
    lat = math.degrees(math.atan2(z, r))
   
    for _ in range(5):
        N = a / math.sqrt(1 - e2 * math.sin(math.radians(lat))**2)
        h = r / math.cos(math.radians(lat)) - N
        lat = math.degrees(math.atan2(z, r * (1 - e2 * N / (N + h))))
   
    return lat, lon, max(h, 0)

def get_orbit_points_for_event(df, event_id, object_num=1, num_points=60):
    event_df = df[df["EVENT_ID"] == event_id]
    if event_df.empty:
        return []
    norad_col = "Primary_SCC" if object_num == 1 else "Secondary_SCC"
    name_col = "Primary_Object" if object_num == 1 else "Secondary_Object"
    try:
        norad = int(float(event_df[norad_col].iloc[0]))
    except:
        return []
    sat_name = str(event_df.get(name_col, pd.Series([f"NORAD {norad}"])).iloc[0]).strip()
    if not sat_name or sat_name.lower() in ["nan", "n/a"]:
        sat_name = f"NORAD {norad}"
    
    # === UPDATED: Use cached version ===
    tle1, tle2, status_msg = fetch_tle_cached(norad)
    
    if not tle1 or not tle2:
        st.warning(f"⚠️ Failed to get TLE for {sat_name} (NORAD {norad})")
        return []
    
    st.caption(status_msg)  # Shows cache status
    return propagate_orbit(tle1, tle2, minutes_ahead=240, step_sec=240)

def propagate_orbit(tle1, tle2, minutes_ahead=240, step_sec=240):
    try:
        sat = Satrec.twoline2rv(tle1, tle2)
        now = datetime.now(timezone.utc)
        pts = []
        for s in range(0, minutes_ahead * 60 + 1, step_sec):
            t = now + timedelta(seconds=s)
            jd, fr = jday(t.year, t.month, t.day, t.hour, t.minute, t.second + t.microsecond/1e6)
            e, r, v = sat.sgp4(jd, fr)
            if e != 0:
                continue
            x, y, z = r
            alt = math.sqrt(x**2 + y**2 + z**2) - 6371.0
            lat = math.degrees(math.asin(z / math.sqrt(x**2 + y**2 + z**2)))
            lon = math.degrees(math.atan2(y, x))
            pts.append((lat, lon, alt, t))
        return pts
    except Exception as e:
        st.error(f"Propagation error: {e}")
        return []

def render_globe_dual(orbit_pts1, sat_name1, orbit_pts2, sat_name2):
    if not orbit_pts1 or not orbit_pts2:
        st.warning("No orbit data available.")
        return
    fig = go.Figure()
    # Sat 1
    lats1 = [p[0] for p in orbit_pts1]
    lons1 = [p[1] for p in orbit_pts1]
    fig.add_trace(go.Scattergeo(
        lat=lats1, lon=lons1, mode="lines",
        line=dict(color="#FFD700", width=3.5),
        name=sat_name1, showlegend=True
    ))
    fig.add_trace(go.Scattergeo(
        lat=[lats1[0]], lon=[lons1[0]],
        mode="markers+text",
        marker=dict(size=14, color="#FFD700", symbol="star"),
        text=[f" {sat_name1}"], textposition="middle right", showlegend=False
    ))
    # Sat 2
    lats2 = [p[0] for p in orbit_pts2]
    lons2 = [p[1] for p in orbit_pts2]
    fig.add_trace(go.Scattergeo(
        lat=lats2, lon=lons2, mode="lines",
        line=dict(color="#ff4081", width=3.5),
        name=sat_name2, showlegend=True
    ))
    fig.add_trace(go.Scattergeo(
        lat=[lats2[0]], lon=[lons2[0]],
        mode="markers+text",
        marker=dict(size=14, color="#ff4081"),
        text=[f" {sat_name2}"], textposition="middle right", showlegend=False
    ))
    fig.update_geos(
        projection_type="orthographic",
        showland=True, landcolor="#1e3a5f",
        showocean=True, oceancolor="#0a1f3d",
        showcountries=False,
        coastlinecolor="rgba(255,255,255,0.55)",
        bgcolor="rgba(0,0,0,0)",
    )
    fig.update_layout(
        height=650,
        title=f"{sat_name1} — {sat_name2}",
        title_x=0.5,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=0, r=0, t=40, b=0),
        legend=dict(bgcolor="rgba(255,255,255,0.85)", bordercolor="rgba(0,0,0,0.4)",
                    borderwidth=1, font=dict(color="black", size=13),
                    yanchor="top", y=0.95, xanchor="left", x=0.02,
                    title=dict(text="<b>Legend</b>", font=dict(size=14, color="black")))
    )
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})