import streamlit as st
import requests
from datetime import datetime

st.set_page_config(page_title="AI Ticket Generator", page_icon="🎫", layout="wide")

BACKEND_URL = "http://localhost:8000"

# ---------------- SESSION STATE ----------------
DEFAULTS = {
    "chat": [],
    "context": {},
    "ui_action": None,
    "user_choices": [],
    "ticket_history": [],
    "processing": False,
    "_pending_prompt": "",
    "options": [],
}

for k, v in DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ---------------- BACKEND CALL ----------------
def send_to_backend(prompt: str):
    try:
        response = requests.post(
            f"{BACKEND_URL}/create-ticket",
            json={
                "prompt": prompt,
                "context": st.session_state.context
            }
        ).json()
    except Exception as e:
        response = {"status": "failure", "message": str(e)}

    handle_backend_response(response)


def handle_backend_response(response):
    status = response.get("status")

    st.session_state.processing = False
    st.session_state.user_choices = []
    st.session_state.ui_action = None

    if status in ["incomplete", "select_user"]:
        st.session_state.context = response.get("context", {})
        st.session_state.chat.append({
            "role": "assistant",
            "content": response["message"]
        })

        st.session_state.ui_action = response.get("ui_action")
        st.session_state.options = response.get("options", [])
        if status == "select_user":
            st.session_state.user_choices = response.get("users", [])

    elif status == "success":
        req = response.get("request_number", "N/A")
        ritm = response.get("ritm_number", "N/A")

        st.session_state.chat.append({
            "role": "assistant",
            "content": f"✅ Ticket Created\n\nRequest: `{req}`\nRITM: `{ritm}`"
        })

        st.session_state.ticket_history.append({
            "request_number": req,
            "ritm": ritm,
            "time": datetime.now().strftime("%d %b %Y, %H:%M")
        })

        st.session_state.context = {}
        st.balloons()

    elif status == "reset":
        st.session_state.chat.append({
            "role": "assistant",
            "content": response.get("message")
        })
        st.session_state.context = {}

    else:
        st.session_state.chat.append({
            "role": "assistant",
            "content": f"❌ {response.get('message')}"
        })


# ====================================================
# SIDEBAR
# ====================================================
with st.sidebar:
    st.markdown("### 🗂️ Ticket History")

    if not st.session_state.ticket_history:
        st.caption("No tickets yet.")
    else:
        for t in reversed(st.session_state.ticket_history):
            with st.expander(f"🎫 {t['ritm']}"):
                st.write(f"Request: {t['request_number']}")
                st.write(f"Time: {t['time']}")

    if st.button("🗑️ Clear"):
        st.session_state.ticket_history = []
        st.rerun()


# ====================================================
# MAIN UI
# ====================================================
st.title("👽 Hi There!")
st.caption("Let me help you create ServiceNow tickets!")
st.divider()

# ---------------- CHAT ----------------
for msg in st.session_state.chat:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])


# ---------------- PROCESSING ----------------
if st.session_state.processing:
    with st.chat_message("assistant"):
        st.write("⏳ Thinking...")

    send_to_backend(st.session_state["_pending_prompt"])
    st.session_state["_pending_prompt"] = ""
    st.rerun()


# ====================================================
# SMART UI CONTROLS
# ====================================================

# ---------- USER SELECTION ----------
if st.session_state.user_choices:
    with st.chat_message("assistant"):
        st.write("👤 Select the correct user:")

        for user in st.session_state.user_choices:
            if st.button(
                f"{user['name']} ({user['email']})",
                key=user["sys_id"],
                use_container_width=True,
                disabled=st.session_state.processing
            ):
                # ✅ Store directly
                st.session_state.context.update({
                    "recipient_sys_id": user["sys_id"],
                    "recipient_name": user["name"],
                    "recipient_email": user["email"]
                })

                # ✅ Show immediate confirmation
                st.session_state.chat.append({
                    "role": "assistant",
                    "content": f"✅ Selected: **{user['name']}**"
                })

                st.session_state.user_choices = []
                st.session_state.processing = True

                # 🔥 IMPORTANT FIX: send meaningful signal
                st.session_state["_pending_prompt"] = user["name"]

                st.rerun()


# ---------- SELECT OPTIONS ----------
if st.session_state.options:
    with st.chat_message("assistant"):
        st.write("**Select an option:**")
        for option in st.session_state.options:
            if st.button(
                option,
                key=f"option_{option}",
                use_container_width=True,
                disabled=st.session_state.processing
            ):
                st.session_state.chat.append({
                    "role": "user",
                    "content": option
                })

                st.session_state.options = []
                st.session_state.processing = True
                st.session_state["_pending_prompt"] = option

                st.rerun()


# ---------- CONFIRM ----------
if st.session_state.ui_action == "show_confirm_buttons":
    with st.chat_message("assistant"):
        col1, col2 = st.columns(2)

        if col1.button("✅ Confirm", use_container_width=True):
            st.session_state.chat.append({"role": "user", "content": "yes"})
            st.session_state.processing = True
            st.session_state["_pending_prompt"] = "yes"
            st.rerun()

        if col2.button("❌ Cancel", use_container_width=True):
            st.session_state.chat.append({"role": "user", "content": "no"})
            st.session_state.processing = True
            st.session_state["_pending_prompt"] = "no"
            st.rerun()


# ====================================================
# INPUT
# ====================================================
user_input = st.chat_input("Type your request...")

if user_input and not st.session_state.processing:
    st.session_state.chat.append({"role": "user", "content": user_input})
    st.session_state.processing = True
    st.session_state["_pending_prompt"] = user_input
    st.rerun()