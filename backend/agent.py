"""
agent.py — Stateless ticket agent with improved intent re-detection,
input validation, and cleaner confirmation summary.
"""

import re
import os
import logging
import traceback
from typing import Dict, List, Optional

from llm import detect_intent, ask_next_field, build_payload, TICKET_CATALOGUE
from ritm_client import search_users, create_ritm, update_request_field
from servicenow_client import (
    get_ticket_details,
    search_existing_ritms,
    _get,
    BASE_URL,
    _map_state,
    _display,
)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


# ---------------- STATES ----------------
class State:
    INIT = "INIT"
    CLARIFYING = "CLARIFYING"
    COLLECTING_FIELDS = "COLLECTING_FIELDS"
    AWAITING_USER_SELECT = "AWAITING_USER_SELECT"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"


# ---------------- FIELD VALIDATORS ----------------
_VALIDATORS = {
    "project_key": (
        re.compile(r'^[A-Z]{2,10}$'),
        "Project key must be 2–10 uppercase letters with no spaces or special characters (e.g. MYPROJ). Please try again."
    ),
}


# ---------------- ENTRY ----------------
def ticket_agent(prompt: str, context: Optional[Dict] = None) -> Dict:
    try:
        return _impl((prompt or "").strip(), context or {})
    except Exception as e:
        logger.error(f"Agent crashed: {e}\n{traceback.format_exc()}")
        return {
            "status": "error",
            "message": "Something went wrong. Please try again.",
            "context": context or {}
        }


# ---------------- MAIN ROUTER ----------------
def _impl(prompt: str, ctx: Dict) -> Dict:
    state = ctx.get("state", State.INIT)
    pl = prompt.lower().strip()

    # Global cancel / reset
    if pl in ("cancel", "stop", "quit", "exit"):
        return _done("cancelled", "Alright, cancelled. Let me know if you need anything else.")
    if pl in ("start over", "reset", "new", "begin again"):
        return _done("reset", "Starting over. What can I help you with?")

    # Ticket lookup — works from any state
    m = re.search(r'\b(REQ\d+|RITM\d+)\b', prompt, re.IGNORECASE)
    if m:
        data = get_ticket_details(m.group(0).upper())
        return {
            "status": "incomplete",
            "message": _fmt_ticket(data),
            "context": ctx
        }

    # Ticket listing — works from any state
    # Detect "all tickets", "show tickets", "list tickets", etc.
    if any(phrase in pl for phrase in ["all tickets", "show tickets", "list tickets", "get all tickets", "get tickets", "tickets list"]):
        # Check for status filters
        status_filter = None
        for status in ["open", "pending", "work in progress", "closed complete", "closed incomplete", "closed skipped"]:
            if status in pl:
                status_filter = status
                break
        
        tickets = get_all_tickets(status=status_filter, limit=50)
        return {
            "status": "incomplete",
            "message": _fmt_tickets_list(tickets, status_filter),
            "context": ctx
        }

    if state == State.INIT:
        return _init(prompt, ctx)
    if state == State.CLARIFYING:
        return _clarifying(prompt, ctx)
    if state == State.COLLECTING_FIELDS:
        # Guard: if the UI echoes a "selected: ..." confirmation message back
        # after _user_select has already advanced state, ignore it and re-show
        # whatever the next step already is (confirmation or next field).
        if prompt.lower().startswith("selected:"):
            return _ask_next(ctx)
        return _collecting(prompt, ctx)
    if state == State.AWAITING_USER_SELECT:
        return _user_select(prompt, ctx)
    if state == State.AWAITING_CONFIRMATION:
        return _confirming(prompt, ctx)

    return _done("error", "I lost track. Can you tell me again what you need?")


# ---------------- INIT ----------------
def _init(prompt: str, ctx: Dict) -> Dict:
    data = detect_intent(prompt)
    intent = data.get("intent")

    if intent == "status_check":
        ticket_id = data.get("ticket_id")
        if ticket_id:
            ticket_data = get_ticket_details(ticket_id)
            return {"status": "incomplete", "message": _fmt_ticket(ticket_data), "context": ctx}

    if intent == "cancel":
        return _done("cancelled", "Nothing to cancel yet. Let me know if you need help.")

    if intent != "create_ticket":
        reply = data.get("reply") or "What can I help you with? I can raise Bitbucket or Jira tickets."
        return _clarify_save(reply, data.get("app"), data.get("ticket_type"))

    return _try_start(prompt, data.get("app"), data.get("ticket_type"))


