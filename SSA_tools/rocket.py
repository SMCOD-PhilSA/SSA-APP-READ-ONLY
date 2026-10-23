import streamlit as st
import os
import re
import math
import io
import zipfile
import pandas as pd
import geopandas as gpd
import folium
from shapely.geometry import Point, Polygon
from datetime import datetime, timedelta, date
from streamlit_folium import st_folium
from streamlit_option_menu import option_menu
# ====================== GOOGLE SHEETS IMPORTS ======================
import gspread
from google.oauth2.service_account import Credentials
# ====================== PDF SUPPORT ======================
try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None
# ====================== PLOTLY FOR DASHBOARD ======================
import plotly.express as px
import plotly.graph_objects as go
# ---------------- REMOVE TOP PADDING ----------------
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
#     Rocket Launch
# </div>
# """, unsafe_allow_html=True)

# #st.title("Rocket Launch")

# -----------------------------
# OPTION MENU + CONFIG
# -----------------------------
#st.space()

selected_option = option_menu(
    menu_title=None,
    #options=["NOTAM Parser", "Launch Dashboard", "Launch Database"],
    #options=["Launch Dashboard", "Upcoming Launches", "NOTAM Parser", "Launch Database"],
    options=["Launch Dashboard", "Upcoming Launches", "Launch Database"],
    icons=["bi bi-bar-chart-fill", "bi bi-rocket-takeoff-fill", "bi bi-filter"],
    menu_icon="cast",
    default_index=0,
    orientation="horizontal",
)
if "selected" not in st.session_state:
    #st.session_state.selected = "NOTAM Parser"
    st.session_state.selected = "Launch Dashboard"
if selected_option != st.session_state.selected:
    st.session_state.selected = selected_option
    st.rerun()

st.set_page_config(page_title="SSA - Rocket Launch", page_icon=":material/rocket_launch:",  layout="wide")

# ====================== SAFE PATHS ======================
if "shape_dir" not in st.session_state:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(script_dir)
    st.session_state.shape_dir = os.path.join(parent_dir, "utils", "shapefiles")
    st.session_state.parent_dir = parent_dir

# ====================== DYNAMIC DROPZONE STORAGE ======================
if "dz_vertices" not in st.session_state:
    st.session_state.dz_vertices = {f"DZ{i}": [""] * 5 for i in range(1, 5)}
    st.session_state.dz_debris = {f"DZ{i}": [""] * 2 for i in range(1, 5)}
if "pending_notam_fill" not in st.session_state:
    st.session_state.pending_notam_fill = None

# Keep lists in sync
for i in range(1, 5):
    dz = f"DZ{i}"
    for idx in range(len(st.session_state.dz_vertices[dz])):
        key = f"{dz}_vert_{idx}"
        if key in st.session_state:
            st.session_state.dz_vertices[dz][idx] = st.session_state[key]
    for j in range(len(st.session_state.dz_debris[dz])):
        key = f"{dz}_deb_{j}"
        if key in st.session_state:
            st.session_state.dz_debris[dz][j] = st.session_state[key]

# ====================== GOOGLE SHEETS (Rocket-Launch-Monitoring + Sheet1) ======================
@st.cache_resource
def get_gspread_client():
    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        creds = Credentials.from_service_account_info(
            st.secrets["philsa_rocket_gcp"],
            scopes=scopes
        )
        return gspread.authorize(creds)
    except Exception as e:
        st.error(f"❌ Google Sheets connection failed: {e}")
        st.stop()

def get_launch_worksheet():
    client = get_gspread_client()
    spreadsheet_name = "Rocket-Launch-Monitoring"
    worksheet_name = "Sheet1"
   
    spreadsheet = client.open(spreadsheet_name)
   
    try:
        return spreadsheet.worksheet(worksheet_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=worksheet_name, rows=2000, cols=100)
        headers = [
            "LAUNCH DATE", "ROCKET NAME", "LAUNCHING STATE", "LAUNCH CENTER",
            "LAUNCH CENTER LOCATION", "START TO END TIME", "DATE RECEIVED FROM CAAP", "WKT",
            "DZ_COUNT", "ARCH_COUNT", "EEZ_COUNT", "FIR_COUNT", "OUTSIDE_COUNT"
        ] + [f"DZ{i} P{j}" for i in range(1,5) for j in range(1,9)] + [f"DEB{i} P{j}" for i in range(1,5) for j in range(1,5)]
        ws.append_row(headers)
        st.success(f"✅ Worksheet '{worksheet_name}' created in '{spreadsheet_name}'")
        return ws

# ====================== NEW: CACHED LAUNCH DATABASE FOR DASHBOARD ======================
@st.cache_data(ttl=300)  # auto-refresh every 5 minutes

@st.cache_data
def classify_dropzones_by_zone(df, shape_dir):
    """
    Classify dropzones by intersection with priority:
    1. Archipelagic Waters (PH_Baseline) - highest priority
    2. EEZ 
    3. Manila FIR - lowest priority (because it's the largest)
    """
    if df.empty:
        return pd.DataFrame({
            "Zone": ["Manila FIR", "EEZ", "Archipelagic Waters"], 
            "Count": [0, 0, 0]
        })

    loaded = load_mapping_data(shape_dir)

    try:
        fir_gdf   = gpd.read_file(loaded["manila_fir"]) if os.path.exists(loaded["manila_fir"]) else None
        eez_gdf   = gpd.read_file(loaded["eez"]) if os.path.exists(loaded["eez"]) else None
        base_gdf  = gpd.read_file(loaded["baseline"]) if os.path.exists(loaded["baseline"]) else None
    except Exception as e:
        st.warning(f"Could not load shapefiles: {e}")
        return pd.DataFrame({
            "Zone": ["Manila FIR", "EEZ", "Archipelagic Waters"], 
            "Count": [0, 0, 0]
        })

    zone_counts = {"Manila FIR": 0, "EEZ": 0, "Archipelagic Waters": 0}

    for _, row in df.iterrows():
        for dz_num in range(1, 5):
            dz = f"DZ{dz_num}"
            
            verts = [
                str(row.get(f"{dz} P{p}", "")).strip()
                for p in range(1, 9)
                if str(row.get(f"{dz} P{p}", "")).strip()
            ]
            
            pts = [parse_coordinates(v) for v in verts]
            valid_pts = [(lat, lon) for lat, lon in pts if lat is not None]

            if len(valid_pts) < 3:
                continue

            # Create polygon (Shapely uses (lon, lat))
            dropzone_poly = Polygon([(lon, lat) for lat, lon in valid_pts])

            # ====================== PRIORITY CHECK ======================
            classified = False

            # 1. Archipelagic Waters (inside baseline) - Highest priority
            if base_gdf is not None and not base_gdf.empty:
                if dropzone_poly.intersects(base_gdf.unary_union):
                    zone_counts["Archipelagic Waters"] += 1
                    classified = True

            # 2. EEZ - Second priority
            if not classified and eez_gdf is not None and not eez_gdf.empty:
                if dropzone_poly.intersects(eez_gdf.unary_union):
                    zone_counts["EEZ"] += 1
                    classified = True

            # 3. Manila FIR - Lowest priority (only if not in the above two)
            if not classified and fir_gdf is not None and not fir_gdf.empty:
                if dropzone_poly.intersects(fir_gdf.unary_union):
                    zone_counts["Manila FIR"] += 1
                    classified = True

    # Create final dataframe with fixed order
    result_df = pd.DataFrame({
        "Zone": ["Manila FIR", "EEZ", "Archipelagic Waters"],
        "Count": [
            zone_counts["Manila FIR"],
            zone_counts["EEZ"],
            zone_counts["Archipelagic Waters"]
        ]
    })

    return result_df

def classify_single_launch_dropzones(shape_dir):
    """Classify current drop zones in session state and return counts"""
    zone_counts = {"Archipelagic Waters": 0, "EEZ": 0, "Manila FIR": 0, "Outside": 0}
    
    loaded = load_mapping_data(shape_dir)
    
    try:
        fir_gdf = gpd.read_file(loaded["manila_fir"]) if os.path.exists(loaded["manila_fir"]) else None
        eez_gdf = gpd.read_file(loaded["eez"]) if os.path.exists(loaded["eez"]) else None
        base_gdf = gpd.read_file(loaded["baseline"]) if os.path.exists(loaded["baseline"]) else None
    except:
        return 0, 0, 0, 0, 0  # dz, arch, eez, fir, outside

    total_dz = 0

    for i in range(1, 5):
        dz_key = f"DZ{i}"
        verts = [c.strip() for c in st.session_state.dz_vertices.get(dz_key, []) if c.strip()]
        
        if len(verts) < 3:
            continue
        
        pts = [parse_coordinates(v) for v in verts]
        valid_pts = [(lat, lon) for lat, lon in pts if lat is not None]
        
        if len(valid_pts) < 3:
            continue
        
        total_dz += 1
        dropzone_poly = Polygon([(lon, lat) for lat, lon in valid_pts])
        
        classified = False
        
        # 1. Archipelagic Waters (Highest priority)
        if base_gdf is not None and not base_gdf.empty:
            if dropzone_poly.intersects(base_gdf.unary_union):
                zone_counts["Archipelagic Waters"] += 1
                classified = True
        
        # 2. EEZ
        if not classified and eez_gdf is not None and not eez_gdf.empty:
            if dropzone_poly.intersects(eez_gdf.unary_union):
                zone_counts["EEZ"] += 1
                classified = True
        
        # 3. Manila FIR
        if not classified and fir_gdf is not None and not fir_gdf.empty:
            if dropzone_poly.intersects(fir_gdf.unary_union):
                zone_counts["Manila FIR"] += 1
                classified = True
        
        if not classified:
            zone_counts["Outside"] += 1

    return (total_dz, 
            zone_counts["Archipelagic Waters"], 
            zone_counts["EEZ"], 
            zone_counts["Manila FIR"], 
            zone_counts["Outside"])

@st.cache_data(ttl=300)
def load_launch_database():
    """Load launch data from Google Sheets (used by Launch Dashboard)"""
    try:
        ws = get_launch_worksheet()
        data = ws.get_all_records()
        df = pd.DataFrame(data)

        # Parse launch dates (format used in your app: "dd B YYYY")
        df["LAUNCH_DATE_PARSED"] = pd.to_datetime(
            df["LAUNCH DATE"],
            format="%m/%d/%Y",
            errors="coerce",
            dayfirst=False  # handles MM/DD/YYYY safely
        )
        
        return df
    except Exception as e:
        st.error(f"❌ Could not load database: {e}")
        return pd.DataFrame()

# ====================== UTILITY FUNCTIONS ======================
def parse_coordinates(coord_str):
    if not coord_str or not isinstance(coord_str, str):
        return None, None

    s = coord_str.strip().upper().replace(",", " ")

    #FAA NOTAM WITH DECIMAL (Added on 16 July 2026
    # ====================== NEW FORMAT: 210652.31N 1235521.69E ======================
    # DDMMSS.SS H   or   DDDMMSS.SS H   (no spaces)
    m = re.search(r'(\d{6}\.\d+)\s*([NS])\s*(\d{7}\.\d+)\s*([EW])', s)
    if m:
        lat_raw, lat_h, lon_raw, lon_h = m.groups()
        
        # Latitude: DDMMSS.SS
        lat_deg = int(lat_raw[:2])
        lat_min = int(lat_raw[2:4])
        lat_sec = float(lat_raw[4:])
        
        # Longitude: DDDMMSS.SS
        lon_deg = int(lon_raw[:3])
        lon_min = int(lon_raw[3:5])
        lon_sec = float(lon_raw[5:])
        
        lat = lat_deg + lat_min / 60 + lat_sec / 3600
        lon = lon_deg + lon_min / 60 + lon_sec / 3600
        
        if lat_h == 'S': lat = -lat
        if lon_h == 'W': lon = -lon
        
        return round(lat, 6), round(lon, 6)

    # -------- EXISTING FORMAT 1 --------
    m = re.search(r'(\d{6})(?:\.\d+)?\s*([NS])\s+(\d{7})(?:\.\d+)?\s*([EW])', s)
    if m:
        lat_raw, lat_h, lon_raw, lon_h = m.groups()
        lat = int(lat_raw[:2]) + int(lat_raw[2:4])/60 + int(lat_raw[4:6])/3600
        lon = int(lon_raw[:3]) + int(lon_raw[3:5])/60 + int(lon_raw[5:7])/3600
        if lat_h == 'S': lat = -lat
        if lon_h == 'W': lon = -lon
        return round(lat, 6), round(lon, 6)

    # -------- EXISTING FORMAT 2 --------
    m = re.match(r'^([NS])(\d{2})(\d{2})\s*([EW])(\d{3})(\d{2})$', s.replace(" ", ""))
    if m:
        lat_h, lat_d, lat_m, lon_h, lon_d, lon_m = m.groups()
        lat = int(lat_d) + int(lat_m)/60
        lon = int(lon_d) + int(lon_m)/60
        if lat_h == 'S': lat = -lat
        if lon_h == 'W': lon = -lon
        return round(lat, 6), round(lon, 6)

    # ====================== NEW BLOCK (FIX) ======================
    # Handle: "N16 14 00 E114 08 00" and variants
    tokens = re.split(r'\s+', s)

    try:
        if len(tokens) >= 6:
            # Case: N 16 14 00 E 114 08 00
            if tokens[0] in ['N', 'S'] and tokens[4] in ['E', 'W']:
                lat_h = tokens[0]
                lat_d = float(tokens[1])
                lat_m = float(tokens[2])
                lat_s = float(tokens[3])

                lon_h = tokens[4]
                lon_d = float(tokens[5])
                lon_m = float(tokens[6]) if len(tokens) > 6 else 0
                lon_s = float(tokens[7]) if len(tokens) > 7 else 0

            # Case: N16 14 00 E114 08 00
            elif tokens[0][0] in ['N', 'S'] and tokens[3][0] in ['E', 'W']:
                lat_h = tokens[0][0]
                lat_d = float(tokens[0][1:])
                lat_m = float(tokens[1])
                lat_s = float(tokens[2])

                lon_h = tokens[3][0]
                lon_d = float(tokens[3][1:])
                lon_m = float(tokens[4])
                lon_s = float(tokens[5]) if len(tokens) > 5 else 0

            else:
                raise ValueError

            lat = lat_d + lat_m / 60 + lat_s / 3600
            lon = lon_d + lon_m / 60 + lon_s / 3600

            if lat_h == 'S':
                lat = -lat
            if lon_h == 'W':
                lon = -lon

            return round(lat, 6), round(lon, 6)
    except:
        pass
    # ============================================================

    # -------- EXISTING DMS WITH SYMBOLS --------
    dms = re.findall(r'(\d+)[°\s]+(\d+)[\'\s]+(\d+)[\"\s]*([NS])', s)
    dms_lon = re.findall(r'(\d+)[°\s]+(\d+)[\'\s]+(\d+)[\"\s]*([EW])', s)
    if dms and dms_lon:
        lat_d, lat_m, lat_s, lat_h = dms[0]
        lon_d, lon_m, lon_s, lon_h = dms_lon[0]
        lat = int(lat_d) + int(lat_m)/60 + int(lat_s)/3600
        lon = int(lon_d) + int(lon_m)/60 + int(lon_s)/3600
        if lat_h == 'S': lat = -lat
        if lon_h == 'W': lon = -lon
        return round(lat, 6), round(lon, 6)

    # -------- EXISTING DECIMAL WITH HEMISPHERE --------
    lat_match = re.search(r'([-+]?\d+(?:\.\d+)?)\s*([NS])', s)
    lon_match = re.search(r'([-+]?\d+(?:\.\d+)?)\s*([EW])', s)
    if lat_match and lon_match:
        lat = float(lat_match.group(1))
        lon = float(lon_match.group(1))
        if lat_match.group(2) == 'S': lat = -abs(lat)
        if lon_match.group(2) == 'W': lon = -abs(lon)
        return round(lat, 6), round(lon, 6)

    # -------- EXISTING PURE DECIMAL --------
    m = re.match(r'\s*([-+]?\d+(?:\.\d+)?)\s*[, \s]\s*([-+]?\d+(?:\.\d+)?)', s)
    if m:
        return round(float(m.group(1)), 6), round(float(m.group(2)), 6)

    return None, None

# def convert_to_compact(raw_str):
#     if not raw_str or str(raw_str).strip() == "":
#         return ""
#     s = str(raw_str).upper().replace(" ", "").strip()
#     def dd_to_formatted(dd, is_lat=True):
#         hemi = 'N' if (is_lat and dd >= 0) else 'S' if is_lat else 'E' if (dd >= 0) else 'W'
#         d = abs(dd)
#         deg = int(d)
#         min_full = (d - deg) * 60
#         minute = int(min_full)
#         sec_full = (min_full - minute) * 60
#         sec = int(sec_full + 0.5)
#         if sec == 60:
#             sec = 0
#             minute += 1
#             if minute == 60:
#                 minute = 0
#                 deg += 1
#         if is_lat:
#             return f"{hemi}{deg:02d} {minute:02d} {sec:02d}"
#         else:
#             return f"{hemi}{deg:03d} {minute:02d} {sec:02d}"
#     m_full = re.match(r'^([NS])(\d{2})(\d{2})(\d{2})([EW])(\d{3})(\d{2})(\d{2})$', s)
#     if m_full:
#         g = m_full.groups()
#         has_sec = True
#     else:
#         m = re.match(r'^([NS])(\d{2})(\d{2})([EW])(\d{3})(\d{2})$', s)
#         if m:
#             g = m.groups()
#             has_sec = False
#         else:
#             m2 = re.search(r'([NS])\s*(\d{2})(\d{2})\s*([EW])\s*(\d{3})(\d{2})', s)
#             if m2:
#                 g = m2.groups()
#                 has_sec = False
#             else:
#                 g = None
#     if g is not None:
#         if has_sec:
#             lat_h, lat_dg_str, lat_mn_str, lat_sc_str, lon_h, lon_dg_str, lon_mn_str, lon_sc_str = g
#             lat_dd = int(lat_dg_str) + int(lat_mn_str) / 60 + int(lat_sc_str) / 3600
#             if lat_h == "S": lat_dd = -lat_dd
#             lon_dd = int(lon_dg_str) + int(lon_mn_str) / 60 + int(lon_sc_str) / 3600
#             if lon_h == "W": lon_dd = -lon_dd
#         else:
#             lat_h, lat_dg_str, lat_mn_str, lon_h, lon_dg_str, lon_mn_str = g
#             lat_dd = int(lat_dg_str) + int(lat_mn_str) / 60
#             if lat_h == "S": lat_dd = -lat_dd
#             lon_dd = int(lon_dg_str) + int(lon_mn_str) / 60
#             if lon_h == "W": lon_dd = -lon_dd
#         return dd_to_formatted(lat_dd, True) + " " + dd_to_formatted(lon_dd, False)
#     latlon = parse_coordinates(raw_str)
#     if not latlon or latlon[0] is None:
#         return ""
#     lat_dd, lon_dd = latlon
#     return dd_to_formatted(lat_dd, True) + " " + dd_to_formatted(lon_dd, False)

#convert_to_compact (as of 16 July 2026) edit 1

# def convert_to_compact(raw_str):
#     if not raw_str or str(raw_str).strip() == "":
#         return ""
   
#     s = str(raw_str).upper().replace(" ", "").strip()
   
#     def dd_to_formatted(dd, is_lat=True):
#         hemi = 'N' if (is_lat and dd >= 0) else 'S' if is_lat else 'E' if (dd >= 0) else 'W'
#         d = abs(dd)
#         deg = int(d)
#         min_full = (d - deg) * 60
#         minute = int(min_full)
#         sec_full = (min_full - minute) * 60
       
#         # === FIXED: Proper handling when seconds reach 60.00 ===
#         seconds = round(sec_full, 2)
        
#         if seconds >= 60.0:          # Carry over to next minute
#             seconds = 0.0
#             minute += 1
#             if minute == 60:
#                 minute = 0
#                 deg += 1
        
#         sec_str = f"{seconds:.2f}"
       
#         if is_lat:
#             return f"{hemi}{deg:02d} {minute:02d} {sec_str}"
#         else:
#             return f"{hemi}{deg:03d} {minute:02d} {sec_str}"
   
#     # === Parsing logic remains the same ===
#     m_full = re.match(r'^([NS])(\d{2})(\d{2})(\d{2})([EW])(\d{3})(\d{2})(\d{2})$', s)
#     if m_full:
#         g = m_full.groups()
#         has_sec = True
#     else:
#         m = re.match(r'^([NS])(\d{2})(\d{2})([EW])(\d{3})(\d{2})$', s)
#         if m:
#             g = m.groups()
#             has_sec = False
#         else:
#             m2 = re.search(r'([NS])\s*(\d{2})(\d{2})\s*([EW])\s*(\d{3})(\d{2})', s)
#             if m2:
#                 g = m2.groups()
#                 has_sec = False
#             else:
#                 g = None
   
#     if g is not None:
#         if has_sec:
#             lat_h, lat_dg_str, lat_mn_str, lat_sc_str, lon_h, lon_dg_str, lon_mn_str, lon_sc_str = g
#             lat_dd = int(lat_dg_str) + int(lat_mn_str) / 60 + int(lat_sc_str) / 3600
#             if lat_h == "S": lat_dd = -lat_dd
#             lon_dd = int(lon_dg_str) + int(lon_mn_str) / 60 + int(lon_sc_str) / 3600
#             if lon_h == "W": lon_dd = -lon_dd
#         else:
#             lat_h, lat_dg_str, lat_mn_str, lon_h, lon_dg_str, lon_mn_str = g
#             lat_dd = int(lat_dg_str) + int(lat_mn_str) / 60
#             if lat_h == "S": lat_dd = -lat_dd
#             lon_dd = int(lon_dg_str) + int(lon_mn_str) / 60
#             if lon_h == "W": lon_dd = -lon_dd
#         return dd_to_formatted(lat_dd, True) + " " + dd_to_formatted(lon_dd, False)
   
