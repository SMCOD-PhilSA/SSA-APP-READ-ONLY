import streamlit as st
import os
from pathlib import Path
import pandas as pd
import geopandas as gpd
import plotly.graph_objects as go
import folium
from streamlit_folium import st_folium

from services.reentry import reentry_event_analyzer as ra
from services.reentry import spacetrack_cache


def render_monthly_events_chart(df: pd.DataFrame):
    """Stacked MAJOR/MINOR event counts per month with a clear total scale."""
    monthly = df.copy()

    # Use categorical month labels instead of datetime values. This prevents
    # browser timezone conversion from shifting a month label backward.
    monthly["month"] = monthly["Hit UTC"].dt.strftime("%b %Y")
    monthly["Severity"] = monthly["Severity"].astype(str).str.strip().str.upper()

    month_order = (
        monthly[["Hit UTC", "month"]]
        .drop_duplicates("month")
        .sort_values("Hit UTC")["month"]
        .tolist()
    )

    counts = (
        monthly.groupby(["month", "Severity"])
        .size()
        .unstack(fill_value=0)
        .reindex(month_order, fill_value=0)
    )

    for col in ("MAJOR", "MINOR"):
        if col not in counts.columns:
            counts[col] = 0

    counts["TOTAL"] = counts["MAJOR"] + counts["MINOR"]

    # Build a readable y-axis that is based on the stacked monthly total.
    max_total = int(counts["TOTAL"].max()) if not counts.empty else 0
    if max_total <= 0:
        y_step, y_max = 1, 1
    else:
        import math

        raw_step = max_total / 7
        magnitude = 10 ** math.floor(math.log10(raw_step)) if raw_step > 0 else 1
        normalized = raw_step / magnitude
        if normalized <= 1:
            nice_step = 1
        elif normalized <= 2:
            nice_step = 2
        elif normalized <= 5:
            nice_step = 5
        else:
            nice_step = 10

        y_step = max(1, int(nice_step * magnitude))
        y_max = int(math.ceil(max_total / y_step) * y_step)
        if y_max <= max_total:
            y_max += y_step

    fig = go.Figure()

    fig.add_trace(go.Bar(
        x=counts.index,
        y=counts["MINOR"],
        name="MINOR",
        marker_color="#2ecc71",
        customdata=counts["TOTAL"],
        hovertemplate=(
            "<b>%{x}</b><br>"
            "MINOR: %{y:,}<br>"
            "Monthly total: %{customdata:,}"
            "<extra></extra>"
        ),
    ))

    fig.add_trace(go.Bar(
        x=counts.index,
        y=counts["MAJOR"],
        name="MAJOR",
        marker_color="#f44336",
        customdata=counts["TOTAL"],
        hovertemplate=(
            "<b>%{x}</b><br>"
            "MAJOR: %{y:,}<br>"
            "Monthly total: %{customdata:,}"
            "<extra></extra>"
        ),
    ))

    # Show the stacked total above each bar so the axis and hover values are
    # immediately understandable.
    fig.add_trace(go.Scatter(
        x=counts.index,
        y=counts["TOTAL"],
        mode="text",
        text=counts["TOTAL"].map(lambda value: f"{int(value):,}"),
        textposition="top center",
        cliponaxis=False,
        hoverinfo="skip",
        showlegend=False,
    ))

    fig.update_layout(
        barmode="stack",
        hovermode="x unified",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=40, r=10, t=22, b=40),
        height=280,
        xaxis=dict(
            type="category",
            categoryorder="array",
            categoryarray=month_order,
            gridcolor="rgba(128,128,128,0.2)",
            linecolor="rgba(128,128,128,0.3)",
            tickfont=dict(color="rgba(128,128,128,0.8)"),
        ),
        yaxis=dict(
            title=dict(
                text="TIP Events (Monthly Total)",
                font=dict(color="rgba(128,128,128,0.8)"),
            ),
            range=[0, y_max],
            dtick=y_step,
            rangemode="tozero",
            gridcolor="rgba(128,128,128,0.2)",
            linecolor="rgba(128,128,128,0.3)",
            tickfont=dict(color="rgba(128,128,128,0.8)"),
        ),
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1,
            font=dict(color="rgba(128,128,128,0.9)"),
        ),
    )

    st.plotly_chart(fig, use_container_width=True)


