import streamlit as st

def require_login():
    """
    Google login gate + optional email allowlist.
    Stops the app if the user is not logged in or not allowed.
    Returns the logged-in user's email.
    """

    def login_screen():
        st.markdown("""
        <style>
            .login-wrap {
                max-width: 420px;
                margin: 4rem auto 0 auto;
                text-align: center;
            }
            .login-card {
                background: linear-gradient(145deg, #0b1f3a, #0e2a57);
                border: 1px solid rgba(255,255,255,0.08);
                border-radius: 16px;
                padding: 2.5rem 2rem 2rem 2rem;
                box-shadow: 0 8px 28px rgba(0,0,0,0.25);
            }
            .login-title {
                font-size: 1.6rem;
                font-weight: 700;
                color: white;
                margin-bottom: 0.4rem;
            }
            .login-sub {
                font-size: 0.95rem;
                color: rgba(255,255,255,0.75);
                margin-bottom: 1.75rem;
                line-height: 1.5;
            }
            .login-footer {
                margin-top: 1.25rem;
                font-size: 0.8rem;
                color: rgba(255,255,255,0.45);
            }
            /* Center the login button column */
            div[data-testid="stVerticalBlock"] > div:has(button) {
                text-align: center;
            }
        </style>
        """, unsafe_allow_html=True)

        st.markdown("""
        <div class="login-wrap">
            <div class="login-card">
                <div class="login-title">PhilSA SSA Dashboard</div>
                <div class="login-sub">
                    Restricted access for authorized users.<br>
                    Sign in with your Google account to continue.
                </div>
            </div>
        </div>
        """, unsafe_allow_html=True)

        # Centered button under the card
        col1, col2, col3 = st.columns([1, 1.2, 1])
        with col2:
            st.markdown("<br>", unsafe_allow_html=True)
            st.button(
                "Log in with Google",
                on_click=st.login,
                type="primary",
                use_container_width=True
            )

        st.markdown("""
        <div class="login-wrap">
            <div class="login-footer">
                Philippine Space Agency · SMCOD
            </div>
        </div>
        """, unsafe_allow_html=True)

    # Not logged in
    if not st.user.is_logged_in:
        login_screen()
        st.stop()

    user_email = (st.user.email or "").lower().strip()

    # Optional allowlist
    allowed = [
        e.lower().strip()
        for e in st.secrets.get("allowed_emails", [])
    ]

    if allowed and user_email not in allowed:
        st.markdown("""
        <div style="max-width:420px;margin:4rem auto;text-align:center;">
            <div style="background:#1a1a2e;border-radius:16px;padding:2rem;border:1px solid rgba(255,80,80,0.3);">
                <div style="font-size:1.3rem;font-weight:650;color:#ff6b6b;margin-bottom:0.5rem;">
                    Access Denied
                </div>
                <div style="color:rgba(255,255,255,0.75);font-size:0.95rem;line-height:1.5;">
                    Your Google account is not authorized for this app.<br>
                    Contact SMCOD to request access.
                </div>
            </div>
        </div>
        """, unsafe_allow_html=True)

        col1, col2, col3 = st.columns([1, 1.2, 1])
        with col2:
            st.button("Log out", on_click=st.logout, use_container_width=True)

        st.stop()

    return user_email

def show_user_sidebar():
    """Show signed-in user + logout in the sidebar."""
    with st.sidebar:
        name = st.user.name or st.user.email or "User"
        st.caption(f"Signed in as: {name}")
        st.button("Log out", on_click=st.logout)