#     # Fallback to parse_coordinates
#     latlon = parse_coordinates(raw_str)
#     if not latlon or latlon[0] is None:
#         return ""
   
#     lat_dd, lon_dd = latlon
#     return dd_to_formatted(lat_dd, True) + " " + dd_to_formatted(lon_dd, False)


#convert_to_compact (as of 16 July 2026) edit 2

def convert_to_compact(raw_str):
    if not raw_str or str(raw_str).strip() == "":
        return ""
   
    s = str(raw_str).upper().replace(" ", "").strip()
   
    def dd_to_formatted(dd, is_lat=True):
        hemi = 'N' if (is_lat and dd >= 0) else 'S' if is_lat else 'E' if (dd >= 0) else 'W'
        d = abs(dd)
        deg = int(d)
        min_full = (d - deg) * 60
        minute = int(min_full)
        sec_full = (min_full - minute) * 60
        
        # === FIXED: Proper carry-over for 60 seconds ===
        seconds = round(sec_full, 2)
        if seconds >= 60.0:
            seconds = 0.0
            minute += 1
            if minute == 60:
                minute = 0
                deg += 1
        
        # === NEW LOGIC: Decide format based on whether there are decimal seconds ===
        if seconds == int(seconds):   # No decimal part (whole seconds)
            sec_str = f"{int(seconds):02d}"          # e.g., "00", "52"
        else:
            sec_str = f"{seconds:.2f}"               # e.g., "00.50", "52.75"
        
        if is_lat:
            return f"{hemi}{deg:02d} {minute:02d} {sec_str}"
        else:
            return f"{hemi}{deg:03d} {minute:02d} {sec_str}"
   
    # Parsing logic (unchanged)
    m_full = re.match(r'^([NS])(\d{2})(\d{2})(\d{2})([EW])(\d{3})(\d{2})(\d{2})$', s)
    if m_full:
        g = m_full.groups()
        has_sec = True
    else:
        m = re.match(r'^([NS])(\d{2})(\d{2})([EW])(\d{3})(\d{2})$', s)
        if m:
            g = m.groups()
            has_sec = False
        else:
            m2 = re.search(r'([NS])\s*(\d{2})(\d{2})\s*([EW])\s*(\d{3})(\d{2})', s)
            if m2:
                g = m2.groups()
                has_sec = False
            else:
                g = None
   
    if g is not None:
        if has_sec:
            lat_h, lat_dg_str, lat_mn_str, lat_sc_str, lon_h, lon_dg_str, lon_mn_str, lon_sc_str = g
            lat_dd = int(lat_dg_str) + int(lat_mn_str) / 60 + int(lat_sc_str) / 3600
            if lat_h == "S": lat_dd = -lat_dd
            lon_dd = int(lon_dg_str) + int(lon_mn_str) / 60 + int(lon_sc_str) / 3600
            if lon_h == "W": lon_dd = -lon_dd
        else:
            lat_h, lat_dg_str, lat_mn_str, lon_h, lon_dg_str, lon_mn_str = g
            lat_dd = int(lat_dg_str) + int(lat_mn_str) / 60
            if lat_h == "S": lat_dd = -lat_dd
            lon_dd = int(lon_dg_str) + int(lon_mn_str) / 60
            if lon_h == "W": lon_dd = -lon_dd
        return dd_to_formatted(lat_dd, True) + " " + dd_to_formatted(lon_dd, False)
   
    # Fallback using parse_coordinates
    latlon = parse_coordinates(raw_str)
    if not latlon or latlon[0] is None:
        return ""
   
    lat_dd, lon_dd = latlon
    return dd_to_formatted(lat_dd, True) + " " + dd_to_formatted(lon_dd, False)

def coordinates_to_wkt():
    """Generate MULTIPOLYGON WKT from current drop zones in session state"""
    polygons = []
    for i in range(1, 5):
        dz_key = f"DZ{i}"
        coords = st.session_state.dz_vertices.get(dz_key, [])
        valid_coords = [c.strip() for c in coords if c.strip()]
        
        if len(valid_coords) < 3:
            continue
        
        points = []
        for coord in valid_coords:
            try:
                # Reuse your existing parser
                lat, lon = parse_coordinates(coord)
                if lat is not None and lon is not None:
                    points.append((lon, lat))  # WKT order: (longitude, latitude)
            except:
                continue
        
        if len(points) < 3:
            continue
        
        # Close polygon
        if points[0] != points[-1]:
            points.append(points[0])
        
        poly_text = ",".join(f"{lon:.6f} {lat:.6f}" for lon, lat in points)
        polygons.append(f"(({poly_text}))")
    
    if not polygons:
        return ""
    elif len(polygons) == 1:
        return f"MULTIPOLYGON({polygons[0]})"
    else:
        return f"MULTIPOLYGON({','.join(polygons)})"

def utc_window_to_phst(window_utc: str) -> str:
    try:
        s = window_utc.upper().replace("UTC", "").strip()
        start_str, end_str = [x.strip() for x in s.split("-")]
        start_utc = datetime.strptime(start_str, "%H%M")
        end_utc = datetime.strptime(end_str, "%H%M")
        if end_utc <= start_utc: end_utc += timedelta(days=1)
        start_ph = start_utc + timedelta(hours=8)
        end_ph = end_utc + timedelta(hours=8)
        return f"{start_ph.strftime('%I:%M %p').lstrip('0')} - {end_ph.strftime('%I:%M %p').lstrip('0')}"
    except:
        return window_utc

def validate_window_format(hhmm):
    try:
        s = str(hhmm).strip()
        if not re.match(r'^\d{3,4}$', s): return False
        return 0 <= int(s) <= 2359
    except:
        return False

def format_window(start, end):
    if not validate_window_format(start) or not validate_window_format(end):
        return None
    return f"{str(start).zfill(4)}-{str(end).zfill(4)} UTC"

# # ====================== NOTAM PDF PARSER ======================
# def extract_notam_data(uploaded_file):
#     if PdfReader is None:
#         st.error("❌ `pypdf` is not installed. Run: `pip install pypdf`")
#         return [], {"b_time": "", "c_time": ""}
#     try:
#         reader = PdfReader(uploaded_file)
#         text = "".join(page.extract_text() or "" for page in reader.pages)
#         text_upper = text.upper()
#         coord_pattern = r'(\d{6}[NS])\s*(\d{7}[EW])'
#         matches = re.findall(coord_pattern, text_upper)
#         coord_strings = [f"{lat} {lon}" for lat, lon in matches]
#         if len(coord_strings) > 3 and coord_strings[0] == coord_strings[-1]:
#             coord_strings = coord_strings[:-1]
#         b_match = re.search(r'B\)\s*(\d{10})', text_upper)
#         b_time = b_match.group(1)[-4:] if b_match and len(b_match.group(1)) >= 10 else ""
#         c_match = re.search(r'C\)\s*(\d{10})', text_upper)
#         c_time = c_match.group(1)[-4:] if c_match and len(c_match.group(1)) >= 10 else ""
#         if not c_time:
#             c_range = re.search(r'C\)\s*(\d{4})-(\d{4})', text_upper)
#             if c_range:
#                 c_time = c_range.group(2)
#         return coord_strings, {"b_time": b_time, "c_time": c_time}
#     except Exception as e:
#         st.error(f"Error parsing {uploaded_file.name}: {str(e)}")
#         return [], {"b_time": "", "c_time": ""}

#NOTAM PDF PARSER (Updated for 16 July 2026)
# ====================== NOTAM PDF PARSER ======================
def extract_notam_data(uploaded_file):
    if PdfReader is None:
        st.error("❌ `pypdf` is not installed. Run: `pip install pypdf`")
        return [], {"b_time": "", "c_time": ""}
    
    try:
        reader = PdfReader(uploaded_file)
        text = "".join(page.extract_text() or "" for page in reader.pages)
        text_upper = text.upper()
        
        # ====================== IMPROVED COORDINATE EXTRACTION ======================
        coord_strings = []
        
        # 1. Compact format with decimals: 210652.31N 1235521.69E
        pattern1 = r'(\d{6}\.\d+[NS])\s*(\d{7}\.\d+[EW])'
        matches = re.findall(pattern1, text_upper)
        for lat, lon in matches:
            coord_strings.append(f"{lat} {lon}")
        
        # 2. Compact without decimal: 210652N 1235521E (your original)
        pattern2 = r'(\d{6}[NS])\s*(\d{7}[EW])'
        matches = re.findall(pattern2, text_upper)
        for lat, lon in matches:
            coord_strings.append(f"{lat} {lon}")
        
        # 3. Spaced format with decimal seconds: N21 06 52.31 E123 55 21.69
        pattern3 = r'([NS]\d{2}\s+\d{2}\s+\d{2}\.\d+)\s*([EW]\d{3}\s+\d{2}\s+\d{2}\.\d+)'
        matches = re.findall(pattern3, text_upper)
        for lat, lon in matches:
            coord_strings.append(f"{lat} {lon}")
        
        # 4. Spaced format without decimal (N21 06 52 E123 55 21)
        pattern4 = r'([NS]\d{2}\s+\d{2}\s+\d{2})\s*([EW]\d{3}\s+\d{2}\s+\d{2})'
        matches = re.findall(pattern4, text_upper)
        for lat, lon in matches:
            coord_strings.append(f"{lat} {lon}")
        
        # Remove duplicates while preserving order
        seen = set()
        unique_coords = []
        for c in coord_strings:
            if c not in seen:
                seen.add(c)
                unique_coords.append(c)
        
        # Remove closing coordinate if it repeats the first (common in NOTAMs)
        if len(unique_coords) > 3 and unique_coords[0] == unique_coords[-1]:
            unique_coords = unique_coords[:-1]
        
        # ====================== TIME EXTRACTION ======================
        b_match = re.search(r'B\)\s*(\d{10})', text_upper)
        b_time = b_match.group(1)[-4:] if b_match and len(b_match.group(1)) >= 10 else ""
        
        c_match = re.search(r'C\)\s*(\d{10})', text_upper)
        c_time = c_match.group(1)[-4:] if c_match and len(c_match.group(1)) >= 10 else ""
        
        if not c_time:
            c_range = re.search(r'C\)\s*(\d{4})-(\d{4})', text_upper)
            if c_range:
                c_time = c_range.group(2)
        
        return unique_coords, {"b_time": b_time, "c_time": c_time}
    
    except Exception as e:
        st.error(f"Error parsing {uploaded_file.name}: {str(e)}")
        return [], {"b_time": "", "c_time": ""}

# ====================== CACHED DATA & MAP ======================
@st.cache_data
def load_mapping_data(shape_dir: str):
    data = {}
    csv_path = os.path.join(shape_dir, "Launch_Centers_Coords.csv")
    if os.path.exists(csv_path):
        try:
            df = pd.read_csv(csv_path)
            data["launch_sites"] = {str(row["Place"]).strip(): (float(row["Lat"]), float(row["Lon"]))
                                   for _, row in df.iterrows()}
        except:
            data["launch_sites"] = {}
    else:
        data["launch_sites"] = {}
    key_path = os.path.join(shape_dir, "Updated_Key_Locations.shp")
    data["key_locations"] = {}
    if os.path.exists(key_path):
        try:
            key_gdf = gpd.read_file(key_path)
            possible = ["Place", "PlaceName", "Name", "NAME", "name", "Label", "Location", "SiteName"]
            name_field = next((f for f in possible if f in key_gdf.columns), None)
            if not name_field:
                non_geom = [c for c in key_gdf.columns if c.lower() not in ("geometry", "geom")]
                name_field = non_geom[0] if non_geom else None
            for idx, row in key_gdf.iterrows():
                if row.geometry and row.geometry.geom_type == "Point":
                    label = str(row[name_field]).strip() if name_field else f"Key_{idx}"
                    data["key_locations"][label] = (row.geometry.y, row.geometry.x)
        except:
            data["key_locations"] = {}
    else:
        data["key_locations"] = {}
    data["manila_fir"] = os.path.join(shape_dir, "Manila_FIR_boundary.shp")
    data["baseline"] = os.path.join(shape_dir, "PH_Baseline.shp")
    data["eez"] = os.path.join(shape_dir, "eez.shp")
    return data

def create_folium_map(launch_site_value, dropzones, shape_dir):
    loaded = load_mapping_data(shape_dir)
    polygons, debris_points = [], []
    for dz_id, dz in dropzones.items():
        pts = [parse_coordinates(c) for c in dz["vertices"]]
        pts = [(lat, lon) for lat, lon in pts if lat is not None]
        if len(pts) >= 3:
            polygons.append((dz_id, pts))
        dpts = [parse_coordinates(c) for c in dz["debris"]]
        dpts = [(lat, lon) for lat, lon in dpts if lat is not None]
        if dpts:
            debris_points.append((dz_id, dpts))
    if not polygons and not debris_points:
        return folium.Map(location=[14.5, 120.5], zoom_start=6)
    all_coords = [p for _, pts in polygons for p in pts] + [p for _, pts in debris_points for p in pts]
    avg_lat = sum(p[0] for p in all_coords) / len(all_coords)
    avg_lon = sum(p[1] for p in all_coords) / len(all_coords)
    m = folium.Map(location=[avg_lat, avg_lon], zoom_start=7)
    if launch_site_value in loaded["launch_sites"]:
        lat, lon = loaded["launch_sites"][launch_site_value]
        folium.Marker([lat, lon], popup=f"Launch Site: {launch_site_value}",
                      icon=folium.Icon(color="green", icon="rocket", prefix="fa")).add_to(m)
    for name, (lat, lon) in loaded["key_locations"].items():
        folium.Marker([lat, lon], popup=name,
                      icon=folium.Icon(color="red", icon="location-crosshairs", prefix="fa")).add_to(m)
    for path, color, weight, fill, name in [
        (loaded["manila_fir"], "orange", 1.5, False, "Manila FIR"),
        (loaded["baseline"], "gold", 1.5, False, "PH Baseline"),
        (loaded["eez"], "blue", 1.5, True, "PH EEZ")
    ]:
        if not os.path.exists(path):
            continue
        try:
            gdf = gpd.read_file(path)
            for _, row in gdf.iterrows():
                geom = row.geometry
                if not geom: continue
                polys = [geom] if geom.geom_type == "Polygon" else list(geom.geoms) if hasattr(geom, "geoms") else []
                for poly in polys:
                    x, y = poly.exterior.xy
                    if fill:
                        folium.Polygon(list(zip(y, x)), color=color, weight=weight, fill=True, fill_opacity=0.05).add_to(m)
                    else:
                        folium.PolyLine(list(zip(y, x)), color=color, weight=weight).add_to(m)
        except:
            pass
    colors = ['darkgreen', 'green', 'darkblue', 'purple']
    for idx, (dzid, pts) in enumerate(polygons):
        folium.Polygon(pts, popup=f"{dzid}", color=colors[idx % 4], fill=True, fill_opacity=0.3).add_to(m)
        for vi, (lat, lon) in enumerate(pts):
            folium.CircleMarker([lat, lon], radius=4, fill=True, popup=f"{dzid} V{vi+1}").add_to(m)
    for _, (dzid, dpts) in enumerate(debris_points):
        for di, (lat, lon) in enumerate(dpts):
            folium.Marker([lat, lon], popup=f"{dzid} debris {di+1}",
                          icon=folium.Icon(color="black", icon="trash", prefix="fa")).add_to(m)
    # Rocket ground track
    def polygon_centroid(pts):
        poly = Polygon([(lon, lat) for lat, lon in pts])
        c = poly.centroid
        return (c.y, c.x)
    def bearing_between(p1, p2):
        lat1, lon1 = map(math.radians, p1)
        lat2, lon2 = map(math.radians, p2)
        dlon = lon2 - lon1
        x = math.sin(dlon) * math.cos(lat2)
        y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
        return (math.degrees(math.atan2(x, y)) + 360) % 360
    def destination_point(lat, lon, bearing_deg, distance_km):
        R = 6371.0
        bearing = math.radians(bearing_deg)
        lat1 = math.radians(lat)
        lon1 = math.radians(lon)
        d_div_r = distance_km / R
        lat2 = math.asin(math.sin(lat1)*math.cos(d_div_r) + math.cos(lat1)*math.sin(d_div_r)*math.cos(bearing))
        lon2 = lon1 + math.atan2(math.sin(bearing)*math.sin(d_div_r)*math.cos(lat1),
                                 math.cos(d_div_r) - math.sin(lat1)*math.sin(lat2))
        return (math.degrees(lat2), math.degrees(lon2))
    centroids = [polygon_centroid(pts) for _, pts in polygons if len(pts) >= 3]
    if launch_site_value in loaded["launch_sites"] and centroids:
        route = [loaded["launch_sites"][launch_site_value]]
        route.extend(centroids)
        if len(centroids) >= 2:
            last_bearing = bearing_between(centroids[-2], centroids[-1])
        else:
            last_bearing = bearing_between(route[0], centroids[-1])
        ext = destination_point(centroids[-1][0], centroids[-1][1], last_bearing, 1852)
        route.append(ext)
        folium.PolyLine(route, color="black", weight=2, dash_array="5,10", popup="Rocket Ground Track").add_to(m)
    legend = """
    <div id='maplegend' class='maplegend' 
         style='position: fixed; z-index: 9999; 
                background-color: rgba(255, 255, 255, 0.85); 
                border-radius: 8px; 
                padding: 12px 15px; 
                font-size: 14px;
                bottom: 30px; left: 30px;
                border: 2px solid rgba(0,0,0,0.4);
                box-shadow: 0 4px 12px rgba(0,0,0,0.25);'>
        
        <div style="color: #000000; font-weight: bold; margin-bottom: 10px; font-size: 15px;">Legend</div>
        
        <div class='legend-scale'>
            <ul class='legend-labels' style="list-style: none; padding: 0; margin: 0;">
                <li style="color: #000000; margin-bottom: 8px;">
                    <i class="fa fa-rocket fa-2x" style="color:#006400; margin-right: 8px;"></i> Launch Site</li>
                <li style="color: #000000; margin-bottom: 8px;">
                    <i class="fa fa-location-crosshairs fa-2x" style="color:red; margin-right: 8px;"></i> Key Locations</li>
                <li style="color: #000000; margin-bottom: 8px;">
                    <i class="fa fa-trash fa-2x" style="color:#333; margin-right: 8px;"></i> Debris Point</li>
                <li style="color: #000000; margin-bottom: 8px;">
                    <span style="display:inline-block; width:30px; border-bottom: 2.5px dashed #333; margin-right: 8px; vertical-align: middle;"></span> Rocket Ground Track</li>
                <li style="color: #000000; margin-bottom: 8px;">
                    <span style='background:#FFA500; width:18px; height:18px; display:inline-block; margin-right:8px;'></span> Manila FIR</li>\
                <li style="color: #000000; margin-bottom: 8px;">
                    <span style='background:#FFD700; width:18px; height:18px; display:inline-block; margin-right:8px;'></span> PH Baseline</li>
                <li style="color: #000000; margin-bottom: 8px;">
                    <span style='background:#0000CD; width:18px; height:18px; display:inline-block; margin-right:8px;'></span> PH EEZ</li>
                <li style="color: #000000; margin-bottom: 8px;">
                    <span style='background:#008000; width:18px; height:18px; display:inline-block; margin-right:8px;'></span> Dropzone</li>
            </ul>
        </div>
    </div>

    <style type='text/css'>
        .maplegend .legend-scale ul { margin: 0; padding: 0; }
        .maplegend .legend-scale ul li { 
            list-style: none; 
            line-height: 22px; 
            margin-bottom: 6px; 
            font-size: 14px;
        }
        .maplegend ul.legend-labels li span { 
            float: left; 
            margin-right: 8px; 
        }
    </style>
    """

    m.get_root().html.add_child(folium.Element(legend))
    return m

# def create_historical_map(df, shape_dir):
#     loaded = load_mapping_data(shape_dir)
    
#     m = folium.Map(location=[14.5, 120.5], zoom_start=5)

#     # ====================== LAUNCH SITES ======================
#     for site_name, (lat, lon) in loaded["launch_sites"].items():
#         folium.Marker(
#             [lat, lon],
#             popup=f"Launch Site: {site_name}",
#             icon=folium.Icon(color="green", icon="rocket", prefix="fa")
#         ).add_to(m)

#     # ====================== DROPZONES - CONSISTENT GREEN STYLE ======================
#     for idx, row in df.iterrows():
#         for dz_num in range(1, 5):
#             dz = f"DZ{dz_num}"
            
#             verts = [
#                 str(row.get(f"{dz} P{p}", "")).strip()
#                 for p in range(1, 9)
#                 if str(row.get(f"{dz} P{p}", "")).strip()
#             ]
            
#             pts = [parse_coordinates(v) for v in verts]
#             pts = [(lat, lon) for lat, lon in pts if lat is not None]

#             if len(pts) >= 3:
#                 folium.Polygon(
#                     pts,
#                     popup=f"{row.get('ROCKET NAME','')} | {dz}",
#                     color="#006400",           # Dark Green outline
#                     weight=2.5,                # Slightly thicker outline
#                     fill=True,
#                     fill_color="#32CD32",      # Lime Green fill
#                     fill_opacity=0.25,         # Semi-transparent
#                     opacity=0.9                # Outline opacity
#                 ).add_to(m)

#             # Debris points (unchanged)
#             debs = [
#                 str(row.get(f"DEB{dz_num} P{p}", "")).strip()
#                 for p in range(1, 5)
#                 if str(row.get(f"DEB{dz_num} P{p}", "")).strip()
#             ]
            