def render_top_objects_chart(summary: pd.DataFrame, top_n: int = 10):
    """
    Horizontal bar of the objects with the most TIP updates in the window.
    A high update count usually means Space-Track keeps revising that
    object's reentry window, i.e. an unstable/uncertain prediction worth
    keeping an eye on -- independent of whether it's currently MAJOR or MINOR.
    """
    top = summary.sort_values("total_rows", ascending=False).head(top_n).copy()
    if top.empty:
        st.info("No objects in this period.")
        return

    labels, colors = [], []
    for _, row in top.iterrows():
        disp_norad, disp_name = ra._demo_display(row["NORAD"], ra.get_object_name(row["NORAD"]))
        # Include the NORAD ID so two objects that happen to share a dummy
        # display name never collapse onto the same y-axis row.
        labels.append(f"{disp_name} · {disp_norad}")
        colors.append("#f44336" if row["severity"] == "MAJOR" else "#2ecc71")

    # Plot top-to-bottom in descending order.
    labels = labels[::-1]
    colors = colors[::-1]
    values = top["total_rows"].tolist()[::-1]
    max_val = max(values) if values else 1

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=values, y=labels, orientation="h", marker_color=colors,
    ))
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=10, r=10, t=10, b=40), height=280,
        xaxis=dict(title=dict(text="TIP Updates", font=dict(color="rgba(128,128,128,0.8)")),
                   range=[0, max_val * 1.1], dtick=max(1, round(max_val / 8)),
                   gridcolor="rgba(128,128,128,0.2)", linecolor="rgba(128,128,128,0.3)",
                   tickfont=dict(color="rgba(128,128,128,0.8)")),
        yaxis=dict(type="category", gridcolor="rgba(0,0,0,0)", linecolor="rgba(128,128,128,0.3)",
                   tickfont=dict(color="rgba(128,128,128,0.9)")),
        showlegend=False,
    )
    st.plotly_chart(fig, use_container_width=True)


# Manila FIR reference — same shapefile used by the Rocket Launch module.
PH_SHAPE_DIR = os.getenv("PH_SHAPE_DIR", "").strip()


def _find_shape_dir() -> Path:
    candidates = []
    if PH_SHAPE_DIR:
        candidates.append(Path(PH_SHAPE_DIR).expanduser())

    here = Path(__file__).resolve().parent
    for base in [here, *here.parents]:
        candidates.append(base / "utils" / "shapefiles")
    candidates.append(Path.cwd() / "utils" / "shapefiles")

    for candidate in candidates:
        if (candidate / "Manila_FIR_boundary.shp").exists():
            return candidate

    checked = "\n  - ".join(str(p) for p in candidates)
    raise FileNotFoundError(
        "Could not locate Manila FIR shapefile (Manila_FIR_boundary.shp). "
        f"Checked:\n  - {checked}\nSet PH_SHAPE_DIR if needed."
    )


def _ensure_wgs84(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    else:
        gdf = gdf.to_crs("EPSG:4326")
    return gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()


@st.cache_resource
def _load_manila_fir_geometry():
    shape_dir = _find_shape_dir()
    fir_path = shape_dir / "Manila_FIR_boundary.shp"
    fir_gdf = _ensure_wgs84(gpd.read_file(fir_path))

    if fir_gdf.empty:
        raise RuntimeError(f"No usable geometry found in {fir_path}")

    fir_union = fir_gdf.geometry.union_all()
    return {
        "shape_dir": shape_dir,
        "fir_gdf": fir_gdf,
        "fir_union": fir_union,
    }


def _classify_map_points(df: pd.DataFrame, fir: dict) -> pd.DataFrame:
    """Classify historical re-entry points by Manila FIR membership."""
    if df.empty:
        return df

    work = df.copy().reset_index(drop=True)
    geo = gpd.GeoDataFrame(
        work.copy(),
        geometry=gpd.points_from_xy(work["lon"], work["lat"]),
        crs="EPSG:4326",
    )

    in_fir = geo.geometry.apply(fir["fir_union"].covers)
    work["in_manila_fir_bool"] = in_fir.astype(bool).values
    work["fir_location_class"] = work["in_manila_fir_bool"].map(
        {True: "Within Manila FIR", False: "Outside Manila FIR"}
    )
    return work


def _load_reentry_points_dataframe() -> pd.DataFrame:
    """Load resolved historical re-entry points and classify against Manila FIR."""
    try:
        client = ra.get_gspread_client()
        sh = client.open_by_key(ra.SHEET_ID)
        ws = sh.worksheet("ReentryPoints")
        rows = ws.get_all_records()
    except Exception:
        return pd.DataFrame()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df.columns = [str(c).strip() for c in df.columns]
    if "norad_id" not in df.columns:
        return pd.DataFrame()

    for col in ("lat", "lon"):
        if col not in df.columns:
            df[col] = pd.NA
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["lat", "lon"]).copy()
    if df.empty:
        return df

    for col in ("object_name", "object_type", "decay_epoch", "tip_msg_epoch", "source"):
        if col not in df.columns:
            df[col] = ""

    df["norad_id"] = df["norad_id"].astype(str).str.strip()
    df = df[df["norad_id"] != ""]
    df = df.drop_duplicates(subset=["norad_id"], keep="last").reset_index(drop=True)

    fir = _load_manila_fir_geometry()
    return _classify_map_points(df, fir)


