import os
import json
import hashlib
import requests
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import gspread
from datetime import datetime, timezone
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

load_dotenv()

# ══════════════════════════════════════════════════════════════════════════
# DUMMY DISPLAY MODE (optional, for demos/screenshots)
#
# When enabled, object names and NORAD IDs shown in the UI (cards, analyzer
# header, history panel) are swapped for deterministic dummy values. All
# underlying Space-Track/CelesTrak lookups, caching, and NORAD-based function
# calls still use the REAL NORAD ID -- only what gets *displayed* changes.
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
    Does NOT affect any fetch/cache/lookup logic -- keep passing the REAL
    norad_id into every function call as usual; only swap in the returned
    values where text is rendered on screen.
    """
    if not DEMO_MODE:
        return real_norad, (real_name if real_name is not None else str(real_norad))
    seed = int(hashlib.sha1(str(real_norad).encode()).hexdigest(), 16)
    dummy_name  = _DEMO_NAME_POOL[seed % len(_DEMO_NAME_POOL)]
    dummy_norad = 10000 + (seed % 90000)
    return dummy_norad, dummy_name

SHEET_ID             = "1isa_rOueh0yDY9OM43Q2AWZoKPGrlvEo7JOSHJdW3jE"
DATA_SHEET           = "Data"
ACK_SHEET            = "ACK"
REENTRY_ANALYSIS_SHEET  = "REENTRY_ANALYSIS"   # one row per NORAD event snapshot
REENTRY_PRODUCTS_SHEET  = "REENTRY_PRODUCTS"   # one row per prediction product
REENTRY_HISTORY_SHEET   = "REENTRY_HISTORY"    # audit log per analysis run

SPACETRACK_BASE = "https://www.space-track.org"
SATCAT_FULL_SHEET       = "SATCAT_FULL"   # shared cache: full locally-merged SATCAT snapshot
SATCAT_STATE_SHEET      = "SATCAT_STATE"  # shared cursor: last_run_utc + last_file_number
SATCAT_REFRESH_HOUR   = 17
SATCAT_REFRESH_MINUTE = 20   # dashboard.py's scheduled daily SATCAT sync window (17:20 UTC)
MAX_ANALYSIS_CACHE_HOURS = 6   # re-fetch live data if cache is older than 6h
TIP_RAW_SHEET            = "TIP_RAW"  # raw TIP rows from Space-Track, one row per TIP record
TIP_FETCH_LIMIT          = 200        # match reentry_event_predictor default
MAX_TIP_CACHE_HOURS      = 6          # how stale TIP_RAW can be before re-fetching


# CSS
def inject_css():
    st.markdown("""
    <style>
    .re-card {
        border: 1px solid rgba(128,128,128,0.3);
        padding: 14px 16px;
        border-radius: 12px;
        margin-bottom: 14px;
        background: var(--background-color);
        color: var(--text-color);
        font-size: 0.88rem;
        line-height: 1.7;
    }
    .re-card b { color: var(--text-color); font-weight: 600; opacity: 0.7; }
    .re-card .val { color: var(--text-color); font-weight: 500; }
    .re-card .major { color: #f44336; font-weight: 700; }
    .re-card .minor { color: #2ecc71; font-weight: 700; }

    .re-row-badge {
        display: inline-block;
        background: var(--secondary-background-color);
        border: 1px solid rgba(128,128,128,0.3);
        color: var(--text-color);
        font-size: 0.72rem;
        font-weight: 600;
        padding: 2px 8px;
        border-radius: 20px;
        margin-left: 8px;
        vertical-align: middle;
        opacity: 0.75;
    }
    .re-ack-badge {
        display: inline-block;
        background: rgba(46,204,113,0.15);
        color: #2ecc71;
        font-size: 0.78rem;
        font-weight: 700;
        padding: 4px 12px;
        border-radius: 8px;
        margin-bottom: 10px;
    }
    .re-metrics {
        display: flex;
        gap: 12px;
        flex-wrap: wrap;
        margin-bottom: 20px;
    }
    .re-metric {
        background: var(--secondary-background-color);
        border-radius: 15px;
        padding: 20px 16px;
        flex: 1;
        min-width: 140px;
        text-align: center;
        box-shadow: 0 4px 15px rgba(0,0,0,0.15);
        border: 1px solid rgba(128,128,128,0.2);
        transition: all 0.3s ease;
        height: 138px;
        display: flex;
        flex-direction: column;
        justify-content: center;
        align-items: center;
    }
    .re-metric:hover { transform: translateY(-4px); box-shadow: 0 8px 20px rgba(0,0,0,0.2); }
    .m-val { font-size: 2.8rem; font-weight: 700; margin: 0; line-height: 1; color: var(--text-color); }
    .m-lbl { font-size: 1.1rem; color: var(--text-color); opacity: 0.6; font-weight: 500; margin-bottom: 12px; }

    div[data-testid="stDataFrame"] thead th {
        background: var(--secondary-background-color) !important;
        color: var(--text-color) !important;
        font-size: 0.75rem !important;
        text-transform: uppercase !important;
        opacity: 0.75;
    }
    div[data-testid="stDataFrame"] td {
        color: var(--text-color) !important;
        font-size: 0.82rem !important;
        border-bottom: 1px solid rgba(128,128,128,0.15) !important;
    }

    /* Analyzer panel */
    .analyzer-header {
        background: var(--secondary-background-color);
        border: 1px solid rgba(128,128,128,0.2);
        border-radius: 12px;
        padding: 16px 20px;
        margin-bottom: 16px;
    }
    .analyzer-header .event-title {
        font-size: 1.4rem;
        font-weight: 800;
        color: var(--text-color);
        margin-bottom: 2px;
    }
    .analyzer-header .event-sub {
        font-size: 0.8rem;
        color: var(--text-color);
        opacity: 0.6;
    }
    .epoch-pill {
        display: inline-block;
        background: rgba(33,150,243,0.12);
        color: #2196f3;
        font-size: 0.78rem;
        font-weight: 700;
        padding: 4px 14px;
        border-radius: 20px;
        border: 1px solid rgba(33,150,243,0.3);
        margin: 2px 4px;
    }
    .risk-major {
        display: inline-block;
        background: rgba(244,67,54,0.15);
        color: #f44336;
        font-size: 0.82rem;
        font-weight: 800;
        padding: 4px 14px;
        border-radius: 8px;
        border: 1px solid rgba(244,67,54,0.35);
    }
    .risk-minor {
        display: inline-block;
        background: rgba(46,204,113,0.15);
        color: #2ecc71;
        font-size: 0.82rem;
        font-weight: 800;
        padding: 4px 14px;
        border-radius: 8px;
        border: 1px solid rgba(46,204,113,0.35);
    }
    .section-title {
        font-weight: 700;
        font-size: 0.85rem;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        opacity: 0.5;
        color: var(--text-color);
        margin: 18px 0 8px;
    }
    .detail-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
        gap: 10px;
        margin-bottom: 12px;
    }
    .detail-cell {
        background: var(--secondary-background-color);
        border-radius: 8px;
        padding: 10px 14px;
        border: 1px solid rgba(128,128,128,0.15);
    }
    .detail-cell .dc-label { font-size: 0.72rem; opacity: 0.55; text-transform: uppercase; letter-spacing: 0.04em; color: var(--text-color); }
    .detail-cell .dc-value { font-size: 0.92rem; font-weight: 600; color: var(--text-color); margin-top: 2px; }

    .product-row {
        display: flex;
        align-items: center;
        gap: 12px;
        padding: 10px 14px;
        border-radius: 8px;
        border: 1px solid rgba(128,128,128,0.15);
        margin-bottom: 6px;
        background: var(--secondary-background-color);
        font-size: 0.83rem;
        color: var(--text-color);
    }
    .product-row .pr-id { font-weight: 700; min-width: 160px; }
    .product-row .pr-range { opacity: 0.7; flex: 1; }
    .product-row .pr-pub { opacity: 0.55; font-size: 0.77rem; }

    .history-row {
        padding: 10px 14px;
        border-radius: 8px;
        border-left: 3px solid #2196f3;
        background: var(--secondary-background-color);
        margin-bottom: 6px;
        font-size: 0.82rem;
        color: var(--text-color);
    }

    .tab-section { margin-top: 8px; }
    </style>""", unsafe_allow_html=True)


# CREDENTIALS
def get_spacetrack_creds():
    try:
        return st.secrets["spacetrack"]["username"], st.secrets["spacetrack"]["password"]
    except Exception:
        return os.getenv("SPACETRACK_USER"), os.getenv("SPACETRACK_PASS")


def get_gspread_client():
    try:
        creds = Credentials.from_service_account_info(
            st.secrets["gcp_service_account_odr"],
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
    except Exception:
        path = os.getenv("GCP_JSON_ODR")
        if not path or not os.path.exists(path):
            st.error("GCP_JSON_ODR not found")
            st.stop()
        creds = Credentials.from_service_account_file(
            path, scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
    return gspread.authorize(creds)


def _get_or_create_ws(sh, title, rows=500, cols=20, header=None):
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=rows, cols=cols)
        if header:
            ws.append_row(header)
        return ws


# EXISTING DATA LOADING (unchanged)
@st.cache_data(ttl=60)
def load_data():
    client = get_gspread_client()
    ws     = client.open_by_key(SHEET_ID).worksheet(DATA_SHEET)
    rows   = ws.get_all_records()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df.columns = [c.strip() for c in df.columns]
    df["sheet_row"] = df.index + 2
    df["Hit UTC"]   = pd.to_datetime(df["Hit UTC"],   errors="coerce")
    df["Distance"]  = pd.to_numeric(df["Distance"],   errors="coerce")
    df = df.drop(columns=[c for c in df.columns if "__PowerAppsId__" in c], errors="ignore")
    return df


def load_acknowledged():
    client = get_gspread_client()
    try:
        ws = client.open_by_key(SHEET_ID).worksheet(ACK_SHEET)
        df = pd.DataFrame(ws.get_all_records())
        return set(df["NORAD"].astype(str)) if not df.empty else set()
    except Exception:
        return set()


def acknowledge(norad):
    client = get_gspread_client()
    try:
        ws = client.open_by_key(SHEET_ID).worksheet(ACK_SHEET)
    except Exception:
        ws = client.open_by_key(SHEET_ID).add_worksheet(title=ACK_SHEET, rows=1000, cols=5)
        ws.append_row(["NORAD", "User", "Time"])
    user = st.session_state.get("user", {}).get("displayName", "local_user")
    ws.append_row([str(norad), user, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")])


# SPACE-TRACK SESSION
def spacetrack_login():
    user, pw = get_spacetrack_creds()
    s = requests.Session()
    resp = s.post(f"{SPACETRACK_BASE}/ajaxauth/login",
                  data={"identity": user, "password": pw}, timeout=15)
    resp.raise_for_status()
    return s


# ============================================================
# SATCAT — local cache lookup + gated once-daily catch-up sync
# ============================================================
# This page never queries SATCAT by object type or by individual NORAD ID.
# The scheduled path is dashboard.py's daily sync at 17:20 UTC — but since
# Streamlit only runs code when a page is actually being viewed, that window
# can be missed entirely if nobody has the app open at that exact minute.
# The catch-up sync below exists ONLY for that missed case: it stays hidden
# unless (a) 17:20 UTC has already passed today, AND (b) no sync — scheduled
# or catch-up — has been recorded yet today. The moment either sync happens,
# it hides itself again until tomorrow's window passes. It is not a
# free-anytime override; it's a once-a-day safety net for a missed run, and
# it still makes only the same single bulk /file/>previous_file_number
# delta call — never per-type or per-NORAD.
def _get_satcat_state_ws(sh):
    return _get_or_create_ws(sh, SATCAT_STATE_SHEET,
                              header=["last_run_utc", "last_file_number"])


def _read_satcat_state():
    try:
        client = get_gspread_client()
        sh     = client.open_by_key(SHEET_ID)
        ws     = _get_satcat_state_ws(sh)
        rows   = ws.get_all_records()
        if not rows:
            return None
        latest = rows[-1]
        return {
            "last_run_utc"    : latest["last_run_utc"],
            "last_file_number": int(latest["last_file_number"]),
        }
    except Exception:
        return None


def _write_satcat_state(file_number):
    client = get_gspread_client()
    sh     = client.open_by_key(SHEET_ID)
    ws     = _get_satcat_state_ws(sh)
    ws.append_row([datetime.now(timezone.utc).isoformat(), int(file_number or 0)])


def _get_satcat_full_ws(sh):
    return _get_or_create_ws(
        sh, SATCAT_FULL_SHEET, rows=200, cols=10,
        header=["norad_cat_id", "object_name", "object_type", "current", "decay"]
    )


def _read_satcat_full():
    """Load the shared full SATCAT snapshot as {norad_cat_id(int): {...}}."""
    try:
        client = get_gspread_client()
        sh     = client.open_by_key(SHEET_ID)
        ws     = _get_satcat_full_ws(sh)
        rows   = ws.get_all_records()
        catalog = {}
        for r in rows:
            try:
                nid = int(r["norad_cat_id"])
            except Exception:
                continue
            catalog[nid] = {
                "OBJECT_NAME": r.get("object_name", "UNKNOWN"),
                "OBJECT_TYPE": r.get("object_type", ""),
                "CURRENT"    : r.get("current", ""),
                "DECAY"      : r.get("decay", ""),
            }
        return catalog
    except Exception:
        return {}


def _write_satcat_full(catalog: dict):
    """Overwrite the shared SATCAT_FULL sheet with the merged catalog."""
    client = get_gspread_client()
    sh     = client.open_by_key(SHEET_ID)
    ws     = _get_satcat_full_ws(sh)
    ws.clear()
    ws.append_row(["norad_cat_id", "object_name", "object_type", "current", "decay"])
    batch = [
        [nid, rec.get("OBJECT_NAME", "UNKNOWN"), rec.get("OBJECT_TYPE", ""),
         rec.get("CURRENT", ""), rec.get("DECAY", "")]
        for nid, rec in catalog.items()
    ]
    if batch:
        ws.append_rows(batch, value_input_option="RAW")


def fetch_satcat_delta(previous_file_number=None):
    """
    ONE Space-Track SATCAT call: a bulk /file/>N delta (or a one-time full
    bootstrap query if no cursor exists yet). Never filtered by OBJECT_TYPE
    or NORAD_CAT_ID. Returns (records, latest_file_number, error).
    """
    try:
        s = spacetrack_login()
        if previous_file_number is None:
            url = (f"{SPACETRACK_BASE}/basicspacedata/query/class/satcat"
                   "/format/json/metadata/false")
        else:
            url = (f"{SPACETRACK_BASE}/basicspacedata/query/class/satcat"
                   f"/file/>{previous_file_number}/format/json/metadata/false")
        data = s.get(url, timeout=60).json()
        latest_file = previous_file_number
        for row in data:
            try:
                fn = int(row.get("FILE", 0))
                if latest_file is None or fn > latest_file:
                    latest_file = fn
            except Exception:
                continue
        return data, latest_file, None
    except Exception as e:
        return None, None, str(e)


def _merge_satcat_delta_and_persist(prev_file):
    """Merge one bulk SATCAT delta into the shared local snapshot and persist it."""
    delta_records, latest_file, err = fetch_satcat_delta(prev_file)
    if err or delta_records is None:
        return err

    catalog = _read_satcat_full()
    for row in delta_records:
        try:
            nid = int(row["NORAD_CAT_ID"])
        except Exception:
            continue
        catalog[nid] = {
            "OBJECT_NAME": row.get("SATNAME") or row.get("OBJECT_NAME", "UNKNOWN"),
            "OBJECT_TYPE": row.get("OBJECT_TYPE", ""),
            "CURRENT"    : row.get("CURRENT", ""),
            "DECAY"      : row.get("DECAY", ""),
        }

    _write_satcat_full(catalog)
    _write_satcat_state(latest_file)
    return None


def satcat_sync_missed_today():
    """
    True only when today's 17:20 UTC scheduled window has already passed AND
    no sync (scheduled or catch-up) has been recorded yet today. Before 17:20
    UTC, this is always False — there's nothing to "catch up" on yet. Once a
    sync lands for today (by either path), this flips back to False and stays
    that way until tomorrow's window passes, which is what limits the
    catch-up button to once per day.
    """
    now = datetime.now(timezone.utc)
    window_passed = (now.hour, now.minute) >= (SATCAT_REFRESH_HOUR, SATCAT_REFRESH_MINUTE)
    if not window_passed:
        return False
    state = _read_satcat_state()
    if state is None:
        return True  # never synced at all yet
    last_run = pd.to_datetime(state["last_run_utc"], utc=True)
    return last_run.date() != now.date()


def catch_up_satcat_sync():
    """
    Run today's missed SATCAT sync. Only does anything if
    satcat_sync_missed_today() is True; otherwise it's a no-op. Uses the
    exact same single bulk /file/>previous_file_number delta as the
    scheduled job. On success, SATCAT_STATE's last_run_utc becomes today,
    which both hides the catch-up button for the rest of the day and tells
    dashboard.py's scheduled check that today is already covered.
    Returns (success: bool, message: str).
    """
    if not satcat_sync_missed_today():
        return False, "Today's SATCAT sync is already up to date — nothing to catch up on."

    state = _read_satcat_state()
    prev_file = state["last_file_number"] if state else None
    err = _merge_satcat_delta_and_persist(prev_file)
    if err:
        return False, f"Catch-up sync failed: {err}"
    return True, "Catch-up SATCAT sync complete — today's cache is now up to date."


@st.cache_data(ttl=300)
def _load_satcat_name_map():
    """
    Cached read-only lookup against the shared SATCAT_FULL cache. Never calls
    Space-Track directly — see catch_up_satcat_sync() above and dashboard.py's
    scheduled job for the only two paths that ever write to this cache.
    Returns {norad_int: object_name}.
    """
    try:
        return {nid: rec.get("OBJECT_NAME", "UNKNOWN") for nid, rec in _read_satcat_full().items()}
    except Exception:
        return {}


def get_object_name(norad):
    """
    Resolve a NORAD ID's object name entirely from local caches — no live
    Space-Track SATCAT call is made here:
      1. The shared SATCAT_FULL cache (dashboard.py's daily sync).
      2. This page's own REENTRY_ANALYSIS cache, if this object was analyzed before.
    Falls back to "UNKNOWN" rather than hitting Space-Track per NORAD ID.
    """
    try:
        name = _load_satcat_name_map().get(int(norad))
        if name and name != "UNKNOWN":
            return name
    except Exception:
        pass

    try:
        client = get_gspread_client()
        sh     = client.open_by_key(SHEET_ID)
        cached_event, _, _ = _read_analysis_cache(sh, norad)
        if cached_event:
            name = cached_event.get("object_name")
            if name and name != "UNKNOWN":
                return name
    except Exception:
        pass

    return "UNKNOWN"


# GOOGLE SHEETS — REENTRY ANALYSIS CACHE
ANALYSIS_HEADER = [
    "fetched_at_utc", "norad_cat_id", "object_name", "intl_des",
    "object_type", "mass_kg", "size",
    "reentry_epoch", "window_start", "window_end",
    "epoch_uncertainty_min", "max_latitude", "altitude_km",
    "reentry_point_lat", "reentry_point_lon",
    "inclination_deg", "direction",
    "apogee", "perigee",
    "controlled_reentry", "confirmed_decay",
    "risk_index", "comments",
    "areas_of_interest",     # JSON list stored as string
]

PRODUCTS_HEADER = [
    "fetched_at_utc", "norad_cat_id", "product_id",
    "window_start", "window_end",
    "creation_date", "publish_date",
    "type", "risk_index",
]

HISTORY_HEADER = [
    "analyzed_at_utc", "norad_cat_id", "object_name",
    "reentry_epoch", "window_start", "window_end",
    "risk_index", "confirmed_decay", "user",
]


def _get_analysis_ws(sh):
    return _get_or_create_ws(sh, REENTRY_ANALYSIS_SHEET, rows=2000, cols=len(ANALYSIS_HEADER), header=ANALYSIS_HEADER)

def _get_products_ws(sh):
    return _get_or_create_ws(sh, REENTRY_PRODUCTS_SHEET, rows=5000, cols=len(PRODUCTS_HEADER), header=PRODUCTS_HEADER)

def _get_history_ws(sh):
    return _get_or_create_ws(sh, REENTRY_HISTORY_SHEET, rows=5000, cols=len(HISTORY_HEADER), header=HISTORY_HEADER)


# ── Cache readers ─────────────────────────────────────────────────────────────
def _read_analysis_cache(sh, norad_id):
    """Return (event_dict, products_list, age_hours) or (None, None, None)."""
    try:
        ws   = _get_analysis_ws(sh)
        rows = ws.get_all_records()
        if not rows:
            return None, None, None
        matching = [r for r in rows if str(r.get("norad_cat_id")) == str(norad_id)]
        if not matching:
            return None, None, None
        latest = matching[-1]
        updated_at = pd.to_datetime(latest["fetched_at_utc"], utc=True)
        age_hours  = (pd.Timestamp.utcnow() - updated_at).total_seconds() / 3600

        # Parse areas_of_interest back from JSON string
        aoi_raw = latest.get("areas_of_interest", "[]")
        try:
            latest["areas_of_interest"] = json.loads(aoi_raw) if aoi_raw else []
        except Exception:
            latest["areas_of_interest"] = []

        # Load products for this NORAD
        pws      = _get_products_ws(sh)
        prows    = pws.get_all_records()
        products = [p for p in prows if str(p.get("norad_cat_id")) == str(norad_id)]

        return latest, products, age_hours
    except Exception:
        return None, None, None


@st.cache_data(ttl=300)
def load_reentry_points():
    """
    Latest reentry_point_lat/reentry_point_lon per NORAD ID from the
    REENTRY_ANALYSIS sheet -- one cached read of the whole sheet, not a
    per-object call like _read_analysis_cache(). Safe to use across every
    row in a dashboard's event list without hitting Sheets N times.

    Only covers NORAD IDs that have gone through get_reentry_analysis()
    at least once (e.g. opened in the Analyzer tab) -- objects that
    haven't been analyzed yet simply won't have a row here.

    Returns dict[str norad_id -> dict(lat: float|None, lon: float|None,
    fetched_at_utc: str)].
    """
    try:
        client = get_gspread_client()
        sh     = client.open_by_key(SHEET_ID)
        ws     = _get_analysis_ws(sh)
        rows   = ws.get_all_records()
    except Exception:
        return {}

    if not rows:
        return {}

    df = pd.DataFrame(rows)
    df.columns = [str(c).strip() for c in df.columns]
    if "norad_cat_id" not in df.columns:
        return {}

    df["norad_cat_id"] = df["norad_cat_id"].astype(str).str.strip()
    if "fetched_at_utc" in df.columns:
        df = df.sort_values("fetched_at_utc")
    df = df.drop_duplicates(subset="norad_cat_id", keep="last")   # latest snapshot per object

    points = {}
    for _, row in df.iterrows():
        try:
            lat = float(row.get("reentry_point_lat"))
            lon = float(row.get("reentry_point_lon"))
        except (TypeError, ValueError):
            lat = lon = None
        points[row["norad_cat_id"]] = {
            "lat"           : lat,
            "lon"           : lon,
            "fetched_at_utc": row.get("fetched_at_utc", ""),
        }
    return points


def _write_analysis_cache(sh, norad_id, event, products):
    """Write one event row + N product rows, replacing previous rows for this NORAD."""
    now_str = datetime.now(timezone.utc).isoformat()

    aws  = _get_analysis_ws(sh)
    aall = aws.get_all_records()
    # Build list of 1-indexed sheet rows to delete (header=row1, data starts row2)
    rows_to_delete = [i + 2 for i, r in enumerate(aall)
                      if str(r.get("norad_cat_id")) == str(norad_id)]
    # Delete from bottom to top so indices don't shift
    for row_idx in sorted(rows_to_delete, reverse=True):
        aws.delete_rows(row_idx)

    aws.append_row([
        now_str,
        str(norad_id),
        event.get("object_name", ""),
        event.get("intl_des", ""),
        event.get("object_type", ""),
        str(event.get("mass_kg", "")),
        event.get("size", ""),
        event.get("reentry_epoch", ""),
        event.get("window_start", ""),
        event.get("window_end", ""),
        str(event.get("epoch_uncertainty_min", "")),
        str(event.get("max_latitude", "")),
        str(event.get("altitude_km", "")),
        str(event.get("reentry_point_lat", "")),
        str(event.get("reentry_point_lon", "")),
        str(event.get("inclination_deg", "")),
        event.get("direction", ""),
        str(event.get("apogee", "")),
        str(event.get("perigee", "")),
        event.get("controlled_reentry", ""),
        event.get("confirmed_decay", ""),
        event.get("risk_index", ""),
        event.get("comments", ""),
        json.dumps(event.get("areas_of_interest", [])),
    ])

    pws  = _get_products_ws(sh)
    pall = pws.get_all_records()
    prod_rows_to_delete = [i + 2 for i, r in enumerate(pall)
                           if str(r.get("norad_cat_id")) == str(norad_id)]
    for row_idx in sorted(prod_rows_to_delete, reverse=True):
        pws.delete_rows(row_idx)

    for p in products:
        pws.append_row([
            now_str,
            str(norad_id),
            p.get("product_id", ""),
            p.get("window_start", ""),
            p.get("window_end", ""),
            p.get("creation_date", ""),
            p.get("publish_date", ""),
            p.get("type", ""),
            p.get("risk_index", ""),
        ])


def _write_history(sh, norad_id, event):
    """Append one row to REENTRY_HISTORY — never deletes, full audit trail."""
    ws   = _get_history_ws(sh)
    user = st.session_state.get("user", {}).get("displayName", "local_user")
    ws.append_row([
        datetime.now(timezone.utc).isoformat(),
        str(norad_id),
        event.get("object_name", ""),
        event.get("reentry_epoch", ""),
        event.get("window_start", ""),
        event.get("window_end", ""),
        event.get("risk_index", ""),
        event.get("confirmed_decay", ""),
        user,
    ])


# TIP_RAW GOOGLE SHEET CACHE  ─  mirrors reentry_event_predictor fetch pattern

# Columns stored in TIP_RAW.  Every raw Space-Track field is JSON-serialised into
# "raw_json" so we never lose data, while the most-used fields get their own cols
# for easy inspection in Sheets.
TIP_RAW_HEADER = [
    "fetched_at_utc", "norad_cat_id",
    "MSG_EPOCH", "DECAY_EPOCH",
    "REV", "DIRECTION",
    "LAT", "LON", "INCL",
    "HIGH_INTEREST",
    "WINDOW_START", "WINDOW_END",
    "INSERT_EPOCH",
    "raw_json",   # full Space-Track record serialised as JSON string
]


def _get_tip_raw_ws(sh):
    return _get_or_create_ws(
        sh, TIP_RAW_SHEET,
        rows=5000, cols=len(TIP_RAW_HEADER),
        header=TIP_RAW_HEADER,
    )


def _read_tip_raw_cache(sh, norad_id):
    """
    Return (rows_list, age_hours) where rows_list is a list of raw-row dicts
    (as stored in TIP_RAW), or ([], None) if nothing is cached.
    age_hours is based on the most recent fetched_at_utc for this NORAD.
    """
    try:
        ws   = _get_tip_raw_ws(sh)
        all_rows = ws.get_all_records()
        matching = [r for r in all_rows if str(r.get("norad_cat_id")) == str(norad_id)]
        if not matching:
            return [], None
        # Age = time since newest fetch for this NORAD
        fetch_times = []
        for r in matching:
            try:
                fetch_times.append(pd.to_datetime(r["fetched_at_utc"], utc=True))
            except Exception:
                pass
        if not fetch_times:
            return matching, None
        newest_fetch = max(fetch_times)
        age_hours = (pd.Timestamp.utcnow() - newest_fetch).total_seconds() / 3600
        return matching, age_hours
    except Exception:
        return [], None


def _write_tip_raw_cache(sh, norad_id, tip_json_list):
    """
    Write all raw TIP records for this NORAD to TIP_RAW, replacing previous rows.
    tip_json_list: the raw list[dict] returned directly by Space-Track.
    """
    ws       = _get_tip_raw_ws(sh)
    all_rows = ws.get_all_records()
    # Delete old rows for this NORAD (bottom-to-top so indices stay valid)
    to_delete = [i + 2 for i, r in enumerate(all_rows)
                 if str(r.get("norad_cat_id")) == str(norad_id)]
    for idx in sorted(to_delete, reverse=True):
        ws.delete_rows(idx)

    now_str = datetime.now(timezone.utc).isoformat()
    for row in tip_json_list:
        ws.append_row([
            now_str,
            str(norad_id),
            row.get("MSG_EPOCH",    ""),
            row.get("DECAY_EPOCH",  ""),
            row.get("REV",          ""),
            row.get("DIRECTION",    ""),
            row.get("LAT",          ""),
            row.get("LON",          ""),
            row.get("INCL",         ""),
            row.get("HIGH_INTEREST",""),
            row.get("WINDOW_START", ""),
            row.get("WINDOW_END",   ""),
            row.get("INSERT_EPOCH", ""),
            json.dumps(row),
        ])


def _tip_raw_rows_to_json(raw_rows):
    """Reconstruct the original Space-Track dicts from TIP_RAW sheet rows."""
    result = []
    for r in raw_rows:
        raw_str = r.get("raw_json", "")
        if raw_str:
            try:
                result.append(json.loads(raw_str))
                continue
            except Exception:
                pass
        # Fallback: reconstruct from individual columns
        result.append({
            "MSG_EPOCH"    : r.get("MSG_EPOCH", ""),
            "DECAY_EPOCH"  : r.get("DECAY_EPOCH", ""),
            "REV"          : r.get("REV", ""),
            "DIRECTION"    : r.get("DIRECTION", ""),
            "LAT"          : r.get("LAT", ""),
            "LON"          : r.get("LON", ""),
            "INCL"         : r.get("INCL", ""),
            "HIGH_INTEREST": r.get("HIGH_INTEREST", ""),
            "WINDOW_START" : r.get("WINDOW_START", ""),
            "WINDOW_END"   : r.get("WINDOW_END", ""),
            "INSERT_EPOCH" : r.get("INSERT_EPOCH", ""),
        })
    return result


def _fetch_tip_from_spacetrack(s, norad_id):
    """
    Fetch full TIP history from Space-Track using the same URL pattern as
    reentry_event_predictor.fetch_tip():
        class/tip/NORAD_CAT_ID/{id}/orderby/MSG_EPOCH desc/limit/{N}/format/json
    Returns raw list[dict] or raises.
    """
    url = (
        f"{SPACETRACK_BASE}/basicspacedata/query/class/tip"
        f"/NORAD_CAT_ID/{norad_id}"
        f"/orderby/MSG_EPOCH%20desc"
        f"/limit/{TIP_FETCH_LIMIT}"
        f"/format/json"
    )
    r = s.get(url, timeout=30)
    r.raise_for_status()
    txt = r.text.strip()
    return r.json() if txt.startswith("[") else json.loads(txt)


def _parse_tip_solutions(tip_json):
    """
    Mirror of reentry_event_predictor.parse_tip_solutions():
    parse raw TIP dicts → sorted list of solution dicts, newest MSG_EPOCH first.
    Returns list[dict] (keeps raw dict accessible as "raw" key).
    """
    sols = []
    for row in tip_json:
        msg_epoch = (row.get("MSG_EPOCH") or row.get("MSG_EPOCH ", "") or "").strip()
        sols.append({
            "msg_epoch"    : msg_epoch,
            "decay_epoch"  : (row.get("DECAY_EPOCH") or "").strip(),
            "rev"          : row.get("REV"),
            "direction"    : row.get("DIRECTION"),
            "lat"          : row.get("LAT"),
            "lon"          : row.get("LON"),
            "incl"         : row.get("INCL"),
            "high_interest": row.get("HIGH_INTEREST"),
            "window_start" : row.get("WINDOW_START", ""),
            "window_end"   : row.get("WINDOW_END", ""),
            "insert_epoch" : row.get("INSERT_EPOCH", ""),
            "raw"          : row,
        })

    def _key(sol):
        try:
            return pd.to_datetime(sol["msg_epoch"], utc=True).timestamp()
        except Exception:
            return 0.0

    sols.sort(key=_key, reverse=True)
    return sols


def _select_latest_batch(solutions):
    """Mirror of reentry_event_predictor.select_latest_tip_batch()."""
    if not solutions:
        return []
    newest_msg = solutions[0]["msg_epoch"]
    if not newest_msg:
        return solutions[:1]
    return [s for s in solutions if s["msg_epoch"] == newest_msg]


def _compute_tip_window(latest_batch, fallback_minutes=48):
    """
    Mirror of reentry_event_predictor.compute_tip_window_from_latest_batch().
    Returns (wmin, wmax) as pd.Timestamp UTC, or (None, None).
    """
    decays = []
    for s in latest_batch:
        de = s.get("decay_epoch", "")
        if de:
            try:
                decays.append(pd.to_datetime(de, utc=True))
            except Exception:
                pass
    if not decays:
        return None, None

    wmin = min(decays)
    wmax = max(decays)
    if (wmax - wmin).total_seconds() > 0:
        return wmin, wmax

    # Zero-spread: expand using fallback
    half = pd.Timedelta(minutes=fallback_minutes)
    return wmin - half, wmax + half


# SPACE-TRACK REENTRY FETCH  (only called when TIP_RAW cache is missing/stale)
def _get_or_refresh_tip_raw(sh, norad_id, force=False):
    """
    Check TIP_RAW sheet first.  Call Space-Track only if:
      - no rows exist for this NORAD (first time), OR
      - cache is older than MAX_TIP_CACHE_HOURS, OR
      - force=True (manual refresh).
    Returns (tip_json_list, from_cache, age_hours, error).
    """
    cached_rows, age_hours = _read_tip_raw_cache(sh, norad_id)

    needs_fetch = (
        force
        or not cached_rows
        or age_hours is None
        or age_hours >= MAX_TIP_CACHE_HOURS
    )

    if not needs_fetch:
        return _tip_raw_rows_to_json(cached_rows), True, age_hours, None

    try:
        s = spacetrack_login()
        tip_json = _fetch_tip_from_spacetrack(s, norad_id)
        if not tip_json:
            if cached_rows:
                return _tip_raw_rows_to_json(cached_rows), True, age_hours,                     f"Space-Track returned no TIP records; using cached data."
            return [], False, None, f"No TIP records found for NORAD {norad_id}."
        _write_tip_raw_cache(sh, norad_id, tip_json)
        return tip_json, False, 0.0, None
    except Exception as e:
        if cached_rows:
            return _tip_raw_rows_to_json(cached_rows), True, age_hours,                 f"Space-Track fetch failed: {e}; using cached data."
        return [], False, None, str(e)


def _build_event_and_products_from_tip(norad_id, tip_json, satcat=None, gp=None):
    """
    Derive event dict + products list from the raw TIP JSON list using the
    same parse/select/window logic as reentry_event_predictor.
    satcat / gp: optional dicts from Space-Track SATCAT / GP endpoints.
    """
    solutions    = _parse_tip_solutions(tip_json)
    latest_batch = _select_latest_batch(solutions)
    wmin, wmax   = _compute_tip_window(latest_batch)

    # Latest individual TIP record (most recent MSG_EPOCH)
    tip = latest_batch[0]["raw"] if latest_batch else (solutions[0]["raw"] if solutions else {})

    object_name = (satcat or {}).get("OBJECT_NAME") or tip.get("OBJECT_NAME", "") or f"NORAD {norad_id}"
    inclination = float((gp or {}).get("INCLINATION", 0) or 0)

    # Risk index: based on window width (mirrors EU SST MAJOR ≤4h window)
    risk_index = "UNKNOWN"
    if wmin is not None and wmax is not None:
        window_h   = (wmax - wmin).total_seconds() / 3600
        risk_index = "MAJOR" if window_h <= 4 else "MINOR"

    event = {
        "object_name"          : object_name,
        "intl_des"             : (satcat or {}).get("INTLDES", ""),
        "object_type"          : (satcat or {}).get("OBJECT_TYPE", ""),
        "mass_kg"              : (satcat or {}).get("RCSVALUE", ""),
        "size"                 : (satcat or {}).get("RCS_SIZE", ""),
        "reentry_epoch"        : tip.get("DECAY_EPOCH", ""),
        "window_start"         : wmin.isoformat() if wmin else tip.get("WINDOW_START", ""),
        "window_end"           : wmax.isoformat() if wmax else tip.get("WINDOW_END", ""),
        "epoch_uncertainty_min": tip.get("UNCERTAINTY_WINDOW", ""),
        "max_latitude"         : tip.get("MAX_WINDOWING", ""),
        "altitude_km"          : tip.get("DECAY_ALT", ""),
        "reentry_point_lat"    : latest_batch[0]["lat"] if latest_batch else tip.get("LAT", ""),
        "reentry_point_lon"    : latest_batch[0]["lon"] if latest_batch else tip.get("LON", ""),
        "inclination_deg"      : (gp or {}).get("INCLINATION", tip.get("INCL", "")),
        "direction"            : latest_batch[0]["direction"] if latest_batch else tip.get("DIRECTION", ""),
        "apogee"               : (gp or {}).get("APOAPSIS", ""),
        "perigee"              : (gp or {}).get("PERIAPSIS", ""),
        "controlled_reentry"   : "No",
        "confirmed_decay"      : "Yes" if tip.get("DECAY_EPOCH") else "No",
        "risk_index"           : risk_index,
        "comments"             : tip.get("COMMENT", ""),
        "areas_of_interest"    : _estimate_aoi(inclination),
    }

    # Products: one per unique MSG_EPOCH batch, sorted oldest → newest (for chart)
    seen_msg = {}
    for sol in reversed(solutions):   # oldest first
        me = sol["msg_epoch"]
        if me not in seen_msg:
            seen_msg[me] = sol

    products = []
    for i, (me, sol) in enumerate(seen_msg.items()):
        # Compute window for this batch
        batch = [s for s in solutions if s["msg_epoch"] == me]
        bwmin, bwmax = _compute_tip_window(batch)
        if bwmin is None or bwmax is None:
            # Use window from the individual record
            bwmin_raw = sol.get("window_start") or sol.get("decay_epoch")
            try:
                bwmin = pd.to_datetime(bwmin_raw, utc=True)
                bwmax = bwmin + pd.Timedelta(minutes=120)
            except Exception:
                continue

        # publish_date = MSG_EPOCH (when Space-Track issued this prediction)
        try:
            pub_dt  = pd.to_datetime(me, utc=True)
            pub_str = pub_dt.isoformat()
        except Exception:
            pub_str = me

        products.append({
            "product_id"   : f"{norad_id}-P{str(i+1).zfill(3)}",
            "window_start" : bwmin.isoformat(),
            "window_end"   : bwmax.isoformat(),
            "creation_date": pub_str,
            "publish_date" : pub_str,
            "type"         : (satcat or {}).get("OBJECT_TYPE", "Rocket Body"),
            "risk_index"   : ("MAJOR" if (bwmax - bwmin).total_seconds() / 3600 <= 4
                              else "MINOR"),
        })

    return event, products


def _fetch_reentry_from_spacetrack(norad_id, sh=None):
    """
    Fetch GP metadata from Space-Track for this NORAD, and SATCAT metadata
    (object name/type) from the shared, locally-cached SATCAT_FULL sheet.
    SATCAT is never queried live by individual NORAD ID here — that's the
    exact pattern Space-Track's polling policy prohibits. The daily sync in
    dashboard.py is the only place that talks to SATCAT.
    Returns (satcat_dict, gp_dict, error).
    """
    satcat = {}
    try:
        name_map = _load_satcat_name_map()
        satcat_name = name_map.get(int(norad_id))
        if satcat_name:
            satcat = {"OBJECT_NAME": satcat_name}
    except Exception:
        pass

    try:
        s = spacetrack_login()

        gp_data = s.get(
            f"{SPACETRACK_BASE}/basicspacedata/query/class/gp"
            f"/NORAD_CAT_ID/{norad_id}/format/json/metadata/false",
            timeout=15,
        ).json()
        gp = gp_data[0] if gp_data else {}

        return satcat, gp, None
    except Exception as e:
        return satcat, {}, str(e)


def _estimate_aoi(inclination_deg):
    """Simplified areas-of-interest from orbital inclination."""
    aoi = []
    inc = abs(float(inclination_deg))
    if inc >= 10:  aoi.append("AFI")
    if inc >= 15:  aoi.append("ASIA")
    if inc >= 10:  aoi.append("PAC")
    if inc >= 25:  aoi.append("MID")
    if inc >= 30:  aoi.append("NAM")
    if inc >= 30:  aoi.append("CAR")
    if inc >= 45:  aoi.append("EUR")
    return aoi


# ORCHESTRATOR — sheet-cached reentry analysis
def get_reentry_analysis(norad_id, force_refresh=False, tip_rows_df=None):
    """
    Full cache-first pipeline:

    1. Check TIP_RAW sheet — fetch from Space-Track only if NORAD is new,
       cache > MAX_TIP_CACHE_HOURS old, or force_refresh=True.
    2. Check REENTRY_ANALYSIS sheet — rebuild event+products from TIP_RAW
       only if analysis cache is missing or stale.
    3. Only call Space-Track SATCAT+GP when a full analysis rebuild is needed
       (those change very rarely; TIP is the fast-moving data).

    tip_rows_df: kept for signature compatibility; no longer the primary source.
    Returns (event, products, tip_age_hours, from_cache, error).
    """
    client = get_gspread_client()
    sh     = client.open_by_key(SHEET_ID)

    tip_json, tip_from_cache, tip_age, tip_err = _get_or_refresh_tip_raw(
        sh, norad_id, force=force_refresh
    )

    if not tip_json:
        # Absolute last resort: build from Data sheet rows if available
        if tip_rows_df is not None and not tip_rows_df.empty:
            rows_for_norad = tip_rows_df[
                tip_rows_df["NORAD"].astype(str) == str(norad_id)
            ]
            if not rows_for_norad.empty:
                # Synthesise minimal tip_json from sheet columns
                tip_json = []
                for _, r in rows_for_norad.iterrows():
                    ws_raw = str(r.get("Window Start", ""))
                    tip_json.append({
                        "MSG_EPOCH"   : str(r.get("Hit UTC", "")),
                        "DECAY_EPOCH" : ws_raw,
                        "WINDOW_START": ws_raw,
                        "WINDOW_END"  : str(r.get("Window End", ws_raw)),
                        "LAT"         : "",
                        "LON"         : "",
                        "INCL"        : "",
                        "DIRECTION"   : "",
                        "REV"         : "",
                        "HIGH_INTEREST": str(r.get("Severity", "")),
                        "INSERT_EPOCH": str(r.get("Hit UTC", "")),
                    })
        if not tip_json:
            return None, [], None, False, tip_err or f"No TIP data for NORAD {norad_id}."

    cached_event, cached_products, analysis_age = _read_analysis_cache(sh, norad_id)

    needs_analysis_rebuild = (
        force_refresh
        or cached_event is None
        or analysis_age is None
        or analysis_age >= MAX_ANALYSIS_CACHE_HOURS
        or not tip_from_cache   # TIP was freshly fetched → rebuild analysis too
    )

    if not needs_analysis_rebuild:
        return cached_event, cached_products, tip_age, True, tip_err

    satcat, gp, meta_err = _fetch_reentry_from_spacetrack(norad_id)
    warn = tip_err or meta_err or None

    # Build event + products from the (now-fresh) TIP data
    event, products = _build_event_and_products_from_tip(norad_id, tip_json, satcat, gp)

    _write_analysis_cache(sh, norad_id, event, products)
    _write_history(sh, norad_id, event)

    return event, products, tip_age or 0.0, False, warn


# HISTORY READER
def load_analysis_history(norad_id=None):
    """
    Load full REENTRY_HISTORY sheet. If norad_id is given, filter to that object.
    Returns a DataFrame.
    """
    try:
        client = get_gspread_client()
        sh     = client.open_by_key(SHEET_ID)
        ws     = _get_history_ws(sh)
        rows   = ws.get_all_records()
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df["analyzed_at_utc"] = pd.to_datetime(df["analyzed_at_utc"], errors="coerce", utc=True)
        if norad_id:
            df = df[df["norad_cat_id"].astype(str) == str(norad_id)]
        return df.sort_values("analyzed_at_utc", ascending=False)
    except Exception:
        return pd.DataFrame()


# EXISTING HELPERS (unchanged)
def build_latest_summary(df: pd.DataFrame) -> pd.DataFrame:
    records = []
    for norad, grp in df.groupby("NORAD"):
        # Sort by sheet_row (original insertion order) descending — highest row = most recent TIP.
        # sheet_row is assigned once in load_data() from the raw sheet index, so it's stable.
        # Cast to int first; gspread sometimes returns strings from cached reads.
        grp = grp.copy()
        grp["sheet_row"] = pd.to_numeric(grp["sheet_row"], errors="coerce").fillna(0).astype(int)
        # Sort by Hit UTC descending — the row with the LATEST Hit UTC is the
        # most current prediction regardless of sheet insertion order.
        # (Space-Track sometimes appends older-epoch TIPs after newer ones.)
        grp_sorted = grp.sort_values("Hit UTC", ascending=False)
        latest     = grp_sorted.iloc[0]

        records.append({
            "NORAD"           : norad,
            "latest_sheet_row": int(latest["sheet_row"]),
            # Severity from the row with the LATEST Hit UTC only
            "severity"        : latest["Severity"],
            "hit_utc"         : latest["Hit UTC"],
            "hit_ph"          : latest.get("Hit PH", ""),
            "min_distance"    : grp["Distance"].min(),
            "total_rows"      : len(grp),
            "has_major"       : (grp["Severity"] == "MAJOR").any(),
            # True only when the current-latest is NOT MAJOR but an earlier row was
            "ever_major"      : (grp["Severity"] == "MAJOR").any() and latest["Severity"] != "MAJOR",
        })

    summary = pd.DataFrame(records)
    # Sort: current MAJOR first, then by most recent sheet_row
    summary["_sev_rank"] = summary["severity"].map({"MAJOR": 0, "MINOR": 1}).fillna(2)
    summary = summary.sort_values(["_sev_rank", "latest_sheet_row"],
                                  ascending=[True, False]).drop(columns="_sev_rank")
    return summary


def render_timeline_chart(df: pd.DataFrame):
    ts = df.set_index("Hit UTC").resample("6h").size().reset_index()
    ts.columns = ["time", "count"]
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=ts["time"], y=ts["count"],
        mode="lines+markers",
        line=dict(color="#2196f3", width=2),
        marker=dict(size=5, color="#2196f3"),
        fill="tozeroy", fillcolor="rgba(33,150,243,0.15)",
    ))
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=40, r=10, t=10, b=40), height=260,
        xaxis=dict(tickformat="%b %d\n%H:%M", gridcolor="rgba(128,128,128,0.2)",
                   linecolor="rgba(128,128,128,0.3)", tickfont=dict(color="rgba(128,128,128,0.8)")),
        yaxis=dict(title=dict(text="Events", font=dict(color="rgba(128,128,128,0.8)")),
                   gridcolor="rgba(128,128,128,0.2)", linecolor="rgba(128,128,128,0.3)",
                   tickfont=dict(color="rgba(128,128,128,0.8)")),
        showlegend=False,
    )
    st.plotly_chart(fig, use_container_width=True)


# ANALYZER UI COMPONENTS
def _fmt(val, suffix="", fallback="N/A"):
    if val is None or val == "" or (isinstance(val, float) and pd.isna(val)):
        return fallback
    return f"{val}{suffix}"


def render_detail_cell(label, value):
    return (f'<div class="detail-cell">'
            f'<div class="dc-label">{label}</div>'
            f'<div class="dc-value">{_fmt(value)}</div>'
            f'</div>')


def render_window_evolution_chart(products: list):
    """
    Reproduce the EU SST Window Evolution chart:
    X-axis = publish date of each prediction product
    Y-axis = reentry window midpoint
    Shaded area = window_start to window_end (uncertainty cone)
    """
    if not products:
        st.info("No prediction products available for this event.")
        return

    rows = []
    for p in products:
        try:
            ws_dt  = pd.to_datetime(p.get("window_start"), utc=True)
            we_dt  = pd.to_datetime(p.get("window_end"),   utc=True)
            pub_dt = pd.to_datetime(p.get("publish_date"), utc=True)
            if pd.isna(ws_dt) or pd.isna(we_dt) or pd.isna(pub_dt):
                continue
            rows.append({
                "pub"  : pub_dt,
                "ws"   : ws_dt,
                "we"   : we_dt,
                "mid"  : ws_dt + (we_dt - ws_dt) / 2,
            })
        except Exception:
            continue

    if not rows:
        st.info("Insufficient data to draw window evolution chart.")
        return

    rows.sort(key=lambda r: r["pub"])
    pubs = [r["pub"] for r in rows]
    ws_s = [r["ws"]  for r in rows]
    we_s = [r["we"]  for r in rows]
    mids = [r["mid"] for r in rows]

    fig = go.Figure()

    # Shaded uncertainty band
    fig.add_trace(go.Scatter(
        x=pubs + pubs[::-1],
        y=ws_s + we_s[::-1],
        fill="toself",
        fillcolor="rgba(33,150,243,0.15)",
        line=dict(color="rgba(33,150,243,0)"),
        hoverinfo="skip",
        showlegend=False,
    ))
    # Window start boundary
    fig.add_trace(go.Scatter(
        x=pubs, y=ws_s, mode="lines+markers",
        line=dict(color="#2196f3", width=1.5, dash="dot"),
        marker=dict(size=6, color="#2196f3", symbol="circle-open"),
        name="Window Start",
    ))
    # Window end boundary
    fig.add_trace(go.Scatter(
        x=pubs, y=we_s, mode="lines+markers",
        line=dict(color="#2196f3", width=1.5, dash="dot"),
        marker=dict(size=6, color="#2196f3", symbol="circle-open"),
        name="Window End",
        showlegend=False,
    ))
    # Midpoint (best estimate)
    fig.add_trace(go.Scatter(
        x=pubs, y=mids, mode="lines+markers",
        line=dict(color="#64b5f6", width=2),
        marker=dict(size=8, color="#64b5f6"),
        name="Best Estimate",
    ))

    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=50, r=20, t=20, b=50),
        height=340,
        legend=dict(
            orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1,
            font=dict(color="rgba(200,200,200,0.8)", size=11),
        ),
        xaxis=dict(
            title="Prediction Publish Date",
            tickformat="%b %d\n%H:%M",
            gridcolor="rgba(128,128,128,0.15)",
            linecolor="rgba(128,128,128,0.3)",
            tickfont=dict(color="rgba(128,128,128,0.8)"),
            title_font=dict(color="rgba(128,128,128,0.7)", size=11),
        ),
        yaxis=dict(
            title="Predicted Re-entry Time (UTC)",
            tickformat="%b %d\n%H:%M",
            gridcolor="rgba(128,128,128,0.15)",
            linecolor="rgba(128,128,128,0.3)",
            tickfont=dict(color="rgba(128,128,128,0.8)"),
            title_font=dict(color="rgba(128,128,128,0.7)", size=11),
        ),
    )
    st.plotly_chart(fig, use_container_width=True)


def render_ground_track_map(event: dict):
    """
    Simplified ground track approximation using reentry point + inclination.
    Draws the last ~one orbit as a great-circle arc on a Plotly geo map,
    plus a ±100 km swath band and the reentry point marker.
    """
    try:
        lat0 = float(event.get("reentry_point_lat") or 0)
        lon0 = float(event.get("reentry_point_lon") or 0)
        inc  = float(event.get("inclination_deg")   or 51.6)
    except Exception:
        lat0, lon0, inc = 0.0, 0.0, 51.6

    import math
    # Generate approximate ground track: parameterise by mean anomaly
    n_pts   = 360
    lats, lons = [], []
    for i in range(n_pts + 1):
        angle = math.radians(i - n_pts // 2)  # orbit arc centred on reentry point
        lat   = math.degrees(math.asin(math.sin(math.radians(inc)) * math.sin(angle)))
        lon   = lon0 + math.degrees(math.atan2(
            math.cos(math.radians(inc)) * math.sin(angle),
            math.cos(angle),
        ))
        lats.append(lat)
        lons.append((lon + 180) % 360 - 180)

    # Swath: offset lats by ~0.9° (≈100 km)
    swath_offset = 0.9
    lats_up   = [min(90,  l + swath_offset) for l in lats]
    lats_down = [max(-90, l - swath_offset) for l in lats]

    fig = go.Figure()

    # Swath band
    fig.add_trace(go.Scattergeo(
        lon=lons + lons[::-1],
        lat=lats_up + lats_down[::-1],
        fill="toself", fillcolor="rgba(33,150,243,0.10)",
        line=dict(color="rgba(0,0,0,0)"),
        hoverinfo="skip", showlegend=False,
    ))

    # Ground track
    fig.add_trace(go.Scattergeo(
        lon=lons, lat=lats,
        mode="lines",
        line=dict(color="#00bcd4", width=2),
        name="Ground Track",
    ))

    # Reentry point
    fig.add_trace(go.Scattergeo(
        lon=[lon0], lat=[lat0],
        mode="markers",
        marker=dict(size=10, color="#f44336", symbol="circle"),
        name="Re-entry Point",
        hovertext=f"Re-entry: {lat0:.2f}°N, {lon0:.2f}°E",
    ))

    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=0, r=0, t=0, b=0),
        height=620,
        legend=dict(
            orientation="h", x=0.01, y=0.01,
            font=dict(color="rgba(200,200,200,0.8)", size=11),
            bgcolor="rgba(0,0,0,0.3)",
        ),
        geo=dict(
            showland=True, landcolor="#2d5a27",
            showocean=True, oceancolor="#1a3a5c",
            showcountries=True, countrycolor="rgba(255,255,255,0.2)",
            showcoastlines=True, coastlinecolor="rgba(255,255,255,0.35)",
            showframe=False,
            projection_type="natural earth",
            bgcolor="rgba(0,0,0,0)",
        ),
    )
    st.plotly_chart(fig, use_container_width=True)


def render_products_table(products: list):
    if not products:
        st.info("No prediction products cached for this event.")
        return

    for p in products:
        risk  = p.get("risk_index", "")
        badge = (f'<span class="risk-major">{risk}</span>' if risk == "MAJOR"
                 else f'<span class="risk-minor">{risk}</span>')
        ws_str = _fmt(p.get("window_start"))
        we_str = _fmt(p.get("window_end"))
        pub_str = _fmt(p.get("publish_date"))
        pid     = p.get("product_id", "—")
        ptype   = p.get("type", "—")
        st.markdown(
            f'<div class="product-row">'
            f'  {badge}'
            f'  <span class="pr-id">{pid}</span>'
            f'  <span class="pr-range">{ws_str} to {we_str}</span>'
            f'  <span style="opacity:0.6;font-size:0.78rem;">{ptype}</span>'
            f'  <span class="pr-pub">Published: {pub_str}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )


def render_history_panel(norad_id, display_norad=None):
    hist = load_analysis_history(norad_id)   # always queries with the REAL norad_id
    if hist.empty:
        st.info("No analysis history found for this object.")
        return

    disp_norad = display_norad if display_norad is not None else _demo_display(norad_id)[0]
    st.markdown(
        f"<p style='color:var(--text-color);opacity:0.6;font-size:0.8rem;margin-bottom:10px;'>"
        f"{len(hist)} analysis run(s) on record for NORAD {disp_norad}</p>",
        unsafe_allow_html=True,
    )
    for _, row in hist.iterrows():
        ts  = row["analyzed_at_utc"].strftime("%Y-%m-%d %H:%M UTC") if pd.notna(row.get("analyzed_at_utc")) else "—"
        ri  = row.get("risk_index", "—")
        ri_color = "#f44336" if ri == "MAJOR" else "#2ecc71"
        st.markdown(
            f'<div class="history-row">'
            f'  <span style="font-weight:700;">{ts}</span> &nbsp;|&nbsp; '
            f'  <span style="color:{ri_color};font-weight:700;">{ri}</span> &nbsp;|&nbsp; '
            f'  Epoch: {_fmt(row.get("reentry_epoch"))} &nbsp;|&nbsp; '
            f'  Window: {_fmt(row.get("window_start"))} → {_fmt(row.get("window_end"))} &nbsp;|&nbsp; '
            f'  Decay confirmed: {_fmt(row.get("confirmed_decay"))} &nbsp;|&nbsp; '
            f'  By: {_fmt(row.get("user"))}'
            f'</div>',
            unsafe_allow_html=True,
        )


# NORAD CARD (updated — now includes Analyze button)
def render_norad_card(row: pd.Series, all_rows: pd.DataFrame, acknowledged: set, tab_key: str = "all"):
    norad     = row["NORAD"]
    name      = get_object_name(norad)
    disp_norad, disp_name = _demo_display(norad, name)   # display-only, real `norad`/`name` unchanged below
    severity  = row["severity"]
    sev_class = "major" if severity == "MAJOR" else "minor"
    is_ack    = str(norad) in acknowledged

    hit_utc_str = (row["hit_utc"].strftime("%Y-%m-%d %H:%M")
                   if pd.notna(row["hit_utc"]) else "N/A")
    hit_ph_str  = str(row["hit_ph"]) if row["hit_ph"] else "N/A"
    min_dist    = row["min_distance"]
    dist_str    = f"{min_dist:.1f} km" if pd.notna(min_dist) else "N/A"

    # Show a subdued historical note only when the current severity has
    # downgraded from MAJOR — uses ever_major which is only True when
    # latest row is NOT MAJOR but an earlier row in the window was.
    any_major_badge = ""
    if row.get("ever_major", False):
        any_major_badge = ("<span style='color:#ff9800;font-size:0.72rem;"
                           "font-weight:500;margin-left:6px;opacity:0.8;'>"
                           "↓ was MAJOR</span>")

    st.markdown(f"""
    <div class="re-card">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px;">
        <div>
          <div style="font-size:1.25rem;font-weight:800;color:var(--text-color);line-height:1.2;">{disp_name}</div>
          <div style="font-size:1.0rem;font-weight:700;color:var(--text-color);opacity:0.7;margin-top:2px;">
            NORAD {disp_norad}
            <span class="re-row-badge">row {row['latest_sheet_row']}</span>
          </div>
        </div>
        <span class="{sev_class}" style="font-size:0.85rem;">{severity}{any_major_badge}</span>
      </div>
      <b>Latest TIP — Hit UTC:</b> <span class="val">{hit_utc_str}</span><br>
      <b>Latest TIP — Hit PH:</b> <span class="val">{hit_ph_str}</span><br>
      <b>Closest Distance to PH:</b> <span class="val">{dist_str}</span><br>
      <b>Total TIP rows:</b> <span class="val">{row['total_rows']}</span>
    </div>
    """, unsafe_allow_html=True)

    if is_ack:
        st.markdown('<div class="re-ack-badge">✓ Acknowledged</div>', unsafe_allow_html=True)

    btn_col1, btn_col2, _ = st.columns([1, 1, 5])
    with btn_col1:
        if not is_ack:
            if st.button("Acknowledge", key=f"ack_{norad}_{tab_key}", use_container_width=True):
                acknowledge(norad)
                st.rerun()
    with btn_col2:
        if st.button("Analyze", key=f"analyze_{norad}_{tab_key}", use_container_width=True):
            st.session_state["analyzing_norad"] = norad
            st.session_state["analyzing_name"]  = name
            st.rerun()

    with st.expander(f"All TIP rows for NORAD {disp_norad} ({row['total_rows']} entries)", key=f"exp_{norad}_{tab_key}"):
        detail = (all_rows[all_rows["NORAD"] == norad]
                  .sort_values("sheet_row", ascending=False)
                  .drop(columns=["sheet_row"], errors="ignore"))
        st.dataframe(detail, use_container_width=True, hide_index=True)


# ANALYZER PANEL — full EU SST-style view for a single NORAD
def render_analyzer_panel(norad_id, object_name):
    """Full reentry analysis view for one NORAD ID."""

    back_col, _ = st.columns([1, 8])
    with back_col:
        if st.button("← Back"):
            del st.session_state["analyzing_norad"]
            st.rerun()

    # Pull the locally-loaded TIP rows so we can build products from the Data
    # sheet without hitting Space-Track again (crucial for already-decayed objects
    # where the TIP endpoint returns nothing).
    tip_df = st.session_state.get("_tip_df_week", None)

    event, products, age_hours, from_cache, err = get_reentry_analysis(
        norad_id,
        force_refresh=st.session_state.pop("force_refresh_analysis", False),
        tip_rows_df=tip_df,
    )

    if err and not event:
        st.error(f"Could not load reentry analysis: {err}")
        return
    if err:
        st.warning(err)

    risk        = event.get("risk_index", "—")
    risk_badge  = (f'<span class="risk-major">{risk}</span>' if risk == "MAJOR"
                   else f'<span class="risk-minor">{risk}</span>')
    cache_label = (f"cached · {age_hours:.1f}h old" if from_cache else "live from Space-Track")
    confirmed   = event.get("confirmed_decay", "—")
    intl_des    = event.get("intl_des", "")
    disp_norad, disp_name = _demo_display(norad_id, object_name)   # display-only, real norad_id used above for the lookup
    norad_label = f"NORAD {disp_norad}"
    if intl_des:
        norad_label += f" · {intl_des}"

    st.markdown(f"""
    <div class="analyzer-header">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;">
        <div>
          <div class="event-title">{disp_name}</div>
          <div class="event-sub">{norad_label} &nbsp;|&nbsp;
            Type: {_fmt(event.get("object_type"))} &nbsp;|&nbsp;
            Mass: {_fmt(event.get("mass_kg"), " kg")}
          </div>
        </div>
        <div style="text-align:right;">
          {risk_badge}
          <div style="font-size:0.75rem;opacity:0.5;margin-top:6px;">{cache_label}</div>
        </div>
      </div>
      <div style="margin-top:12px;">
        <span class="epoch-pill">R. Epoch: {_fmt(event.get("reentry_epoch"))}</span>
        <span class="epoch-pill">Window Start: {_fmt(event.get("window_start"))}</span>
        <span class="epoch-pill">Window End: {_fmt(event.get("window_end"))}</span>
        <span class="epoch-pill">Confirmed Decay: {confirmed}</span>
      </div>
    </div>
    """, unsafe_allow_html=True)

    # Refresh button
    ref_col, _ = st.columns([2, 7])
    with ref_col:
        if st.button("Refresh from Space-Track", key="refresh_analysis"):
            st.session_state["force_refresh_analysis"] = True
            st.rerun()

    tab1, tab2, tab3, tab4 = st.tabs([
        "Product Viewer",
        "Window Evolution",
        "Products",
        "Analysis History",
    ])

    with tab1:
        st.markdown('<div class="tab-section">', unsafe_allow_html=True)

        info_col, map_col = st.columns([1, 3])

        with info_col:
            st.markdown("**Re-entry Prediction**")
            with st.container(border=True):
                prediction_metrics = [
                    ("Re-entry Epoch",      _fmt(event.get("reentry_epoch"))),
                    ("Window Start",        _fmt(event.get("window_start"))),
                    ("Window End",          _fmt(event.get("window_end"))),
                    ("Epoch Uncertainty",   _fmt(event.get("epoch_uncertainty_min"), " min")),
                    ("Max Latitude",        _fmt(event.get("max_latitude"), "°")),
                    ("Altitude",            _fmt(event.get("altitude_km"), " km")),
                    ("Re-entry Pt Lat",     _fmt(event.get("reentry_point_lat"), "°")),
                    ("Re-entry Pt Lon",     _fmt(event.get("reentry_point_lon"), "°")),
                    ("Inclination",         _fmt(event.get("inclination_deg"), "°")),
                    ("Direction",           _fmt(event.get("direction"))),
                    ("Apogee",              _fmt(event.get("apogee"), " km")),
                    ("Perigee",             _fmt(event.get("perigee"), " km")),
                    ("Controlled R/E",      _fmt(event.get("controlled_reentry"))),
                    ("Confirmed Decay",     _fmt(event.get("confirmed_decay"))),
                ]

                p_col1, p_col2 = st.columns(2)
                for idx, (label, value) in enumerate(prediction_metrics):
                    with (p_col1 if idx % 2 == 0 else p_col2):
                        with st.container(height=80, border=False):
                            st.markdown(f":blue-badge[{label}]")
                            st.markdown(
                                f'<div style="font-size:0.92rem;font-weight:700;'
                                f'margin-top:4px;line-height:1.2;">{value}</div>',
                                unsafe_allow_html=True,
                            )

            aoi_list = event.get("areas_of_interest", [])
            if aoi_list:
                st.markdown("**Areas of Interest**")
                aoi_html = " ".join(
                    f'<span class="epoch-pill">{a}</span>' for a in aoi_list
                )
                st.markdown(aoi_html, unsafe_allow_html=True)

            if event.get("comments"):
                st.markdown("**Comments**")
                st.caption(event["comments"])

        with map_col:
            st.markdown("**Ground Track**")
            render_ground_track_map(event)

        st.markdown('</div>', unsafe_allow_html=True)

    with tab2:
        st.markdown('<div class="tab-section">', unsafe_allow_html=True)
        st.markdown(
            "<p style='color:var(--text-color);opacity:0.65;font-size:0.82rem;'>"
            "Each point = one prediction product. The shaded band is the uncertainty "
            "window (start → end). As more data arrives the cone narrows.</p>",
            unsafe_allow_html=True,
        )
        render_window_evolution_chart(products)
        st.markdown('</div>', unsafe_allow_html=True)

    with tab3:
        st.markdown('<div class="tab-section">', unsafe_allow_html=True)
        n_prod = len(products)
        st.markdown(
            f"<p style='color:var(--text-color);opacity:0.65;font-size:0.82rem;margin-bottom:10px;'>"
            f"{n_prod} prediction product(s) on record for NORAD {disp_norad}.</p>",
            unsafe_allow_html=True,
        )
        render_products_table(products)
        st.markdown('</div>', unsafe_allow_html=True)

    with tab4:
        st.markdown('<div class="tab-section">', unsafe_allow_html=True)
        render_history_panel(norad_id, display_norad=disp_norad)
        st.markdown('</div>', unsafe_allow_html=True)


# MAIN RENDER
def render():
    inject_css()

    # Always load the Data sheet first so _tip_df_week is available whether
    # we're showing the list view or the analyzer panel.
    df = load_data()
    if df.empty:
        st.warning("No data available.")
        return

    valid = df.dropna(subset=["Hit UTC"])
    if valid.empty:
        st.warning("No valid Hit UTC values found.")
        return

    cutoff  = valid["Hit UTC"].max() - pd.Timedelta(days=7)
    df_week = valid[valid["Hit UTC"] >= cutoff].copy()

    # Keep df_week in session state so render_analyzer_panel can use it for
    # building products from local TIP rows (no extra Space-Track call needed).
    st.session_state["_tip_df_week"] = df_week

    # If an analysis is open, show the full analyzer panel
    if "analyzing_norad" in st.session_state:
        render_analyzer_panel(
            st.session_state["analyzing_norad"],
            st.session_state.get("analyzing_name", "Unknown Object"),
        )
        return

    n_total  = len(df_week)
    n_major  = int((df_week["Severity"] == "MAJOR").sum())
    n_minor  = int((df_week["Severity"] == "MINOR").sum())
    n_norads = df_week["NORAD"].nunique()

    st.markdown(f"""
    <div class="re-metrics">
        <div class="re-metric"><div class="m-lbl">Objects</div><div class="m-val">{n_norads}</div></div>
        <div class="re-metric"><div class="m-lbl">Events Monitored</div><div class="m-val">{n_total}</div></div>
        <div class="re-metric"><div class="m-lbl">MAJOR Events</div><div class="m-val" style="color:#f44336;">{n_major}</div></div>
        <div class="re-metric"><div class="m-lbl">MINOR Events</div><div class="m-val" style="color:#2ecc71;">{n_minor}</div></div>
    </div>""", unsafe_allow_html=True)

    st.markdown(
        "<div style='font-weight:700;color:var(--text-color);font-size:0.95rem;"
        "margin-bottom:8px;'>Events Over Time (6h buckets)</div>",
        unsafe_allow_html=True)
    render_timeline_chart(df_week)

    st.markdown(
        "<div style='font-weight:700;color:var(--text-color);font-size:0.95rem;"
        "margin:16px 0 4px;'>Latest TIP per Object</div>"
        "<p style='color:var(--text-color);opacity:0.7;font-size:0.8rem;margin-bottom:12px;'>"
        "Card shows the most recent TIP update per NORAD ID. Press Analyze for the full breakdown.</p>",
        unsafe_allow_html=True)

    # ---- SATCAT catch-up sync ----
    # Only appears when today's 17:20 UTC scheduled sync was missed (e.g. no
    # one had the app open at that minute). Hidden before 17:20 UTC, and
    # hides itself again the moment a sync — scheduled or catch-up — lands
    # for today, so it can only be used once per day.
    if satcat_sync_missed_today():
        with st.expander("🛰️ SATCAT sync missed today's 17:20 UTC window", expanded=True):
            st.caption(
                "No one had the app open at 17:20 UTC, so today's scheduled "
                "SATCAT sync didn't run. You can run today's catch-up sync once below."
            )
            if st.button("Run Today's Catch-Up Sync", key="satcat_catchup_sync_btn"):
                with st.spinner("Running catch-up SATCAT delta sync…"):
                    _ok, _msg = catch_up_satcat_sync()
                if _ok:
                    _load_satcat_name_map.clear()  # so refreshed names show immediately
                    st.success(_msg)
                else:
                    st.warning(_msg)

    acknowledged = load_acknowledged()
    summary      = build_latest_summary(df_week)

    n_all   = len(summary)
    n_maj   = int((summary["severity"] == "MAJOR").sum())
    n_min   = int((summary["severity"] == "MINOR").sum())

    filter_tab_all, filter_tab_major, filter_tab_minor = st.tabs([
        f"All ({n_all})",
        f"Major ({n_maj})",
        f"Minor ({n_min})",
    ])

    def _render_cards(subset, tab_key):
        cols = st.columns(2)
        for i, (_, row) in enumerate(subset.iterrows()):
            with cols[i % 2]:
                render_norad_card(row, df_week, acknowledged, tab_key=tab_key)

    with filter_tab_all:
        _render_cards(summary, "all")

    with filter_tab_major:
        major_subset = summary[summary["severity"] == "MAJOR"]
        if major_subset.empty:
            st.info("No MAJOR severity objects in the current window.")
        else:
            _render_cards(major_subset, "major")

    with filter_tab_minor:
        minor_subset = summary[summary["severity"] == "MINOR"]
        if minor_subset.empty:
            st.info("No MINOR severity objects in the current window.")
        else:
            _render_cards(minor_subset, "minor")

    st.markdown("""
    <table style="width:100%;border-collapse:collapse;margin-top:24px;font-size:0.82rem;">
      <thead>
        <tr style="background:var(--secondary-background-color);">
          <th style="padding:8px 12px;text-align:left;font-weight:700;width:120px;">Risk Level</th>
          <th style="padding:8px 12px;text-align:left;font-weight:700;">Description</th>
        </tr>
      </thead>
      <tbody>
        <tr style="border-top:1px solid rgba(255,255,255,0.07);">
          <td style="padding:8px 12px;">
            <span style="background:#e53935;color:#fff;font-weight:700;font-size:0.78rem;
              padding:3px 10px;border-radius:4px;display:inline-block;">MAJOR</span>
          </td>
          <td style="padding:8px 12px;color:var(--text-color);">
            The re-entry ground track passes near or over the Philippines.
          </td>
        </tr>
        <tr style="border-top:1px solid rgba(255,255,255,0.07);">
          <td style="padding:8px 12px;">
            <span style="background:#2e7d32;color:#fff;font-weight:700;font-size:0.78rem;
              padding:3px 10px;border-radius:4px;display:inline-block;">MINOR</span>
          </td>
          <td style="padding:8px 12px;color:var(--text-color);">
            The re-entry ground track is far from the Philippines. Low concern for the country.
          </td>
        </tr>
      </tbody>
    </table>
    """, unsafe_allow_html=True)