#             dpts = [parse_coordinates(d) for d in debs]
#             dpts = [(lat, lon) for lat, lon in dpts if lat is not None]

#             for lat, lon in dpts:
#                 folium.CircleMarker(
#                     [lat, lon],
#                     radius=3,
#                     color="black",
#                     fill=True,
#                     fill_opacity=0.7,
#                     popup=f"{row.get('ROCKET NAME','')} debris"
#                 ).add_to(m)

#     # ====================== OVERLAYS ======================
#     for path, color, weight, fill in [
#         (loaded["manila_fir"], "orange", 1.5, False),
#         (loaded["baseline"], "gold", 1.5, False),
#         (loaded["eez"], "blue", 1.5, True)
#     ]:
#         if not os.path.exists(path):
#             continue
#         try:
#             gdf = gpd.read_file(path)
#             for geom in gdf.geometry:
#                 if not geom:
#                     continue
#                 polys = [geom] if geom.geom_type == "Polygon" else list(geom.geoms) if hasattr(geom, "geoms") else []
#                 for poly in polys:
#                     x, y = poly.exterior.xy
#                     if fill:
#                         folium.Polygon(list(zip(y, x)), color=color, weight=weight, fill=True, fill_opacity=0.05).add_to(m)
#                     else:
#                         folium.PolyLine(list(zip(y, x)), color=color, weight=weight).add_to(m)
#         except:
#             pass

#     # ====================== LEGEND (Updated) ======================
#     legend = """
#     <div id='maplegend' class='maplegend' 
#          style='position: fixed; z-index: 9999; 
#                 background-color: rgba(255, 255, 255, 0.85); 
#                 border-radius: 8px; 
#                 padding: 12px 15px; 
#                 font-size: 14px;
#                 bottom: 30px; left: 30px;
#                 border: 2px solid rgba(0,0,0,0.4);
#                 box-shadow: 0 4px 12px rgba(0,0,0,0.25);'>
        
#         <div style="color: #000000; font-weight: bold; margin-bottom: 10px; font-size: 15px;">Legend</div>
        
#         <div class='legend-scale'>
#             <ul class='legend-labels' style="list-style: none; padding: 0; margin: 0;">
#                 <li style="color: #000000; margin-bottom: 8px;">
#                     <i class="fa fa-rocket fa-2x" style="color:#006400; margin-right: 8px;"></i> Launch Site</li>
#                 <li style="color: #000000; margin-bottom: 8px;">
#                     <i class="fa fa-trash fa-2x" style="color:#333; margin-right: 8px;"></i> Debris Point</li>
#                 <li style="color: #000000; margin-bottom: 8px;">
#                     <span style="display:inline-block; width:30px; border-bottom: 2.5px dashed #333; margin-right: 8px; vertical-align: middle;"></span> Rocket Ground Track</li>
#                 <li style="color: #000000; margin-bottom: 8px;">
#                     <span style='background:#006400; width:18px; height:18px; display:inline-block; margin-right:8px;'></span> Drop Zone</li>
#                 <li style="color: #000000; margin-bottom: 8px;">
#                     <span style='background:#FFA500; width:18px; height:18px; display:inline-block; margin-right:8px;'></span> Manila FIR</li>
#                 <li style="color: #000000; margin-bottom: 8px;">
#                     <span style='background:#FFD700; width:18px; height:18px; display:inline-block; margin-right:8px;'></span> PH Baseline</li>
#                 <li style="color: #000000; margin-bottom: 8px;">
#                     <span style='background:#0000CD; width:18px; height:18px; display:inline-block; margin-right:8px;'></span> PH EEZ</li>
#             </ul>
#         </div>
#     </div>

#     <style type='text/css'>
#         .maplegend .legend-scale ul { margin: 0; padding: 0; }
#         .maplegend .legend-scale ul li { 
#             list-style: none; 
#             line-height: 22px; 
#             margin-bottom: 6px; 
#             font-size: 14px;
#         }
#         .maplegend ul.legend-labels li span { 
#             float: left; 
#             margin-right: 8px; 
#         }
#     </style>
#     """
#     m.get_root().html.add_child(folium.Element(legend))

#     return m

def create_historical_map(df, shape_dir):
    loaded = load_mapping_data(shape_dir)
    
    m = folium.Map(location=[14.5, 120.5], zoom_start=5)

    # ====================== LAUNCH SITES ======================
    for site_name, (lat, lon) in loaded["launch_sites"].items():
        folium.Marker(
            [lat, lon],
            popup=f"Launch Site: {site_name}",
            icon=folium.Icon(color="green", icon="rocket", prefix="fa")
        ).add_to(m)

    # ====================== DROPZONES ======================
    for idx, row in df.iterrows():
        for dz_num in range(1, 5):
            dz = f"DZ{dz_num}"
            
            verts = [
                str(row.get(f"{dz} P{p}", "")).strip()
                for p in range(1, 9)
                if str(row.get(f"{dz} P{p}", "")).strip()
            ]
            
            pts = [parse_coordinates(v) for v in verts]
            pts = [(lat, lon) for lat, lon in pts if lat is not None]

            if len(pts) >= 3:
                folium.Polygon(
                    pts,
                    popup=f"{row.get('ROCKET NAME','')} | {dz}",
                    color="#006400",
                    weight=2.5,
                    fill=True,
                    fill_color="#32CD32",
                    fill_opacity=0.25,
                    opacity=0.9
                ).add_to(m)

            # Debris points from launches
            debs = [
                str(row.get(f"DEB{dz_num} P{p}", "")).strip()
                for p in range(1, 5)
                if str(row.get(f"DEB{dz_num} P{p}", "")).strip()
            ]
            
            dpts = [parse_coordinates(d) for d in debs]
            dpts = [(lat, lon) for lat, lon in dpts if lat is not None]

            for lat, lon in dpts:
                folium.CircleMarker(
                    [lat, lon],
                    radius=3,
                    color="black",
                    fill=True,
                    fill_opacity=0.7,
                    popup=f"{row.get('ROCKET NAME','')} debris"
                ).add_to(m)

    # ====================== DEBRIS RETRIEVAL LOCATIONS (NEW) ======================
    debris_csv = os.path.join(shape_dir, "debris.csv")
    if os.path.exists(debris_csv):
        try:
            debris_df = pd.read_csv(debris_csv)
            for _, row in debris_df.iterrows():
                lat = float(row["Lat"])
                lon = float(row["Lon"])
                place = str(row.get("Place", "")).strip()
                location = str(row.get("Location", "")).strip()

                folium.Marker(
                    [lat, lon],
                    popup=f"<b>Debris Retrieved</b><br>{place}<br>{location}",
                    tooltip=place,
                    icon=folium.Icon(color="purple", icon="recycle", prefix="fa")
                ).add_to(m)
        except Exception as e:
            st.warning(f"Could not load Debris Retrieval Locations: {e}")

    # ====================== OVERLAYS ======================
    for path, color, weight, fill in [
        (loaded["manila_fir"], "orange", 1.5, False),
        (loaded["baseline"], "gold", 1.5, False),
        (loaded["eez"], "blue", 1.5, True)
    ]:
        if not os.path.exists(path):
            continue
        try:
            gdf = gpd.read_file(path)
            for geom in gdf.geometry:
                if not geom:
                    continue
                polys = [geom] if geom.geom_type == "Polygon" else list(geom.geoms) if hasattr(geom, "geoms") else []
                for poly in polys:
                    x, y = poly.exterior.xy
                    if fill:
                        folium.Polygon(list(zip(y, x)), color=color, weight=weight, fill=True, fill_opacity=0.05).add_to(m)
                    else:
                        folium.PolyLine(list(zip(y, x)), color=color, weight=weight).add_to(m)
        except:
            pass

    # ====================== LEGEND ======================
    legend = """
    <div id='maplegend' class='maplegend' 
         style='position: fixed; z-index: 9999; 
                background-color: rgba(255, 255, 255, 0.85); 
                border-radius: 8px; 
                padding: 12px 15px; 
                font-size: 14px;
                bottom: 30px; left: 30px;
                border: 2px solid rgba(0,0,0,0.4);
                box-shadow: 0 4px 12px rgba(0,0,0,0.25);'>
        
        <div style="color: #000000; font-weight: bold; margin-bottom: 10px; font-size: 15px;">Legend</div>
        
        <div class='legend-scale'>
            <ul class='legend-labels' style="list-style: none; padding: 0; margin: 0;">
                <li style="color: #000000; margin-bottom: 8px;">
                    <i class="fa fa-rocket fa-2x" style="color:#006400; margin-right: 8px;"></i> Launch Site</li>
                <li style="color: #000000; margin-bottom: 8px;">
                    <i class="fa fa-trash fa-2x" style="color:#333; margin-right: 8px;"></i> Debris Point</li>
                <li style="color: #000000; margin-bottom: 8px;">
                    <i class="fa fa-recycle fa-2x" style="color:purple; margin-right: 8px;"></i> Debris Retrieved</li>
                <li style="color: #000000; margin-bottom: 8px;">
                    <span style='background:#006400; width:18px; height:18px; display:inline-block; margin-right:8px;'></span> Drop Zone</li>
                <li style="color: #000000; margin-bottom: 8px;">
                    <span style='background:#FFA500; width:18px; height:18px; display:inline-block; margin-right:8px;'></span> Manila FIR</li>
                <li style="color: #000000; margin-bottom: 8px;">
                    <span style='background:#FFD700; width:18px; height:18px; display:inline-block; margin-right:8px;'></span> PH Baseline</li>
                <li style="color: #000000; margin-bottom: 8px;">
                    <span style='background:#0000CD; width:18px; height:18px; display:inline-block; margin-right:8px;'></span> PH EEZ</li>
            </ul>
        </div>
    </div>

    <style type='text/css'>
        .maplegend .legend-scale ul { margin: 0; padding: 0; }
        .maplegend .legend-scale ul li { 
            list-style: none; 
            line-height: 22px; 
            margin-bottom: 6px; 
            font-size: 14px;
        }
        .maplegend ul.legend-labels li span { 
            float: left; 
            margin-right: 8px; 
        }
    </style>
    """
    m.get_root().html.add_child(folium.Element(legend))

    return m

# ====================== UPDATED LAUNCH DASHBOARD (modern style) ======================
if st.session_state.selected == "Launch Dashboard":
    #st.title("Launch Dashboard")
    
    df = load_launch_database()
    
    if st.button("🔄 Refresh Data from Google Sheets"):
        load_launch_database.clear()
        st.rerun()
    
    if df.empty or len(df) == 0:
        st.info("No launches in the database yet.")
    else:
        st.markdown("""
        <style>

        /* KPI Card */
        .kpi-card {
            background: var(--secondaryBackgroundColor);
            border-radius: 15px;
            padding: 25px 15px;
            text-align: center;
            box-shadow: 0 4px 15px rgba(0,0,0,0.08);
            border: 2px solid rgba(0,0,0,0.12);
            height: 100%;
            transition: all 0.3s ease;
        }

            .kpi-card {
                border: 2px solid rgba(255,255,255,1);
                box-shadow: 0 4px 15px rgba(0,0,0,0.4);
            }
                    
        .kpi-card:hover {
            transform: translateY(-4px);
            box-shadow: 0 8px 20px rgba(0,0,0,0.25);
        }

        /* Text styles */
        .kpi-title {
            font-size: 1.1rem;
            color: var(--textColor);
            margin-bottom: 12px;
            font-weight: 500;
        }

        .kpi-value {
            font-size: 2.8rem;
            font-weight: 700;
            margin: 0;
        }

        /* Status colors */
        .critical { color: #d32f2f; }
        .high { color: #f57c00; }
        .developing { color: #1976d2; }

        </style>
        """, unsafe_allow_html=True)

        # total_launches = len(df)
        # today = datetime.now().date()
        # upcoming_launches = len(df[df["LAUNCH_DATE_PARSED"].dt.date >= today]) if "LAUNCH_DATE_PARSED" in df.columns else 0
        # china_launches = len(df[df["LAUNCHING STATE"] == "People's Republic of China"]) if "LAUNCHING STATE" in df.columns else 0
        # last_30_days = len(df[df["LAUNCH_DATE_PARSED"] >= (pd.Timestamp.now() - pd.Timedelta(days=30))]) if "LAUNCH_DATE_PARSED" in df.columns else 0

        # k1, k2, k3, k4 = st.columns(4)
        # with k1:
        #     st.markdown(f'<div class="kpi-card"><div class="kpi-title">Total Launches</div><div class="kpi-value">{total_launches}</div></div>', unsafe_allow_html=True)
        # with k2:
        #     st.markdown(f'<div class="kpi-card"><div class="kpi-title">Upcoming Launches</div><div class="kpi-value developing">{upcoming_launches}</div></div>', unsafe_allow_html=True)
        # with k3:
        #     st.markdown(f'<div class="kpi-card"><div class="kpi-title">Launches from China</div><div class="kpi-value high">{china_launches}</div></div>', unsafe_allow_html=True)
        # with k4:
        #     st.markdown(f'<div class="kpi-card"><div class="kpi-title">Last 30 Days</div><div class="kpi-value critical">{last_30_days}</div></div>', unsafe_allow_html=True)
        
        #pseudo code for new KPI cards
        total_launches = len(df)
        today = datetime.now().date()
        upcoming_launches = len(df[df["LAUNCH_DATE_PARSED"].dt.date >= today]) if "LAUNCH_DATE_PARSED" in df.columns else 0
        
        # === NEW: Total Drop Zone Count ===
        drop_zone_cols = ["ARCH_COUNT", "EEZ_COUNT", "FIR_COUNT", "OUTSIDE_COUNT"]
        total_drop_zones = 0
        for col in drop_zone_cols:
            if col in df.columns:
                total_drop_zones += pd.to_numeric(df[col], errors='coerce').fillna(0).sum()
        
        last_30_days = len(df[df["LAUNCH_DATE_PARSED"] >= (pd.Timestamp.now() - pd.Timedelta(days=30))]) if "LAUNCH_DATE_PARSED" in df.columns else 0

        k1, k2, k3, k4 = st.columns(4)
        with k1:
            st.markdown(f'<div class="kpi-card"><div class="kpi-title">Total Launches</div><div class="kpi-value">{total_launches}</div></div>', unsafe_allow_html=True)
        with k2:
            st.markdown(f'<div class="kpi-card"><div class="kpi-title">Upcoming Launches</div><div class="kpi-value developing">{upcoming_launches}</div></div>', unsafe_allow_html=True)
        with k3:
            st.markdown(f'<div class="kpi-card"><div class="kpi-title">Drop Zone Count</div><div class="kpi-value high">{int(total_drop_zones):,}</div></div>', unsafe_allow_html=True)
        with k4:
            st.markdown(f'<div class="kpi-card"><div class="kpi-title">Last 30 Days</div><div class="kpi-value critical">{last_30_days}</div></div>', unsafe_allow_html=True)

        st.divider()

 
    # ==================== MAIN LAYOUT: Wide Left + Tall Right ===================
    left_col, right_col = st.columns([2.8, 1.0])
        # ================== FILTERS ==================
        # Country Filter using LAUNCHING STATE
    if "LAUNCHING STATE" in df.columns:
        country_options = sorted(df["LAUNCHING STATE"].dropna().unique())
        selected_countries = st.multiselect(
            "Filter by Launching State:",
            options=country_options,
            default=country_options,   # Select all by default
            placeholder="Select states/countries..."
        )
    else:
        selected_countries = None
        st.warning("Launching State column not found. Showing all data.")
    
    #pseudo code for new country filter with pseudo names
    left_col, right_col = st.columns([2.8, 1.0])
    
    # # ================== FILTERS ==================
    # # Country Filter using LAUNCHING STATE (with Pseudo Names)
    # if "LAUNCHING STATE" in df.columns:
    #     country_options = sorted(df["LAUNCHING STATE"].dropna().unique())
        
    #     # Create mapping from real country to pseudo name
    #     country_mapping = {country: f"Country {chr(65 + i)}" 
    #                       for i, country in enumerate(country_options)}
        
    #     # Create reverse mapping for filtering later
    #     st.session_state.country_reverse_mapping = {v: k for k, v in country_mapping.items()}
        
    #     # Display pseudo names in the multiselect
    #     pseudo_options = list(country_mapping.values())
        
    #     selected_pseudo = st.multiselect(
    #         "Filter by Launching State:",
    #         options=pseudo_options,
    #         default=pseudo_options,  # Select all by default
    #         placeholder="Select states/countries..."
    #     )
        
    #     # Convert pseudo back to real country names for filtering
    #     selected_countries = [st.session_state.country_reverse_mapping[p] for p in selected_pseudo]
        
    # else:
    #     selected_countries = None
    #     st.warning("Launching State column not found. Showing all data.")
    # ==================== LEFT COLUMN ====================
    with left_col:
        # ==================== TOP ROW - Two Charts Side by Side ====================
        col1, col2 = st.columns([1.4, 1.4])

        # ==================== COLUMN 1: Dropzones per Maritime Zone ====================
        with col1:
            st.subheader("Launch Activity Trends")
            container1 = st.container(border=True, height=600)
            with container1:
                st.markdown("**Number of Drop Zone per Maritime Zone**")
               
                zone_view_option = st.radio(
                    "Select View:",
                    ["Yearly", "Monthly"],
                    horizontal=True,
                    label_visibility="collapsed",
                    key="zone_view_option"
                )
                
                df_graph = df.copy()
               
                if "LAUNCH_DATE_PARSED" in df_graph.columns and not df_graph.empty:
                   
                    if selected_countries and "LAUNCHING STATE" in df_graph.columns:
                        df_graph = df_graph[df_graph["LAUNCHING STATE"].isin(selected_countries)]
                   
                    # Create Period column
                    if zone_view_option == "Monthly":
                        df_graph["Period"] = df_graph["LAUNCH_DATE_PARSED"].dt.to_period("M").astype(str)
                        x_title = "Month"
                        tickformat = "%b %Y"
                        dtick = "M1"
                    else: 
                        df_graph["Period"] = df_graph["LAUNCH_DATE_PARSED"].dt.to_period("Y").astype(str)
                        x_title = "Year"
                        tickformat = "%Y"
                        dtick = "M12"
                   
                    # === FIX: Convert count columns to numeric first ===
                    count_cols = ["ARCH_COUNT", "EEZ_COUNT", "FIR_COUNT", "OUTSIDE_COUNT"]
                    for col in count_cols:
                        if col in df_graph.columns:
                            df_graph[col] = pd.to_numeric(df_graph[col], errors='coerce').fillna(0)
                   
                    # Aggregate
                    agg_dict = {
                        "ARCH_COUNT": "sum",
                        "EEZ_COUNT": "sum",
                        "FIR_COUNT": "sum",
                        "OUTSIDE_COUNT": "sum"
                    }
                   
                    zone_trend = df_graph.groupby("Period").agg(agg_dict).reset_index()
                    zone_trend["Period"] = pd.to_datetime(zone_trend["Period"])
                   
                    zone_melted = pd.melt(
                        zone_trend,
                        id_vars=["Period"],
                        value_vars=count_cols,
                        var_name="Zone",
                        value_name="Count"
                    )
                   
                    zone_melted["Zone"] = zone_melted["Zone"].replace({
                        "ARCH_COUNT": "Archipelagic Waters",
                        "EEZ_COUNT": "EEZ",
                        "FIR_COUNT": "Manila FIR",
                        "OUTSIDE_COUNT": "Outside PH Zones"
                    })
                   
                    fig_zone = px.bar(
                        zone_melted,
                        x="Period",
                        y="Count",
                        color="Zone",
                        labels={"Count": "Number of Drop Zone", "Period": x_title},
                        template="plotly",
                        height=460,
                        color_discrete_sequence=["#FFD700", "#00008B", "#FFA500", "#808080"]
                    )
                   
                    fig_zone.update_layout(
                        xaxis_title=x_title,
                        yaxis_title="Number of Drop Zone",
                        barmode='stack',
                        showlegend=True,
                        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, font=dict(size=11)),
                        xaxis_title_font=dict(size=15),
                        yaxis_title_font=dict(size=15),
                        xaxis_tickfont=dict(size=12),
                        yaxis_tickfont=dict(size=12),
                        margin=dict(t=30, b=40, l=10, r=10)
                    )
                   
                    fig_zone.update_xaxes(tickformat=tickformat, dtick=dtick)
                    fig_zone.update_traces(marker=dict(line=dict(color="white", width=1.5)))
                   
                    st.plotly_chart(fig_zone, use_container_width=True)
                   
                    total_dzs = int(zone_melted["Count"].sum())
                    st.caption(f"**Total Drop Zone**: {total_dzs:,} across all selected periods")

        # ==================== COLUMN 2: Launches by Country ====================
        with col2:
            st.subheader("Launches by Country")
            container2 = st.container(border=True, height=600)   # ← Same height as left
            with container2:
                st.markdown("**Overall Distribution**")
                st.space()
               
                df_graph = df.copy()
               
                # No country filter - full picture
                if "LAUNCHING STATE" in df_graph.columns and not df_graph.empty:
                    country_counts = df_graph["LAUNCHING STATE"].value_counts().reset_index()
                    country_counts.columns = ["Country", "Count"]
                   
                    fig_pie = px.pie(
                        country_counts,
                        names="Country",
                        values="Count",
                        hole=0.45,
                        color_discrete_sequence=px.colors.qualitative.Set2,
                        template="simple_white",
                        height=420                     # ← Adjusted to fit container
                    )
                    fig_pie.update_traces(
                        textinfo="percent+label",
                        marker=dict(line=dict(color="white", width=2)),
                        textfont_size=14
                    )
                    fig_pie.update_layout(
                        margin=dict(t=30, b=30)
                    )
                    st.plotly_chart(fig_pie, use_container_width=True)
                else:
                    st.info("No country data available.")
        
        # #pseudo code for new Launches by Country chart with anonymization
        # with col2:
        #     st.subheader("Launches by Country")
        #     container2 = st.container(border=True, height=600)
        #     with container2:
        #         st.markdown("**Overall Distribution**")
        #         st.space()
              
        #         df_graph = df.copy()
              
        #         if "LAUNCHING STATE" in df_graph.columns and not df_graph.empty:
        #             # === ANONYMIZE COUNTRIES ===
        #             country_counts = df_graph["LAUNCHING STATE"].value_counts().reset_index()
        #             country_counts.columns = ["Country", "Count"]
                    
        #             # Create pseudo names: Country A, Country B, Country C...
        #             country_counts = country_counts.sort_values("Count", ascending=False).reset_index(drop=True)
        #             country_counts["Pseudo_Country"] = [f"Country {chr(65 + i)}" for i in range(len(country_counts))]
                    
        #             # For hover/legend, we can keep original if needed, but display pseudo
        #             fig_pie = px.pie(
        #                 country_counts,
        #                 names="Pseudo_Country",
        #                 values="Count",
        #                 hole=0.45,
        #                 color_discrete_sequence=px.colors.qualitative.Set2,
        #                 template="simple_white",
        #                 height=420,
        #                 title="Launches by Country"
        #             )
                    
        #             fig_pie.update_traces(
        #                 textinfo="percent+label",
        #                 marker=dict(line=dict(color="white", width=2)),
        #                 textfont_size=14
        #             )
                    
        #             fig_pie.update_layout(
        #                 margin=dict(t=40, b=30),
        #                 showlegend=True
        #             )
                    
        #             st.plotly_chart(fig_pie, use_container_width=True)
                    
        #             # Optional: Show mapping table below the chart (for your reference)
        #             # with st.expander("View Country Mapping (Original → Pseudo)"):
        #             #     mapping_df = country_counts[["Pseudo_Country", "Country"]].rename(columns={"Country": "Original Country"})
        #             #     st.dataframe(mapping_df, hide_index=True, use_container_width=True)
                        
        #         else:
        #             st.info("No country data available.")

        # ==================== MONTHLY LAUNCH TREND (Below the two charts) ====================
        st.subheader("Launch Trend")

        # ================== CHART CONTAINER ==================
        container = st.container(border=True)
        with container:
            st.markdown("**Launch Count Trend**")

            
            # Radio button for Monthly/Yearly
            view_option = st.radio(
                "Select Trend View:",
                ["Yearly", "Monthly"],
                horizontal=True,
                label_visibility="collapsed"
            )
            
            df_graph = df.copy()
            
            # Apply Launching State Filter
            if selected_countries and "LAUNCHING STATE" in df_graph.columns:
                df_graph = df_graph[df_graph["LAUNCHING STATE"].isin(selected_countries)]
            
            if "LAUNCH_DATE_PARSED" in df_graph.columns and not df_graph.empty:
                
                if view_option == "Monthly":
                    df_graph["Period"] = df_graph["LAUNCH_DATE_PARSED"].dt.to_period("M").astype(str)
                    x_title = "Month"
                    tickformat = "%b %Y"
                    dtick = "M1"
                
                else:  # Yearly
                    df_graph["Period"] = df_graph["LAUNCH_DATE_PARSED"].dt.to_period("Y").astype(str)
                    x_title = "Year"
                    tickformat = "%Y"
                    dtick = "M12"
            
                # Aggregate
                count_df = df_graph.groupby("Period").size().reset_index(name="Launch Count")
                count_df["Period"] = pd.to_datetime(count_df["Period"])
            
                # Plot
                fig_line = px.line(
                    count_df,
                    x="Period",
                    y="Launch Count",
                    markers=True,
                    template="plotly",
                    height=420
                )
            
                fig_line.update_xaxes(
                    tickformat=tickformat,
                    dtick=dtick
                )
            
                fig_line.update_traces(
                    line=dict(color="#1976d2", width=4),
                    marker=dict(size=10),
                    fill='tozeroy',
                    fillcolor='rgba(25,118,210,0.2)'
                )
            
                fig_line.update_layout(
                    xaxis_title=x_title,
                    yaxis_title="Launch Count",
                    xaxis_title_font=dict(size=16),
                    yaxis_title_font=dict(size=16),
                    xaxis_tickfont=dict(size=13),
                    yaxis_tickfont=dict(size=13)
                )
            
                st.plotly_chart(fig_line, use_container_width=True)
                
                # Summary
                total_launches = len(df_graph)
                num_selected = len(selected_countries) if selected_countries else "all"
                st.caption(f"Showing **{total_launches:,} launches** from {num_selected} launching states")
                
            else:
                st.info("No launch date data available for trend analysis.")

    # ==================== RIGHT COLUMN - Extended Launches ====================
    with right_col:
        st.subheader("Launches")
        container_activities = st.container(border=True, height=1100)  # Adjust this height if needed
        
        with container_activities:
            events_df = df.copy()
            if "LAUNCH_DATE_PARSED" in events_df.columns:
                events_df = events_df.sort_values("LAUNCH_DATE_PARSED", ascending=False)
            
            # Increased number of visible launches
            for _, row in events_df.head(14).iterrows():
                date_str = row.get("LAUNCH DATE", "—")
                rocket = row.get("ROCKET NAME", "Unknown")
                site = row.get("LAUNCH CENTER", "—")
                country = row.get("LAUNCHING STATE", "—")
                
                st.markdown(f"""
                <div style="background-color: #1a4fa3; padding: 12px; border-radius: 10px;
                            margin-bottom: 10px; color: white; font-size: 0.95rem;">
                    <strong>{rocket}</strong><br>
                    <small>{date_str} • {site}</small><br>
                    <small>{country}</small>
                </div>
                """, unsafe_allow_html=True)
            
            if len(events_df) > 14:
                st.caption(f"... and {len(events_df)-14} more")

    #pseudo code for new right column with anonymization
        # ==================== RIGHT COLUMN - Extended Launches ====================
    # with right_col:
    #     st.subheader("Launches")
    #     container_activities = st.container(border=True, height=1100)
       
    #     with container_activities:
    #         events_df = df.copy()
            
    #         if "LAUNCH_DATE_PARSED" in events_df.columns:
    #             events_df = events_df.sort_values("LAUNCH_DATE_PARSED", ascending=False)
            
    #         # ==================== PSEUDO ANONYMIZATION ====================
    #         if not events_df.empty:
    #             # Country Pseudo
    #             if "LAUNCHING STATE" in events_df.columns:
    #                 unique_countries = sorted(events_df["LAUNCHING STATE"].dropna().unique())
    #                 country_mapping = {c: f"Country {chr(65 + i)}" for i, c in enumerate(unique_countries)}
    #                 events_df["Pseudo_Country"] = events_df["LAUNCHING STATE"].map(country_mapping)
                
    #             # Rocket Name Pseudo
    #             if "ROCKET NAME" in events_df.columns:
    #                 unique_rockets = sorted(events_df["ROCKET NAME"].dropna().unique())
    #                 rocket_mapping = {r: f"Rocket {chr(65 + i)}" for i, r in enumerate(unique_rockets)}
    #                 events_df["Pseudo_Rocket"] = events_df["ROCKET NAME"].map(rocket_mapping)
                
    #             # Launch Center Pseudo
    #             if "LAUNCH CENTER" in events_df.columns:
    #                 unique_sites = sorted(events_df["LAUNCH CENTER"].dropna().unique())
    #                 site_mapping = {s: f"Site {chr(65 + i)}" for i, s in enumerate(unique_sites)}
    #                 events_df["Pseudo_Site"] = events_df["LAUNCH CENTER"].map(site_mapping)
                
    #             # Launch Date Pseudo (e.g., Launch #001, Launch #002, ...)
    #             events_df = events_df.reset_index(drop=True)
    #             events_df["Pseudo_Date"] = [f"Launch #{str(i+1).zfill(3)}" for i in range(len(events_df))]
            
    #         # ==================== DISPLAY ====================
    #         for _, row in events_df.head(14).iterrows():
    #             pseudo_date = row.get("Pseudo_Date", "—")
    #             pseudo_rocket = row.get("Pseudo_Rocket", row.get("ROCKET NAME", "Unknown"))
    #             pseudo_site = row.get("Pseudo_Site", row.get("LAUNCH CENTER", "—"))
    #             pseudo_country = row.get("Pseudo_Country", row.get("LAUNCHING STATE", "—"))
               
    #             st.markdown(f"""
    #             <div style="background-color: #1a4fa3; padding: 12px; border-radius: 10px;
    #                         margin-bottom: 10px; color: white; font-size: 0.95rem;">
    #                 <strong>{pseudo_rocket}</strong><br>
    #                 <small>{pseudo_date} • {pseudo_site}</small><br>
    #                 <small>{pseudo_country}</small>
    #             </div>
    #             """, unsafe_allow_html=True)
           
    #         if len(events_df) > 14:
    #             st.caption(f"... and {len(events_df)-14} more")

    # ==================== HISTORICAL MAP ====================
    st.divider()
    st.subheader("Historical Drop Zone Map")
    st.caption("**Since 2022 up to present**")

    if not df.empty:
        hist_map = create_historical_map(df, st.session_state.shape_dir)
        
        # Stronger centering
        padding1, main_col, padding2 = st.columns([1, 8, 1])
        
        with main_col:
            st_folium(
                hist_map, 
                width="100%",     # Let it take full column width
                height=700, 
                returned_objects=[]
            )
    else:
        col1, col2, col3 = st.columns([3, 4, 3])
        with col2:
            st.info("No data available for mapping.")

