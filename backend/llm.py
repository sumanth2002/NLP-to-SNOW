"""
llm.py — Three focused LLM calls, nothing more.

1. detect_intent(prompt)      — what does the user want?
2. ask_next_field(field, ...) — how should we phrase the next question?
3. build_payload(schema, ...) — map collected fields -> ServiceNow variables

Field EXTRACTION and STATE are handled entirely in agent.py (Python).
The LLM never decides what has been collected or what is missing.
"""

import google.generativeai as genai
import json
import re
import os
import logging
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_api_key = os.getenv("GEMINI_API_KEY")
if not _api_key:
    raise EnvironmentError("GEMINI_API_KEY is not set in environment / .env")

genai.configure(api_key=_api_key)

# ------------------------------------------------------------------
# Load catalogue once at import time
# ------------------------------------------------------------------
_catalogue_path = Path(__file__).parent / "ticket_catalogue.json"
with open(_catalogue_path) as _f:
    TICKET_CATALOGUE: dict = json.load(_f)


def _catalogue_summary() -> str:
    lines = []
    for app, types in TICKET_CATALOGUE.items():
        for tt_id, tt in types.items():
            lines.append(f"  {app}/{tt_id}: {tt['display_name']}")
    return "\n".join(lines)


# ------------------------------------------------------------------
# Model — shared singleton
# ------------------------------------------------------------------
_model = genai.GenerativeModel(
    "gemini-1.5-pro",
    generation_config={
        "response_mime_type": "application/json",
        "temperature": 0.0,
    },
)

# ------------------------------------------------------------------
# 1. detect_intent
# ------------------------------------------------------------------
_INTENT_SYSTEM = f"""You are an intent detector for a ServiceNow IT ticket agent.

Available ticket types:
{_catalogue_summary()}

Analyse the user message and return ONLY valid JSON with these exact keys:
{{
  "intent": "create_ticket | status_check | cancel | clarify | out_of_scope",
  "app": "bitbucket | jira | null",
  "ticket_type": "user_access | create_project | repo_permission | null",
  "ticket_id": "REQxxxxx or RITMxxxxx or null",
  "reply": "short clarification question if intent=clarify, else null"
}}

Rules:
- Only set ticket_type if you are confident from the message.
- If app is clear but ticket_type is ambiguous → intent=clarify, set reply asking which type.
- If neither app nor type is clear → intent=clarify, set reply asking which app.
- status_check: user asks about an existing REQ/RITM number.
- "add user", "remove user", "access" → ticket_type=user_access.
- "create project", "new project" → ticket_type=create_project.
- "repo permission", "repository" → ticket_type=repo_permission.

Examples:
User: "I need access to Bitbucket"
Output: {{"intent":"create_ticket","app":"bitbucket","ticket_type":"user_access","ticket_id":null,"reply":null}}

User: "Create a bitbucket project"
Output: {{"intent":"create_ticket","app":"bitbucket","ticket_type":"create_project","ticket_id":null,"reply":null}}

User: "add user"
Output: {{"intent":"create_ticket","app":null,"ticket_type":"user_access","ticket_id":null,"reply":null}}

User: "I need something in Jira"
Output: {{"intent":"clarify","app":"jira","ticket_type":null,"ticket_id":null,"reply":"What do you need in Jira? I can help with user access or creating a project."}}

User: "Status of REQ001234"
Output: {{"intent":"status_check","app":null,"ticket_type":null,"ticket_id":"REQ001234","reply":null}}
"""


def detect_intent(prompt: str) -> dict:
    """
    Detect intent from user prompt.
    Fast rule-based paths run first; LLM is called only when needed.
    Falls back to regex rules if the LLM fails.
    """
    prompt = (prompt or "").strip()

    # Fast path: ticket number patterns
    m = re.search(r'\b(REQ\d+|RITM\d+)\b', prompt, re.IGNORECASE)
    if m:
        pl = prompt.lower()
        if any(w in pl for w in ("status", "state", "check", "details", "info", "where")):
            return {
                "intent": "status_check",
                "app": None,
                "ticket_type": None,
                "ticket_id": m.group(0).upper(),
                "reply": None,
            }
        if any(w in pl for w in ("cancel", "delete", "close", "withdraw")):
            return {
                "intent": "cancel",
                "app": None,
                "ticket_type": None,
                "ticket_id": m.group(0).upper(),
                "reply": None,
            }

    # LLM call
    try:
        resp = _model.generate_content(f"{_INTENT_SYSTEM}\n\nUser: {prompt}")
        result = _safe_json(resp.text)
        if result and "intent" in result:
            return result
    except Exception as e:
        logger.warning(f"[llm] detect_intent LLM call failed: {e}")

    # Rule-based fallback
    return _rule_intent(prompt)


# ------------------------------------------------------------------
# 2. ask_next_field
#    Uses deterministic fallback by default; LLM only when needed.
# ------------------------------------------------------------------
_QUESTION_SYSTEM = """You are a friendly IT helpdesk assistant phrasing a single question
to collect one field for a ServiceNow ticket.

Return ONLY valid JSON:
{
  "question": "a short, friendly, single-sentence question"
}

Rules:
- If an error string is provided, briefly acknowledge it then re-ask.
- For select fields, list the options naturally in the question.
- For user_search fields, ask for a name or email.
- Never mention JSON, field keys, sys_id, or technical terms.
- Maximum 30 words.
"""

# Fields whose labels are self-explanatory — skip the LLM call entirely.
_SIMPLE_FIELD_TYPES = {"select", "user_search", "text"}


