import streamlit as st
from streamlit_option_menu import option_menu

st.set_page_config(
    page_title="Documentation - Knowledge Base",
    page_icon=":material/menu_book:",
    layout="wide"
)


#---------------- REMOVE TOP PADDING ----------------
st.markdown("""
<style>
    .block-container {
        padding-top: 4.8rem !important;
    }
</style>
""", unsafe_allow_html=True)

# # ---------------- CUSTOM STYLING ----------------
# st.markdown("""
# <style>
#     .block-container {
#         padding-top: 4.8rem !important;
#     }
    
# .page-title {
#         font-size: 2rem;
#         font-weight: 700;
#         margin-bottom: 0.3rem;
#     }
    
#     .page-subtitle {
#         font-size: 1.1rem;
#         opacity: 0.85;
#         margin-bottom: 1.8rem;
#     }
    
#     /* ===== Clickable KPI Cards (same style as About page) ===== */
#     .kpi-link {
#         text-decoration: none !important;
#         color: inherit !important;
#     }
    
#     .kpi-card {
#         border-radius: 14px;
#         padding: 1.6rem 1.3rem;
#         height: 100%;
#         color: white;
#         transition: transform 0.18s ease, box-shadow 0.18s ease;
#         box-shadow: 0 4px 14px rgba(0,0,0,0.18);
#         text-align: center;
#         cursor: pointer;
#     }
    
#     .kpi-card:hover {
#         transform: translateY(-5px);
#         box-shadow: 0 10px 24px rgba(0,0,0,0.28);
#     }
    
#     .kpi-card .material-symbols-outlined {
#         font-size: 2.6rem;
#         margin-bottom: 0.7rem;
#         display: block;
#     }
    
#     .kpi-title {
#         font-size: 1.25rem;
#         font-weight: 650;
#         margin-bottom: 0.45rem;
#     }
    
#     .kpi-desc {
#         font-size: 0.92rem;
#         line-height: 1.45;
#         opacity: 0.9;
#     }
    
#     .footer {
#         text-align: center;
#         color: #888;
#         padding: 1.5rem 0 0.5rem 0;
#         font-size: 0.9rem;
#         border-top: 1px solid rgba(128,128,128,0.18);
#         margin-top: 2.5rem;
#     }
# </style>

# <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@24,400,0,0" />
# """, unsafe_allow_html=True)

# def kpi_card(icon, title, description, page_path, color):
#     st.markdown(f"""
#     <a href="/{page_path}" target="_self" class="kpi-link">
#         <div class="kpi-card" style="background: {color};">
#             <span class="material-symbols-outlined">{icon}</span>
#             <div class="kpi-title">{title}</div>
#             <div class="kpi-desc">{description}</div>
#         </div>
#     </a>
#     """, unsafe_allow_html=True)

# # ---------------- HEADER ----------------
# st.markdown('<div class="page-title">📖 Documentation</div>', unsafe_allow_html=True)
# st.markdown('<div class="page-subtitle">How to use the PhilSA SSA Dashboard</div>', unsafe_allow_html=True)

# # ---------------- KPI CARDS ----------------
# kpis = [
#     (
#         "menu_book", 
#         "Knowledge Base", 
#         "Learn about SSA concepts, features, and how to use the dashboard", 
#         "resources/knowledge.py",      # ← change to your actual page name
#         "#0b1f3a"
#     ),
#     (
#         "help", 
#         "FAQs", 
#         "Find answers to commonly asked questions about the platform", 
#         "resources/faq.py",                # ← change to your actual page name
#         "#0e2a57"
#     ),
#     (
#         "forum", 
#         "More Questions & Feedback", 
#         "Have more questions or suggestions? Reach out to the team", 
#         "resources/feedback.py",            # ← change to your actual page name
#         "#123a7a"
#     ),
# ]

# cols = st.columns(3, gap="large")

# for col, (icon, title, desc, page_path, color) in zip(cols, kpis):
#     with col:
#         kpi_card(icon, title, desc, page_path, color)

# #knowledge base

# # ---------------- HEADER ----------------
# st.markdown('<div class="page-title">📚 Knowledge Base</div>', unsafe_allow_html=True)
# st.markdown('<div class="page-subtitle">Guides and how-to resources for the PhilSA SSA Dashboard</div>', unsafe_allow_html=True)

# ---------------- OPTION MENU ----------------
selected_option = option_menu(
    menu_title=None,
    options=["SSA Dashboard", "Re-entry", "Rocket Launch"],
    icons=["bi bi-bar-chart-fill", "bi bi-fire", "bi bi-rocket-takeoff-fill"],
    menu_icon="cast",
    default_index=0,
    orientation="horizontal",
)

st.markdown("<br>", unsafe_allow_html=True)