#====================== TAB ROUTING ======================
# elif st.session_state.selected == "Upcoming Launches":
#     st.subheader("Upcoming Rocket Launches")
#     st.caption("Live from Google Sheets • Future launches only")
    
#     df = load_launch_database()
#     if st.button("🔄 Refresh Data from Google Sheets"):
#         load_launch_database.clear()
#         st.rerun()
 
#     if df.empty:
#         st.info("No launch data available yet. Add some launches using the NOTAM Parser tab.")
#         st.stop()
  
#     # Filter upcoming launches
#     today = datetime.now().date()
#     upcoming_df = df.copy()
#     if "LAUNCH_DATE_PARSED" in upcoming_df.columns and not upcoming_df.empty:
#         upcoming_df = upcoming_df[upcoming_df["LAUNCH_DATE_PARSED"].dt.date >= today].copy()
#         upcoming_df = upcoming_df.sort_values("LAUNCH_DATE_PARSED").reset_index(drop=True)
#     else:
#         upcoming_df = pd.DataFrame()
  
#     if upcoming_df.empty:
#         st.success("No upcoming launches scheduled at the moment.")
#         st.balloons()
#         st.info("Check back later or add new launches in the NOTAM Parser tab.")
#     else:
#         st.success(f"**{len(upcoming_df)} Upcoming Launch(es) Scheduled**")
        
#         if "selected_upcoming_idx" not in st.session_state or st.session_state.selected_upcoming_idx >= len(upcoming_df):
#             st.session_state.selected_upcoming_idx = 0
            
#         # Launch Selector
#         launch_options = [
#             f"{row.get('LAUNCH DATE', '—')} • {row.get('ROCKET NAME', 'Unknown')} @ {row.get('LAUNCH CENTER', '—')}"
#             for _, row in upcoming_df.iterrows()
#         ]
    
#         selected_label = st.selectbox(
#             "Select a Launch",
#             options=launch_options,
#             index=st.session_state.selected_upcoming_idx,
#             key="upcoming_selector"
#         )
    
#         selected_idx = launch_options.index(selected_label)
#         if selected_idx != st.session_state.selected_upcoming_idx:
#             st.session_state.selected_upcoming_idx = selected_idx
      
#         st.divider()

#         # ====================== LAYOUT ======================
#         selected_row = upcoming_df.iloc[selected_idx]
#         left_col, right_col = st.columns([1, 3])

#         # ==================== LEFT SIDE - LAUNCH INFO + DROPZONE ====================
#         with left_col:
#             # --- Launch Information ---
#             st.markdown("**Launch Information**")
            
#             with st.container(border=True):

#                 raw_launch_date = selected_row.get('LAUNCH DATE', '—')
#                 window_time = selected_row.get('START TO END TIME', '—')
                
#                 # Convert date from DD/MM/YYYY to "10 July 2026" format
#                 try:
#                     if raw_launch_date != '—' and raw_launch_date:
#                         # Try parsing DD/MM/YYYY format
#                         parsed_date = datetime.strptime(raw_launch_date.strip(), "%m/%d/%Y")
#                         formatted_date = parsed_date.strftime("%d %B %Y")
#                     else:
#                         formatted_date = raw_launch_date
#                 except:
#                     formatted_date = raw_launch_date  # fallback if format is different
                
#                 window_display = f"{formatted_date} between {window_time}" if formatted_date != '—' else window_time
                
#                 metrics = [
#                     ("Rocket Name", selected_row.get('ROCKET NAME', '—')),
#                     ("Launch Center", selected_row.get('LAUNCH CENTER', '—')),
#                     ("Launching State", selected_row.get('LAUNCHING STATE', '—')),
#                     ("Date and Time (PhST)", window_display),
#                 ]
                
#                 col1, col2 = st.columns(2)
                
#                 # ==================== FIRST ROW ====================
#                 with col1:
#                     title, value = metrics[0]
#                     with st.container(height=130, border=False):
#                         st.markdown(f":blue-badge[{title}]")
#                         st.markdown(f"""
#                             <div style="font-size: 1.32rem; font-weight: 700; text-align: left; margin-top: 8px; line-height: 1.3;">
#                                 {value}
#                             </div>
#                         """, unsafe_allow_html=True)
                
#                 with col2:
#                     title, value = metrics[1]
#                     with st.container(height=130, border=False):
#                         st.markdown(f":blue-badge[{title}]")
#                         st.markdown(f"""
#                             <div style="font-size: 1.32rem; font-weight: 700; text-align: left; margin-top: 8px; line-height: 1.3;">
#                                 {value}
#                             </div>
#                         """, unsafe_allow_html=True)

#                 # ==================== SECOND ROW ====================
#                 with col1:
#                     title, value = metrics[2]
#                     with st.container(height=130, border=False):
#                         st.markdown(f":blue-badge[{title}]")
#                         st.markdown(f"""
#                             <div style="font-size: 1.32rem; font-weight: 700; text-align: left; margin-top: 8px; line-height: 1.3;">
#                                 {value}
#                             </div>
#                         """, unsafe_allow_html=True)
                
#                 with col2:
#                     title, value = metrics[3]
#                     with st.container(height=130, border=False):
#                         st.markdown(f":blue-badge[{title}]")
#                         st.markdown(f"""
#                             <div style="font-size: 1.32rem; font-weight: 700; text-align: left; margin-top: 8px; line-height: 1.3;">
#                                 {value}
#                             </div>
#                         """, unsafe_allow_html=True)

#             st.markdown("**Drop Zone Summary**")
            
#             # Color mapping
#             zone_colors = {
#                 "Archipelagic Waters": "#FFD700",
#                 "EEZ": "#00008B",
#                 "Manila FIR": "#FFA500",
#                 "Outside PH Zones": "#808080"
#             }
            
#             counts = {
#                 "Archipelagic Waters": int(selected_row.get("ARCH_COUNT", 0) or 0),
#                 "EEZ": int(selected_row.get("EEZ_COUNT", 0) or 0),
#                 "Manila FIR": int(selected_row.get("FIR_COUNT", 0) or 0),
#                 "Outside PH Zones": int(selected_row.get("OUTSIDE_COUNT", 0) or 0)
#             }
            
#             # Each zone in its own contained box
#             dz_cols = st.columns(2)
#             for i, (zone, count) in enumerate(counts.items()):
#                 color = zone_colors.get(zone, "#1a4fa3")
#                 with dz_cols[i % 2]:
#                     with st.container(border=True):
#                         st.markdown(f":blue-badge[{zone}]")
#                         st.markdown(f"""
#                             <div style="font-size: 3.0rem; font-weight: 700; color: {color}; text-align: center; margin-bottom: 16px;">
#                                 {count}
#                             </div>
#                         """, unsafe_allow_html=True)

#         # ==================== RIGHT SIDE - MAP ====================
#         with right_col:
#             st.markdown("**Drop Zone Map**")
#             dropzones_db = {}
#             for dz_num in range(1, 5):
#                 dz = f"DZ{dz_num}"
#                 verts = [str(selected_row.get(f"{dz} P{p}", "")).strip()
#                         for p in range(1, 9) if str(selected_row.get(f"{dz} P{p}", "")).strip()]
#                 debs = [str(selected_row.get(f"DEB{dz_num} P{p}", "")).strip()
#                        for p in range(1, 5) if str(selected_row.get(f"DEB{dz_num} P{p}", "")).strip()]
#                 dropzones_db[dz] = {"vertices": verts, "debris": debs}
          
#             preview_map = create_folium_map(
#                 selected_row.get("LAUNCH CENTER", ""),
#                 dropzones_db,
#                 st.session_state.shape_dir
#             )
         
#             st_folium(
#                 preview_map,
#                 width="100%",
#                 height=720,
#                 returned_objects=[],
#                 key=f"upcoming_map_{selected_idx}"
#             )
      
#         st.caption(f"Launch **#{selected_idx + 1}** of **{len(upcoming_df)}**")

# #Upcoming Launches Tab (latest - 09 July 2026)
# elif st.session_state.selected == "Upcoming Launches":
#     st.subheader("Upcoming Rocket Launches")
#     st.caption("Live from Google Sheets • Future launches only")
   
#     df = load_launch_database()
#     if st.button("🔄 Refresh Data from Google Sheets"):
#         load_launch_database.clear()
#         st.rerun()
   
#     if df.empty:
#         st.info("No launch data available yet. Add some launches using the NOTAM Parser tab.")
#         st.stop()
 
#     # Filter upcoming launches
#     today = datetime.now().date()
#     upcoming_df = df.copy()
#     if "LAUNCH_DATE_PARSED" in upcoming_df.columns and not upcoming_df.empty:
#         upcoming_df = upcoming_df[upcoming_df["LAUNCH_DATE_PARSED"].dt.date >= today].copy()
#         upcoming_df = upcoming_df.sort_values("LAUNCH_DATE_PARSED").reset_index(drop=True)
#     else:
#         upcoming_df = pd.DataFrame()
 
#     if upcoming_df.empty:
#         st.success("No upcoming launches scheduled at the moment.")
#         st.balloons()
#         st.info("Check back later or add new launches in the NOTAM Parser tab.")
#     else:
#         st.success(f"**{len(upcoming_df)} Upcoming Launch(es) Scheduled**")
       
#         if "selected_upcoming_idx" not in st.session_state or st.session_state.selected_upcoming_idx >= len(upcoming_df):
#             st.session_state.selected_upcoming_idx = 0
           
#         # Launch Selector
#         launch_options = [
#             f"{row.get('LAUNCH DATE', '—')} • {row.get('ROCKET NAME', 'Unknown')} @ {row.get('LAUNCH CENTER', '—')}"
#             for _, row in upcoming_df.iterrows()
#         ]
   
#         selected_label = st.selectbox(
#             "Select a Launch",
#             options=launch_options,
#             index=st.session_state.selected_upcoming_idx,
#             key="upcoming_selector"
#         )
   
#         selected_idx = launch_options.index(selected_label)
#         if selected_idx != st.session_state.selected_upcoming_idx:
#             st.session_state.selected_upcoming_idx = selected_idx
     
#         st.divider()

#         # ====================== LAYOUT ======================
#         selected_row = upcoming_df.iloc[selected_idx]
#         original_row_index = selected_row.name  # Used for deletion

#         # ====================== DELETE SECTION ======================
#         col_del1, col_del2 = st.columns([1, 4])
#         with col_del1:
#             if st.button("🗑️ Delete This Launch", type="secondary", use_container_width=True):
#                 st.session_state["delete_confirm"] = True
#                 st.session_state["delete_launch_date"] = selected_row.get('LAUNCH DATE')
#                 st.session_state["delete_rocket_name"] = selected_row.get('ROCKET NAME')
#                 st.rerun()

#         if st.session_state.get("delete_confirm"):
#             st.error("⚠️ **Warning:** This will permanently delete the launch from the database.")
#             st.warning("After deletion, go to the **NOTAM Parser** tab to submit the updated version.")
            
#             c1, c2 = st.columns(2)
#             with c1:
#                 if st.button("✅ Yes, Delete Launch", type="primary"):
#                     try:
#                         ws = get_launch_worksheet()
#                         # Refresh worksheet
#                         spreadsheet = ws.spreadsheet
#                         ws = spreadsheet.worksheet(ws.title)
                        
#                         # Find the row by matching Launch Date + Rocket Name
#                         data = ws.get_all_records()
#                         target_row = None
                        
#                         for idx, row in enumerate(data, start=2):  # start=2 because row 1 is header
#                             if (str(row.get('LAUNCH DATE', '')).strip() == str(st.session_state["delete_launch_date"]).strip() and
#                                 str(row.get('ROCKET NAME', '')).strip() == str(st.session_state["delete_rocket_name"]).strip()):
#                                 target_row = idx
#                                 break
                        
#                         if target_row is None:
#                             st.error("❌ Could not find the launch in the sheet.")
#                         else:
#                             ws.delete_rows(target_row)
#                             st.success(f"✅ Launch deleted successfully (Row {target_row})!")
                            
