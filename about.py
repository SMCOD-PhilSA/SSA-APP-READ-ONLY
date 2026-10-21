import streamlit as st

st.set_page_config(
    page_title="SSA - About",
    page_icon=":material/info:",
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

# ---------------- MAIN CONTENT ----------------
st.title("About This Application")

st.markdown("""
**PhilSA SSA App** is a real-time monitoring and decision-support tool developed to support the Philippine Space Agency's **Space Situational Awareness (SSA) operations**.

It provides the Agency with consolidated, near real-time information on orbital safety, re-entry risks, launch activities, and space weather conditions to enhance operational monitoring, assessment, and decision-making. In the future, the platform may be expanded to serve researchers, academic institutions, industry partners, and other stakeholders in the Philippine space sector.
""")

st.divider()

col1, col2 = st.columns([2, 1])

with col1:
    st.subheader("What is Space Situational Awareness?")
    st.write("""
    **Space Situational Awareness (SSA)** is the knowledge and characterization of objects and events in outer space. 
    It involves detecting, tracking, and predicting the behavior of satellites, debris, and other objects orbiting Earth 
    to prevent collisions, assess risks, and support safe space operations.
    """)

    st.subheader("Key Features")
    st.markdown("""
    - **Conjunction Analysis**: Monitor close approaches and collision risks (CDM events)
    - **Re-entry Monitoring**: Track objects predicted to re-enter Earth's atmosphere
    - **Rocket Launch Tracker**: Stay updated with upcoming and recent launches of neighboring countries
    - **Space Weather**: Real-time geomagnetic storm (Kp Index) forecasts
    """)

with col2:
    st.subheader("Data Sources")
    st.markdown("""
    - Space-Track.org
    - NOAA Space Weather Prediction Center (SWPC)
    - NASASpaceFlight.com
    - Civil Aviation Authority of the Philippines (CAAP)
    """)

    st.caption("Note: This is a demonstration dashboard with mock/real-time integrated data.")

st.divider()

st.subheader("Purpose")
st.write("""
This platform aims to make complex space domain awareness information more accessible and actionable — 
especially for institutions and researchers in the Philippines and the Asia-Pacific region.
""")

st.markdown("""
**Developed as part of initiatives supporting the Philippine Space Agency (PhilSA)**  
*Promoting responsible and sustainable use of outer space.*
""")

st.divider()

# Footer
st.markdown("""
<div style="text-align: center; color: #888; padding: 20px 0;">
    © 2026 Philippine Space Agency • SSA App<br>
    For demonstration purposes
</div>
""", unsafe_allow_html=True)

st.caption("Compiled by the Space Mission Control and Operations Division (SMCOD) of Philippine Space Agency (PhilSA)")