import streamlit as st
import pandas as pd

from services.reentry import reentry_event_analyzer as ra

SATCAT_LOG_SHEET = "SpaceTrack_SATCAT"


@st.cache_data(ttl=600)
def fetch_satcat_interest_map():
    """
    Returns dict[str norad_id -> dict(object_type, rcs_size, country_code,
    decay_date, interest_level)] from the most recent logged row per
    NORAD ID in the SpaceTrack_SATCAT worksheet.
    """
    try:
        client = ra.get_gspread_client()
        ws     = client.open_by_key(ra.SHEET_ID).worksheet(SATCAT_LOG_SHEET)
        rows   = ws.get_all_records()
    except Exception:
        return {}

    if not rows:
        return {}

    df = pd.DataFrame(rows)
    df.columns = [str(c).strip() for c in df.columns]
    if "norad_id" not in df.columns:
        return {}

    df["norad_id"] = df["norad_id"].astype(str).str.strip()
    if "logged_at_utc" in df.columns:
        df = df.sort_values("logged_at_utc")
    df = df.drop_duplicates(subset="norad_id", keep="last")   # latest logged row per object

    result = {}
    for _, row in df.iterrows():
        result[row["norad_id"]] = {
            "object_type"   : row.get("object_type", ""),
            "rcs_size"      : row.get("rcs_size", ""),
            "country_code"  : row.get("country_code", ""),
            "decay_date"    : row.get("decay_date", ""),
            "interest_level": row.get("interest_level", "LOW"),
        }
    return result