#                             load_launch_database.clear()
#                             if "delete_confirm" in st.session_state:
#                                 del st.session_state["delete_confirm"]
#                             if "delete_launch_date" in st.session_state:
#                                 del st.session_state["delete_launch_date"]
#                             if "delete_rocket_name" in st.session_state:
#                                 del st.session_state["delete_rocket_name"]
#                             st.rerun()
#                     except Exception as e:
#                         st.error(f"❌ Delete failed: {str(e)}")
#             with c2:
#                 if st.button("❌ Cancel", type="secondary"):
#                     if "delete_confirm" in st.session_state:
#                         del st.session_state["delete_confirm"]
#                     if "delete_launch_date" in st.session_state:
#                         del st.session_state["delete_launch_date"]
#                     if "delete_rocket_name" in st.session_state:
#                         del st.session_state["delete_rocket_name"]
#                     st.rerun()

#         # ==================== LEFT SIDE - LAUNCH INFO + DROPZONE ====================
#         left_col, right_col = st.columns([1, 3])
#         with left_col:
#             st.markdown("**Launch Information**")
#             with st.container(border=True):
#                 raw_launch_date = selected_row.get('LAUNCH DATE', '—')
#                 window_time = selected_row.get('START TO END TIME', '—')
               
#                 try:
#                     if raw_launch_date != '—' and raw_launch_date:
#                         parsed_date = datetime.strptime(raw_launch_date.strip(), "%m/%d/%Y")
#                         formatted_date = parsed_date.strftime("%d %B %Y")
#                     else:
#                         formatted_date = raw_launch_date
#                 except:
#                     formatted_date = raw_launch_date
               
#                 window_display = f"{formatted_date} between {window_time}" if formatted_date != '—' else window_time
               
#                 metrics = [
#                     ("Rocket Name", selected_row.get('ROCKET NAME', '—')),
#                     ("Launch Center", selected_row.get('LAUNCH CENTER', '—')),
#                     ("Launching State", selected_row.get('LAUNCHING STATE', '—')),
#                     ("Date and Time (PhST)", window_display),
#                 ]
               
#                 col1, col2 = st.columns(2)
               
#                 with col1:
#                     title, value = metrics[0]
#                     with st.container(height=130, border=False):
#                         st.markdown(f":blue-badge[{title}]")
#                         st.markdown(f"""
#                             <div style="font-size: 1.32rem; font-weight: 700; text-align: left; margin-top: 8px; line-height: 1.3;">
#                                 {value}
#                             </div>
#                         """, unsafe_allow_html=True)
               
#                 with col2:
#                     title, value = metrics[1]
#                     with st.container(height=130, border=False):
#                         st.markdown(f":blue-badge[{title}]")
#                         st.markdown(f"""
#                             <div style="font-size: 1.32rem; font-weight: 700; text-align: left; margin-top: 8px; line-height: 1.3;">
#                                 {value}
#                             </div>
#                         """, unsafe_allow_html=True)

#                 with col1:
#                     title, value = metrics[2]
#                     with st.container(height=130, border=False):
#                         st.markdown(f":blue-badge[{title}]")
#                         st.markdown(f"""
#                             <div style="font-size: 1.32rem; font-weight: 700; text-align: left; margin-top: 8px; line-height: 1.3;">
#                                 {value}
#                             </div>
#                         """, unsafe_allow_html=True)
               
#                 with col2:
#                     title, value = metrics[3]
#                     with st.container(height=130, border=False):
#                         st.markdown(f":blue-badge[{title}]")
#                         st.markdown(f"""
#                             <div style="font-size: 1.32rem; font-weight: 700; text-align: left; margin-top: 8px; line-height: 1.3;">
#                                 {value}
#                             </div>
#                         """, unsafe_allow_html=True)

#             st.markdown("**Drop Zone Summary**")
#             zone_colors = {
#                 "Archipelagic Waters": "#FFD700",
#                 "EEZ": "#1976d2",
#                 "Manila FIR": "#FFA500",
#                 "Outside PH Zones": "#808080"
#             }
           
#             counts = {
#                 "Archipelagic Waters": int(selected_row.get("ARCH_COUNT", 0) or 0),
#                 "EEZ": int(selected_row.get("EEZ_COUNT", 0) or 0),
#                 "Manila FIR": int(selected_row.get("FIR_COUNT", 0) or 0),
#                 "Outside PH Zones": int(selected_row.get("OUTSIDE_COUNT", 0) or 0)
#             }
           
#             dz_cols = st.columns(2)
#             for i, (zone, count) in enumerate(counts.items()):
#                 color = zone_colors.get(zone, "#1a4fa3")
#                 with dz_cols[i % 2]:
#                     with st.container(border=True):
#                         st.markdown(f":blue-badge[{zone}]")
#                         st.markdown(f"""
#                             <div style="font-size: 3.0rem; font-weight: 700; color: {color}; text-align: center; margin-bottom: 16px;">
#                                 {count}
#                             </div>
#                         """, unsafe_allow_html=True)

#         # ==================== RIGHT SIDE - MAP ====================
#         with right_col:
#             st.markdown("**Drop Zone Map**")
#             dropzones_db = {}
#             for dz_num in range(1, 5):
#                 dz = f"DZ{dz_num}"
#                 verts = [str(selected_row.get(f"{dz} P{p}", "")).strip()
#                         for p in range(1, 9) if str(selected_row.get(f"{dz} P{p}", "")).strip()]
#                 debs = [str(selected_row.get(f"DEB{dz_num} P{p}", "")).strip()
#                        for p in range(1, 5) if str(selected_row.get(f"DEB{dz_num} P{p}", "")).strip()]
#                 dropzones_db[dz] = {"vertices": verts, "debris": debs}
         
#             preview_map = create_folium_map(
#                 selected_row.get("LAUNCH CENTER", ""),
#                 dropzones_db,
#                 st.session_state.shape_dir
#             )
        
#             st_folium(
#                 preview_map,
#                 width="100%",
#                 height=720,
#                 returned_objects=[],
#                 key=f"upcoming_map_{selected_idx}"
#             )
     
#         st.caption(f"Launch **#{selected_idx + 1}** of **{len(upcoming_df)}**")
#         st.info("💡 **To update a launch:** Delete it here, then go to the **NOTAM Parser** tab and submit the corrected version.")

#with map_layout

elif st.session_state.selected == "Upcoming Launches":
    st.subheader("Upcoming Rocket Launches")
    st.caption("Live from Google Sheets • Future launches only")
   
    df = load_launch_database()
    if st.button("🔄 Refresh Data from Google Sheets"):
        load_launch_database.clear()
        st.rerun()
   
    if df.empty:
        st.info("No launch data available yet. Add some launches using the NOTAM Parser tab.")
        st.stop()
 
    # Filter upcoming launches
    today = datetime.now().date()
    upcoming_df = df.copy()
    if "LAUNCH_DATE_PARSED" in upcoming_df.columns and not upcoming_df.empty:
        upcoming_df = upcoming_df[upcoming_df["LAUNCH_DATE_PARSED"].dt.date >= today].copy()
        upcoming_df = upcoming_df.sort_values("LAUNCH_DATE_PARSED").reset_index(drop=True)
    else:
        upcoming_df = pd.DataFrame()
 
    if upcoming_df.empty:
        st.success("No upcoming launches scheduled at the moment.")
        st.balloons()
        st.info("Check back later or add new launches in the NOTAM Parser tab.")
    else:
        st.success(f"**{len(upcoming_df)} Upcoming Launch(es) Scheduled**")
       
        if "selected_upcoming_idx" not in st.session_state or st.session_state.selected_upcoming_idx >= len(upcoming_df):
            st.session_state.selected_upcoming_idx = 0
           
        # Launch Selector
        launch_options = [
            f"{row.get('LAUNCH DATE', '—')} • {row.get('ROCKET NAME', 'Unknown')} @ {row.get('LAUNCH CENTER', '—')}"
            for _, row in upcoming_df.iterrows()
        ]
   
        selected_label = st.selectbox(
            "Select a Launch",
            options=launch_options,
            index=st.session_state.selected_upcoming_idx,
            key="upcoming_selector"
        )
   
        selected_idx = launch_options.index(selected_label)
        if selected_idx != st.session_state.selected_upcoming_idx:
            st.session_state.selected_upcoming_idx = selected_idx
     
        st.divider()

        # ====================== LAYOUT ======================
        selected_row = upcoming_df.iloc[selected_idx]
        original_row_index = selected_row.name  # Used for deletion

        # # ====================== DELETE SECTION ======================
        # col_del1, col_del2 = st.columns([1, 4])
        # with col_del1:
        #     if st.button("🗑️ Delete This Launch", type="secondary", use_container_width=True):
        #         st.session_state["delete_confirm"] = True
        #         st.session_state["delete_launch_date"] = selected_row.get('LAUNCH DATE')
        #         st.session_state["delete_rocket_name"] = selected_row.get('ROCKET NAME')
        #         st.rerun()

        # if st.session_state.get("delete_confirm"):
        #     st.error("⚠️ **Warning:** This will permanently delete the launch from the database.")
        #     st.warning("After deletion, go to the **NOTAM Parser** tab to submit the updated version.")
            
        #     c1, c2 = st.columns(2)
        #     with c1:
        #         if st.button("✅ Yes, Delete Launch", type="primary"):
        #             try:
        #                 ws = get_launch_worksheet()
        #                 spreadsheet = ws.spreadsheet
        #                 ws = spreadsheet.worksheet(ws.title)
                        
        #                 data = ws.get_all_records()
        #                 target_row = None
                        
        #                 for idx, row in enumerate(data, start=2):
        #                     if (str(row.get('LAUNCH DATE', '')).strip() == str(st.session_state["delete_launch_date"]).strip() and
        #                         str(row.get('ROCKET NAME', '')).strip() == str(st.session_state["delete_rocket_name"]).strip()):
        #                         target_row = idx
        #                         break
                        
        #                 if target_row is None:
        #                     st.error("❌ Could not find the launch in the sheet.")
        #                 else:
        #                     ws.delete_rows(target_row)
        #                     st.success(f"✅ Launch deleted successfully (Row {target_row})!")
                            
        #                     load_launch_database.clear()
        #                     for key in ["delete_confirm", "delete_launch_date", "delete_rocket_name"]:
        #                         if key in st.session_state:
        #                             del st.session_state[key]
        #                     st.rerun()
        #             except Exception as e:
        #                 st.error(f"❌ Delete failed: {str(e)}")
        #     with c2:
        #         if st.button("❌ Cancel", type="secondary"):
        #             for key in ["delete_confirm", "delete_launch_date", "delete_rocket_name"]:
        #                 if key in st.session_state:
        #                     del st.session_state[key]
        #             st.rerun()

        # ==================== LEFT SIDE - LAUNCH INFO + DROPZONE ====================
        left_col, right_col = st.columns([1, 3])
        with left_col:
            st.markdown("**Launch Information**")
            with st.container(border=True):
                raw_launch_date = selected_row.get('LAUNCH DATE', '—')
                window_time = selected_row.get('START TO END TIME', '—')
               
                try:
                    if raw_launch_date != '—' and raw_launch_date:
                        parsed_date = datetime.strptime(raw_launch_date.strip(), "%m/%d/%Y")
                        formatted_date = parsed_date.strftime("%d %B %Y")
                    else:
                        formatted_date = raw_launch_date
                except:
                    formatted_date = raw_launch_date
               
                window_display = f"{formatted_date} between {window_time}" if formatted_date != '—' else window_time
               
                metrics = [
                    ("Rocket Name", selected_row.get('ROCKET NAME', '—')),
                    ("Launch Center", selected_row.get('LAUNCH CENTER', '—')),
                    ("Launching State", selected_row.get('LAUNCHING STATE', '—')),
                    ("Date and Time (PhST)", window_display),
                ]
               
                col1, col2 = st.columns(2)
               
                with col1:
                    title, value = metrics[0]
                    with st.container(height=130, border=False):
                        st.markdown(f":blue-badge[{title}]")
                        st.markdown(f"""
                            <div style="font-size: 1.32rem; font-weight: 700; text-align: left; margin-top: 8px; line-height: 1.3;">
                                {value}
                            </div>
                        """, unsafe_allow_html=True)
               
                with col2:
                    title, value = metrics[1]
                    with st.container(height=130, border=False):
                        st.markdown(f":blue-badge[{title}]")
                        st.markdown(f"""
                            <div style="font-size: 1.32rem; font-weight: 700; text-align: left; margin-top: 8px; line-height: 1.3;">
                                {value}
                            </div>
                        """, unsafe_allow_html=True)

                with col1:
                    title, value = metrics[2]
                    with st.container(height=130, border=False):
                        st.markdown(f":blue-badge[{title}]")
                        st.markdown(f"""
                            <div style="font-size: 1.32rem; font-weight: 700; text-align: left; margin-top: 8px; line-height: 1.3;">
                                {value}
                            </div>
                        """, unsafe_allow_html=True)
               
                with col2:
                    title, value = metrics[3]
                    with st.container(height=130, border=False):
                        st.markdown(f":blue-badge[{title}]")
                        st.markdown(f"""
                            <div style="font-size: 1.32rem; font-weight: 700; text-align: left; margin-top: 8px; line-height: 1.3;">
                                {value}
                            </div>
                        """, unsafe_allow_html=True)

            st.markdown("**Drop Zone Summary**")
            zone_colors = {
                "Archipelagic Waters": "#FFD700",
                "EEZ": "#1976d2",
                "Manila FIR": "#FFA500",
                "Outside PH Zones": "#808080"
            }
           
            counts = {
                "Archipelagic Waters": int(selected_row.get("ARCH_COUNT", 0) or 0),
                "EEZ": int(selected_row.get("EEZ_COUNT", 0) or 0),
                "Manila FIR": int(selected_row.get("FIR_COUNT", 0) or 0),
                "Outside PH Zones": int(selected_row.get("OUTSIDE_COUNT", 0) or 0)
            }
           
            dz_cols = st.columns(2)
            for i, (zone, count) in enumerate(counts.items()):
                color = zone_colors.get(zone, "#1a4fa3")
                with dz_cols[i % 2]:
                    with st.container(border=True):
                        st.markdown(f":blue-badge[{zone}]")
                        st.markdown(f"""
                            <div style="font-size: 3.0rem; font-weight: 700; color: {color}; text-align: center; margin-bottom: 16px;">
                                {count}
                            </div>
                        """, unsafe_allow_html=True)

        # ==================== RIGHT SIDE - MAP + QGIS LAYOUT ====================
        with right_col:
            st.markdown("**Drop Zone Map**")
            dropzones_db = {}
            for dz_num in range(1, 5):
                dz = f"DZ{dz_num}"
                verts = [str(selected_row.get(f"{dz} P{p}", "")).strip()
                        for p in range(1, 9) if str(selected_row.get(f"{dz} P{p}", "")).strip()]
                debs = [str(selected_row.get(f"DEB{dz_num} P{p}", "")).strip()
                       for p in range(1, 5) if str(selected_row.get(f"DEB{dz_num} P{p}", "")).strip()]
                dropzones_db[dz] = {"vertices": verts, "debris": debs}
         
            preview_map = create_folium_map(
                selected_row.get("LAUNCH CENTER", ""),
                dropzones_db,
                st.session_state.shape_dir
            )
        
            st_folium(
                preview_map,
                width="100%",
                height=720,
                returned_objects=[],
                key=f"upcoming_map_{selected_idx}"
            )

            # ====================== QGIS MAP LAYOUT (NEW FEATURE) ======================
            # st.markdown("### Drop Zone Map Layout")
            # st.caption("Paste the Google Drive share link (Anyone with the link) for this launch’s map layout.")

            # current_map_url = selected_row.get("MAP_URL", "")
            # if current_map_url and str(current_map_url).strip() and str(current_map_url).lower() not in ["nan", "none", ""]:
            #     st.success("A map link is already saved for this launch.")
                
            #     # Show View / Download buttons if link exists
            #     file_id = None
            #     map_url = str(current_map_url).strip()
            #     if "/d/" in map_url:
            #         file_id = map_url.split("/d/")[1].split("/")[0]
            #     elif "id=" in map_url:
            #         file_id = map_url.split("id=")[1].split("&")[0]

            #     if file_id:
            #         view_link = f"https://drive.google.com/file/d/{file_id}/view"
            #         download_link = f"https://drive.google.com/uc?export=download&id={file_id}"
                    
            #         btn1, btn2 = st.columns(2)
            #         with btn1:
            #             st.link_button("🗺️ View Full Map", url=view_link, use_container_width=True)
            #         with btn2:
            #             st.link_button("⬇️ Download Map", url=download_link, use_container_width=True)
            # else:
            #     st.info("No map link saved yet for this launch.")

            # new_map_url = st.text_input(
            #     "Google Drive Share Link",
            #     value=str(current_map_url) if current_map_url and str(current_map_url).lower() not in ["nan", "none"] else "",
            #     placeholder="https://drive.google.com/file/d/xxxxx/view?usp=sharing",
            #     key=f"map_url_input_{selected_idx}"
            # )

            # ====================== DROP ZONE MAP LAYOUT ======================
            st.markdown("### Drop Zone Map Layout")

            current_map_url = str(selected_row.get("MAP_URL", "")).strip()
            has_valid_link = current_map_url and current_map_url.lower() not in ["nan", "none", ""]

            if has_valid_link:
                st.success("Map layout is available for this launch.")

                file_id = None
                if "/d/" in current_map_url:
                    file_id = current_map_url.split("/d/")[1].split("/")[0]
                elif "id=" in current_map_url:
                    file_id = current_map_url.split("id=")[1].split("&")[0]

                if file_id:
                    view_link = f"https://drive.google.com/file/d/{file_id}/view"
                    download_link = f"https://drive.google.com/uc?export=download&id={file_id}"

                    btn1, btn2 = st.columns(2)
                    with btn1:
                        st.link_button("🗺️ View Full Map", url=view_link, use_container_width=True)
                    with btn2:
                        st.link_button("⬇️ Download Map", url=download_link, use_container_width=True)
                else:
                    st.warning("Could not extract a valid File ID from the saved link.")
            else:
                st.info("No QGIS map layout available for this launch.")

            #st.caption("💡 Make sure the link is set to 'Anyone with the link can view' in Google Drive sharing settings.")
            # if st.button("💾 Save Map Link", type="primary", use_container_width=True, key=f"save_map_{selected_idx}"):
            #     if not new_map_url or not new_map_url.strip():
            #         st.error("Please paste a valid Google Drive link.")
            #     else:
            #         try:
            #             ws = get_launch_worksheet()
            #             data = ws.get_all_records()

            #             target_row = None
            #             launch_date = str(selected_row.get("LAUNCH DATE", "")).strip()
            #             rocket_name = str(selected_row.get("ROCKET NAME", "")).strip()

            #             for idx, row in enumerate(data, start=2):  # row 1 is header
            #                 if (str(row.get("LAUNCH DATE", "")).strip() == launch_date and
            #                     str(row.get("ROCKET NAME", "")).strip() == rocket_name):
            #                     target_row = idx
            #                     break

            #             if target_row is None:
            #                 st.error("Could not find this launch in the Google Sheet.")
            #             else:
            #                 headers = ws.row_values(1)
            #                 if "MAP_URL" not in headers:
            #                     # Automatically add the column if it doesn't exist
            #                     ws.update_cell(1, len(headers) + 1, "MAP_URL")
            #                     headers.append("MAP_URL")

            #                 col_index = headers.index("MAP_URL") + 1  # 1-based
            #                 ws.update_cell(target_row, col_index, new_map_url.strip())

            #                 st.success("✅ Map link saved successfully!")
            #                 load_launch_database.clear()
            #                 st.rerun()

            #         except Exception as e:
            #             st.error(f"Failed to save map link: {e}")
            # ====================== PHILSA ADVISORY ======================
            st.markdown("### PhilSA Advisory")

            advisory_url = str(selected_row.get("ADVISORY_URL", "")).strip()
            has_advisory = advisory_url and advisory_url.lower() not in ["nan", "none", ""]

            if has_advisory:
                st.success("Advisory is available for this launch.")
                st.link_button(
                    "📄 View Agency Advisory",
                    url=advisory_url,
                    use_container_width=True
                )
            else:
                st.info("No agency advisory available for this launch.")
     
        st.caption(f"Launch **#{selected_idx + 1}** of **{len(upcoming_df)}**")
        #st.info("💡 **To update a launch:** Delete it here, then go to the **NOTAM Parser** tab and submit the corrected version.")

#pseudo for Upcoming Launches Tab

# elif st.session_state.selected == "Upcoming Launches":
#     st.subheader("Upcoming Rocket Launches")
#     st.caption("Live from Google Sheets • Future launches only")
  
#     df = load_launch_database()
#     if st.button("🔄 Refresh Data from Google Sheets"):
#         load_launch_database.clear()
#         st.rerun()
  
#     if df.empty:
#         st.info("No launch data available yet. Add some launches using the NOTAM Parser tab.")
#         st.stop()

#     # Filter upcoming launches
#     today = datetime.now().date()
#     upcoming_df = df.copy()
#     if "LAUNCH_DATE_PARSED" in upcoming_df.columns and not upcoming_df.empty:
#         upcoming_df = upcoming_df[upcoming_df["LAUNCH_DATE_PARSED"].dt.date >= today].copy()
#         upcoming_df = upcoming_df.sort_values("LAUNCH_DATE_PARSED").reset_index(drop=True)
#     else:
#         upcoming_df = pd.DataFrame()

#     if upcoming_df.empty:
#         st.success("No upcoming launches scheduled at the moment.")
#         st.balloons()
#         st.info("Check back later or add new launches in the NOTAM Parser tab.")
#     else:
#         st.success(f"**{len(upcoming_df)} Upcoming Launch(es) Scheduled**")
      
#         if "selected_upcoming_idx" not in st.session_state or st.session_state.selected_upcoming_idx >= len(upcoming_df):
#             st.session_state.selected_upcoming_idx = 0
          
#         # ==================== PSEUDO MAPPING ====================
#         upcoming_df = upcoming_df.copy()
        
