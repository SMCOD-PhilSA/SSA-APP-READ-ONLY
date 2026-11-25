import streamlit as st
import base64
from pathlib import Path

# ---------------- REMOVE TOP PADDING ----------------
st.markdown("""
<style>
    .block-container {
        padding-top: 4.8rem !important;
    }
</style>
""", unsafe_allow_html=True)

# Adjust this path to your actual image location
IMAGE_PATH = Path(__file__).parent / "utils" / "d2-1.jpg"   # or .jpg

def get_base64_image(image_path: Path) -> str:
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode()

def require_login():
    """
    Google login gate + optional email allowlist.
    Stops the app if the user is not logged in or not allowed.
    Returns the logged-in user's email.
    """

    def login_screen():
        # Load image once
        try:
            img_b64 = get_base64_image(IMAGE_PATH)
            img_mime = "image/png" if IMAGE_PATH.suffix.lower() == ".png" else "image/jpeg"
        except Exception:
            img_b64 = None

        st.markdown("""
        <style>
            .login-card {
                background: linear-gradient(145deg, #0b1f3a, #0e2a57);
                border: 1px solid rgba(255,255,255,0.08);
                border-radius: 16px;
                margin: 3.5rem auto 0 auto;
                padding: 2.6rem 2rem 2.2rem 2rem;
                box-shadow: 0 8px 28px rgba(0,0,0,0.25);
                margin-bottom: 1.8rem;
                text-align: center;
            }

            .login-title {
                font-size: 1.55rem;
                font-weight: 700;
                color: white;
                margin-bottom: 0.5rem;
                line-height: 1.3;
            }

            .login-sub {
                font-size: 0.95rem;
                color: rgba(255,255,255,0.75);
                margin-bottom: 0.5rem;
                line-height: 1.55;
            }

            .login-image-wrap {
                border-radius: 16px;
                overflow: hidden;
                box-shadow: 0 8px 28px rgba(0,0,0,0.25);
                margin-top: 1rem;
                margin-bottom: 1.8rem;
            }

            .login-image-wrap img {
                width: 100%;
                height: auto;
                display: block;
                max-height: 280px;
                object-fit: cover;
            }

            .login-footer {
                text-align: center;
                color: #888;
                padding: 1.25rem 0 0.5rem 0;
                font-size: 0.9rem;
                border-top: 1px solid rgba(128,128,128,0.18);
                margin-top: 1.5rem;
            }

            div[data-testid="stButton"] {
                margin-bottom: 0 !important;
        </style>
        """, unsafe_allow_html=True)

        # ===== 3 COLUMNS (content only in the middle) =====
        left, mid, right = st.columns([1.1, 2, 1.1])   # adjust these numbers to control width

        with mid:
            # Login Card
            st.markdown("""
            <div class="login-card">
                <div class="login-title">Space Situational Awareness (SSA) Dashboard</div>
                <div class="login-sub">
                    Restricted access for authorized users.<br>
                    Sign in with your Google account to continue.
                </div>
            </div>
            """, unsafe_allow_html=True)

            # Button
            st.button(
                "Log in with Google",
                on_click=st.login,
                type="primary",
                use_container_width=True,
            )

            #st.markdown("<br>", unsafe_allow_html=True)
            #st.space

            # Image
            if img_b64:
                st.markdown(f"""
                <div class="login-image-wrap">
                    <img src="data:{img_mime};base64,{img_b64}" alt="SSA Dashboard">
                </div>
                """, unsafe_allow_html=True)
            else:
                st.info("Login image not found")

            st.markdown("""
            <div class="login-footer">
                © 2026 Philippine Space Agency • SSA Dashboard<br>
                For access, contact us at smcod@philsa.gov.ph<br><br>
                <span style="font-size: 0.85rem; opacity: 0.85;">
                    Compiled by the Space Mission Control and Operations Division (SMCOD) of the Philippine Space Agency (PhilSA)
                </span>
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