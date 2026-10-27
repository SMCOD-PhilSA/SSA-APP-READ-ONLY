import streamlit as st
import streamlit.components.v1 as components

# ====================== FORCE BASE URL ======================
if "base_url_set" not in st.session_state:
    base_url = st.secrets.get("STREAMLIT_BASE_URL", None)
    if base_url:
        st.config.set_option("server.baseUrl", f"https://{base_url}")
    st.session_state.base_url_set = True

# ====================== PAGE CONFIG ======================
st.set_page_config(
    page_title="PhilSA SSA App",
    page_icon="utils/PhilSA_v1_White.png",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ====================== GLOBAL STYLES ======================
st.markdown("""
<style>
header[data-testid="stHeader"] {
    background-color: #0e224b !important;
}

header[data-testid="stHeader"] * {
    color: white !important;
}

/* Keep normal spacing below clock */
.block-container {
    padding-top: 1rem !important;
}
</style>
""", unsafe_allow_html=True)

# ====================== LOGO ======================
st.logo("utils/PhilSA_v5_White.png", size="large")

# ====================== DEFINE PAGES ======================
dashboard = st.Page(
    "dashboard.py",
    title="Dashboard",
    icon=":material/dashboard:",
    default=True,
    url_path="dashboard"
)

about = st.Page(
    "about.py",
    title="About",
    icon=":material/info:",
    url_path="about"
)

ssa_rocket = st.Page(
    "SSA_tools/rocket.py",
    title="Rocket Launch",
    icon=":material/rocket_launch:",
    url_path="rocket"
)

# ssa_collision = st.Page(
#     "SSA_tools/collision.py",
#     title="Conjunction",
#     icon=":material/compare_arrows:",
#     url_path="collision"
# )

ssa_reenter = st.Page(
    "SSA_tools/reenter.py",
    title="Re-entry",
    icon=":material/local_fire_department:",
    url_path="reenter"
)

ssa_weather = st.Page(
    "SSA_tools/weather.py",
    title="Space Weather",
    icon=":material/sunny:",
    url_path="weather"
)

document = st.Page(
    "resources/documentation.py",
    title="Documentation",
    icon=":material/document_search:",
    url_path="documentation"
)

faqs = st.Page(
    "resources/FAQ.py",
    title="FAQs",
    icon=":material/help:",
    url_path="faqs"
)

# ====================== NAVIGATION ======================
pg = st.navigation(
    {
        "Home": [about, dashboard],
        #"SSA Tools": [ssa_collision, ssa_reenter, ssa_rocket, ssa_weather],
        "SSA Domain": [ssa_reenter, ssa_rocket],
        "Resources": [document, faqs]
    },
    position="top"
)

# ====================== PhST LIVE CLOCK BAR ======================
# components.html(
#     """
#     <!DOCTYPE html>
#     <html>
#     <head>
#     <style>
#         html, body {
#             margin: 0;
#             padding: 0;
#             overflow: hidden;
#             background: #0e224b;
#             font-family: 'Segoe UI', Arial, sans-serif;
#         }

#         #clockbar {
#             width: 100%;
#             height: 32px;
#             background: #0e224b;
#             border-bottom: 1px solid rgba(255,255,255,0.15);
#             display: flex;
#             align-items: center;
#             justify-content: flex-end;
#             gap: 10px;
#             padding: 0 24px;
#             color: white;
#             box-sizing: border-box;
#         }

#         .label {
#             font-size: 11px;
#             font-weight: 600;
#             letter-spacing: 1px;
#             text-transform: uppercase;
#             color: rgba(255,255,255,0.60);
#         }

#         .date {
#             font-size: 12px;
#             color: rgba(255,255,255,0.85);
#         }

#         .time {
#             font-size: 13px;
#             font-weight: 700;
#             color: white;
#             font-variant-numeric: tabular-nums;
#         }

#         .sep {
#             color: rgba(255,255,255,0.35);
#         }
#     </style>
#     </head>

#     <body>
#         <div id="clockbar">
#             <span class="label">Philippine Standard Time</span>
#             <span class="sep">|</span>
#             <span class="date" id="date">Loading...</span>
#             <span class="sep">•</span>
#             <span class="time" id="time">Loading...</span>
#         </div>

#         <script>
#             function updateClock() {
#                 const now = new Date();

#                 const dateOptions = {
#                     timeZone: "Asia/Manila",
#                     weekday: "long",
#                     year: "numeric",
#                     month: "short",
#                     day: "2-digit"
#                 };

#                 const timeOptions = {
#                     timeZone: "Asia/Manila",
#                     hour: "2-digit",
#                     minute: "2-digit",
#                     second: "2-digit",
#                     hour12: true
#                 };

#                 document.getElementById("date").innerHTML =
#                     now.toLocaleDateString("en-US", dateOptions);

#                 document.getElementById("time").innerHTML =
#                     now.toLocaleTimeString("en-US", timeOptions) + " PhST";
#             }

#             updateClock();
#             setInterval(updateClock, 1000);
#         </script>
#     </body>
#     </html>
#     """,
#     height=32,
#     scrolling=False
# )

# ====================== UNIFIED HEADER (Clock + Page Title) ======================
PAGE_META = {
    "Dashboard": {
        "title"   : "Welcome to the Space Situational Awareness (SSA) Dashboard",
        "subtitle": "Compiled and operated by the Space Mission Control and Operations Division (SMCOD) of Philippine Space Agency (PhilSA)",
    },
}
_meta     = PAGE_META.get(pg.title, {"title": pg.title, "subtitle": ""})
_title    = _meta["title"]
_subtitle = _meta["subtitle"]

# Taller header when there is a subtitle
_has_sub  = bool(_subtitle)
_height   = 100 if _has_sub else 68

components.html(
    f"""
    <!DOCTYPE html>
    <html>
    <head>
    <style>
        html, body {{
            margin: 0;
            padding: 0;
            background: transparent;
            font-family: 'Segoe UI', Arial, sans-serif;
        }}
        #header {{
            width: 100%;
            height: {_height}px;
            background: #1a4fa3;
            border-radius: 12px;
            display: flex;
            align-items: center;
            padding: 0 28px;
            color: white;
            box-sizing: border-box;
            gap: 16px;
        }}
        .title-block {{
            flex: 1;
            display: flex;
            flex-direction: column;
            justify-content: center;
            text-align: center;
        }}
        .page-title {{
            font-size: {"22px" if _has_sub else "28px"};
            font-weight: 700;
            color: white;
            letter-spacing: 0.3px;
            margin: 0;
            line-height: 1.2;
        }}
        .page-subtitle {{
            font-size: 13px;
            font-weight: 400;
            font-style: italic;
            color: rgba(255,255,255,0.78);
            margin-top: 5px;
            line-height: 1.3;
        }}
        .clock-container {{
            flex-shrink: 0;
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 13px;
            background: rgba(255,255,255,0.12);
            padding: 8px 16px;
            border-radius: 30px;
            white-space: nowrap;
        }}
        .label {{
            font-size: 11px;
            font-weight: 600;
            letter-spacing: 1px;
            text-transform: uppercase;
            color: rgba(255,255,255,0.70);
        }}
        .date {{ color: rgba(255,255,255,0.90); font-size: 12px; }}
        .time {{
            font-weight: 700;
            font-variant-numeric: tabular-nums;
            font-size: 13px;
        }}
        .sep {{ color: rgba(255,255,255,0.40); }}
    </style>
    </head>
    <body>
        <div id="header">
            <div class="title-block">
                <div class="page-title">{_title}</div>
                {"<div class='page-subtitle'>" + _subtitle + "</div>" if _has_sub else ""}
            </div>
            <div class="clock-container">
                <span class="label">Philippine Standard Time</span>
                <span class="sep">|</span>
                <span class="date" id="date">Loading...</span>
                <span class="sep">•</span>
                <span class="time" id="time">Loading...</span>
            </div>
        </div>
        <script>
            function updateClock() {{
                const now = new Date();
                document.getElementById("date").innerHTML =
                    now.toLocaleDateString("en-US", {{
                        timeZone: "Asia/Manila",
                        weekday: "long", year: "numeric",
                        month: "short",  day: "2-digit"
                    }});
                document.getElementById("time").innerHTML =
                    now.toLocaleTimeString("en-US", {{
                        timeZone: "Asia/Manila",
                        hour: "2-digit", minute: "2-digit",
                        second: "2-digit", hour12: true
                    }}) + " PhST";
            }}
            updateClock();
            setInterval(updateClock, 1000);
        </script>
    </body>
    </html>
    """,
    height=_height,
    scrolling=False
)

# ====================== RUN SELECTED PAGE ======================
pg.run()