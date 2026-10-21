import streamlit as st

st.set_page_config(
    page_title="SSA - Documentation",
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

# ---------------- MAIN CONTENT ----------------
st.title("📖 Documentation")

st.markdown("### How to Use the SSA Dashboard")

st.divider()

tab1, tab2, tab3, tab4 = st.tabs(["Overview", "Features", "Data Sources", "FAQ"])

with tab1:
    st.subheader("Application Overview")
    st.write("""
    The **SSA Dashboard** is a comprehensive Space Situational Awareness platform that consolidates 
    critical information about orbital objects, collision risks, re-entries, launches, and space weather.
    """)
    
    st.info("This tool is designed for researchers, space agencies, satellite operators, and space enthusiasts.")

with tab2:
    st.subheader("Core Features")

    col1, col2 = st.columns(2)
    
    with col1:
        st.markdown("#### 🛰️ Conjunction Analysis")
        st.write("""
        - Monitor close approach events (Conjunction Data Messages)
        - View Probability of Collision (Pc)
        - Time to Closest Approach (TCA)
        """)

        st.markdown("#### 🔥 Re-entry Monitoring")
        st.write("""
        - Track objects predicted to re-enter the atmosphere
        - Estimated re-entry date and time
        """)

    with col2:
        st.markdown("#### 🚀 Rocket Launch Tracker")
        st.write("""
        - Upcoming and recent rocket launches
        - Launch provider, vehicle, and site information
        """)

        st.markdown("#### 🌤️ Space Weather")
        st.write("""
        - Geomagnetic Storm (Kp Index) forecast
        - Solar activity insights
        """)

with tab3:
    st.subheader("Data Sources")
    st.markdown("""
    | Module              | Primary Source                          | Update Frequency     |
    |---------------------|-----------------------------------------|----------------------|
    | Conjunctions        | Space-Track.org (CDM)                   | Real-time / Hourly   |
    | Re-entries          | Space-Track.org                         | Daily                |
    | Rocket Launches     | NASASpaceFlight, SpaceflightNow        | Live                 |
    | Space Weather       | NOAA SWPC                               | Real-time            |
    """)

    st.caption("Note: Some sections currently use mock data for demonstration purposes.")

with tab4:
    st.subheader("Frequently Asked Questions")

    with st.expander("What does Probability of Collision (Pc) mean?"):
        st.write("""
        Pc is the estimated probability that two objects in space will collide. 
        Values above **1 in 10,000** (1e-4) are generally considered high risk and require attention.
        """)

    with st.expander("How accurate are the re-entry predictions?"):
        st.write("""
        Re-entry predictions have significant uncertainty. Accuracy improves as the object gets closer to Earth 
        (usually reliable within ± few hours in the last 24–48 hours).
        """)

    with st.expander("How do I interpret the Kp Index?"):
        st.write("""
        - **0–3**: Quiet  
        - **4**: Unsettled  
        - **5–6**: Active (minor storms)  
        - **7+**: Geomagnetic Storm (can affect satellites and power grids)
        """)

st.divider()

st.subheader("Need More Help?")
st.write("Contact the development team or submit an issue on the project repository.")

st.markdown("""
<div style="text-align: center; color: #888; padding: 30px 0;">
    © 2026 Philippine Space Agency • SSA Dashboard<br>
    Documentation v0.1
</div>
""", unsafe_allow_html=True)