#         # Rocket Name Pseudo
#         if "ROCKET NAME" in upcoming_df.columns:
#             unique_rockets = sorted(upcoming_df["ROCKET NAME"].dropna().unique())
#             rocket_mapping = {r: f"Rocket {chr(65 + i)}" for i, r in enumerate(unique_rockets)}
#             upcoming_df["Pseudo_Rocket"] = upcoming_df["ROCKET NAME"].map(rocket_mapping)
        
#         # Launch Center Pseudo
#         if "LAUNCH CENTER" in upcoming_df.columns:
#             unique_sites = sorted(upcoming_df["LAUNCH CENTER"].dropna().unique())
#             site_mapping = {s: f"Site {chr(65 + i)}" for i, s in enumerate(unique_sites)}
#             upcoming_df["Pseudo_Site"] = upcoming_df["LAUNCH CENTER"].map(site_mapping)
        
#         # Launching State (Country) Pseudo
#         if "LAUNCHING STATE" in upcoming_df.columns:
#             unique_countries = sorted(upcoming_df["LAUNCHING STATE"].dropna().unique())
#             country_mapping = {c: f"Country {chr(65 + i)}" for i, c in enumerate(unique_countries)}
#             upcoming_df["Pseudo_Country"] = upcoming_df["LAUNCHING STATE"].map(country_mapping)

#         # Launch Selector (using pseudo names)
#         launch_options = [
#             f"{row.get('LAUNCH DATE', '—')} • {row.get('Pseudo_Rocket', row.get('ROCKET NAME', 'Unknown'))} @ {row.get('Pseudo_Site', row.get('LAUNCH CENTER', '—'))}"
#             for _, row in upcoming_df.iterrows()
#         ]
  
#         selected_label = st.selectbox(
#             "Select a Launch",
#             options=launch_options,
#             index=st.session_state.selected_upcoming_idx,
#             key="upcoming_selector"
#         )
  
#         selected_idx = launch_options.index(selected_label)
#         if selected_idx != st.session_state.selected_upcoming_idx:
#             st.session_state.selected_upcoming_idx = selected_idx
    
#         st.divider()
        
#         # ====================== LAYOUT ======================
#         selected_row = upcoming_df.iloc[selected_idx]
#         original_row_index = selected_row.name

#         # ====================== DELETE SECTION ======================
#         # (Deletion logic remains unchanged - uses original values)
#         col_del1, col_del2 = st.columns([1, 4])
#         with col_del1:
#             if st.button("🗑️ Delete This Launch", type="secondary", use_container_width=True):
#                 st.session_state["delete_confirm"] = True
#                 st.session_state["delete_launch_date"] = selected_row.get('LAUNCH DATE')
#                 st.session_state["delete_rocket_name"] = selected_row.get('ROCKET NAME')
#                 st.rerun()

#         # ... [Delete confirmation logic remains the same - no changes needed] ...
#         if st.session_state.get("delete_confirm"):
#             st.error("⚠️ **Warning:** This will permanently delete the launch from the database.")
#             st.warning("After deletion, go to the **NOTAM Parser** tab to submit the updated version.")
           
#             c1, c2 = st.columns(2)
#             with c1:
#                 if st.button("✅ Yes, Delete Launch", type="primary"):
#                     try:
#                         ws = get_launch_worksheet()
#                         spreadsheet = ws.spreadsheet
#                         ws = spreadsheet.worksheet(ws.title)
                       
#                         data = ws.get_all_records()
#                         target_row = None
                       
#                         for idx, row in enumerate(data, start=2):
#                             if (str(row.get('LAUNCH DATE', '')).strip() == str(st.session_state["delete_launch_date"]).strip() and
#                                 str(row.get('ROCKET NAME', '')).strip() == str(st.session_state["delete_rocket_name"]).strip()):
#                                 target_row = idx
#                                 break
                       
#                         if target_row is None:
#                             st.error("❌ Could not find the launch in the sheet.")
#                         else:
#                             ws.delete_rows(target_row)
#                             st.success(f"✅ Launch deleted successfully (Row {target_row})!")
#                             load_launch_database.clear()
#                             for key in ["delete_confirm", "delete_launch_date", "delete_rocket_name"]:
#                                 if key in st.session_state:
#                                     del st.session_state[key]
#                             st.rerun()
#                     except Exception as e:
#                         st.error(f"❌ Delete failed: {str(e)}")
#             with c2:
#                 if st.button("❌ Cancel", type="secondary"):
#                     for key in ["delete_confirm", "delete_launch_date", "delete_rocket_name"]:
#                         if key in st.session_state:
#                             del st.session_state[key]
#                     st.rerun()

#         # ==================== LEFT SIDE - LAUNCH INFO ====================
#         left_col, right_col = st.columns([1, 3])
#         with left_col:
#             st.markdown("**Launch Information**")
#             with st.container(border=True):
#                 raw_launch_date = selected_row.get('LAUNCH DATE', '—')
#                 window_time = selected_row.get('START TO END TIME', '—')
              
#                 try:
#                     if raw_launch_date != '—' and raw_launch_date:
#                         parsed_date = datetime.strptime(raw_launch_date.strip(), "%m/%d/%Y")
#                         formatted_date = parsed_date.strftime("%d %B %Y")
#                     else:
#                         formatted_date = raw_launch_date
#                 except:
#                     formatted_date = raw_launch_date
              
#                 window_display = f"{formatted_date} between {window_time}" if formatted_date != '—' else window_time
              
#                 metrics = [
#                     ("Rocket Name", selected_row.get('Pseudo_Rocket', selected_row.get('ROCKET NAME', '—'))),
#                     ("Launch Center", selected_row.get('Pseudo_Site', selected_row.get('LAUNCH CENTER', '—'))),
#                     ("Launching State", selected_row.get('Pseudo_Country', selected_row.get('LAUNCHING STATE', '—'))),
#                     ("Date and Time (PhST)", window_display),
#                 ]
              
#                 col1, col2 = st.columns(2)
               
#                 with col1:
#                     title, value = metrics[0]
#                     with st.container(height=130, border=False):
#                         st.markdown(f":blue-badge[{title}]")
#                         st.markdown(f"""
#                             <div style="font-size: 1.32rem; font-weight: 700; text-align: left; margin-top: 8px; line-height: 1.3;">
#                                 {value}
#                             </div>
#                         """, unsafe_allow_html=True)
               
#                 with col2:
#                     title, value = metrics[1]
#                     with st.container(height=130, border=False):
#                         st.markdown(f":blue-badge[{title}]")
#                         st.markdown(f"""
#                             <div style="font-size: 1.32rem; font-weight: 700; text-align: left; margin-top: 8px; line-height: 1.3;">
#                                 {value}
#                             </div>
#                         """, unsafe_allow_html=True)

#                 with col1:
#                     title, value = metrics[2]
#                     with st.container(height=130, border=False):
#                         st.markdown(f":blue-badge[{title}]")
#                         st.markdown(f"""
#                             <div style="font-size: 1.32rem; font-weight: 700; text-align: left; margin-top: 8px; line-height: 1.3;">
#                                 {value}
#                             </div>
#                         """, unsafe_allow_html=True)
               
#                 with col2:
#                     title, value = metrics[3]
#                     with st.container(height=130, border=False):
#                         st.markdown(f":blue-badge[{title}]")
#                         st.markdown(f"""
#                             <div style="font-size: 1.32rem; font-weight: 700; text-align: left; margin-top: 8px; line-height: 1.3;">
#                                 {value}
#                             </div>
#                         """, unsafe_allow_html=True)

#             st.markdown("**Drop Zone Summary**")
#             zone_colors = {
#                 "Archipelagic Waters": "#FFD700",
#                 "EEZ": "#00008B",
#                 "Manila FIR": "#FFA500",
#                 "Outside PH Zones": "#808080"
#             }
           
#             counts = {
#                 "Archipelagic Waters": int(selected_row.get("ARCH_COUNT", 0) or 0),
#                 "EEZ": int(selected_row.get("EEZ_COUNT", 0) or 0),
#                 "Manila FIR": int(selected_row.get("FIR_COUNT", 0) or 0),
#                 "Outside PH Zones": int(selected_row.get("OUTSIDE_COUNT", 0) or 0)
#             }
           
#             dz_cols = st.columns(2)
#             for i, (zone, count) in enumerate(counts.items()):
#                 color = zone_colors.get(zone, "#1a4fa3")
#                 with dz_cols[i % 2]:
#                     with st.container(border=True):
#                         st.markdown(f":blue-badge[{zone}]")
#                         st.markdown(f"""
#                             <div style="font-size: 3.0rem; font-weight: 700; color: {color}; text-align: center; margin-bottom: 16px;">
#                                 {count}
#                             </div>
#                         """, unsafe_allow_html=True)

#         # ==================== RIGHT SIDE - MAP ====================
#         with right_col:
#             st.markdown("**Drop Zone Map**")
#             dropzones_db = {}
#             for dz_num in range(1, 5):
#                 dz = f"DZ{dz_num}"
#                 verts = [str(selected_row.get(f"{dz} P{p}", "")).strip()
#                         for p in range(1, 9) if str(selected_row.get(f"{dz} P{p}", "")).strip()]
#                 debs = [str(selected_row.get(f"DEB{dz_num} P{p}", "")).strip()
#                        for p in range(1, 5) if str(selected_row.get(f"DEB{dz_num} P{p}", "")).strip()]
#                 dropzones_db[dz] = {"vertices": verts, "debris": debs}
         
#             preview_map = create_folium_map(
#                 selected_row.get("LAUNCH CENTER", ""),
#                 dropzones_db,
#                 st.session_state.shape_dir
#             )
        
#             st_folium(
#                 preview_map,
#                 width="100%",
#                 height=720,
#                 returned_objects=[],
#                 key=f"upcoming_map_{selected_idx}"
#             )
     
#         st.caption(f"Launch **#{selected_idx + 1}** of **{len(upcoming_df)}**")
#         st.info("💡 **To update a launch:** Delete it here, then go to the **NOTAM Parser** tab and submit the corrected version.")

#====================== TAB ROUTING ======================

# elif st.session_state.selected == "NOTAM Parser":
#     #st.title("NOTAM Parser")
#     st.subheader("Rocket Launch Drop Zone Map")
#     st.caption("Updates automatically as you edit drop zones, launch info, or upload NOTAM PDFs")
    
#     dropzones_preview = {
#         f"DZ{i}": {
#             "vertices": st.session_state.dz_vertices[f"DZ{i}"].copy(),
#             "debris": st.session_state.dz_debris[f"DZ{i}"].copy()
#         }
#         for i in range(1, 5)
#     }
    
#     live_map = create_folium_map(
#         st.session_state.get("launch_site", "Select..."),
#         dropzones_preview,
#         st.session_state.shape_dir
#     )

#     col1, col2, col3 = st.columns([0.5, 8, 0.5])

#     with col2:
#         st_folium(
#             live_map,
#             width="100%",
#             height=680,
#             returned_objects=[],
#             key="live_map_preview"
#         )

#     launch_sites = ["Select...", "Hainan International Commercial Launch Center", "Jiuquan Satellite Launch Center",
#                     "Wenchang Space Launch Site", "Xichang Satellite Launch Center",
#                     "Naro Space Center", "Sohae Satellite Launching Station", "Unchinoura Space Center", "Yellow Sea Launch Area",
#                     "East China Sea Launch Area", "Taiyuan Satellite Launch Center",
#                     "Other"]
#     countries = ["Select...", "People's Republic of China", "Japan",
#                  "Democratic People's Republic of Korea", "Republic of Korea"]
#     if st.session_state.get("pending_notam_fill"):
#         fill = st.session_state.pending_notam_fill
#         if fill.get("start_time"): st.session_state.start_time = fill["start_time"]
#         if fill.get("end_time"): st.session_state.end_time = fill["end_time"]
#         st.session_state.pending_notam_fill = None
#     with st.form("rocket_launch_form"):
#         st.subheader("Launch Information")
#         col1, col2, col3 = st.columns([2, 1, 1])
#         with col1: st.text_input("Mission Name", placeholder="SLV-XX", key="mission")
#         with col2: st.selectbox("Launch Site", launch_sites, key="launch_site")
#         with col3: st.selectbox("Country", countries, key="launch_country")
#         col4, col5, col6 = st.columns([1, 1, 1])
#         with col4: st.date_input("Launch Date", value=date.today(), key="launch_date")
#         with col5: st.text_input("Window Start (HHMM)", placeholder="0745", key="start_time")
#         with col6: st.text_input("Window End (HHMM)", placeholder="0810", key="end_time")
#         submitted = st.form_submit_button("🚀 Submit – Generate Files (ZIP + Excel + CSV) & Log to Google Sheets",
#                                           type="primary", use_container_width=True)
#     st.subheader("Import Drop Zones from FAA NOTAM PDF(s)")
#     st.caption("**Each PDF = one drop zone** (1st PDF → DZ1, 2nd → DZ2, etc.)")
#     uploaded_pdfs = st.file_uploader("Upload NOTAM PDF file(s)", type="pdf", accept_multiple_files=True, key="notam_uploader")
#     if uploaded_pdfs:
#         st.info(f"📌 {len(uploaded_pdfs)} PDF(s) selected")
#         if st.button("🔄 Parse NOTAMs & Auto-fill Drop Zones", type="primary", use_container_width=True):
#             processed = 0
#             overall_start = ""
#             overall_end = ""
#             for pdf_idx, pdf_file in enumerate(uploaded_pdfs):
#                 coord_strings, meta = extract_notam_data(pdf_file)
#                 if pdf_idx == 0: overall_start = meta.get("b_time", "")
#                 overall_end = meta.get("c_time", "")
#                 if processed < 4 and coord_strings and len(coord_strings) >= 3:
#                     dz_key = f"DZ{processed + 1}"
#                     st.session_state.dz_vertices[dz_key] = coord_strings[:]
#                     for idx, val in enumerate(coord_strings):
#                         st.session_state[f"{dz_key}_vert_{idx}"] = val
#                     processed += 1
#             if overall_start or overall_end or processed > 0:
#                 st.session_state.pending_notam_fill = {"start_time": overall_start, "end_time": overall_end}
#                 st.success(f"✅ Done! Start = **{overall_start or '—'}** | End = **{overall_end or '—'}** | Drop Zones filled: {processed}")
#                 st.rerun()
#     st.subheader("Drop Zones DZ1–DZ4")
#     for i in range(1, 5):
#         dz = f"DZ{i}"
#         with st.expander(f"**Drop Zone {i}**", expanded=(i == 1)):
#             st.caption("**Vertices** (min 3 recommended)")
#             current_verts = st.session_state.dz_vertices[dz]
#             for idx in range(len(current_verts)):
#                 st.text_input(label=f"Vertex {idx+1}", value=current_verts[idx], key=f"{dz}_vert_{idx}", placeholder="193600N 1183100E")
#             col_add, col_rem, _ = st.columns([1, 1, 4])
#             with col_add:
#                 if st.button("➕ Add Vertex", key=f"add_v_{dz}", use_container_width=True):
#                     if len(current_verts) < 10:
#                         st.session_state.dz_vertices[dz].append("")
#                         st.rerun()
#             with col_rem:
#                 if len(current_verts) > 3 and st.button("➖ Remove Last", key=f"rem_v_{dz}", use_container_width=True):
#                     st.session_state.dz_vertices[dz].pop()
#                     st.rerun()
#             st.caption("**Debris Points** (min 2, max 4)")
#             current_deb = st.session_state.dz_debris[dz]
#             for idx in range(len(current_deb)):
#                 st.text_input(label=f"Debris {idx+1}", value=current_deb[idx], key=f"{dz}_deb_{idx}", placeholder="14.5N 120.5E")
#             col_add_d, col_rem_d, _ = st.columns([1, 1, 4])
#             with col_add_d:
#                 if st.button("➕ Add Debris", key=f"add_d_{dz}", use_container_width=True):
#                     if len(current_deb) < 4:
#                         st.session_state.dz_debris[dz].append("")
#                         st.rerun()
#             with col_rem_d:
#                 if len(current_deb) > 2 and st.button("➖ Remove Last", key=f"rem_d_{dz}", use_container_width=True):
#                     removed_idx = len(st.session_state.dz_debris[dz]) - 1
#                     stale_key = f"{dz}_deb_{removed_idx}"
#                     if stale_key in st.session_state: del st.session_state[stale_key]
#                     st.session_state.dz_debris[dz].pop()
#                     st.rerun()
#     # ====================== FILE GENERATION + GOOGLE SHEETS LOGGING ======================
#     if submitted:
#         window_utc = format_window(st.session_state.start_time, st.session_state.end_time)
#         if window_utc is None:
#             st.error("❌ Invalid time window. Use HHMM format (e.g. 0745)")
#             st.stop()
#         dropzones = {}
#         for i in range(1, 5):
#             dz_key = f"DZ{i}"
#             dropzones[dz_key] = {"vertices": st.session_state.dz_vertices[dz_key].copy(), "debris": st.session_state.dz_debris[dz_key].copy()}
#         for dz_id, dz in dropzones.items():
#             valid_verts = [p.strip() for p in dz["vertices"] if p.strip()]
#             if len(valid_verts) < 3 and any(p.strip() for p in dz["vertices"]):
#                 st.error(f"❌ {dz_id} needs at least 3 valid vertices")
#                 st.stop()
#         window_phst = utc_window_to_phst(window_utc)
       
#         dz_compact = []
#         for i in range(1, 5):
#             verts = dropzones[f"DZ{i}"]["vertices"]
#             compact_verts = [convert_to_compact(c) for c in verts]
#             compact_verts += [""] * (8 - len(compact_verts))
#             dz_compact.append(compact_verts[:8])
#         deb_compact = []
#         for i in range(1, 5):
#             comp = [convert_to_compact(c) for c in dropzones[f"DZ{i}"]["debris"]]
#             comp += [""] * (4 - len(comp))
#             deb_compact.append(comp)
#         date_str = st.session_state.launch_date.strftime("%m%d%y")
#         formatted_date = f"{st.session_state.launch_date.month}/{st.session_state.launch_date.day}/{st.session_state.launch_date.year}"
#         mission_str = st.session_state.mission.replace(" ", "") or "N/A"
#         wkt_string = coordinates_to_wkt()
#         dz_count, arch_count, eez_count, fir_count, outside_count = classify_single_launch_dropzones(
#             st.session_state.shape_dir
#         )
#         row_base = {
#             "ROCKET NAME": st.session_state.mission,
#             "LAUNCHING STATE": st.session_state.launch_country,
#             "LAUNCH CENTER": st.session_state.launch_site,
#             "LAUNCH CENTER LOCATION": "",
#             "START TO END TIME": window_phst,
#             "DATE RECEIVED FROM CAAP": "",
#             "WKT": wkt_string,
#             "DZ_COUNT": int(dz_count),
#             "ARCH_COUNT": int(arch_count),
#             "EEZ_COUNT": int(eez_count),
#             "FIR_COUNT": int(fir_count),
#             "OUTSIDE_COUNT": int(outside_count),
#         }
#         for i in range(4):
#             for j in range(8):
#                 row_base[f"DZ{i+1} P{j+1}"] = dz_compact[i][j]
#         for i in range(4):
#             for j in range(4):
#                 row_base[f"DEB{i+1} P{j+1}"] = deb_compact[i][j]
#         info_df = pd.DataFrame([{"LAUNCH DATE": formatted_date, **row_base}])
#         # =====================================================
#         # BUILD DZ + DEBRIS DATAFRAMES
#         # =====================================================
#         vertices_rows = []
#         debris_rows = []

#         for i in range(1, 5):

#             dz = dropzones[f"DZ{i}"]

#             # -------------------------
#             # DZ VERTICES
#             # -------------------------
#             for vi, coord in enumerate(dz["vertices"]):

#                 lat, lon = parse_coordinates(coord)

#                 if lat is not None and lon is not None:
#                     vertices_rows.append({
#                         "DZ_ID": f"DZ{i}",
#                         "VERTEX_ID": vi + 1,
#                         "LAT": lat,
#                         "LON": lon
#                     })

#             # -------------------------
#             # DEBRIS POINTS
#             # -------------------------
#             for di, coord in enumerate(dz["debris"]):

#                 lat, lon = parse_coordinates(coord)

#                 if lat is not None and lon is not None:
#                     debris_rows.append({
#                         "DZ_ID": f"DZ{i}",
#                         "DB_ID": di + 1,
#                         "LAT": lat,
#                         "LON": lon
#                     })

#         vertices_df = pd.DataFrame(vertices_rows)
#         debris_df = pd.DataFrame(debris_rows)

#         # ==================== LOG TO GOOGLE SHEETS ====================
#         try:
#             ws = get_launch_worksheet()
#             headers = ws.row_values(1)

#             # row_to_append = [
#             #     info_df.iloc[0].get(col, "")
#             #     for col in headers
#             # ]

#             row_to_append = [
#                 str(info_df.iloc[0].get(col, "")) if col not in ["DZ_COUNT","ARCH_COUNT","EEZ_COUNT","FIR_COUNT","OUTSIDE_COUNT"] 
#                 else int(info_df.iloc[0].get(col, 0)) 
#                 for col in headers
#             ]
        
#             ws.append_row(row_to_append, value_input_option="USER_ENTERED")

#             # ws.append_row(
#             #     row_to_append,
#             #     value_input_option="USER_ENTERED"
#             # )

#             st.success(
#                 "✅ Launch successfully logged to "
#                 "**Google Sheets** "
#                 "(Rocket-Launch-Monitoring → Sheet1)"
#             )

#         except Exception as e:
#             st.error(f"⚠️ Could not log to Google Sheets: {e}")

#         # ====================== GENERATE ZIP FILES ======================
#         zip_buffer = io.BytesIO()

