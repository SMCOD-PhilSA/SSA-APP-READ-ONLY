import os
import streamlit as st
import msal
import requests
from typing import Optional, Dict, Any

# ====================== AZURE CONFIG ======================
def _secret(key: str) -> str:
    """Get secret from Streamlit secrets or environment variables."""
    try:
        return st.secrets["azure"][key]
    except (KeyError, FileNotFoundError):
        value = os.getenv(key.upper())
        if not value:
            st.error(f"Missing Azure credential: {key}")
            st.stop()
        return value


CLIENT_ID = _secret("client_id")
CLIENT_SECRET = _secret("client_secret")
TENANT_ID = _secret("tenant_id")
REDIRECT_URI = _secret("redirect_uri")
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPES = ["User.Read"]


# ====================== MSAL CLIENT ======================
@st.cache_resource
def _build_msal_app():
    """Build and cache the MSAL Confidential Client Application."""
    return msal.ConfidentialClientApplication(
        CLIENT_ID,
        authority=AUTHORITY,
        client_credential=CLIENT_SECRET,
    )


# ====================== AUTH FUNCTIONS ======================
def get_auth_url() -> str:
    """Generate Microsoft login URL."""
    return _build_msal_app().get_authorization_request_url(
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )


def exchange_code_for_token(auth_code: str) -> Optional[Dict[str, Any]]:
    """Exchange authorization code for access token."""
    result = _build_msal_app().acquire_token_by_authorization_code(
        auth_code, scopes=SCOPES, redirect_uri=REDIRECT_URI
    )
    if "access_token" in result:
        return result
    else:
        st.error("Failed to acquire token from Microsoft")
        st.error(result.get("error_description", str(result)))
        return None


def get_user_info(access_token: str) -> Optional[Dict[str, Any]]:
    """Fetch user information from Microsoft Graph."""
    resp = requests.get(
        "https://graph.microsoft.com/v1.0/me",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    if resp.status_code == 200:
        return resp.json()
    else:
        st.error(f"Failed to get user info: {resp.status_code}")
        return None


def is_authenticated() -> bool:
    """Check if user is authenticated."""
    return st.session_state.get("authenticated", False)


def logout():
    """Clear authentication session and rerun."""
    for key in ["authenticated", "user", "access_token"]:
        st.session_state.pop(key, None)
    st.query_params.clear()
    st.rerun()

# ====================== LOGIN PAGE UI ======================
def login_page():
    """Display the login page (moved from home.py)."""
    st.markdown("""
    <style>
        [data-testid="stAppViewContainer"] {
            background: linear-gradient(135deg,
                #0a1428 0%,
                #0e224b 45%,
                #1a4fa3 80%,
                #0a1428 100%) !important;
            background-attachment: fixed;
        }
                
        [data-testid="stHeader"] {
            background-color: rgba(14, 34, 75, 0.95) !important;
            backdrop-filter: blur(8px);
        }
        [data-testid="stSidebar"] {
            background-color: rgba(14, 34, 75, 0.98) !important;
        }
                
        .login-content *,
        .login-content h1,
        .login-content p,
        [data-testid="stMarkdown"] {
            color: #ffffff !important;
        }
                
        .login-content * {
            color: #ffffff !important;
        }
                
        .welcome-title {
            font-size: 4.8rem !important;
            font-weight: 700 !important;
            margin-bottom: 1.2rem !important;
            text-shadow: 0 6px 30px rgba(0, 0, 0, 0.7);
            letter-spacing: -0.02em;
            line-height: 1.1;
            color: #ffffff !important;
        }
        .subtitle {
            font-size: 1.45rem;
            font-weight: 500;
            margin-bottom: 3rem;
            line-height: 1.5;
            opacity: 0.95;
        }
        .description {
            font-size: 1.22rem;
            margin-bottom: 3rem;
            opacity: 0.9;
        }
        .login-content .stButton > button {
            height: 4.1rem;
            font-size: 1.32rem;
            font-weight: 600;
            border-radius: 12px;
            background: linear-gradient(90deg, #0078d4, #00a0ff) !important;
            color: white !important;
            box-shadow: 0 10px 30px rgba(0, 120, 212, 0.5);
            border: none !important;
        }
        .login-content .stButton > button:hover {
            background: linear-gradient(90deg, #0066b3, #0090e6) !important;
            transform: translateY(-4px);
            box-shadow: 0 15px 35px rgba(0, 120, 212, 0.6);
        }
        .caption {
            margin-top: 5rem;
            font-size: 1.05rem;
            opacity: 0.85;
        }
        footer { display: none !important; }
    </style>
    """, unsafe_allow_html=True)

    st.logo("utils/PhilSA_v5_White.png", size="large")

    left_col, right_col = st.columns([3, 1], gap="large")
    with right_col:
        st.image("utils/PhilSA_v1_White.png", width=180)

    st.markdown('<div class="login-content">', unsafe_allow_html=True)
    st.markdown('<h1 class="welcome-title">Welcome</h1>', unsafe_allow_html=True)
    st.markdown('<p class="subtitle">PhilSA Space Mission Control and Operations Division (SMCOD)<br>SSA App</p>', unsafe_allow_html=True)
    st.markdown('<p class="description">Sign in with your Microsoft account to continue</p>', unsafe_allow_html=True)

    auth_url = get_auth_url()
    st.link_button(
        "Log in with Microsoft",
        url=auth_url,
        type="primary",
        use_container_width=True
    )

    st.markdown('<p class="caption">Secure authentication powered by Microsoft Entra ID</p>', unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)