import streamlit as st

def require_login():
    """
    Show Google login if needed.
    Optionally enforce allowed_emails from secrets.
    Stops the app if the user is not allowed.
    Returns the logged-in user's email.
    """
    def login_screen():
        st.markdown("### Restricted Access")
        st.caption("Sign in with your Google account to use the PhilSA SSA Dashboard.")
        st.button("Log in with Google", on_click=st.login, type="primary")

    if not st.user.is_logged_in:
        login_screen()
        st.stop()

    user_email = (st.user.email or "").lower().strip()

    # Optional allowlist (mixed domains OK)
    allowed = [
        e.lower().strip()
        for e in st.secrets.get("allowed_emails", [])
    ]

    if allowed and user_email not in allowed:
        st.error("Access denied. Your Google account is not authorized for this app.")
        st.caption("Contact SMCOD (smcod@philsa.gov.ph) to request access.")
        st.button("Log out", on_click=st.logout)
        st.stop()

    return user_email

def show_user_sidebar():
    """Show signed-in user + logout in the sidebar."""
    with st.sidebar:
        name = st.user.name or st.user.email or "User"
        st.caption(f"Signed in as: {name}")
        st.button("Log out", on_click=st.logout)