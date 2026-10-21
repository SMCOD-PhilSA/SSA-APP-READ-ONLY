import os
import json
from datetime import datetime, timezone

import requests
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials


SPACETRACK_BASE = "https://www.space-track.org"

# Same spreadsheet reentry_event_analyzer.py / dashboard.py already read
# the re-entry "Data" tab from -- the SATCAT log lives alongside it so
# the dashboard can join the two without a second spreadsheet lookup.
SHEET_ID         = "1isa_rOueh0yDY9OM43Q2AWZoKPGrlvEo7JOSHJdW3jE"
DATA_SHEET       = "Data"
SATCAT_LOG_SHEET = "SpaceTrack_SATCAT"

SATCAT_LOG_HEADER = [
    "logged_at_utc", "norad_id", "object_name", "object_type",
    "rcs_size", "country_code", "decay_date", "interest_level", "source",
]

HEADERS = {
    "User-Agent": (
        "PhilSA-SSA-Dashboard-SpaceTrack-Relay/1.0 "
        "(+https://philsa.gov.ph; contact: smcod@philsa.gov.ph)"
    )
}


# ============================================================
# GOOGLE SHEETS CLIENT
# ============================================================
def get_client():
    raw_json = os.environ.get("ODR_JSON")
    if not raw_json:
        raise RuntimeError("Missing GitHub secret: ODR_JSON")

    info = json.loads(raw_json)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds)


def get_or_create_worksheet(spreadsheet, title, rows=2000, cols=10):
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)


# ============================================================
# SPACE-TRACK AUTH
# ============================================================
def get_spacetrack_creds():
    """
    Reuses the exact credential names the Streamlit app already falls
    back to (SPACETRACK_USER / SPACETRACK_PASS), supplied here as GitHub
    Actions repository secrets rather than st.secrets["spacetrack"].
    """
    user = os.environ.get("SPACETRACK_USER")
    pw   = os.environ.get("SPACETRACK_PASS")
    if not user or not pw:
        raise RuntimeError("Missing GitHub secrets: SPACETRACK_USER / SPACETRACK_PASS")
    return user, pw


def spacetrack_login():
    user, pw = get_spacetrack_creds()
    s = requests.Session()
    s.headers.update(HEADERS)
    resp = s.post(
        f"{SPACETRACK_BASE}/ajaxauth/login",
        data={"identity": user, "password": pw},
        timeout=30,
    )
    resp.raise_for_status()
    return s


# ============================================================
# STEP 1: SCOPE TO TODAY'S NORAD IDs FROM THE REENTRY SHEET
# ============================================================
def get_todays_norad_ids(sh):
    """
    Distinct NORAD IDs from re-entry TIP rows logged today (UTC) in the
    "Data" worksheet. This is what scopes the Space-Track query to only
    objects with an actual event today, instead of a bulk catalog pull.
    """
    ws = sh.worksheet(DATA_SHEET)
    rows = ws.get_all_records()
    if not rows:
        return []

    df = pd.DataFrame(rows)
    df.columns = [str(c).strip() for c in df.columns]
    if "Hit UTC" not in df.columns or "NORAD" not in df.columns:
        print("WARNING: 'Hit UTC' or 'NORAD' column missing from Data sheet.")
        return []

    df["Hit UTC"] = pd.to_datetime(df["Hit UTC"], errors="coerce", utc=True)
    today = datetime.now(timezone.utc).date()
    todays_rows = df[df["Hit UTC"].dt.date == today]

    return (
        todays_rows["NORAD"]
        .dropna()
        .astype(str)
        .str.strip()
        .unique()
        .tolist()
    )


# ============================================================
# STEP 2: ONE BATCH SATCAT QUERY FOR TODAY'S NORAD IDs
# ============================================================
def fetch_satcat_batch(session, norad_ids):
    """One Space-Track SATCAT query for exactly today's NORAD IDs."""
    if not norad_ids:
        return []

    id_list = ",".join(norad_ids)
    url = (
        f"{SPACETRACK_BASE}/basicspacedata/query/class/satcat"
        f"/NORAD_CAT_ID/{id_list}/orderby/NORAD_CAT_ID/format/json"
    )
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ============================================================
# STEP 3: CLASSIFY HIGH / LOW INTEREST
# ============================================================
def classify_interest(record):
    """
    High interest: OBJECT_TYPE == PAYLOAD, or RCS_SIZE == LARGE.
    Everything else logs as Low interest.

    This is a first-pass heuristic, not a fixed rule -- worth checking
    against real past PH events before treating it as authoritative.
    Adjust here if the threshold needs tuning; nothing downstream needs
    to change since the dashboard reads interest_level as a plain column.
    """
    obj_type = str(record.get("OBJECT_TYPE", "")).strip().upper()
    rcs_size = str(record.get("RCS_SIZE", "")).strip().upper()

    if obj_type == "PAYLOAD" or rcs_size == "LARGE":
        return "HIGH"
    return "LOW"


# ============================================================
# STEP 4: LOG TO GOOGLE SHEETS
# ============================================================
def log_satcat_records(ws, records):
    if not records:
        return 0

    logged_at = datetime.now(timezone.utc).isoformat()
    rows = []
    for rec in records:
        rows.append([
            logged_at,
            rec.get("NORAD_CAT_ID", ""),
            rec.get("OBJECT_NAME", ""),
            rec.get("OBJECT_TYPE", ""),
            rec.get("RCS_SIZE", ""),
            rec.get("COUNTRY", rec.get("COUNTRY_CODE", "")),
            rec.get("DECAY", rec.get("DECAY_DATE", "")),
            classify_interest(rec),
            "space-track/satcat",
        ])

    if not ws.get_all_values():
        ws.append_row(SATCAT_LOG_HEADER)

    ws.append_rows(rows)
    return len(rows)


# ============================================================
# MAIN
# ============================================================
def main():
    client = get_client()
    sh = client.open_by_key(SHEET_ID)

    norad_ids = get_todays_norad_ids(sh)
    print(f"Today's re-entry NORAD IDs: {len(norad_ids)}")

    if not norad_ids:
        print("No re-entry events logged today -- nothing to enrich. Exiting.")
        return

    session = spacetrack_login()
    records = fetch_satcat_batch(session, norad_ids)
    print(f"SATCAT records returned: {len(records)}")

    ws = get_or_create_worksheet(sh, SATCAT_LOG_SHEET)
    n = log_satcat_records(ws, records)
    print(f"Logged {n} SATCAT rows to '{SATCAT_LOG_SHEET}'.")


if __name__ == "__main__":
    main()