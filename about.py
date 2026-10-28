import streamlit as st
import base64

st.set_page_config(
    page_title="SSA - About",
    page_icon=":material/info:",
    layout="wide"
)

# ---------------- CUSTOM STYLING ----------------
st.markdown("""
<style>
    .block-container {
        padding-top: 4.8rem !important;
    }
    
    .intro-text {
        text-align: center;
        font-size: 1.15rem;
        line-height: 1.75;
        width: 100%;
        margin: 0 auto 1.75rem auto;
        opacity: 0.95;
    }
    
    .kpi-container {
        display: flex;
        justify-content: space-between;
        gap: 1.5rem;
        margin-bottom: 0.75rem;
    }
    
    .kpi-card-top {
        flex: 1;
        text-align: center;
        padding: 0.5rem 0.5rem;
        display: flex;
        flex-direction: column;
        justify-content: flex-start;
    }
    
    .kpi-label {
        font-size: 0.85rem;
        opacity: 0.7;
        margin-bottom: 0.45rem;
        text-transform: uppercase;
        letter-spacing: 0.4px;
    }
    
    .kpi-value-top {
        font-size: 1.35rem;
        font-weight: 700;
        line-height: 1.35;
    }

    /* ===== Info cards ===== */
    .info-card {
        border-radius: 14px;
        padding: 1.2rem 1.1rem 1.4rem 1.1rem;
        height: 100%;
        min-height: 420px;
        color: white;
        background: #0b1f3a;
        box-shadow: 0 4px 14px rgba(0,0,0,0.18);
        transition: transform 0.18s ease, box-shadow 0.18s ease;
        text-align: center;
        display: flex;
        flex-direction: column;
    }
    
    .info-card:hover {
        transform: translateY(-4px);
        box-shadow: 0 8px 22px rgba(0,0,0,0.28);
    }
    
    .info-card img {
        width: 100%;
        height: 220px;
        object-fit: cover;
        border-radius: 10px;
        margin-bottom: 0.9rem;
        flex-shrink: 0;
    }
    
    .info-title {
        font-size: 1.5rem;
        font-weight: 650;
        margin-bottom: 0.5rem;
        flex-shrink: 0;
    }
    
    .info-text {
        font-size: 1.0rem;
        line-height: 1.5;
        opacity: 0.92;
        flex-grow: 1;
    }

    /* ===== Domain cards ===== */
    .domain-card {
        border-radius: 14px;
        padding: 1.6rem 1.4rem;
        height: 100%;
        min-height: 850px;
        color: white;
        box-shadow: 0 4px 14px rgba(0,0,0,0.18);
        transition: transform 0.18s ease, box-shadow 0.18s ease;
        text-align: center;
    }
    
    .domain-card:hover {
        transform: translateY(-4px);
        box-shadow: 0 8px 22px rgba(0,0,0,0.28);
    }
    
    .domain-card .material-symbols-outlined {
        font-size: 2.6rem;
        margin-bottom: 0.7rem;
        display: block;
    }
    
    .domain-title {
        font-size: 1.25rem;
        font-weight: 650;
        margin-bottom: 0.5rem;
    }
    
    .domain-desc {
        font-size: 0.95rem;
        line-height: 1.5;
        opacity: 0.92;
    }

    /* ===== Bottom KPI Cards (Knowledge / FAQs / Feedback) ===== */
    .kpi-link {
        text-decoration: none !important;
        color: inherit !important;
    }
    
    .kpi-card {
        border-radius: 14px;
        padding: 1.6rem 1.3rem;
        height: 100%;
        color: white;
        transition: transform 0.18s ease, box-shadow 0.18s ease;
        box-shadow: 0 4px 14px rgba(0,0,0,0.18);
        text-align: center;
        cursor: pointer;
    }
    
    .kpi-card:hover {
        transform: translateY(-5px);
        box-shadow: 0 10px 24px rgba(0,0,0,0.28);
    }
    
    .kpi-card .material-symbols-outlined {
        font-size: 2.6rem;
        margin-bottom: 0.7rem;
        display: block;
    }
    
    .kpi-title {
        font-size: 1.25rem;
        font-weight: 650;
        margin-bottom: 0.45rem;
    }
    
    .kpi-desc {
        font-size: 0.92rem;
        line-height: 1.45;
        opacity: 0.9;
    }
    
    .footer {
        text-align: center;
        color: #888;
        padding: 1.25rem 0 0.5rem 0;
        font-size: 0.9rem;
        border-top: 1px solid rgba(128,128,128,0.18);
        margin-top: 1.5rem;
    }
</style>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@24,400,0,0" />
""", unsafe_allow_html=True)

def kpi_card(icon, title, description, page_path, color):
    st.markdown(f"""
    <a href="/{page_path}" target="_self" class="kpi-link">
        <div class="kpi-card" style="background: {color};">
            <span class="material-symbols-outlined">{icon}</span>
            <div class="kpi-title">{title}</div>
            <div class="kpi-desc">{description}</div>
        </div>
    </a>
    """, unsafe_allow_html=True)

# ---------------- INTRO ----------------
st.markdown("""
<div class="intro-text">
    The <strong>PhilSA SSA Dashboard</strong> is a visualization and monitoring
    platform developed for PhilSA stakeholders to support Space Situational
    Awareness (SSA). It provides interactive maps, historical data,
    visualizations, and alerts on orbital debris re-entry events
    and rocket launch activities, supporting situational awareness,
    operational monitoring, and informed decision-making.
</div>
""", unsafe_allow_html=True)

