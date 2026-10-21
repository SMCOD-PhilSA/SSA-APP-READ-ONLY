import requests
from datetime import datetime, date, timedelta
from collections import defaultdict


def safe_json(url):
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception:
        return []


def classify_kp(kp):
    if kp < 3:
        return "Quiet"
    elif kp < 5:
        return "Unsettled"
    elif kp < 6:
        return "Minor Storm"
    elif kp < 7:
        return "Moderate Storm"
    elif kp < 8:
        return "Strong Storm"
    return "Severe Storm"


def _parse_kp_rows(data):
    """
    NOAA Kp endpoints return a list of lists:
      Row 0 (header): ["time_tag", "kp", "observed", "noaa_scale"]
      Row 1+  (data): ["2026-05-21 00:00:00", "2.33", "observed", "None"]

    Both the forecast and 1-min observed endpoints share this format.
    Returns a list of (datetime, float) tuples, skipping the header row.
    """
    rows = []
    for item in data[1:]:   # skip header row
        try:
            # item is a list: [time_tag, kp, observed, noaa_scale]
            dt  = datetime.strptime(item[0], "%Y-%m-%d %H:%M:%S")
            kp  = float(item[1])
            rows.append((dt, kp))
        except Exception:
            continue
    return rows


def get_kp_index():
    url = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index-forecast.json"
    data = safe_json(url)

    if not data:
        return {"kp": "N/A", "status": "No Data"}

    rows = _parse_kp_rows(data)
    if not rows:
        return {"kp": "N/A", "status": "No Data"}

    # Most recent entry
    kp = rows[-1][1]
    return {"kp": kp, "status": classify_kp(kp)}


def get_daily_kp():
    url = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index-forecast.json"
    data = safe_json(url)

    if not data:
        return ["No Data"], [0]

    rows = _parse_kp_rows(data)
    if not rows:
        return ["No Data"], [0]

    today = date.today()
    start_date = today - timedelta(days=7)
    end_date = today + timedelta(days=2)

    daily = defaultdict(list)

    for dt, kp in rows:
        d = dt.date()
        if start_date <= d <= end_date:
            daily[d].append(kp)

    if not daily:
        return ["No Data"], [0]

    ordered_days = sorted(daily.keys())
    labels = [d.strftime("%b %d") for d in ordered_days]
    values = [round(sum(daily[d]) / len(daily[d]), 2) for d in ordered_days]

    return labels, values