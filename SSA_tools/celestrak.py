"""
CelesTrak data access for the SSA app.

Covers three things that used to come from Space-Track in dashboard.py:
  1. SATCAT-derived KPI counts (active satellites, debris, rocket bodies,
     sovereign satellites, total tracked objects).
  2. Object-name lookup by NORAD ID.
  3. GP/TLE fetching, either for a known NORAD ID or by searching a name.

CelesTrak publishes this data for exactly this kind of programmatic use, with
no per-object query restriction, so there is no Space-Track-style rate
gating here: st.cache_data ttls are the only throttling in place.

If every request in this module times out at the TCP-connect stage (rather
than returning an HTTP error), that means the host this app runs on cannot
open an outbound HTTPS connection to celestrak.org/celestrak.com at all.
That is a network/firewall issue on the deployment host, not something this
code can route around by itself. See get_proxies() below if outbound
traffic has to go through a corporate/agency proxy.

Reference:
  SATCAT CSV        https://celestrak.org/pub/satcat.csv
  SATCAT records API https://celestrak.org/satcat/records.php
  GP/TLE API        https://celestrak.org/NORAD/elements/gp.php
"""

import csv
import io
from datetime import datetime, timedelta

import requests
import streamlit as st

SATCAT_HOSTS = ["https://celestrak.org", "https://celestrak.com"]
SATCAT_CSV_PATH = "/pub/satcat.csv"
SATCAT_RECORDS_PATH = "/satcat/records.php"
GP_PATH = "/NORAD/elements/gp.php"

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PhilSA-SSA-App/1.0; +https://philsa.gov.ph)"
}
CONNECT_TIMEOUT = 10   # fail fast on a blocked/unreachable host instead of hanging 30s per call
READ_TIMEOUT = 30
REQUEST_TIMEOUT = (CONNECT_TIMEOUT, READ_TIMEOUT)

CACHE_TTL_SATCAT = 21600   # 6h - CelesTrak's own SATCAT file updates roughly daily
CACHE_TTL_GP = 3600        # 1h - GP/TLE accuracy degrades over time regardless of source
CACHE_TTL_SEARCH = 3600


def get_proxies():
    """
    Optional HTTP(S) proxy support for deployments where outbound internet
    access is only permitted through an agency/corporate proxy. Configure
    in .streamlit/secrets.toml:

        [network]
        http_proxy = "http://proxy.example.gov.ph:8080"
        https_proxy = "http://proxy.example.gov.ph:8080"

    Returns None (no proxy) if not configured.
    """
    try:
        network_secrets = st.secrets["network"]
    except Exception:
        return None
    proxies = {}
    if network_secrets.get("http_proxy"):
        proxies["http"] = network_secrets["http_proxy"]
    if network_secrets.get("https_proxy"):
        proxies["https"] = network_secrets["https_proxy"]
    return proxies or None


def _get(path, params=None):
    """
    GET a CelesTrak path, trying each host in SATCAT_HOSTS in turn.
    Returns a requests.Response. Raises the last exception if every host
    fails (e.g. every host times out because outbound access is blocked).
    """
    proxies = get_proxies()
    last_error = None
    for host in SATCAT_HOSTS:
        try:
            resp = requests.get(
                host + path,
                params=params,
                headers=HTTP_HEADERS,
                timeout=REQUEST_TIMEOUT,
                proxies=proxies,
            )
            resp.raise_for_status()
            return resp
        except Exception as e:
            last_error = e
            continue
    raise last_error


def _network_error_message(e):
    """
    Turn a requests exception into a message that distinguishes a genuine
    network/firewall block (TCP connect never completes) from an ordinary
    HTTP-level error, since the two need very different fixes.
    """
    if isinstance(e, (requests.exceptions.ConnectTimeout, requests.exceptions.ConnectionError)):
        return (
            f"{e} — this looks like a network/firewall issue on the host running "
            "the app (it cannot open an outbound HTTPS connection to CelesTrak at "
            "all), not a bug in the fetch code. Confirm the host allows outbound "
            "HTTPS to celestrak.org/celestrak.com, or configure [network] "
            "http_proxy/https_proxy in secrets.toml if outbound traffic must go "
            "through a proxy."
        )
    return str(e)


def tle_epoch_to_datetime(tle_line1: str):
    """Parse the epoch field (columns 19-32) of a TLE line 1 into a datetime."""
    try:
        epoch_year = int(tle_line1[18:20])
        epoch_day = float(tle_line1[20:32])
        full_year = 2000 + epoch_year if epoch_year < 57 else 1900 + epoch_year
        return datetime(full_year, 1, 1) + timedelta(days=epoch_day - 1)
    except Exception:
        return None


@st.cache_data(ttl=CACHE_TTL_SATCAT)
def fetch_satcat_full():
    """
    Download the full CelesTrak SATCAT CSV.
    Returns (catalog: dict[int, dict], error: str|None), where each dict
    value is the raw CSV row keyed by CelesTrak's own field names
    (OBJECT_NAME, OBJECT_TYPE, OPS_STATUS_CODE, OWNER, DECAY_DATE, ...).
    """
    try:
        resp = _get(SATCAT_CSV_PATH)
    except Exception as e:
        return {}, _network_error_message(e)

    catalog = {}
    reader = csv.DictReader(io.StringIO(resp.text))
    for row in reader:
        try:
            norad_id = int(row["NORAD_CAT_ID"])
        except (KeyError, ValueError, TypeError):
            continue
        catalog[norad_id] = row

    if not catalog:
        return {}, "CelesTrak SATCAT download returned no parsable rows."
    return catalog, None


