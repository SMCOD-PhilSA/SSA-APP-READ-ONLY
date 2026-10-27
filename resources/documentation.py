import streamlit as st

st.set_page_config(
    page_title="SSA - Documentation",
    page_icon=":material/menu_book:",
    layout="wide"
)


# #---------------- REMOVE TOP PADDING ----------------
# st.markdown("""
# <style>
#     .block-container {
#         padding-top: 4.8rem !important;
#     }
# </style>
# """, unsafe_allow_html=True)

# ---------------- CUSTOM STYLING ----------------
st.markdown("""
<style>
    .block-container {
        padding-top: 4.8rem !important;
    }
    
.page-title {
        font-size: 2rem;
        font-weight: 700;
        margin-bottom: 0.3rem;
    }
    
    .page-subtitle {
        font-size: 1.1rem;
        opacity: 0.85;
        margin-bottom: 1.8rem;
    }
    
    /* ===== Clickable KPI Cards (same style as About page) ===== */
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
        padding: 1.5rem 0 0.5rem 0;
        font-size: 0.9rem;
        border-top: 1px solid rgba(128,128,128,0.18);
        margin-top: 2.5rem;
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

# ---------------- HEADER ----------------
st.markdown('<div class="page-title">📖 Documentation</div>', unsafe_allow_html=True)
st.markdown('<div class="page-subtitle">How to use the PhilSA SSA Dashboard</div>', unsafe_allow_html=True)

# ---------------- KPI CARDS ----------------
kpis = [
    (
        "menu_book", 
        "Knowledge Base", 
        "Learn about SSA concepts, features, and how to use the dashboard", 
        "knowledge",      # ← change to your actual page name
        "#0b1f3a"
    ),
    (
        "help", 
        "FAQs", 
        "Find answers to commonly asked questions about the platform", 
        "faqs",                # ← change to your actual page name
        "#0e2a57"
    ),
    (
        "forum", 
        "More Questions & Feedback", 
        "Have more questions or suggestions? Reach out to the team", 
        "feedback",            # ← change to your actual page name
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
    Documentation v0.1
</div>
""", unsafe_allow_html=True)