# ---------------- CLARIFY ----------------
def _clarifying(prompt: str, ctx: Dict) -> Dict:
    data = detect_intent(prompt)

    app = ctx.get("partial_app") or data.get("app") or _match_app(prompt)
    tt = ctx.get("partial_ticket_type") or data.get("ticket_type") or _match_ticket_type(prompt, app)

    return _try_start(prompt, app, tt)


def _clarify_save(msg: str, app: Optional[str], tt: Optional[str]) -> Dict:
    return {
        "status": "incomplete",
        "message": msg,
        "context": {
            "state": State.CLARIFYING,
            "partial_app": app,
            "partial_ticket_type": tt,
        }
    }


# ---------------- START ----------------
def _try_start(prompt: str, app: Optional[str], tt: Optional[str]) -> Dict:
    if not app:
        return _clarify_save("Which app is this for? Bitbucket or Jira?", None, tt)

    if not tt:
        opts = ", ".join(
            v["display_name"] for v in TICKET_CATALOGUE.get(app, {}).values()
        )
        return _clarify_save(
            f"What do you need in {app.title()}? Options: {opts}.",
            app,
            None
        )

    schema = TICKET_CATALOGUE.get(app, {}).get(tt)
    if not schema:
        return _done("error", "That request type isn't configured yet.")

    new_ctx = {
        "state": State.COLLECTING_FIELDS,
        "app": app,
        "ticket_type": tt,
        "schema": schema,
        "collected": {},
        "pending_field_key": None,
    }

    return _ask_next(new_ctx)


# ---------------- COLLECT ----------------
def _collecting(prompt: str, ctx: Dict) -> Dict:
    schema = ctx["schema"]
    collected = ctx.get("collected", {})
    pending = ctx.get("pending_field_key")

    if pending:
        field_def = _field_by_key(schema, pending)
        if field_def is None:
            logger.warning(f"pending_field_key '{pending}' not found in schema — skipping")
        else:
            result = _apply_answer(prompt, field_def, collected, ctx)
            if result:
                return result

    return _ask_next(ctx)


def _apply_answer(raw: str, field_def: dict, collected: dict, ctx: dict) -> Optional[Dict]:
    """
    Validates and stores the user's answer for field_def.
    Returns a response dict if we need to re-ask, or None to proceed.
    """
    key = field_def["key"]

    # user_search fields are handled separately
    if field_def["type"] == "user_search":
        return _do_user_search(key, raw, ctx, collected)

    value = raw.strip()

    # --- select validation ---
    if field_def["type"] == "select":
        options = [o.lower() for o in field_def.get("options", [])]
        if value.lower() not in options:
            opts_str = ", ".join(field_def.get("options", []))
            return {
                "status": "incomplete",
                "message": (
                    f"'{value}' isn't a valid option. Please choose one of: {opts_str}."
                ),
                "context": ctx,
            }
        value = field_def["options"][[o.lower() for o in field_def["options"]].index(value.lower())]

    # --- custom field validators ---
    if key in _VALIDATORS:
        pattern, error_msg = _VALIDATORS[key]
        if not pattern.match(value):
            return {
                "status": "incomplete",
                "message": error_msg,
                "context": ctx,
            }

    collected[key] = value
    ctx["collected"] = collected
    return None


# ---------------- USER SEARCH ----------------
def _do_user_search(field_key: str, raw: str, ctx: dict, collected: dict) -> Dict:
    raw = raw.strip()
    if len(raw) < 2:
        return {
            "status": "incomplete",
            "message": "Please enter at least 2 characters (name or email).",
            "context": ctx,
        }

    try:
        users = search_users(raw)
    except Exception as e:
        logger.error(f"User search error: {e}")
        return {
            "status": "incomplete",
            "message": "I had trouble searching for users. Please try again.",
            "context": ctx,
        }

    if not users:
        return {
            "status": "incomplete",
            "message": f"I couldn't find anyone matching '{raw}'. Try a name or email address.",
            "context": ctx,
        }

    if len(users) == 1:
        u = users[0]
        collected[field_key] = u["name"]
        collected[f"{field_key}_sys_id"] = u["sys_id"]
        ctx["collected"] = collected
        # FIX 1: Clear pending_field_key so _collecting doesn't re-process
        # this field when the UI echoes back the selection message.
        ctx["pending_field_key"] = None

        next_step = _ask_next(ctx)
        next_step["message"] = f"✅ Selected: {u['name']}\n\n" + next_step["message"]
        return next_step

    # Multiple matches
    ctx.update({
        "search_results": users,
        "pending_field_key": field_key,
        "collected": collected,
        "state": State.AWAITING_USER_SELECT,
    })

    return {
        "status": "select_user",
        "message": f"I found {len(users)} matches for '{raw}'. Pick the right one:",
        "users": users,
        "context": ctx,
    }