# ---------------- TOP KPI CARDS ----------------
st.markdown("""
<div class="kpi-container">
    <div class="kpi-card-top">
        <div class="kpi-label">Operated by</div>
        <div class="kpi-value-top">Space Mission Control and Operations Division<br>(SMCOD)</div>
    </div>
    <div class="kpi-card-top">
        <div class="kpi-label">Launched on</div>
        <div class="kpi-value-top">November<br>2026</div>
    </div>
    <div class="kpi-card-top">
        <div class="kpi-label">Presented in</div>
        <div class="kpi-value-top">International Astronautical Congress<br>(IAC) 2026</div>
    </div>
</div>
""", unsafe_allow_html=True)

st.divider()

# ---------------- WHAT IS SSA ----------------
st.markdown("<h3 style='text-align: center; margin-bottom: 1.8rem;'>Space Situational Awareness (SSA)</h3>", unsafe_allow_html=True)

img_meaning     = "utils/d2-1.jpg"
img_initiatives = "utils/Advisory-Featured-Image1-1.png"
img_data        = "utils/d2.jpg"

info_cards = [
    {
        "img": img_meaning,
        "title": "Definition",
        "desc": "Space Situational Awareness (SSA) is the knowledge and characterization of objects and events in outer space. It involves detecting, tracking, and predicting the behavior of satellites, debris, and other objects to prevent collisions and support safe space operations.",
        "color": "#0b1f3a"
    },
    {
        "img": img_initiatives,
        "title": "Initiatives",
        "desc": "PhilSA supports key SSA domains including Orbital Debris Re-entry Monitoring and Rocket Launch Tracking. This dashboard aims to make complex space domain awareness information more accessible and actionable — especially for institutions and researchers in the Philippines and the Asia-Pacific region.",
        "color": "#0e2a57"
    },
    {
        "img": img_data,
        "title": "Data Sources",
        "desc": "Space-Track.org<br>CelesTrak<br>Civil Aviation Authority of the Philippines (CAAP)",
        "color": "#123a7a"
    },
]

cols = st.columns(3, gap="large")
for col, card in zip(cols, info_cards):
    with col:
        with open(card["img"], "rb") as f:
            img_base64 = base64.b64encode(f.read()).decode()
        
        st.markdown(f"""
        <div class="info-card" style="background: {card['color']};">
            <img src="data:image/png;base64,{img_base64}" alt="{card['title']}">
            <div class="info-title">{card['title']}</div>
            <div class="info-text">{card['desc']}</div>
        </div>
        """, unsafe_allow_html=True)

st.divider()

# ---------------- SSA DOMAINS ----------------
st.markdown("<h3 style='text-align: center; margin-bottom: 1.8rem;'>SSA Domains</h3>", unsafe_allow_html=True)

col1, col2 = st.columns(2, gap="large")

# ===== RE-ENTRY =====
with col1:
    with open("utils/ZQ_RE-ENTRY.png", "rb") as f:
        img_base64 = base64.b64encode(f.read()).decode()
    
    st.markdown(f"""
    <a href="/reenter" target="_self" class="kpi-link">
    <div class="domain-card" style="background: #0e2a57;">
        <span class="material-symbols-outlined">local_fire_department</span>
        <div class="domain-title">Re-entry</div>
        <div class="domain-desc">
            The process of determining, tracking, analyzing, and communicating the potential re-entry of space objects 
            to assess risks and support preparedness and response activities.
        </div>
        <br><br><br>
        <img src="data:image/png;base64,{img_base64}" alt="Re-entry">
    </div>
    """, unsafe_allow_html=True)

# ===== ROCKET LAUNCH =====
with col2:
    with open("utils/DZmap_LM8A_20261025.png", "rb") as f:
        img_base64 = base64.b64encode(f.read()).decode()
    
    st.markdown(f"""
    <a href="/rocket" target="_parent" class="kpi-link">
    <div class="domain-card" style="background: #123a7a;">
        <span class="material-symbols-outlined">rocket_launch</span>
        <div class="domain-title">Rocket Launch</div>
        <div class="domain-desc">
            The process of identifying and mapping of rocket drop zones, monitoring of launch activities, 
            and dissemination of advisories to enhance public awareness and maritime safety.
        </div>
        <br>
        <img src="data:image/png;base64,{img_base64}" alt="Rocket Launch">
    </div>
    """, unsafe_allow_html=True)

#st.markdown("<br>", unsafe_allow_html=True)
st.divider()
# ---------------- RESOURCES ----------------
st.markdown("<h3 style='text-align: center; margin-bottom: 1.8rem;'>Resources</h3>", unsafe_allow_html=True)
kpis = [
    (
        "menu_book", 
        "Knowledge Base", 
        "Learn about SSA concepts, features, and how to use the dashboard", 
        "knowledge",
        "#0b1f3a"
    ),
    (
        "help", 
        "FAQs", 
        "Find answers to commonly asked questions about the dashboard", 
        "faqs",
        "#0e2a57"
    ),
    (
        "forum", 
        "More Questions & Feedback", 
        "Have more questions or suggestions? Reach out to the team", 
        "feedback",
        "#123a7a"
    ),
]

cols = st.columns(3, gap="large")
for col, (icon, title, desc, page_path, color) in zip(cols, kpis):
    with col:
        kpi_card(icon, title, desc, page_path, color)

# ---------------- FOOTER ----------------
st.markdown("""
<div class="footer">
    © 2026 Philippine Space Agency • SSA Dashboard<br>
    For access, contact us at smcod@philsa.gov.ph<br><br>
    <span style="font-size: 0.85rem; opacity: 0.85;">
        Compiled by the Space Mission Control and Operations Division (SMCOD) of the Philippine Space Agency (PhilSA)
    </span>
</div>
""", unsafe_allow_html=True)