def compute_kpis_from_satcat(catalog: dict):
    """
    Derive dashboard KPI counts from a NORAD-keyed SATCAT catalog dict.
    An object counts as on-orbit when DECAY_DATE is blank.

    CelesTrak's OBJECT_TYPE field uses short codes, not full words:
      PAY = payload, R/B = rocket body, DEB = debris, TBA/UNK = unknown.
    A prior version of this function checked for "PAYLOAD"/"ROCKET BODY"/
    "DEBRIS" (Space-Track's GP-class wording), which never matched anything
    in the CSV and silently produced zero counts everywhere.
    """
    if not catalog:
        return None

    active_sats = debris_count = rocket_bodies = 0
    for rec in catalog.values():
        obj_type = str(rec.get("OBJECT_TYPE", "")).strip().upper()
        decay_date = str(rec.get("DECAY_DATE", "")).strip()
        on_orbit = not decay_date

        if obj_type == "PAY" and on_orbit:
            active_sats += 1
        elif obj_type == "DEB" and on_orbit:
            debris_count += 1
        elif obj_type == "R/B" and on_orbit:
            rocket_bodies += 1

    return {
        "active_sats": active_sats,
        "debris_count": debris_count,
        "rocket_bodies": rocket_bodies,
        "total_tracked": active_sats + debris_count + rocket_bodies,
    }


def lookup_names(norad_ids, catalog=None):
    """
    Resolve OBJECT_NAME for an iterable of NORAD IDs against the local
    SATCAT catalog. Fetches the catalog itself if none is passed in.
    Returns dict[int, str].
    """
    if not norad_ids:
        return {}
    if catalog is None:
        catalog, _ = fetch_satcat_full()
    names = {}
    for raw_id in norad_ids:
        try:
            norad_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        rec = catalog.get(norad_id)
        names[norad_id] = rec.get("OBJECT_NAME", "UNKNOWN") if rec else "UNKNOWN"
    return names


@st.cache_data(ttl=CACHE_TTL_SEARCH)
def search_satcat(name: str = None, catnr=None, max_results: int = 25):
    """
    Search the CelesTrak SATCAT by name (partial match) or exact NORAD
    catalog number. Returns (records: list[dict], error: str|None).
    """
    if not name and not catnr:
        return [], "Provide a satellite name or NORAD catalog number to search."

    params = {"FORMAT": "JSON", "MAX": max_results}
    if catnr:
        params["CATNR"] = catnr
    else:
        params["NAME"] = name

    try:
        resp = _get(SATCAT_RECORDS_PATH, params=params)
        data = resp.json()
    except Exception as e:
        return [], _network_error_message(e)

    if not data:
        return [], "No matching objects found in the CelesTrak SATCAT."
    return data, None


@st.cache_data(ttl=CACHE_TTL_GP)
def fetch_gp_tle(catnr=None, name=None, intdes=None):
    """
    Fetch GP/TLE data from CelesTrak for exactly one of catnr, name, or
    intdes. A name search can return several matches (e.g. "STARLINK").
    Returns (records: list[dict], error: str|None); each record has
    OBJECT_NAME, NORAD_CAT_ID, TLE_LINE1, TLE_LINE2, EPOCH.
    """
    if not any([catnr, name, intdes]):
        return [], "Provide one of catnr, name, or intdes."

    params = {"FORMAT": "TLE"}
    if catnr:
        params["CATNR"] = catnr
    elif intdes:
        params["INTDES"] = intdes
    else:
        params["NAME"] = name

    try:
        resp = _get(GP_PATH, params=params)
        text = resp.text.strip()
    except Exception as e:
        return [], _network_error_message(e)

    if not text:
        return [], "No GP/TLE data returned for that object."

    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) % 3 != 0:
        return [], "Unexpected response format from the CelesTrak GP endpoint."

    records = []
    for i in range(0, len(lines), 3):
        name_line, line1, line2 = lines[i].strip(), lines[i + 1].strip(), lines[i + 2].strip()
        try:
            norad_id = int(line1[2:7])
        except ValueError:
            norad_id = None
        epoch_dt = tle_epoch_to_datetime(line1)
        records.append({
            "OBJECT_NAME": name_line,
            "NORAD_CAT_ID": norad_id,
            "TLE_LINE1": line1,
            "TLE_LINE2": line2,
            "EPOCH": epoch_dt.strftime("%Y-%m-%d %H:%M:%S UTC") if epoch_dt else "Unknown",
        })
    return records, None


def parse_manual_tle(raw_text: str):
    """
    Parse a user-pasted TLE: either 2 lines (line1 + line2) or 3 lines
    (name + line1 + line2). Returns (record: dict|None, error: str|None)
    in the same shape as fetch_gp_tle() records.
    """
    lines = [ln.strip() for ln in raw_text.strip().splitlines() if ln.strip()]

    if len(lines) == 2:
        name_line, line1, line2 = "USER-DEFINED", lines[0], lines[1]
    elif len(lines) == 3:
        name_line, line1, line2 = lines[0], lines[1], lines[2]
    else:
        return None, "Paste either 2 lines (TLE only) or 3 lines (name + TLE)."

    if not (line1.startswith("1 ") and line2.startswith("2 ")):
        return None, "Line 1 must start with '1 ' and line 2 must start with '2 '."

    try:
        norad_id = int(line1[2:7])
    except ValueError:
        norad_id = None

    epoch_dt = tle_epoch_to_datetime(line1)
    return {
        "OBJECT_NAME": name_line,
        "NORAD_CAT_ID": norad_id,
        "TLE_LINE1": line1,
        "TLE_LINE2": line2,
        "EPOCH": epoch_dt.strftime("%Y-%m-%d %H:%M:%S UTC") if epoch_dt else "Unknown",
    }, None