def _add_manila_fir_overlay(m: folium.Map, fir: dict):
    folium.GeoJson(
        fir["fir_gdf"].to_json(),
        name="Manila FIR",
        style_function=lambda _: {
            "color": "#FFA500",
            "weight": 2.2,
            "fillColor": "#FFA500",
            "fillOpacity": 0.035,
        },
    ).add_to(m)


def create_ph_reentry_map(points_df: pd.DataFrame, view_mode: str = "Within Manila FIR"):
    """Historical re-entry map using Manila FIR as the Philippine reference area."""
    fir = _load_manila_fir_geometry()
    fir_only = view_mode == "Within Manila FIR"

    m = folium.Map(location=[14.5, 120.5], zoom_start=5 if fir_only else 2)
    _add_manila_fir_overlay(m, fir)

    if fir_only:
        minx, miny, maxx, maxy = fir["fir_gdf"].total_bounds
        m.fit_bounds([[miny, minx], [maxy, maxx]], padding=(20, 20))

    plotted = 0
    if points_df is None or points_df.empty:
        return m, plotted

    work = points_df.copy()
    if fir_only:
        work = work[work["in_manila_fir_bool"]].copy()

    for _, row in work.iterrows():
        lat = row.get("lat")
        lon = row.get("lon")
        if pd.isna(lat) or pd.isna(lon):
            continue

        in_fir = bool(row.get("in_manila_fir_bool", False))
        norad = str(row.get("norad_id", "")).strip()
        object_name = str(row.get("object_name", "")).strip()
        object_type = str(row.get("object_type", "")).strip()
        label_name = object_name or f"NORAD {norad}"
        label = f"{label_name} · NORAD {norad}" if norad else label_name

        decay_epoch = str(row.get("decay_epoch", "")).strip() or "—"
        msg_epoch = str(row.get("tip_msg_epoch", "")).strip() or "—"
        source = str(row.get("source", "")).strip() or "—"
        location_class = str(row.get("fir_location_class", "")).strip() or "—"

        marker_color = "#dc2626" if in_fir else "#2563eb"

        popup_html = (
            f"<b>{label}</b><br>"
            f"Type: {object_type or '—'}<br>"
            f"Latitude: {float(lat):.4f}<br>"
            f"Longitude: {float(lon):.4f}<br>"
            f"FIR classification: {location_class}<br>"
            f"Decay epoch: {decay_epoch}<br>"
            f"TIP MSG_EPOCH: {msg_epoch}<br>"
            f"Source: {source}"
        )

        folium.CircleMarker(
            [float(lat), float(lon)],
            radius=6 if in_fir else 5,
            color=marker_color,
            weight=2 if in_fir else 1,
            fill=True,
            fill_color=marker_color,
            fill_opacity=0.88,
            popup=popup_html,
            tooltip=label,
        ).add_to(m)

        plotted += 1

    if not fir_only and plotted:
        lat_min = float(work["lat"].min())
        lat_max = float(work["lat"].max())
        lon_min = float(work["lon"].min())
        lon_max = float(work["lon"].max())
        if all(pd.notna(v) for v in [lat_min, lat_max, lon_min, lon_max]):
            m.fit_bounds([[lat_min, lon_min], [lat_max, lon_max]], padding=(20, 20))

    legend_items = """
        <li style="color:#000000; margin-bottom:6px;">
            <span style='background:#dc2626;width:14px;height:14px;display:inline-block;
                         border-radius:50%;margin-right:8px;'></span> Within Manila FIR</li>
    """
    if not fir_only:
        legend_items += """
        <li style="color:#000000; margin-bottom:6px;">
            <span style='background:#2563eb;width:14px;height:14px;display:inline-block;
                         border-radius:50%;margin-right:8px;'></span> Outside Manila FIR</li>
        """

    legend = f"""
    <div id='ph-reentry-legend' class='maplegend'
         style='position: fixed; z-index: 9999;
                background-color: rgba(255, 255, 255, 0.88);
                border-radius: 8px;
                padding: 10px 13px;
                font-size: 13px;
                bottom: 30px; left: 30px;
                border: 2px solid rgba(0,0,0,0.35);'>
        <ul style="list-style:none;padding:0;margin:0;">
            {legend_items}
            <li style="color:#000000;">
                <span style='display:inline-block;width:16px;border-bottom:2px solid #FFA500;
                             margin-right:8px;vertical-align:middle;'></span> Manila FIR</li>
        </ul>
    </div>
    """
    m.get_root().html.add_child(folium.Element(legend))
    return m, plotted