# ---------------- USER SELECT ----------------
def _user_select(prompt: str, ctx: Dict) -> Dict:
    candidates: List[Dict] = ctx.get("search_results", [])
    field_key: str = ctx.get("pending_field_key")

    pl = prompt.lower().replace("selected:", "").strip()

    selected = None
    if pl.isdigit():
        idx = int(pl) - 1
        if 0 <= idx < len(candidates):
            selected = candidates[idx]

    if not selected:
        for u in candidates:
            if pl in u["name"].lower() or pl in u.get("email", "").lower():
                selected = u
                break

    if not selected:
        return {
            "status": "select_user",
            "message": "Please select a valid user from the list, or type part of their name.",
            "users": candidates,
            "context": ctx,
        }

    collected = ctx["collected"]
    collected[field_key] = selected["name"]
    collected[f"{field_key}_sys_id"] = selected["sys_id"]

    logger.info(f"[_user_select] Stored user: {field_key}={selected['name']}, {field_key}_sys_id={selected['sys_id']}")
    logger.info(f"[_user_select] Full collected data: {collected}")

    ctx.update({
        "collected": collected,
        "search_results": [],
        "pending_field_key": None,
        "state": State.COLLECTING_FIELDS,
    })

    next_step = _ask_next(ctx)
    next_step["message"] = f"✅ Selected: {selected['name']}\n\n" + next_step["message"]
    return next_step


# ---------------- ASK NEXT ----------------
def _ask_next(ctx: Dict) -> Dict:
    missing = _missing(ctx["schema"], ctx["collected"])

    if not missing:
        return _confirm(ctx)

    field = missing[0]
    ctx["pending_field_key"] = field["key"]
    ctx["state"] = State.COLLECTING_FIELDS

    logger.info(f"Asking for field: {field}")

    if field["type"] == "select":
        logger.info(f"Sending select options: {field['options']}")
        return {
            "status": "incomplete",
            "message": f"What is the {field['label']}?",
            "ui_action": "select_options",
            "options": field["options"],
            "context": ctx,
        }
    else:
        return {
            "status": "incomplete",
            "message": ask_next_field(field, ctx["collected"]),
            "context": ctx,
        }


# ---------------- CONFIRM ----------------
def _confirm(ctx: Dict) -> Dict:
    ctx["state"] = State.AWAITING_CONFIRMATION

    summary = _build_summary(ctx["schema"], ctx["collected"])

    return {
        "status": "incomplete",
        "message": f"Here's a summary of your request:\n\n{summary}\n\nShall I submit this?",
        "context": ctx,
        "ui_action": "show_confirm_buttons",
    }


def _build_summary(schema: dict, collected: dict) -> str:
    """Build a readable bullet summary of collected fields (hides _sys_id keys)."""
    lines = []
    for field in schema.get("fields", []):
        key = field["key"]
        label = field["label"]
        value = collected.get(key, "—")
        lines.append(f"• {label}: {value}")
    return "\n".join(lines)


def _confirming(prompt: str, ctx: Dict) -> Dict:
    if prompt.lower().strip() in ("yes", "y", "sure", "ok", "submit", "confirm"):
        return _submit(ctx)
    return _done("cancelled", "No problem, I didn't submit anything. Let me know if you'd like to start over.")