#         with zipfile.ZipFile(
#             zip_buffer,
#             "w",
#             zipfile.ZIP_DEFLATED
#         ) as zf:

#             # -------------------------------------------------
#             # MAIN INFO CSV
#             # -------------------------------------------------
#             zf.writestr(
#                 f"Info_{date_str}.csv",
#                 info_df.to_csv(index=False).encode("utf-8")
#             )

#             # -------------------------------------------------
#             # DZ VERTICES CSV
#             # -------------------------------------------------
#             zf.writestr(
#                 f"DZ_{date_str}.csv",
#                 vertices_df.to_csv(index=False).encode("utf-8")
#             )

#             # -------------------------------------------------
#             # DEBRIS CSV
#             # -------------------------------------------------
#             zf.writestr(
#                 f"Deb_{date_str}.csv",
#                 debris_df.to_csv(index=False).encode("utf-8")
#             )

#             # -------------------------------------------------
#             # XLSX FILE
#             # -------------------------------------------------
#             xlsx_buf = io.BytesIO()

#             with pd.ExcelWriter(
#                 xlsx_buf,
#                 engine="openpyxl"
#             ) as writer:

#                 info_df.to_excel(
#                     writer,
#                     sheet_name="Launch_Info",
#                     index=False
#                 )

#                 vertices_df.to_excel(
#                     writer,
#                     sheet_name="Dropzone_Vertices",
#                     index=False
#                 )

#                 debris_df.to_excel(
#                     writer,
#                     sheet_name="Debris_Points",
#                     index=False
#                 )

#             xlsx_buf.seek(0)

#             zf.writestr(
#                 f"Info_{date_str}.xlsx",
#                 xlsx_buf.read()
#             )

#         zip_buffer.seek(0)

#         st.download_button(
#             "📦 Download All Files (ZIP)",
#             data=zip_buffer,
#             file_name=f"PhilSA_{mission_str}_{date_str}.zip",
#             mime="application/zip",
#             use_container_width=True
#         )

#Launch Database tab latest (09 July 2026)
# ====================== LAUNCH DATABASE TAB ======================
elif st.session_state.selected == "Launch Database":
    st.caption("Live from Google Sheets → Rocket-Launch-Monitoring / Sheet1")
    
    try:
        ws = get_launch_worksheet()
        data = ws.get_all_records()
        database_df = pd.DataFrame(data)
        st.success(f"✅ Connected to Google Sheets • {len(database_df)} launches")
    except Exception as e:
        st.error(f"Could not load database: {e}")
        database_df = pd.DataFrame()

    if len(database_df) == 0:
        st.stop()

    tab_browse, tab_query = st.tabs(["Browse", "Query"])

    # =====================================================
    # TAB 1: BROWSE / PREVIEW
    # =====================================================
    

    # st.subheader("Filters")
    # col_f1, col_f2, col_f3 = st.columns(3)
    
    # with col_f1:
    #     try:
    #         database_df["LAUNCH DATE"] = pd.to_datetime(database_df["LAUNCH DATE"], errors="coerce")
    #         date_filter = st.date_input("Filter by Launch Date", value=None)
    #     except:
    #         date_filter = None
            
    # with col_f2:
    #     country_opts = sorted(database_df["LAUNCHING STATE"].dropna().unique().tolist())
    #     countries_sel = st.multiselect("Country", country_opts, default=[])
        
    # with col_f3:
    #     center_opts = sorted(database_df["LAUNCH CENTER"].dropna().unique().tolist())
    #     centers_sel = st.multiselect("Launch Center", center_opts, default=[])

    # # Apply filters
    # filtered_df = database_df.copy()
    # if date_filter:
    #     filtered_df = filtered_df[filtered_df["LAUNCH DATE"].dt.date == date_filter]
    # if countries_sel:
    #     filtered_df = filtered_df[filtered_df["LAUNCHING STATE"].isin(countries_sel)]
    # if centers_sel:
    #     filtered_df = filtered_df[filtered_df["LAUNCH CENTER"].isin(centers_sel)]

    # st.subheader(f"Showing {len(filtered_df)} launches")

    # if len(filtered_df) == 0:
    #     st.info("No launches match your filters.")
    #     st.stop()

    # # Preview Selector
    # select_options = [
    #     f"{row.get('LAUNCH DATE','—')} | {row.get('ROCKET NAME','Unknown')} | {row.get('LAUNCH CENTER','—')}" 
    #     for _, row in filtered_df.iterrows()
    # ]
    # selected_label = st.selectbox("Preview drop zone map for:", select_options)
    # selected_idx = select_options.index(selected_label)
    # selected_row = filtered_df.iloc[selected_idx]
    with tab_browse:
        #st.subheader("Preview Launch")
        st.subheader("Preview Launch")
        st.caption("Preview the drop zone map for a selected launch from the database below. Use the filters to narrow down your search.")

        # ====================== FILTERS ======================
        st.markdown("### Filters")

        # Ensure LAUNCH DATE is datetime
        try:
            database_df["LAUNCH DATE"] = pd.to_datetime(database_df["LAUNCH DATE"], errors="coerce")
        except Exception:
            pass

        col_f1, col_f2, col_f3, col_f4 = st.columns(4)

        with col_f1:
            # Year filter
            available_years = sorted(
                database_df["LAUNCH DATE"].dt.year.dropna().unique().astype(int).tolist()
            )
            years_sel = st.multiselect(
                "Year",
                options=available_years,
                default=[],
                placeholder="All years"
            )

        with col_f2:
            # Month filter
            month_options = list(range(1, 13))
            month_labels = {
                1: "January", 2: "February", 3: "March", 4: "April",
                5: "May", 6: "June", 7: "July", 8: "August",
                9: "September", 10: "October", 11: "November", 12: "December"
            }
            months_sel = st.multiselect(
                "Month",
                options=month_options,
                format_func=lambda x: month_labels[x],
                default=[],
                placeholder="All months"
            )

        with col_f3:
            country_opts = sorted(database_df["LAUNCHING STATE"].dropna().unique().tolist())
            countries_sel = st.multiselect(
                "Country",
                options=country_opts,
                default=[],
                placeholder="All countries"
            )

        with col_f4:
            center_opts = sorted(database_df["LAUNCH CENTER"].dropna().unique().tolist())
            centers_sel = st.multiselect(
                "Launch Center",
                options=center_opts,
                default=[],
                placeholder="All centers"
            )

        # ====================== APPLY FILTERS ======================
        filtered_df = database_df.copy()

        if years_sel:
            filtered_df = filtered_df[filtered_df["LAUNCH DATE"].dt.year.isin(years_sel)]

        if months_sel:
            filtered_df = filtered_df[filtered_df["LAUNCH DATE"].dt.month.isin(months_sel)]

        if countries_sel:
            filtered_df = filtered_df[filtered_df["LAUNCHING STATE"].isin(countries_sel)]

        if centers_sel:
            filtered_df = filtered_df[filtered_df["LAUNCH CENTER"].isin(centers_sel)]

        st.subheader(f"Showing {len(filtered_df)} launches")

        if len(filtered_df) == 0:
            st.info("No launches match your filters.")
            st.stop()

        # ====================== PREVIEW SELECTOR ======================
        def format_launch_date(val):
            """Format date nicely: 15 July 2024"""
            if pd.isna(val) or val in [None, "", "—"]:
                return "—"
            try:
                return pd.to_datetime(val).strftime("%d %B %Y")
            except Exception:
                return str(val)

        select_options = [
            f"{format_launch_date(row.get('LAUNCH DATE'))} | {row.get('ROCKET NAME', 'Unknown')} | {row.get('LAUNCH CENTER', '—')}"
            for _, row in filtered_df.iterrows()
        ]

        selected_label = st.selectbox("Preview drop zone map for:", select_options)
        selected_idx = select_options.index(selected_label)
        selected_row = filtered_df.iloc[selected_idx]

        # ====================== MAIN LAYOUT (Same as Upcoming Launches) ======================
        left_col, right_col = st.columns([1, 3])

        with left_col:
                # --- Launch Information ---
            st.markdown("**Launch Information**")
            
            with st.container(border=True):
                
                raw_date = selected_row.get('LAUNCH DATE', '—')
                window_time = selected_row.get('START TO END TIME', '—')
                
                # Robust Date Formatting
                try:
                    if pd.isna(raw_date) or raw_date in ['—', None, '']:
                        formatted_date = '—'
                    else:
                        # Handle both string and datetime objects
                        dt = pd.to_datetime(raw_date, errors='coerce')
                        if pd.isna(dt):
                            formatted_date = str(raw_date).split()[0]
                        else:
                            formatted_date = dt.strftime("%d %B %Y")
                except:
                    formatted_date = str(raw_date).split()[0] if str(raw_date) != '—' else '—'

                window_display = f"{formatted_date} between {window_time}" if formatted_date != '—' else window_time

                metrics = [
                    ("Rocket Name", selected_row.get('ROCKET NAME', '—')),
                    ("Launch Center", selected_row.get('LAUNCH CENTER', '—')),
                    ("Launching State", selected_row.get('LAUNCHING STATE', '—')),
                    ("Date and Time (PhST)", window_display),
                ]
                
                col1, col2 = st.columns(2)
                
                # First Row
                for i in [0, 1]:
                    with (col1 if i == 0 else col2):
                        title, value = metrics[i]
                        with st.container(height=130, border=False):
                            st.markdown(f":blue-badge[{title}]")
                            st.markdown(f"""
                                <div style="font-size: 1.32rem; font-weight: 700; 
                                        text-align: left; margin-top: 8px; line-height: 1.3;">
                                    {value}
                                </div>
                            """, unsafe_allow_html=True)
                
                # Second Row
                for i in [2, 3]:
                    with (col1 if i == 2 else col2):
                        title, value = metrics[i]
                        with st.container(height=130, border=False):
                            st.markdown(f":blue-badge[{title}]")
                            st.markdown(f"""
                                <div style="font-size: 1.32rem; font-weight: 700; 
                                        text-align: left; margin-top: 8px; line-height: 1.3;">
                                    {value}
                                </div>
                            """, unsafe_allow_html=True)

            # --- Drop Zone Summary ---
            st.markdown("**Drop Zone Summary**")
            
            zone_colors = {
                "Archipelagic Waters": "#FFD700",
                "EEZ": "#1976d2",
                "Manila FIR": "#FFA500",
                "Outside PH Zones": "#808080"
            }
            
            counts = {
                "Archipelagic Waters": int(selected_row.get("ARCH_COUNT", 0) or 0),
                "EEZ": int(selected_row.get("EEZ_COUNT", 0) or 0),
                "Manila FIR": int(selected_row.get("FIR_COUNT", 0) or 0),
                "Outside PH Zones": int(selected_row.get("OUTSIDE_COUNT", 0) or 0)
            }
            
            dz_cols = st.columns(2)
            for i, (zone, count) in enumerate(counts.items()):
                color = zone_colors.get(zone, "#1a4fa3")
                with dz_cols[i % 2]:
                    with st.container(border=True):
                        st.markdown(f":blue-badge[{zone}]")
                        st.markdown(f"""
                            <div style="font-size: 3.0rem; font-weight: 700; color: {color}; 
                                    text-align: center; margin-bottom: 16px;">
                                {count}
                            </div>
                        """, unsafe_allow_html=True)

        # ==================== RIGHT SIDE - MAP + QGIS LAYOUT ====================
        with right_col:
            st.markdown("**Drop Zone Map**")
            
            dropzones_db = {}
            for dz_num in range(1, 5):
                dz = f"DZ{dz_num}"
                verts = [str(selected_row.get(f"{dz} P{p}", "")).strip()
                        for p in range(1, 9) if str(selected_row.get(f"{dz} P{p}", "")).strip()]
                debs = [str(selected_row.get(f"DEB{dz_num} P{p}", "")).strip()
                    for p in range(1, 5) if str(selected_row.get(f"DEB{dz_num} P{p}", "")).strip()]
                dropzones_db[dz] = {"vertices": verts, "debris": debs}
            
            preview_map = create_folium_map(
                selected_row.get("LAUNCH CENTER", ""),
                dropzones_db,
                st.session_state.shape_dir
            )
            
            st_folium(
                preview_map,
                width="100%",
                height=720,
                returned_objects=[],
                key=f"db_map_{selected_idx}"
            )

            # ====================== DROP ZONE MAP LAYOUT ======================
            # st.markdown("### Drop Zone Map Layout")
            # st.caption("Paste the Google Drive share link (Anyone with the link) for this launch’s map layout.")

            # current_map_url = str(selected_row.get("MAP_URL", "")).strip()
            # has_valid_link = current_map_url and current_map_url.lower() not in ["nan", "none", ""]

            # # Initialize edit mode
            # edit_map_key = f"edit_map_{selected_idx}"
            # if edit_map_key not in st.session_state:
            #     st.session_state[edit_map_key] = False

            # if has_valid_link and not st.session_state[edit_map_key]:
            #     # ----- Link already exists -----
            #     st.success("Map layout is available for this launch.")

            #     file_id = None
            #     if "/d/" in current_map_url:
            #         file_id = current_map_url.split("/d/")[1].split("/")[0]
            #     elif "id=" in current_map_url:
            #         file_id = current_map_url.split("id=")[1].split("&")[0]

            #     if file_id:
            #         view_link = f"https://drive.google.com/file/d/{file_id}/view"
            #         download_link = f"https://drive.google.com/uc?export=download&id={file_id}"

            #         btn1, btn2 = st.columns(2)
            #         with btn1:
            #             st.link_button("🗺️ View Full Map", url=view_link, use_container_width=True)
            #         with btn2:
            #             st.link_button("⬇️ Download Map", url=download_link, use_container_width=True)
            #     else:
            #         st.warning("Could not extract a valid File ID from the saved link.")

            #     if st.button("✏️ Edit / Change Map Link", use_container_width=True, key=f"edit_map_btn_{selected_idx}"):
            #         st.session_state[edit_map_key] = True
            #         st.rerun()

            # else:
            #     # ----- No link yet OR user clicked Edit -----
            #     if has_valid_link:
            #         st.info("You are editing the existing map link.")

            #     new_map_url = st.text_input(
            #         "Google Drive Share Link",
            #         value=str(current_map_url) if has_valid_link else "",
            #         placeholder="https://drive.google.com/file/d/xxxxx/view?usp=sharing",
            #         key=f"map_url_input_{selected_idx}"
            #     )

            #     col_save, col_cancel = st.columns(2)

            #     with col_save:
            #         if st.button("💾 Save Map Link", type="primary", use_container_width=True, key=f"save_map_{selected_idx}"):
            #             if not new_map_url or not new_map_url.strip():
            #                 st.error("Please paste a valid Google Drive link.")
            #             else:
            #                 try:
            #                     ws = get_launch_worksheet()

            #                     # Use original index from the dataframe
            #                     original_index = selected_row.name
            #                     target_row = int(original_index) + 2

            #                     headers = ws.row_values(1)

            #                     if "MAP_URL" not in headers:
            #                         ws.update_cell(1, len(headers) + 1, "MAP_URL")
            #                         headers.append("MAP_URL")

            #                     col_index = headers.index("MAP_URL") + 1
            #                     ws.update_cell(target_row, col_index, new_map_url.strip())

            #                     st.success(f"✅ Map link saved successfully! (Sheet row {target_row})")
            #                     st.session_state[edit_map_key] = False
            #                     load_launch_database.clear()
            #                     st.rerun()

            #                 except Exception as e:
            #                     st.error(f"Failed to save map link: {e}")

            #     with col_cancel:
            #         if has_valid_link and st.button("❌ Cancel Edit", use_container_width=True, key=f"cancel_map_edit_{selected_idx}"):
            #             st.session_state[edit_map_key] = False
            #             st.rerun()

            # ====================== DROP ZONE MAP LAYOUT ======================
            st.markdown("### Drop Zone Map Layout")

            current_map_url = str(selected_row.get("MAP_URL", "")).strip()
            has_valid_link = current_map_url and current_map_url.lower() not in ["nan", "none", ""]

            if has_valid_link:
                st.success("Map layout is available for this launch.")

                file_id = None
                if "/d/" in current_map_url:
                    file_id = current_map_url.split("/d/")[1].split("/")[0]
                elif "id=" in current_map_url:
                    file_id = current_map_url.split("id=")[1].split("&")[0]

                if file_id:
                    view_link = f"https://drive.google.com/file/d/{file_id}/view"
                    download_link = f"https://drive.google.com/uc?export=download&id={file_id}"

                    btn1, btn2 = st.columns(2)
                    with btn1:
                        st.link_button("🗺️ View Full Map", url=view_link, use_container_width=True)
                    with btn2:
                        st.link_button("⬇️ Download Map", url=download_link, use_container_width=True)
                else:
                    st.warning("Could not extract a valid File ID from the saved link.")
            else:
                st.info("No QGIS map layout available for this launch.")

           # ====================== PHILSA ADVISORY ======================
            # st.markdown("### PhilSA Advisory")
            # st.caption("Paste the advisory link from the agency website for this launch.")

            # advisory_url = str(selected_row.get("ADVISORY_URL", "")).strip()
            # has_advisory = advisory_url and advisory_url.lower() not in ["nan", "none", ""]

            # if has_advisory:
            #     st.success("Advisory link is saved for this launch.")
            #     st.link_button(
            #         "📄 View Agency Advisory",
            #         url=advisory_url,
            #         use_container_width=True
            #     )
            # else:
            #     st.info("No agency advisory saved yet for this launch.")

            # new_advisory_url = st.text_input(
            #     "Advisory URL",
            #     value=str(advisory_url) if advisory_url and str(advisory_url).lower() not in ["nan", "none"] else "",
            #     placeholder="https://philsa.gov.ph/....",
            #     key=f"advisory_url_input_{selected_idx}"
            # )

            # if st.button("💾 Save Advisory Link", type="primary", use_container_width=True, key=f"save_advisory_{selected_idx}"):
            #     if not new_advisory_url or not new_advisory_url.strip():
            #         st.error("Please paste a valid advisory link.")
            #     else:
            #         try:
            #             ws = get_launch_worksheet()

            #             # Use the original index from the dataframe
            #             # selected_row.name is the original position in database_df
            #             original_index = selected_row.name

            #             # Google Sheet: row 1 = header, data starts at row 2
            #             # pandas index is 0-based → sheet row = index + 2
            #             target_row = int(original_index) + 2

            #             headers = ws.row_values(1)

            #             if "ADVISORY_URL" not in headers:
            #                 # Automatically add the column if it doesn't exist
            #                 ws.update_cell(1, len(headers) + 1, "ADVISORY_URL")
            #                 headers.append("ADVISORY_URL")

            #             col_index = headers.index("ADVISORY_URL") + 1
            #             ws.update_cell(target_row, col_index, new_advisory_url.strip())

            #             st.success(f"✅ Advisory link saved successfully! (Sheet row {target_row})")
            #             load_launch_database.clear()
            #             st.rerun()

            #         except Exception as e:
            #             st.error(f"Failed to save advisory link: {e}")
           
            # ====================== PHILSA ADVISORY ======================
            # st.markdown("### PhilSA Advisory")
            # st.caption("Paste the advisory link from the agency website for this launch.")

            # advisory_url = str(selected_row.get("ADVISORY_URL", "")).strip()
            # has_advisory = advisory_url and advisory_url.lower() not in ["nan", "none", ""]

            # # Initialize edit mode in session state
            # edit_key = f"edit_advisory_{selected_idx}"
            # if edit_key not in st.session_state:
            #     st.session_state[edit_key] = False

            # if has_advisory and not st.session_state[edit_key]:
            #     # ----- Link already exists -----
            #     st.success("Advisory link is saved for this launch.")
                
            #     st.link_button(
            #         "📄 View Agency Advisory",
            #         url=advisory_url,
            #         use_container_width=True
            #     )

            #     if st.button("✏️ Edit / Change Advisory Link", use_container_width=True, key=f"edit_btn_{selected_idx}"):
            #         st.session_state[edit_key] = True
            #         st.rerun()

            # else:
            #     # ----- No link yet OR user clicked Edit -----
            #     if has_advisory:
            #         st.info("You are editing the existing advisory link.")

            #     new_advisory_url = st.text_input(
            #         "Advisory URL",
            #         value=str(advisory_url) if has_advisory else "",
            #         placeholder="https://philsa.gov.ph/....",
            #         key=f"advisory_url_input_{selected_idx}"
            #     )

            #     col_save, col_cancel = st.columns(2)

            #     with col_save:
            #         if st.button("💾 Save Advisory Link", type="primary", use_container_width=True, key=f"save_advisory_{selected_idx}"):
            #             if not new_advisory_url or not new_advisory_url.strip():
            #                 st.error("Please paste a valid advisory link.")
            #             else:
            #                 try:
            #                     ws = get_launch_worksheet()

            #                     original_index = selected_row.name
            #                     target_row = int(original_index) + 2

            #                     headers = ws.row_values(1)

            #                     if "ADVISORY_URL" not in headers:
            #                         ws.update_cell(1, len(headers) + 1, "ADVISORY_URL")
            #                         headers.append("ADVISORY_URL")

            #                     col_index = headers.index("ADVISORY_URL") + 1
            #                     ws.update_cell(target_row, col_index, new_advisory_url.strip())

            #                     st.success(f"✅ Advisory link saved successfully! (Sheet row {target_row})")
            #                     st.session_state[edit_key] = False
            #                     load_launch_database.clear()
            #                     st.rerun()

            #                 except Exception as e:
            #                     st.error(f"Failed to save advisory link: {e}")

            #     with col_cancel:
            #         if has_advisory and st.button("❌ Cancel Edit", use_container_width=True, key=f"cancel_edit_{selected_idx}"):
            #             st.session_state[edit_key] = False
            #             st.rerun()

            # ====================== PHILSA ADVISORY ======================
            st.markdown("### PhilSA Advisory")

            advisory_url = str(selected_row.get("ADVISORY_URL", "")).strip()
            has_advisory = advisory_url and advisory_url.lower() not in ["nan", "none", ""]

            if has_advisory:
                st.success("Advisory is available for this launch.")
                st.link_button(
                    "📄 View Agency Advisory",
                    url=advisory_url,
                    use_container_width=True
                )
            else:
                st.info("No agency advisory available for this launch.")

    with tab_query:
        st.subheader("Query Launch Database")
        st.caption("Filter launches and explore results with summary, map, and charts")

        # ====================== FILTERS ======================
        st.markdown("### Filters")

        qcol1, qcol2, qcol3, qcol4 = st.columns(4)

        with qcol1:
            q_years = st.multiselect(
                "Year",
                options=available_years,
                default=[],
                placeholder="All years",
                key="query_years"
            )

        with qcol2:
            q_months = st.multiselect(
                "Month",
                options=month_options,
                format_func=lambda x: month_labels[x],
                default=[],
                placeholder="All months",
                key="query_months"
            )

        with qcol3:
            q_countries = st.multiselect(
                "Launching State",
                options=country_opts,
                default=[],
                placeholder="All states",
                key="query_countries"
            )

        with qcol4:
            q_centers = st.multiselect(
                "Launch Center",
                options=center_opts,
                default=[],
                placeholder="All centers",
                key="query_centers"
            )

        # Apply filters
        query_df = database_df.copy()

        if q_years:
            query_df = query_df[query_df["LAUNCH DATE"].dt.year.isin(q_years)]
        if q_months:
            query_df = query_df[query_df["LAUNCH DATE"].dt.month.isin(q_months)]
        if q_countries:
            query_df = query_df[query_df["LAUNCHING STATE"].isin(q_countries)]
        if q_centers:
            query_df = query_df[query_df["LAUNCH CENTER"].isin(q_centers)]

        st.markdown(f"**Results: {len(query_df)} launches**")

        if query_df.empty:
            st.warning("No launches match your current filters.")
            st.stop()

        # ====================== CALCULATE METRICS ======================
        for col in ["ARCH_COUNT", "EEZ_COUNT", "FIR_COUNT", "OUTSIDE_COUNT", "DZ_COUNT"]:
            if col in query_df.columns:
                query_df[col] = pd.to_numeric(query_df[col], errors="coerce").fillna(0)

        total_launches = len(query_df)
        total_dz = int(query_df["DZ_COUNT"].sum()) if "DZ_COUNT" in query_df.columns else 0
        arch_count = int(query_df["ARCH_COUNT"].sum()) if "ARCH_COUNT" in query_df.columns else 0
        eez_count = int(query_df["EEZ_COUNT"].sum()) if "EEZ_COUNT" in query_df.columns else 0
        fir_count = int(query_df["FIR_COUNT"].sum()) if "FIR_COUNT" in query_df.columns else 0
        outside_count = int(query_df["OUTSIDE_COUNT"].sum()) if "OUTSIDE_COUNT" in query_df.columns else 0

        # ----- Top Launch Site (combine Wenchang + Hainan) -----
        def normalize_site(site):
            if pd.isna(site):
                return "Unknown"
            site = str(site).strip()
            if site in ["Wenchang Space Launch Site", "Hainan International Commercial Launch Center"]:
                return "Wenchang / Hainan"
            return site

        query_df["SITE_NORMALIZED"] = query_df["LAUNCH CENTER"].apply(normalize_site)
        site_counts = query_df["SITE_NORMALIZED"].value_counts()
        top_site = site_counts.idxmax() if not site_counts.empty else "—"
        top_site_count = int(site_counts.max()) if not site_counts.empty else 0

        # ====================== TOP SECTION: Summary + Map ======================
        left_col, right_col = st.columns([1, 3])

        # ---------- LEFT: SUMMARY (1 container, 2 columns) ----------
        # ---------- LEFT: SUMMARY ----------
        with left_col:
            st.markdown("### Launch Summary")

            # Calculate top country
            country_counts = query_df["LAUNCHING STATE"].value_counts()
            top_country = country_counts.idxmax() if not country_counts.empty else "—"
            top_country_count = int(country_counts.max()) if not country_counts.empty else 0

            # Calculate top site (Wenchang + Hainan combined)
            def normalize_site(site):
                if pd.isna(site):
                    return "Unknown"
                site = str(site).strip()
                if site in ["Wenchang Space Launch Site", "Hainan International Commercial Launch Center"]:
                    return "Wenchang / Hainan"
                return site

            query_df["SITE_NORMALIZED"] = query_df["LAUNCH CENTER"].apply(normalize_site)
            site_counts = query_df["SITE_NORMALIZED"].value_counts()
            top_site = site_counts.idxmax() if not site_counts.empty else "—"
            top_site_count = int(site_counts.max()) if not site_counts.empty else 0

            # ====================== CONTAINER 1 ======================
            # Total Launches, Debris Retrieved, Top Launching Country, Top Launch Site
            metrics1 = [
                ("Total Launches", total_launches),
                ("Debris Retrieved (all years)", 11),
                ("Top Launching Country", f"{top_country}<br>{top_country_count}"),
                ("Top Launch Site", f"{top_site}<br>{top_site_count}"),
            ]

            with st.container(border=True):
                col1, col2 = st.columns(2)

                for i in [0, 1]:
                    with (col1 if i == 0 else col2):
                        title, value = metrics1[i]
                        with st.container(height=130, border=False):
                            st.markdown(f":blue-badge[{title}]")
                            st.markdown(f"""
                                <div style="font-size: 1.8rem; font-weight: 700; 
                                            text-align: left; margin-top: 8px; line-height: 1.3;">
                                    {value}
                                </div>
                            """, unsafe_allow_html=True)

                for i in [2, 3]:
                    with (col1 if i == 2 else col2):
                        title, value = metrics1[i]
                        with st.container(height=130, border=False):
                            st.markdown(f":blue-badge[{title}]")
                            st.markdown(f"""
                                <div style="font-size: 1.4rem; font-weight: 700; 
                                            text-align: left; margin-top: 8px; line-height: 1.3;">
                                    {value}
                                </div>
                            """, unsafe_allow_html=True)

            # ====================== CONTAINER 2 ======================
            # Total Drop Zones, Archipelagic Waters, EEZ, Manila FIR
            st.markdown("### Drop Zone Summary")
            metrics2 = [
                ("Total Drop Zones", total_dz),
                ("Archipelagic Waters", arch_count),
                ("EEZ", eez_count),
                ("Manila FIR", fir_count),
            ]

            with st.container(border=True):
                col1, col2 = st.columns(2)

                for i in [0, 3]:
                    with (col1 if i == 0 else col2):
                        title, value = metrics2[i]
                        with st.container(height=130, border=False):
                            st.markdown(f":blue-badge[{title}]")
                            st.markdown(f"""
                                <div style="font-size: 1.8rem; font-weight: 700; 
                                            text-align: left; margin-top: 8px; line-height: 1.3;">
                                    {value}
                                </div>
                            """, unsafe_allow_html=True)

                for i in [2, 1]:
                    with (col1 if i == 2 else col2):
                        title, value = metrics2[i]
                        with st.container(height=130, border=False):
                            st.markdown(f":blue-badge[{title}]")
                            st.markdown(f"""
                                <div style="font-size: 1.8rem; font-weight: 700; 
                                            text-align: left; margin-top: 8px; line-height: 1.3;">
                                    {value}
                                </div>
                            """, unsafe_allow_html=True)

            # st.markdown("**Drop Zone Breakdown**")

            # zone_colors = {
            #     "Archipelagic Waters": "#FFD700",
            #     "EEZ": "#00008B",
            #     "Manila FIR": "#FFA500",
            #     "Outside PH Zones": "#808080"
            # }
            # counts = {
            #     "Archipelagic Waters": arch_count,
            #     "EEZ": eez_count,
            #     "Manila FIR": fir_count,
            #     "Outside PH Zones": outside_count
            # }

            # dz_cols = st.columns(2)
            # for i, (zone, count) in enumerate(counts.items()):
            #     color = zone_colors.get(zone, "#1a4fa3")
            #     with dz_cols[i % 2]:
            #         with st.container(border=True):
            #             st.markdown(f":blue-badge[{zone}]")
            #             st.markdown(f"""
            #                 <div style="font-size: 2.3rem; font-weight: 700; color: {color}; 
            #                         text-align: center; margin-bottom: 10px;">
            #                     {count}
            #                 </div>
            #             """, unsafe_allow_html=True)

        # ---------- RIGHT: MAP ----------
        with right_col:
            st.markdown("### Drop Zone Map (Filtered Results)")
            st.caption("All drop zones from launches matching your filters")

            if not query_df.empty:
                query_map = create_historical_map(query_df, st.session_state.shape_dir)
                st_folium(
                    query_map,
                    width="100%",
                    height=620,
                    returned_objects=[],
                    key="query_map"
                )
            else:
                st.info("No drop zones to display.")

        st.divider()

        # ====================== CHARTS SECTION ======================
        st.markdown("### Charts")

        chart_left, chart_right = st.columns(2)

        # Left: Drop Zones by Maritime Zone
        with chart_left:
            st.markdown("**Drop Zones by Maritime Zone**")
            zone_data = pd.DataFrame({
                "Zone": ["Archipelagic Waters", "EEZ", "Manila FIR", "Outside PH Zones"],
                "Count": [arch_count, eez_count, fir_count, outside_count]
            })

            fig_zone = px.bar(
                zone_data,
                x="Zone",
                y="Count",
                text="Count",
                color="Zone",
                color_discrete_sequence=["#FFD700", "#00008B", "#FFA500", "#808080"],
                height=400,
                template="plotly"
            )
            fig_zone.update_traces(textposition="outside", textfont_size=15)
            fig_zone.update_layout(
                showlegend=False,
                xaxis_title="",
                yaxis_title="Number of Drop Zones",
                margin=dict(t=20, b=40)
            )
            st.plotly_chart(fig_zone, use_container_width=True)

        # Right: Launches by Launching State
        with chart_right:
            st.markdown("**Launches by Launching State**")
            country_counts = query_df["LAUNCHING STATE"].value_counts().reset_index()
            country_counts.columns = ["Country", "Launches"]

            fig_country = px.pie(
                country_counts,
                names="Country",
                values="Launches",
                hole=0.42,
                height=400,
                color_discrete_sequence=px.colors.qualitative.Set2
            )
            fig_country.update_traces(textinfo="percent", textfont_size=15)
            fig_country.update_layout(margin=dict(t=45, b=45))
            st.plotly_chart(fig_country, use_container_width=True)

       # Full width: Launches by Year (Line Chart)
        st.markdown("**Launches by Year**")

        year_counts = (
            query_df["LAUNCH DATE"].dt.year
            .value_counts()
            .sort_index()
            .reset_index()
        )
        year_counts.columns = ["Year", "Launches"]
        year_counts["Year"] = year_counts["Year"].astype(int)

        fig_year = px.line(
            year_counts,
            x="Year",
            y="Launches",
            markers=True,
            text="Launches",               # Show values on markers
            template="plotly",
            height=400
        )

        fig_year.update_traces(
            line=dict(color="#1976d2", width=3),
            marker=dict(size=9),
            textposition="top center",
            textfont_size=14
        )

        fig_year.update_layout(
            xaxis_title="Year",
            yaxis_title="Number of Launches",
            margin=dict(t=30, b=40),
            xaxis=dict(
                dtick=1,                   # Force yearly increments
                tickmode="linear",
                type="linear"
            )
        )

        st.plotly_chart(fig_year, use_container_width=True)
        st.divider()

        # ====================== TABLE + DOWNLOAD ======================
        st.markdown("### Filtered Results")

        display_cols = [
            "LAUNCH DATE", "ROCKET NAME", "LAUNCHING STATE", "LAUNCH CENTER",
            "START TO END TIME", "DZ_COUNT", "ARCH_COUNT", "EEZ_COUNT", "FIR_COUNT", "OUTSIDE_COUNT"
        ]
        display_cols = [c for c in display_cols if c in query_df.columns]

        display_df = query_df[display_cols].copy()
        if "LAUNCH DATE" in display_df.columns:
            display_df["LAUNCH DATE"] = display_df["LAUNCH DATE"].dt.strftime("%d %B %Y")

        st.dataframe(display_df, use_container_width=True, hide_index=True)

        dl1, dl2 = st.columns(2)
        with dl1:
            csv = query_df.to_csv(index=False).encode("utf-8")
            st.download_button(
                "📥 Download CSV",
                data=csv,
                file_name="query_results.csv",
                mime="text/csv",
                use_container_width=True,
                key="query_csv"
            )
        with dl2:
            excel_buffer = io.BytesIO()
            with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
                query_df.to_excel(writer, index=False, sheet_name="Query Results")
            excel_buffer.seek(0)
            st.download_button(
                "📥 Download Excel",
                data=excel_buffer,
                file_name="query_results.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
                key="query_excel"
            )