def ask_next_field(
    field_def: dict,
    collected: dict,
    error: Optional[str] = None,
) -> str:
    """
    Returns a human-friendly question for the given field.

    Strategy:
    - If no error and the field type is simple → use deterministic fallback (fast, free).
    - If error or the field has a rich description → call LLM for a better phrasing.
    """
    ftype = field_def.get("type", "text")
    has_description = bool(field_def.get("description"))

    use_llm = error is not None or (has_description and ftype not in _SIMPLE_FIELD_TYPES)

    if use_llm:
        payload = {
            "field": field_def,
            "already_collected": list(collected.keys()),
            "error": error,
        }
        try:
            resp = _model.generate_content(
                f"{_QUESTION_SYSTEM}\n\n{json.dumps(payload)}"
            )
            result = _safe_json(resp.text)
            if result and result.get("question"):
                return result["question"]
        except Exception as e:
            logger.warning(f"[llm] ask_next_field LLM call failed: {e}")

    return _fallback_question(field_def, error)


def _fallback_question(field_def: dict, error: Optional[str] = None) -> str:
    prefix = f"{error} " if error else ""
    label = field_def["label"]
    ftype = field_def.get("type", "text")
    hint = field_def.get("validation_hint", "")

    if ftype == "select":
        opts = ", ".join(field_def.get("options", []))
        return f"{prefix}What is the {label.lower()}? Choose one of: {opts}."
    if ftype == "user_search":
        return f"{prefix}Who is the {label.lower()}? Please enter their name or email."
    if hint:
        return f"{prefix}Please provide the {label.lower()}. ({hint})"
    return f"{prefix}Please provide the {label.lower()}."


# ------------------------------------------------------------------
# 3. build_payload — maps collected fields -> ServiceNow variables
# ------------------------------------------------------------------
_PAYLOAD_SYSTEM = """You are a ServiceNow RITM payload builder.

Given the ticket schema and confirmed collected fields, produce the `variables` dict
for the ServiceNow catalog API order_now endpoint.

Rules:
- For user_search fields, use the _sys_id sibling value
  (e.g. use recipient_sys_id value for "recipient" variable key, or `variable_name` if provided).
- For select fields, use the value as-is.
- For user access tickets, also set "requested_for" to the recipient's sys_id.
- Only include fields that have non-empty values.
- Do NOT include _sys_id keys directly as variable names.

Return ONLY valid JSON:
{"variables": { ...key: value pairs... }}
"""


def build_payload(schema: dict, collected: dict) -> dict:
    """
    Map collected form data to a ServiceNow catalog variables dict.
    Falls back to deterministic mapping if the LLM fails.
    """
    payload = {
        "ticket_schema": schema,
        "collected_fields": collected,
    }
    try:
        resp = _model.generate_content(
            f"{_PAYLOAD_SYSTEM}\n\n{json.dumps(payload, indent=2)}"
        )
        result = _safe_json(resp.text)
        if result and "variables" in result:
            return result["variables"]
    except Exception as e:
        logger.warning(f"[llm] build_payload LLM call failed: {e}")

    return _fallback_payload(schema, collected)


def _fallback_payload(schema: dict, collected: dict) -> dict:
    variables: dict = {}
    for field in schema.get("fields", []):
        key = field["key"]
        dest_key = field.get("variable_name", key)
        if field["type"] == "user_search":
            sid = collected.get(f"{key}_sys_id")
            if sid:
                variables[dest_key] = sid
        else:
            val = collected.get(key)
            if val:
                variables[dest_key] = val
    return variables


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------
def _safe_json(text: str) -> Optional[dict]:
    """Extract the first JSON object from an LLM response string."""
    if not text:
        return None
    # Try the whole string first (clean responses)
    try:
        return json.loads(text.strip())
    except Exception:
        pass
    # Fall back to regex extraction
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group())
    except Exception:
        return None


def _rule_intent(prompt: str) -> dict:
    """
    Pure regex / keyword fallback for detect_intent.
    Called when the LLM is unavailable or returns garbage.
    """
    pl = prompt.lower()
    app = next((a for a in TICKET_CATALOGUE if a in pl), None)
    tt: Optional[str] = None

    if app or True:  # also try type without app
        if any(w in pl for w in ("access", "add user", "remove user", "add me")):
            tt = "user_access"
        elif any(w in pl for w in ("create project", "new project", "make project")):
            tt = "create_project"
        elif any(w in pl for w in ("repo", "repository", "permission")):
            tt = "repo_permission"
        elif any(w in pl for w in ("project",)):
            tt = "create_project"

    if app and tt:
        return {
            "intent": "create_ticket",
            "app": app,
            "ticket_type": tt,
            "ticket_id": None,
            "reply": None,
        }
    if app:
        opts = ", ".join(
            v["display_name"] for v in TICKET_CATALOGUE.get(app, {}).values()
        )
        return {
            "intent": "clarify",
            "app": app,
            "ticket_type": None,
            "ticket_id": None,
            "reply": f"What do you need in {app.title()}? Options: {opts}.",
        }
    if tt:
        return {
            "intent": "clarify",
            "app": None,
            "ticket_type": tt,
            "ticket_id": None,
            "reply": "Which app is this for — Bitbucket or Jira?",
        }
    return {
        "intent": "clarify",
        "app": None,
        "ticket_type": None,
        "ticket_id": None,
        "reply": "Which app do you need help with? I support Bitbucket and Jira.",
    }