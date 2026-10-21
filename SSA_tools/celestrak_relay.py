import os
import json
from io import StringIO
from datetime import datetime, timezone

import requests
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials


SATCAT_URL = "https://celestrak.org/pub/satcat.csv"
GP_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP=active&FORMAT=tle"

SATCAT_SHEET = "SATCAT"
GP_TLE_SHEET = "GP_TLE"
METADATA_SHEET = "metadata"


HEADERS = {
    "User-Agent": "SSA-Dashboard-CelesTrak-Relay/1.0"
}


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


def get_or_create_worksheet(spreadsheet, title, rows=1000, cols=30):
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(
            title=title,
            rows=rows,
            cols=cols,
        )


def update_dataframe(ws, df):
    df = df.fillna("")
    values = [df.columns.tolist()] + df.astype(str).values.tolist()

    ws.clear()
    ws.update(values)


def fetch_satcat():
    print("Fetching SATCAT...")

    response = requests.get(
        SATCAT_URL,
        headers=HEADERS,
        timeout=60,
    )
    response.raise_for_status()

    df = pd.read_csv(StringIO(response.text))
    df.columns = [str(c).strip() for c in df.columns]

    print(f"SATCAT rows: {len(df)}")
    return df


def fetch_gp_tle():
    print("Fetching active GP TLE...")

    response = requests.get(
        GP_URL,
        headers=HEADERS,
        timeout=60,
    )
    response.raise_for_status()

    lines = [line.strip() for line in response.text.splitlines() if line.strip()]
    records = []

    i = 0
    while i < len(lines) - 2:
        name = lines[i]
        line1 = lines[i + 1]
        line2 = lines[i + 2]

        if line1.startswith("1 ") and line2.startswith("2 "):
            records.append({
                "OBJECT_NAME": name,
                "NORAD_CAT_ID": line1[2:7].strip(),
                "TLE_LINE1": line1,
                "TLE_LINE2": line2,
                "EPOCH": line1[18:32].strip(),
            })
            i += 3
        else:
            i += 1

    df = pd.DataFrame(
        records,
        columns=[
            "OBJECT_NAME",
            "NORAD_CAT_ID",
            "TLE_LINE1",
            "TLE_LINE2",
            "EPOCH",
        ],
    )

    print(f"GP_TLE rows: {len(df)}")
    return df


def update_metadata(spreadsheet, satcat_count, gp_count):
    ws = get_or_create_worksheet(
        spreadsheet,
        METADATA_SHEET,
        rows=20,
        cols=5,
    )

    values = [
        ["key", "value"],
        ["last_updated_utc", datetime.now(timezone.utc).isoformat()],
        ["satcat_source", SATCAT_URL],
        ["gp_tle_source", GP_URL],
        ["satcat_rows", str(satcat_count)],
        ["gp_tle_rows", str(gp_count)],
    ]

    ws.clear()
    ws.update(values)


def main():
    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    if not sheet_id:
        raise RuntimeError("Missing GitHub secret: GOOGLE_SHEET_ID")

    client = get_client()
    spreadsheet = client.open_by_key(sheet_id)

    satcat_df = fetch_satcat()
    gp_df = fetch_gp_tle()

    satcat_ws = get_or_create_worksheet(
        spreadsheet,
        SATCAT_SHEET,
        rows=max(len(satcat_df) + 10, 1000),
        cols=max(len(satcat_df.columns) + 5, 30),
    )

    gp_ws = get_or_create_worksheet(
        spreadsheet,
        GP_TLE_SHEET,
        rows=max(len(gp_df) + 10, 1000),
        cols=10,
    )

    print("Updating SATCAT worksheet...")
    update_dataframe(satcat_ws, satcat_df)

    print("Updating GP_TLE worksheet...")
    update_dataframe(gp_ws, gp_df)

    print("Updating metadata worksheet...")
    update_metadata(spreadsheet, len(satcat_df), len(gp_df))

    print("CelesTrak Google Sheet cache updated successfully.")


if __name__ == "__main__":
    main()