def render():
    """
    Reentry overview dashboard: headline metrics, monthly trend charts, and
    a recent-events panel. Pulls its data through reentry_event_analyzer's
    loaders/cache (load_data, load_acknowledged, build_latest_summary,
    get_object_name, _demo_display) and its inject_css() theme, so it always
    reflects the same Google Sheet cache as the Analyzer tab without
    duplicating any of that logic here.
    """
    ra.inject_css()

    # Event-card badge styling, matching collision.py's solid-color risk-btn
    # pills (namespaced with an "re-" prefix so it doesn't collide with the
    # outline-style .risk-major/.risk-minor classes used on the Analyzer tab).
    st.markdown("""
    <style>
    .re-event-badge {
        display: inline-block;
        padding: 6px 16px;
        border-radius: 20px;
        font-weight: 700;
        font-size: 13.5px;
        text-align: center;
        min-width: 90px;
    }
    .re-event-badge.re-major { background-color: #dc2626; color: white; }
    .re-event-badge.re-minor { background-color: #4ade80; color: black; }
    .re-event-badge.re-high  { background-color: #eab308; color: black; }
    </style>
    """, unsafe_allow_html=True)

    df = ra.load_data()
    if df.empty:
        st.warning("No data available.")
        return

    valid = df.dropna(subset=["Hit UTC"])
    if valid.empty:
        st.warning("No valid Hit UTC values found.")
        return

    cutoff   = valid["Hit UTC"].max() - pd.Timedelta(days=180)
    df_range = valid[valid["Hit UTC"] >= cutoff].copy()

    acknowledged = ra.load_acknowledged()
    summary      = ra.build_latest_summary(df_range)

    # Full-history summary (no 180-day cutoff), used ONLY for the map.
    # summary/df_range above is intentionally windowed for the KPI row
    # and Events panel (those are meant to show recent activity) -- but
    # that same window would silently drop historical backfilled
    # reentries from the map, since the point join below is keyed off
    # whichever summary dataframe gets passed to create_ph_reentry_map().
    summary_all = ra.build_latest_summary(valid)

    # SATCAT high/low-interest enrichment, read from the cached
    # SpaceTrack_SATCAT sheet -- never a live Space-Track call from here.
    # NORAD IDs without a logged row (not re-entered yet, or relay hasn't
    # run for them) default to LOW rather than showing blank.
    interest_map = spacetrack_cache.fetch_satcat_interest_map()
    summary["interest_level"] = (
        summary["NORAD"].astype(str)
        .map(lambda n: interest_map.get(n, {}).get("interest_level", "LOW"))
    )
    summary_all["interest_level"] = (
        summary_all["NORAD"].astype(str)
        .map(lambda n: interest_map.get(n, {}).get("interest_level", "LOW"))
    )

    # Historical map data comes directly from ReentryPoints. The revised
    # retry_get.py logs BOTH PH-near and outside-PH historical reentries, so
    # the map no longer depends on the 180-day Data-sheet summary.
    try:
        map_points = _load_reentry_points_dataframe()
    except Exception as exc:
        st.error(f"Could not load Manila FIR reference geometry: {exc}")
        map_points = pd.DataFrame()

    n_objects = len(summary)
    n_major   = int((summary["severity"] == "MAJOR").sum())
    n_minor   = int((summary["severity"] == "MINOR").sum())

    st.markdown(f"""
    <div class="re-metrics">
        <div class="re-metric"><div class="m-lbl">Critical (MAJOR)</div><div class="m-val" style="color:#f44336;">{n_major}</div></div>
        <div class="re-metric"><div class="m-lbl">Minor</div><div class="m-val" style="color:#2ecc71;">{n_minor}</div></div>
        <div class="re-metric"><div class="m-lbl">Events Monitored</div><div class="m-val">{n_objects}</div></div>
    </div>""", unsafe_allow_html=True)

    st.markdown(
        "<div style='font-weight:700;color:var(--text-color);font-size:0.95rem;"
        "margin:16px 0 8px;'>Historical Re-entry Map</div>",
        unsafe_allow_html=True,
    )

    view_mode = st.radio(
        "Map scope",
        options=["Within Manila FIR", "All reentries"],
        horizontal=True,
        key="reentry_map_scope",
        label_visibility="collapsed",
    )

    if map_points.empty:
        st.info(
            "No resolved historical re-entry points are available in the "
            "ReentryPoints worksheet yet. Run retry_get.py to populate it."
        )
    else:
        ph_map, n_plotted = create_ph_reentry_map(map_points, view_mode=view_mode)
        st_folium(ph_map, height=500, use_container_width=True, returned_objects=[])


    chart_col, events_col = st.columns([2, 1])



    with chart_col:
        st.markdown(
            "<div style='font-weight:700;color:var(--text-color);font-size:0.95rem;"
            "margin-bottom:8px;'>TIP Events per Month</div>",
            unsafe_allow_html=True)
        render_monthly_events_chart(df_range)

        st.markdown(
            "<div style='font-weight:700;color:var(--text-color);font-size:0.95rem;"
            "margin:16px 0 8px;'>Top Objects by TIP Update Count</div>"
            "<p style='color:var(--text-color);opacity:0.7;font-size:0.8rem;margin-bottom:8px;'>",
            unsafe_allow_html=True)
        render_top_objects_chart(summary)

    with events_col:
        st.subheader("Events")
        container_events = st.container(border=True, height=600)

        with container_events:
            if summary.empty:
                st.info("No events in the last 6 months.")
            else:
                for _, row in summary.head(30).iterrows():
                    disp_norad, disp_name = ra._demo_display(row["NORAD"], ra.get_object_name(row["NORAD"]))
                    sev        = row["severity"]
                    sev_class  = "re-major" if sev == "MAJOR" else "re-minor"
                    interest   = row["interest_level"]
                    hit_str    = (row["hit_utc"].strftime("%Y-%m-%d %H:%M PhST")
                                  if pd.notna(row["hit_utc"]) else "—")
                    dist_str   = (f"{row['min_distance']:.1f} km"
                                  if pd.notna(row["min_distance"]) else "—")
                    interest_badge = (
                        '<span class="re-event-badge re-high" style="margin-left:6px;">HIGH INTEREST</span>'
                        if interest == "HIGH" else ""
                    )
                    st.markdown(f"""
                    <div style="background-color:#1a4fa3;padding:14px;border-radius:10px;
                                margin-bottom:12px;color:white;">
                        <strong>{disp_name}</strong><br>
                        <small>NORAD {disp_norad}</small><br>
                        <small>{hit_str} • Closest: <b>{dist_str}</b></small><br>
                        <span class="re-event-badge {sev_class}">{sev}</span>{interest_badge}
                    </div>
                    """, unsafe_allow_html=True)

                if len(summary) > 30:
                    st.caption(f"... and {len(summary) - 30} more objects")