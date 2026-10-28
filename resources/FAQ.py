import streamlit as st

st.set_page_config(
    page_title="FAQs",
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

st.subheader("Frequently Asked Questions (FAQs)")