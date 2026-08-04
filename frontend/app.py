import streamlit as st
import requests
import pandas as pd
from datetime import datetime

st.set_page_config(page_title="AI Ticket Generator", page_icon="🎫", layout="wide")

BACKEND_URL = "http://localhost:8003"

# ----------------------------------------------------------------
# Routing: detect Splunk / log queries vs ServiceNow ticket requests
# ----------------------------------------------------------------

# Explicit log/splunk words — any of these alone → Splunk
_LOG_KEYWORDS = [
    "log", "logs", "splunk", "error log", "search log",
    "show me", "get me", "fetch", "retrieve", "query log",
    "yesterday", "today's log", "last hour", "last 24", "last 7",
    # time-range patterns users type naturally
    "from", "between", "to time", "at time",
    # outage / health check phrases
    "outage", "any outage", "check outage", "was there an outage",
    "is there an outage", "service down", "check for outage",
]

# Investigative verbs — when paired with an app/index context → Splunk
_INVESTIGATE_VERBS = [
    "check", "find", "search", "look", "detect", "any", "was there",
    "were there", "did", "is there", "are there", "show", "list",
    "occurred", "happening", "happened", "trace", "monitor",
]

# Events that only make sense in logs, not tickets
_LOG_EVENTS = [
    "account lock", "locked", "login fail", "failed login", "exception",
    "error", "crash", "timeout", "latency", "slow", "spike", "outage",
    "alert", "warning", "trace", "debug", "stack trace", "unauthori",
    "forbidden", "500", "404", "connection refused", "memory", "cpu",
    "authentication fail", "auth fail", "password", "brute force",
    "service down", "service unavailable",
]

def _is_log_request(text: str) -> bool:
    pl = text.lower()
    # Direct log keywords
    if any(kw in pl for kw in _LOG_KEYWORDS):
        return True
    # Investigative verb + log event → clearly a log query
    has_verb  = any(v in pl for v in _INVESTIGATE_VERBS)
    has_event = any(e in pl for e in _LOG_EVENTS)
    if has_verb and has_event:
        return True
    return False


# ---------------- SESSION STATE ----------------
DEFAULTS = {
    "chat": [],
    "context": {},
    "ui_action": None,
    "user_choices": [],
    "ticket_history": [],
    "splunk_history": [],
    "processing": False,
    "_pending_prompt": "",
    "_pending_is_splunk": False,
    "options": [],
}

for k, v in DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ---------------- SPLUNK BACKEND CALL ----------------
def send_to_splunk(prompt: str):
    """Call /splunk-query and render results into the chat."""
    try:
        response = requests.post(
            f"{BACKEND_URL}/splunk-query",
            json={"prompt": prompt},
            timeout=90,
        ).json()
    except Exception as e:
        response = {"status": "failure", "message": str(e)}

    handle_splunk_response(response)


def handle_splunk_response(response: dict):
    st.session_state.processing = False

    if response.get("status") in ("failure", "quota"):
        icon = "⏳" if response.get("status") == "quota" else "❌"
        st.session_state.chat.append({
            "role": "assistant",
            "content": f"{icon} {response.get('message', 'Unknown error')}",
        })
        return

    msg     = response.get("message", "")
    rows    = response.get("rows", [])
    fields  = response.get("fields", [])
    spl     = response.get("spl", "")
    count   = response.get("count", 0)

    # Store in session so we can render the table after the message loop
    st.session_state.chat.append({
        "role":            "assistant",
        "content":         msg,
        "_type":           "splunk",
        "_rows":           rows,
        "_fields":         fields,
        "_spl":            spl,
        "_count":          count,
        "_ai_summary":     response.get("ai_summary", ""),
        "_outage":         response.get("outage_analysis", {}),
    })

    # Sidebar history
    st.session_state.splunk_history.append({
        "spl":   spl,
        "count": count,
        "time":  datetime.now().strftime("%d %b %Y, %H:%M"),
    })


