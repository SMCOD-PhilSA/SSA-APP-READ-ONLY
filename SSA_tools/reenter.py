import streamlit as st
from streamlit_option_menu import option_menu

from services.reentry import reentry_event_analyzer
from services.reentry import reentry_event_predictor
from services.reentry import reentry_dashboard


def render():
    st.markdown("""
    <style>
        .block-container { padding-top: 4.8rem !important; }

        div[data-baseweb="select"] > div {
            background-color: var(--background-color) !important;
            color: var(--text-color) !important;
            border: 1px solid rgba(128,128,128,0.35) !important;
        }
        div[data-baseweb="select"] > div:focus-within {
            border-color: var(--primary-color) !important;
            box-shadow: 0 0 0 1px var(--primary-color) !important;
        }
        div[data-baseweb="select"] span { color: var(--text-color) !important; }
        div[data-baseweb="popover"] > div,
        div[data-baseweb="menu"] {
            background-color: var(--background-color) !important;
            border: 1px solid rgba(128,128,128,0.35) !important;
        }
        ul[role="listbox"] {
            background-color: var(--background-color) !important;
            color: var(--text-color) !important;
        }
        li[role="option"] {
            background-color: var(--background-color) !important;
            color: var(--text-color) !important;
        }
        li[role="option"]:hover { background-color: var(--secondary-background-color) !important; }
        li[aria-selected="true"] {
            background-color: var(--primary-color) !important;
            color: #ffffff !important;
        }
        div[data-testid="stRadio"] label p { color: var(--text-color) !important; }
        div[data-testid="stTextInput"] input {
            background-color: var(--background-color) !important;
            color: var(--text-color) !important;
            border: 1px solid rgba(128,128,128,0.35) !important;
        }
        div[data-testid="stTextInput"] input::placeholder {
            color: rgba(128,128,128,0.6) !important;
        }
        div[data-testid="stNumberInput"] input {
            background-color: var(--background-color) !important;
            color: var(--text-color) !important;
            border: 1px solid rgba(128,128,128,0.35) !important;
        }
    </style>
    """, unsafe_allow_html=True)

    # ── Option menu ───────────────────────────────────────────────────────────
    _re_tabs = ["Re-entry Dashboard", "Analyzer", "Predictor"]

    # Deep-link support: /reenter?tab=dashboard|analyzer|predictor
    # (dashboard.py's "Analyze Re-entry" / "Predict Re-entry" quick-action
    # tiles link here with ?tab=analyzer / ?tab=predictor so each tile opens
    # the right sub-tab instead of whatever tab happens to be selected.)
    _TAB_PARAM_MAP = {
        "dashboard": "Re-entry Dashboard",
        "analyzer" : "Analyzer",
        "predictor": "Predictor",
    }
    _tab_param = st.query_params.get("tab")
    if _tab_param in _TAB_PARAM_MAP:
        st.session_state.reentry_selected = _TAB_PARAM_MAP[_tab_param]
        st.query_params.pop("tab", None)   # one-time use — don't keep forcing this tab

    _re_default = _re_tabs.index(st.session_state.get("reentry_selected", "Analyzer")) \
                  if st.session_state.get("reentry_selected") in _re_tabs else 1

    selected_option = option_menu(
        menu_title=None,
        options=_re_tabs,
        icons=["bi bi-bar-chart-fill", "bi bi-search", "bi bi-graph-up-arrow"],
        menu_icon="cast",
        default_index=_re_default,
        orientation="horizontal",
    )

    if "reentry_selected" not in st.session_state:
        st.session_state.reentry_selected = "Re-entry Dashboard"
    if selected_option != st.session_state.reentry_selected:
        st.session_state.reentry_selected = selected_option
        st.rerun()

    st.set_page_config(page_title="SSA - Re-entry", page_icon=":material/local_fire_department:", layout="wide")

    # ── Section routing ───────────────────────────────────────────────────────
    if st.session_state.reentry_selected == "Re-entry Dashboard":
        st.subheader("Reentry Dashboard")
        try:
            reentry_dashboard.render()
        except Exception as e:
            st.error("Dashboard failed to load.")
            st.exception(e)

    elif st.session_state.reentry_selected == "Analyzer":
        st.subheader("Reentry Event Analyzer")
        reentry_event_analyzer.render()

    elif st.session_state.reentry_selected == "Predictor":
        st.subheader("Reentry Prediction")
        reentry_event_predictor.render()

    st.caption("Compiled by the Space Mission Control and Operations Division (SMCOD) of the Philippine Space Agency (PhilSA)")


render()