# ---------------- CONTENT PER TAB ----------------

# ========== SSA DASHBOARD ==========
if selected_option == "SSA Dashboard":
    st.subheader("SSA Dashboard Guides")
    
    col1, col2 = st.columns(2, gap="large")
    
    with col1:
        with st.popover("How to navigate the SSA Dashboard", use_container_width=True):
            st.markdown("""
            ### Navigating the Dashboard
            - Use the **sidebar** to switch between modules (Conjunction, Re-entry, Rocket Launch, Space Weather).
            - The top navigation allows quick access to About, Documentation, and Knowledge Base.
            - Each module has interactive maps, tables, and filters.
            """)
        
        with st.popover("How to track a sovereign satellite", use_container_width=True):
            st.markdown("""
            ### Tracking a Sovereign Satellite
            1. Go to the **Conjunction** or **Re-entry** module.
            2. Use the search/filter bar and type the satellite name or NORAD ID.
            3. Select the object from the results.
            4. View its current status, close approaches, and predicted events.
            """)
    
    with col2:
        with st.popover("Understanding the main metrics", use_container_width=True):
            st.markdown("""
            ### Key Metrics Explained
            - **Pc (Probability of Collision)** — Likelihood of two objects colliding.
            - **TCA (Time of Closest Approach)** — When the closest approach will occur.
            - **Miss Distance** — Minimum distance between objects at TCA.
            """)
        
        with st.popover("How to interpret alerts", use_container_width=True):
            st.markdown("""
            ### Alert Levels
            - 🟢 **Low** — Routine monitoring
            - 🟡 **Medium** — Elevated risk, continue observation
            - 🔴 **High** — Requires attention / possible mitigation
            """)

# ========== RE-ENTRY ==========
elif selected_option == "Re-entry":
    st.subheader("Re-entry Monitoring Guides")
    
    col1, col2 = st.columns(2, gap="large")
    
    with col1:
        with st.popover("How to monitor re-entry events", use_container_width=True):
            st.markdown("""
            ### Monitoring Re-entries
            1. Open the **Re-entry** module.
            2. View the list of objects predicted to re-enter.
            3. Sort by predicted re-entry time or risk level.
            4. Click an object to see detailed prediction windows.
            """)
        
        with st.popover("Understanding re-entry prediction windows", use_container_width=True):
            st.markdown("""
            ### Prediction Uncertainty
            Re-entry predictions have uncertainty windows.
            - Early predictions may have ±1 day uncertainty.
            - Closer to re-entry (last 24–48 hrs), accuracy improves significantly.
            """)
    
    with col2:
        with st.popover("What objects are tracked for re-entry?", use_container_width=True):
            st.markdown("""
            ### Tracked Objects
            - Rocket bodies
            - Defunct satellites
            - Large debris objects
            - Objects with predicted decay within the selected time horizon
            """)
        
        with st.popover("How accurate are re-entry forecasts?", use_container_width=True):
            st.markdown("""
            ### Accuracy Notes
            Re-entry forecasts depend on atmospheric density models and object characteristics.
            Accuracy is generally good within the final 1–2 days before re-entry.
            """)

# ========== ROCKET LAUNCH ==========
elif selected_option == "Rocket Launch":
    st.subheader("Rocket Launch Tracker Guides")
    
    col1, col2 = st.columns(2, gap="large")
    
    with col1:
        with st.popover("How to track upcoming launches", use_container_width=True):
            st.markdown("""
            ### Tracking Launches
            1. Go to the **Rocket Launch** module.
            2. View the list of upcoming and recent launches.
            3. Filter by country, launch provider, or date range.
            4. Click a launch for detailed information (vehicle, pad, payload).
            """)
        
        with st.popover("Which launches are shown?", use_container_width=True):
            st.markdown("""
            ### Launch Coverage
            The tracker focuses on launches relevant to the region, including:
            - Neighboring countries
            - Major global launches that may affect the local space environment
            """)
    
    with col2:
        with st.popover("Understanding launch status", use_container_width=True):
            st.markdown("""
            ### Launch Status Labels
            - **Scheduled** — Confirmed date/time
            - **TBD** — Date not yet finalized
            - **Success / Failure** — Outcome of completed launches
            - **Hold / Delay** — Temporarily postponed
            """)
        
        with st.popover("How often is launch data updated?", use_container_width=True):
            st.markdown("""
            ### Data Freshness
            Launch information is updated regularly from public sources such as NASASpaceFlight and Spaceflight Now.
            """)



# ---------------- FOOTER ----------------
st.markdown("""
<div class="footer">
    © 2026 Philippine Space Agency • SSA Dashboard<br>
    Documentation v0.1
</div>
""", unsafe_allow_html=True)