# ---------------- TICKET BACKEND CALL ----------------
def send_to_backend(prompt: str):
    try:
        response = requests.post(
            f"{BACKEND_URL}/create-ticket",
            json={
                "prompt":  prompt,
                "context": st.session_state.context,
            },
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
            "role":    "assistant",
            "content": response["message"],
        })

        st.session_state.ui_action = response.get("ui_action")
        st.session_state.options = response.get("options", [])
        if status == "select_user":
            st.session_state.user_choices = response.get("users", [])

    elif status == "success":
        req  = response.get("request_number", "N/A")
        ritm = response.get("ritm_number",    "N/A")

        st.session_state.chat.append({
            "role":    "assistant",
            "content": f"✅ Ticket Created\n\nRequest: `{req}`\nRITM: `{ritm}`",
        })

        st.session_state.ticket_history.append({
            "request_number": req,
            "ritm":           ritm,
            "time":           datetime.now().strftime("%d %b %Y, %H:%M"),
        })

        st.session_state.context = {}
        st.balloons()

    elif status == "reset":
        st.session_state.chat.append({
            "role":    "assistant",
            "content": response.get("message"),
        })
        st.session_state.context = {}

    elif status == "quota":
        st.session_state.chat.append({
            "role":    "assistant",
            "content": response.get("message"),
        })
    else:
        st.session_state.chat.append({
            "role":    "assistant",
            "content": f"❌ {response.get('message')}",
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

    if st.button("🗑️ Clear Tickets"):
        st.session_state.ticket_history = []
        st.rerun()

    st.divider()
    st.markdown("### 🔍 Splunk Query History")

    if not st.session_state.splunk_history:
        st.caption("No queries yet.")
    else:
        for q in reversed(st.session_state.splunk_history):
            with st.expander(f"🕐 {q['time']} — {q['count']} rows"):
                st.code(q["spl"], language="splunk-spl")

    if st.button("🗑️ Clear Queries"):
        st.session_state.splunk_history = []
        st.rerun()


# ====================================================
# MAIN UI
# ====================================================
st.title("👽 Hi There!")
st.caption("Create ServiceNow tickets or query Splunk logs — just ask!")
st.divider()

# ---------------- CHAT ----------------
for msg in st.session_state.chat:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

        # Render Splunk table inline if this message carries rows
        if msg.get("_type") == "splunk" and msg.get("_rows"):
            rows = msg["_rows"]
            df   = pd.DataFrame(rows)

            # Always show _time + _raw first, then useful metadata, drop noisy internals
            priority = [c for c in ["_time", "_raw"] if c in df.columns]
            meta     = [c for c in ["host", "source", "sourcetype"] if c in df.columns]
            noise    = [c for c in df.columns
                        if c not in priority + meta]
            df = df[priority + meta + [c for c in df.columns
                                       if c not in priority + meta + noise]]
            # Final clean: keep only priority + meta
            df = df[priority + meta]

            # Rename for readability
            df = df.rename(columns={"_time": "Time", "_raw": "Log Message"})

            st.dataframe(df, use_container_width=True, hide_index=True)

            # Download button
            csv = df.to_csv(index=False).encode("utf-8")
            st.download_button(
                label="⬇️ Download CSV",
                data=csv,
                file_name=f"splunk_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv",
                key=f"dl_{id(msg)}",
            )

            # ── Outage Analysis panel ──────────────────────────────────
            outage = msg.get("_outage", {})
            if outage:
                severity = outage.get("severity", "none")
                detected = outage.get("outage_detected", False)

                _SEVERITY_COLOUR = {
                    "critical": "🔴",
                    "high":     "🟠",
                    "medium":   "🟡",
                    "low":      "🔵",
                    "none":     "🟢",
                }
                icon = _SEVERITY_COLOUR.get(severity, "⚪")

                if detected:
                    st.divider()
                    st.markdown(f"**{icon} Outage Analysis — `{severity.upper()}`**")
                    st.error(f"**{outage.get('title', '')}**")
                    signals = outage.get("signals", [])
                    if signals:
                        for s in signals:
                            st.markdown(f"- {s}")
                    rec = outage.get("recommendation", "")
                    if rec:
                        st.info(f"💡 **Recommendation:** {rec}")
                else:
                    st.divider()
                    st.success(f"{icon} **Outage Analysis:** {outage.get('title', 'No outage detected')}")

            # ── AI Summary ─────────────────────────────────────────────
            ai_summary = msg.get("_ai_summary", "")
            st.divider()
            st.markdown("**🤖 AI Summary**")
            st.markdown(ai_summary or "_(summary not available)_")


# ---------------- PROCESSING ----------------
if st.session_state.processing:
    with st.chat_message("assistant"):
        st.write("⏳ Thinking...")

    pending = st.session_state["_pending_prompt"]
    is_splunk = st.session_state.get("_pending_is_splunk", False)

    st.session_state["_pending_prompt"]    = ""
    st.session_state["_pending_is_splunk"] = False

    if is_splunk:
        send_to_splunk(pending)
    else:
        send_to_backend(pending)

    st.rerun()


# ====================================================
# SMART UI CONTROLS (ticket flow only)
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
                disabled=st.session_state.processing,
            ):
                st.session_state.context.update({
                    "recipient_sys_id":  user["sys_id"],
                    "recipient_name":    user["name"],
                    "recipient_email":   user["email"],
                })

                st.session_state.chat.append({
                    "role":    "assistant",
                    "content": f"✅ Selected: **{user['name']}**",
                })

                st.session_state.user_choices           = []
                st.session_state.processing             = True
                st.session_state["_pending_prompt"]     = user["name"]
                st.session_state["_pending_is_splunk"]  = False
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
                disabled=st.session_state.processing,
            ):
                st.session_state.chat.append({"role": "user", "content": option})
                st.session_state.options                = []
                st.session_state.processing             = True
                st.session_state["_pending_prompt"]     = option
                st.session_state["_pending_is_splunk"]  = False
                st.rerun()


# ---------- CONFIRM ----------
if st.session_state.ui_action == "show_confirm_buttons":
    with st.chat_message("assistant"):
        col1, col2 = st.columns(2)

        if col1.button("✅ Confirm", use_container_width=True):
            st.session_state.chat.append({"role": "user", "content": "yes"})
            st.session_state.processing            = True
            st.session_state["_pending_prompt"]    = "yes"
            st.session_state["_pending_is_splunk"] = False
            st.rerun()

        if col2.button("❌ Cancel", use_container_width=True):
            st.session_state.chat.append({"role": "user", "content": "no"})
            st.session_state.processing            = True
            st.session_state["_pending_prompt"]    = "no"
            st.session_state["_pending_is_splunk"] = False
            st.rerun()


# ====================================================
# INPUT
# ====================================================
user_input = st.chat_input("Create a ticket or query logs — e.g. 'get me yesterday's logs from app dummy'")

if user_input and not st.session_state.processing:
    st.session_state.chat.append({"role": "user", "content": user_input})
    st.session_state.processing            = True
    st.session_state["_pending_prompt"]    = user_input
    st.session_state["_pending_is_splunk"] = _is_log_request(user_input)
    st.rerun()