# ---------------- SUBMIT ----------------
def _submit(ctx: Dict) -> Dict:
    schema = ctx["schema"]
    app = ctx["app"]
    ticket_type = ctx["ticket_type"]
    collected = ctx["collected"]

    variables = build_payload(schema, collected)

    logger.info(f"[_submit] Final variables before RITM creation: {variables}")

    # Use the selected recipient sys_id explicitly for request assignment.
    requested_for = collected.get("recipient_sys_id")

    if requested_for:
        variables["requested_for"] = requested_for
        logger.info(f"[_submit] Added requested_for to variables: {requested_for}")

    # Apply explicit variable_name mappings and also keep the original field key.
    # Also try common ServiceNow variable names for requested_for.
    for field in schema.get("fields", []):
        if field.get("type") != "user_search":
            continue

        key = field["key"]
        sid = collected.get(f"{key}_sys_id")
        if not sid:
            continue

        dest_key = field.get("variable_name", key)
        variables[key] = sid
        if dest_key != key:
            variables[dest_key] = sid
        logger.info(f"[_submit] Added user mapping {key}={sid}, {dest_key}={sid}")

        # Try additional common variable names for requested_for
        if key == "recipient":
            for alt_key in ["u_requested_for", "requested_user", "user_requested_for", "recipient_user"]:
                variables[alt_key] = sid
                logger.info(f"[_submit] Added alternative requested_for mapping {alt_key}={sid}")

    # Duplicate check - use the same catalog item as the one we're about to create
    rsid = collected.get("recipient_sys_id")
    if ticket_type == "user_access" and rsid and not ctx.get("skip_dup"):
        catalog_env = schema.get("catalog_item_sys_id_env", "")
        if catalog_env:
            # Extract the catalog type from env var name (e.g. "CATALOG_ITEM_BITBUCKET_SYS_ID" -> "bitbucket")
            catalog_type = catalog_env.replace("CATALOG_ITEM_", "").replace("_SYS_ID", "").lower()
            try:
                existing = search_existing_ritms(rsid, catalog_type)
            except Exception as e:
                logger.warning(f"Duplicate check failed (proceeding): {e}")
                existing = []
        else:
            existing = []

        if existing:
            dup_number = existing[0]["number"]
            return {
                "status": "incomplete",
                "message": (
                    f"There's already an open ticket for this user ({dup_number}). "
                    "Would you like to submit another one anyway?"
                ),
                "context": {**ctx, "state": State.AWAITING_CONFIRMATION, "skip_dup": True},
                "ui_action": "show_confirm_buttons",
            }

    catalog_id = os.getenv(schema.get("catalog_item_sys_id_env", ""))
    if not catalog_id:
        logger.error(f"Missing env var: {schema.get('catalog_item_sys_id_env')}")
        return _done("error", "This catalog item isn't configured on the server. Please contact your admin.")

    try:
        result = create_ritm({
            "catalog_item_sys_id": catalog_id,
            "variables": variables,
            "requested_for": requested_for,
        })
    except Exception as e:
        logger.error(f"create_ritm failed: {e}")
        return _done("error", f"Failed to create the ticket: {str(e)[:200]}")

    request_number = result.get("request_number")
    ritm_number = result.get("ritm_number")

    # Update the request's requested_for field to ensure the correct user is set
    if request_number and requested_for:
        success = update_request_field(request_number, "requested_for", requested_for)
        if success:
            logger.info(f"[_submit] Updated request {request_number} requested_for to {requested_for}")
        else:
            logger.warning(f"[_submit] Failed to update request {request_number} requested_for, but RITM was created")

    # FIX 3: Warn loudly if create_ritm didn't return distinct request/RITM numbers,
    # which would indicate a mapping bug in ritm_client.py.
    if request_number and ritm_number and request_number == ritm_number:
        logger.error(
            f"[_submit] create_ritm returned identical request_number and ritm_number "
            f"({request_number}). Check ritm_client.py — it may be mapping both fields "
            f"from the same response key."
        )

    return {
        "status": "success",
        "request_number": request_number,
        "ritm_number": ritm_number,
        "message": (
            f"Done! Your ticket has been submitted.\n"
            f"Request: {request_number} | RITM: {ritm_number}"
        ),
        "context": {"state": State.INIT},
    }


# ---------------- FORMAT TICKET ----------------
def _fmt_ticket(data: Optional[dict]) -> str:
    if not data:
        return "I couldn't find that ticket. Please double-check the number."

    if data.get("type") == "REQ":
        req = data.get("request", {})
        ritms = data.get("ritms", [])

        lines = [
            "### 📋 Ticket Status",
            "",
            "**Request Details:**",
            f"- **Number:** {req.get('number')}",
            f"- **Status:** {_status_icon(req.get('state'))} {req.get('state')}",
            f"- **Requested For:** {req.get('requested_for') or 'Unknown'}",
            f"- **Opened By:** {req.get('opened_by') or 'Unknown'}",
        ]

        if ritms:
            lines.append("")
            lines.append("**RITM Items:**")
            for r in ritms:
                lines.append(
                    f"- **{r.get('number')}:** {_status_icon(r.get('state'))} {r.get('state')} | Assigned to: {r.get('assigned_to') or 'Unassigned'}"
                )
        else:
            lines.append("\n*No RITM items found under this request.*")

        return "\n".join(lines)

    if data.get("type") == "RITM":
        ritm = data.get("ritm", {})
        return (
            "### 🎫 RITM Status\n\n"
            f"**Number:** {ritm.get('number')}\n"
            f"**Status:** {_status_icon(ritm.get('state'))} {ritm.get('state')}\n"
            f"**Assigned To:** {ritm.get('assigned_to') or 'Unassigned'}"
        )

    return str(data)