#pseudo mapping for Launch Database tab (09 July 2026)
# elif st.session_state.selected == "Launch Database":
#     st.caption("Live from Google Sheets → Rocket-Launch-Monitoring / Sheet1")
   
#     try:
#         ws = get_launch_worksheet()
#         data = ws.get_all_records()
#         database_df = pd.DataFrame(data)
#         st.success(f"✅ Connected to Google Sheets • {len(database_df)} launches")
#     except Exception as e:
#         st.error(f"Could not load database: {e}")
#         database_df = pd.DataFrame()
    
#     if len(database_df) == 0:
#         st.stop()

#     st.subheader("Filters")
#     col_f1, col_f2, col_f3 = st.columns(3)
   
#     # ==================== PSEUDO MAPPING ====================
#     database_df = database_df.copy()
    
#     # Rocket Name Pseudo
#     if "ROCKET NAME" in database_df.columns:
#         unique_rockets = sorted(database_df["ROCKET NAME"].dropna().unique())
#         rocket_mapping = {r: f"Rocket {chr(65 + i)}" for i, r in enumerate(unique_rockets)}
#         database_df["Pseudo_Rocket"] = database_df["ROCKET NAME"].map(rocket_mapping)
    
#     # Launch Center Pseudo
#     if "LAUNCH CENTER" in database_df.columns:
#         unique_sites = sorted(database_df["LAUNCH CENTER"].dropna().unique())
#         site_mapping = {s: f"Site {chr(65 + i)}" for i, s in enumerate(unique_sites)}
#         database_df["Pseudo_Site"] = database_df["LAUNCH CENTER"].map(site_mapping)
    
#     # Launching State (Country) Pseudo
#     if "LAUNCHING STATE" in database_df.columns:
#         unique_countries = sorted(database_df["LAUNCHING STATE"].dropna().unique())
#         country_mapping = {c: f"Country {chr(65 + i)}" for i, c in enumerate(unique_countries)}
#         database_df["Pseudo_Country"] = database_df["LAUNCHING STATE"].map(country_mapping)

#     # ==================== FILTERS ====================
#     with col_f1:
#         try:
#             database_df["LAUNCH DATE"] = pd.to_datetime(database_df["LAUNCH DATE"], errors="coerce")
#             date_filter = st.date_input("Filter by Launch Date", value=None)
#         except:
#             date_filter = None
           
#     with col_f2:
#         # Show pseudo country names in filter
#         country_opts = sorted(database_df["Pseudo_Country"].dropna().unique().tolist())
#         countries_sel = st.multiselect("Country", country_opts, default=[])
       
#     with col_f3:
#         # Show pseudo site names in filter
#         center_opts = sorted(database_df["Pseudo_Site"].dropna().unique().tolist())
#         centers_sel = st.multiselect("Launch Center", center_opts, default=[])

#     # Apply filters (using pseudo for display, real for filtering)
#     filtered_df = database_df.copy()
#     if date_filter:
#         filtered_df = filtered_df[filtered_df["LAUNCH DATE"].dt.date == date_filter]
#     if countries_sel:
#         filtered_df = filtered_df[filtered_df["Pseudo_Country"].isin(countries_sel)]
#     if centers_sel:
#         filtered_df = filtered_df[filtered_df["Pseudo_Site"].isin(centers_sel)]

#     st.subheader(f"Showing {len(filtered_df)} launches")
#     if len(filtered_df) == 0:
#         st.info("No launches match your filters.")
#         st.stop()

#     # Preview Selector (using pseudo names)
#     select_options = [
#         f"{row.get('LAUNCH DATE','—')} | {row.get('Pseudo_Rocket', row.get('ROCKET NAME','Unknown'))} | {row.get('Pseudo_Site', row.get('LAUNCH CENTER','—'))}"
#         for _, row in filtered_df.iterrows()
#     ]
    
#     selected_label = st.selectbox("Preview drop zone map for:", select_options)
#     selected_idx = select_options.index(selected_label)
#     selected_row = filtered_df.iloc[selected_idx]

#     # ====================== MAIN LAYOUT ======================
#     left_col, right_col = st.columns([1, 3])
#     with left_col:
#         st.markdown("**Launch Information**")
       
#         with st.container(border=True):
#             raw_date = selected_row.get('LAUNCH DATE', '—')
#             window_time = selected_row.get('START TO END TIME', '—')
           
#             # Date formatting (unchanged)
#             try:
#                 if pd.isna(raw_date) or raw_date in ['—', None, '']:
#                     formatted_date = '—'
#                 else:
#                     dt = pd.to_datetime(raw_date, errors='coerce')
#                     if pd.isna(dt):
#                         formatted_date = str(raw_date).split()[0]
#                     else:
#                         formatted_date = dt.strftime("%d %B %Y")
#             except:
#                 formatted_date = str(raw_date).split()[0] if str(raw_date) != '—' else '—'
                
#             window_display = f"{formatted_date} between {window_time}" if formatted_date != '—' else window_time
           
#             metrics = [
#                 ("Rocket Name", selected_row.get('Pseudo_Rocket', selected_row.get('ROCKET NAME', '—'))),
#                 ("Launch Center", selected_row.get('Pseudo_Site', selected_row.get('LAUNCH CENTER', '—'))),
#                 ("Launching State", selected_row.get('Pseudo_Country', selected_row.get('LAUNCHING STATE', '—'))),
#                 ("Date and Time (PhST)", window_display),
#             ]
           
#             col1, col2 = st.columns(2)
           
#             for i in [0, 1]:
#                 with (col1 if i == 0 else col2):
#                     title, value = metrics[i]
#                     with st.container(height=130, border=False):
#                         st.markdown(f":blue-badge[{title}]")
#                         st.markdown(f"""
#                             <div style="font-size: 1.32rem; font-weight: 700;
#                                        text-align: left; margin-top: 8px; line-height: 1.3;">
#                                 {value}
#                             </div>
#                         """, unsafe_allow_html=True)
           
#             for i in [2, 3]:
#                 with (col1 if i == 2 else col2):
#                     title, value = metrics[i]
#                     with st.container(height=130, border=False):
#                         st.markdown(f":blue-badge[{title}]")
#                         st.markdown(f"""
#                             <div style="font-size: 1.32rem; font-weight: 700;
#                                        text-align: left; margin-top: 8px; line-height: 1.3;">
#                                 {value}
#                             </div>
#                         """, unsafe_allow_html=True)

#         # Drop Zone Summary (unchanged)
#         st.markdown("**Drop Zone Summary**")
#         zone_colors = {
#             "Archipelagic Waters": "#FFD700",
#             "EEZ": "#00008B",
#             "Manila FIR": "#FFA500",
#             "Outside PH Zones": "#808080"
#         }
       
#         counts = {
#             "Archipelagic Waters": int(selected_row.get("ARCH_COUNT", 0) or 0),
#             "EEZ": int(selected_row.get("EEZ_COUNT", 0) or 0),
#             "Manila FIR": int(selected_row.get("FIR_COUNT", 0) or 0),
#             "Outside PH Zones": int(selected_row.get("OUTSIDE_COUNT", 0) or 0)
#         }
       
#         dz_cols = st.columns(2)
#         for i, (zone, count) in enumerate(counts.items()):
#             color = zone_colors.get(zone, "#1a4fa3")
#             with dz_cols[i % 2]:
#                 with st.container(border=True):
#                     st.markdown(f":blue-badge[{zone}]")
#                     st.markdown(f"""
#                         <div style="font-size: 3.0rem; font-weight: 700; color: {color};
#                                    text-align: center; margin-bottom: 16px;">
#                             {count}
#                         </div>
#                     """, unsafe_allow_html=True)

#     # ==================== RIGHT SIDE - MAP ====================
#     with right_col:
#         st.markdown("**Drop Zone Map**")
       
#         dropzones_db = {}
#         for dz_num in range(1, 5):
#             dz = f"DZ{dz_num}"
#             verts = [str(selected_row.get(f"{dz} P{p}", "")).strip()
#                     for p in range(1, 9) if str(selected_row.get(f"{dz} P{p}", "")).strip()]
#             debs = [str(selected_row.get(f"DEB{dz_num} P{p}", "")).strip()
#                    for p in range(1, 5) if str(selected_row.get(f"DEB{dz_num} P{p}", "")).strip()]
#             dropzones_db[dz] = {"vertices": verts, "debris": debs}
       
#         preview_map = create_folium_map(
#             selected_row.get("LAUNCH CENTER", ""),   # Keep original for map centering
#             dropzones_db,
#             st.session_state.shape_dir
#         )
       
#         st_folium(
#             preview_map,
#             width="100%",
#             height=720,
#             returned_objects=[],
#             key=f"db_map_{selected_idx}"
#         )
                
st.caption("Compiled by the Space Mission Control and Operations Division (SMCOD) of the Philippine Space Agency (PhilSA)")