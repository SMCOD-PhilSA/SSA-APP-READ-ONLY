import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
import requests
import json

st.set_page_config(
    page_title="SSA Dashboard - Feedback",
    page_icon=":material/forum:",
    layout="wide"
)

# ---------------- REMOVE TOP PADDING ----------------
st.markdown("""
<style>
    .block-container {
        padding-top: 4.8rem !important;
    }
    .footer {
        text-align: center;
        color: #888;
        padding: 1.25rem 0 0.5rem 0;
        font-size: 0.9rem;
        border-top: 1px solid rgba(128,128,128,0.18);
        margin-top: 1.5rem;
    }
</style>
""", unsafe_allow_html=True)

# ---------------- GOOGLE SHEETS CONNECTION ----------------
def connect_to_sheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    
    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account_feedback"],
        scopes=scopes
    )
    
    client = gspread.authorize(creds)
    spreadsheet = client.open("SSA Feedback")
    sheet = spreadsheet.worksheet("Dashboard")
    return sheet

def get_next_ticket_number(sheet):
    """
    Generate next ticket number in format P-SSA-XXX
    """
    try:
        records = sheet.get_all_records()
        
        if not records:
            return "P-SSA-001"
        
        ticket_numbers = []
        for row in records:
            ticket = str(row.get("Ticket", "")).strip()
            if ticket.startswith("P-SSA-"):
                try:
                    num = int(ticket.replace("P-SSA-", ""))
                    ticket_numbers.append(num)
                except:
                    continue
        
        next_num = max(ticket_numbers) + 1 if ticket_numbers else 1
        return f"P-SSA-{next_num:03d}"
    
    except Exception:
        return "P-SSA-001"

# ---------------- TEAMS WEBHOOK ----------------
def send_feedback_to_teams(ticket_no, date, name, email, agency, message, status="OPEN"):
    """
    Posts feedback details to a Microsoft Teams channel via Incoming Webhook.
    """
    webhook_url = st.secrets["TEAMS_WEBHOOK_URL"]
    
    payload = {
        "@type": "MessageCard",
        "@context": "https://schema.org/extensions",
        "summary": f"New SSA Feedback Ticket: {ticket_no}",
        "themeColor": "0078D4",
        "title": f"🎫 New Feedback Ticket — {ticket_no}",
        "sections": [
            {
                "facts": [
                    {"name": "Ticket", "value": ticket_no},
                    {"name": "Date", "value": date},
                    {"name": "Name", "value": name or "—"},
                    {"name": "Email", "value": email or "—"},
                    {"name": "Agency", "value": agency or "—"},
                    {"name": "Status", "value": status},
                ],
                "text": f"**Message:**\n\n{message}"
            }
        ]
    }
    
    response = requests.post(
        webhook_url,
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload),
        timeout=10
    )
    
    if response.status_code not in (200, 201, 202):
        raise Exception(f"Teams webhook failed: {response.status_code} — {response.text}")
    
    return True

# ---------------- PAGE UI ----------------
st.markdown("Have questions or suggestions about the SSA Dashboard? Submit a ticket below.")

with st.form("feedback_form", clear_on_submit=True):
    col1, col2 = st.columns(2)
    
    with col1:
        name = st.text_input("Name")
        email = st.text_input("Email")
    
    with col2:
        agency = st.text_input("Agency / Organization")
    
    message = st.text_area("Your question or feedback", height=150)
    
    submitted = st.form_submit_button("Submit Ticket", type="primary")
    
    if submitted:
        if not message.strip():
            st.warning("Please enter a message before submitting.")
        else:
            try:
                sheet = connect_to_sheet()
                
                # Generate ticket number
                ticket_no = get_next_ticket_number(sheet)
                date_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                
                # Ticket | Date | Name | Email | Agency | Message | Urgency | Status
                row = [
                    ticket_no,           # Ticket
                    date_now,            # Date
                    name.strip(),        # Name
                    email.strip(),       # Email
                    agency.strip(),      # Agency
                    message.strip(),     # Message
                    "",                  # Urgency (developer field)
                    "OPEN"               # Status (default)
                ]
                
                # 1. Save to Google Sheet
                sheet.append_row(row)
                
                # 2. Post to Teams
                send_feedback_to_teams(
                    ticket_no=ticket_no,
                    date=date_now,
                    name=name.strip(),
                    email=email.strip(),
                    agency=agency.strip(),
                    message=message.strip(),
                    status="OPEN"
                )
                
                st.success("Ticket submitted successfully!")
                st.info(f"**Your Ticket Number:** `{ticket_no}`")
                st.markdown("SMCOD will reach out to you regarding this ticket.")
                
            except Exception as e:
                st.error(f"Failed to submit ticket. Error: {e}")

# ---------------- FOOTER ----------------
st.markdown("""
<div class="footer">
    © 2026 Philippine Space Agency • SSA Dashboard<br>
    For access, contact us at smcod@philsa.gov.ph<br><br>
    <span style="font-size: 0.85rem; opacity: 0.85;">
        Compiled by the Space Mission Control and Operations Division (SMCOD) of the Philippine Space Agency (PhilSA)
    </span>
</div>
""", unsafe_allow_html=True)