def _status_icon(state: Optional[str]) -> str:
    if not state:
        return "⚪"
    s = state.lower()
    if "progress" in s:
        return "🟡"
    if "requested" in s:
        return "🔵"
    if "complete" in s or "closed" in s:
        return "🟢"
    if "incomplete" in s or "skipped" in s:
        return "🔴"
    return "⚪"


# ---------------- HELPERS ----------------
def _done(status: str, msg: str) -> Dict:
    return {"status": status, "message": msg, "context": {"state": State.INIT}}


def _missing(schema: dict, collected: dict) -> List[dict]:
    return [f for f in schema.get("fields", []) if f["key"] not in collected]


def _field_by_key(schema: dict, key: str) -> Optional[dict]:
    return next((f for f in schema.get("fields", []) if f["key"] == key), None)


# ------------------------------------------------------------------
# Ticket listing functions
# ------------------------------------------------------------------
def get_all_tickets(status: Optional[str] = None, limit: int = 50) -> List[Dict]:
    """
    Fetch all sc_request (REQ) tickets, optionally filtered by status.
    """
    status_map = {
        "open": "1",
        "pending": "1",
        "requested": "1",
        "work in progress": "2",
        "in progress": "2",
        "closed complete": "3",
        "complete": "3",
        "closed incomplete": "4",
        "incomplete": "4",
        "closed skipped": "7",
        "skipped": "7",
    }
    
    url = f"{BASE_URL}/api/now/table/sc_request"
    
    query_parts = []
    if status:
        state_code = status_map.get(status.lower().strip())
        if state_code:
            query_parts.append(f"state={state_code}")
    
    query = "^".join(query_parts) if query_parts else ""
    
    params = {
        "sysparm_query": query,
        "sysparm_fields": "number,state,requested_for,opened_by,sys_created_on",
        "sysparm_limit": limit,
        "sysparm_display_value": "true",
        "sysparm_order_by": "-sys_created_on",
    }
    
    try:
        data = _get(url, params).get("result", [])
        return [
            {
                "number": item.get("number"),
                "status": _map_state(item.get("state")),
                "requested_for": _display(item.get("requested_for")),
                "opened_by": _display(item.get("opened_by")),
                "created_at": item.get("sys_created_on"),
            }
            for item in data
        ]
    except Exception as e:
        logger.error(f"[get_all_tickets] error: {e}")
        return []


def _fmt_tickets_list(tickets: List[Dict], status_filter: Optional[str] = None) -> str:
    """Format a list of tickets for display."""
    if not tickets:
        msg = f" with status '{status_filter}'" if status_filter else ""
        return f"📭 No tickets found{msg}."
    
    lines = [f"📋 **{len(tickets)} Tickets" + (f" - {status_filter.upper()}**" if status_filter else "**")]
    lines.append("")
    
    for ticket in tickets:
        status_icon = _status_icon(ticket.get("status"))
        lines.append(
            f"- **{ticket.get('number')}** {status_icon} {ticket.get('status')} | "
            f"Requested for: {ticket.get('requested_for') or 'Unknown'} | "
            f"Opened by: {ticket.get('opened_by') or 'Unknown'}"
        )
    
    return "\n".join(lines)

def _match_app(text: str) -> Optional[str]:
    pl = text.lower()
    for a in TICKET_CATALOGUE:
        if a in pl:
            return a
    return None


def _match_ticket_type(text: str, app: Optional[str]) -> Optional[str]:
    if not app:
        return None
    pl = text.lower()
    for t in TICKET_CATALOGUE.get(app, {}):
        if t.replace("_", " ") in pl:
            return t
    if any(w in pl for w in ("access", "add user", "remove user")):
        return "user_access"
    if any(w in pl for w in ("project", "create project", "new project")):
        return "create_project"
    if any(w in pl for w in ("repo", "repository", "permission")):
        return "repo_permission"
    return None