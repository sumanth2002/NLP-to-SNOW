"""
servicenow_client.py — Ticket lookup and duplicate-check queries.

Uses a shared requests.Session with retry logic (mirrors ritm_client.py).
"""

import os
import logging
from typing import Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Config
# ------------------------------------------------------------------
SN_INSTANCE = os.getenv("SNOW_INSTANCE", "").strip().rstrip("/")
SN_USER = os.getenv("SNOW_USER", "").strip()
SN_PASS = os.getenv("SNOW_PASS", "").strip()

_missing = [k for k, v in [
    ("SNOW_INSTANCE", SN_INSTANCE),
    ("SNOW_USER", SN_USER),
    ("SNOW_PASS", SN_PASS),
] if not v]

if _missing:
    raise EnvironmentError(
        f"Missing required ServiceNow credentials: {', '.join(_missing)}. "
        "Set them in your .env file."
    )

BASE_URL = f"https://{SN_INSTANCE}"
_DEFAULT_TIMEOUT = (5.0, 15.0)  # (connect, read) seconds

# ------------------------------------------------------------------
# State mapping
# FIX 4: Added display-value labels ("Open", "Work in Progress", etc.)
# alongside the numeric codes so _map_state handles both cases.
# ServiceNow returns display values when sysparm_display_value=true is
# set, which is the case in all queries below.
# ------------------------------------------------------------------
_STATE_MAP: Dict[str, str] = {
    # Numeric codes (sysparm_display_value omitted or false)
    "1": "Requested",
    "2": "In Progress",
    "3": "Closed Complete",
    "4": "Closed Incomplete",
    "7": "Closed Skipped",
    # Display-value strings (sysparm_display_value=true)
    "open": "Requested",
    "requested": "Requested",
    "in progress": "In Progress",
    "work in progress": "In Progress",
    "closed complete": "Closed Complete",
    "closed incomplete": "Closed Incomplete",
    "closed skipped": "Closed Skipped",
    "complete": "Closed Complete",
    "fulfilled": "Closed Complete",
}


def _map_state(state: any) -> str:
    if state is None:
        return "Unknown"
    key = str(state).strip().lower()
    # Try lowercase lookup first (handles display values), then original (handles numeric codes)
    return _STATE_MAP.get(key) or _STATE_MAP.get(str(state).strip(), str(state))


# ------------------------------------------------------------------
# Shared session — retry + pooling (same pattern as ritm_client)
# ------------------------------------------------------------------
_SESSION: Optional[requests.Session] = None


def _build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=5,
        pool_maxsize=10,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.auth = (SN_USER, SN_PASS)
    session.headers.update({"Accept": "application/json"})
    return session


def _session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = _build_session()
    return _SESSION


def _get(url: str, params: dict) -> dict:
    """GET with consistent timeout, raises on HTTP errors."""
    resp = _session().get(url, params=params, timeout=_DEFAULT_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


# ------------------------------------------------------------------
# REQ lookup
# ------------------------------------------------------------------
def get_request(ticket_number: str) -> Optional[Dict]:
    """
    Fetch a single sc_request record by number.
    Returns a normalised dict or None if not found.
    """
    url = f"{BASE_URL}/api/now/table/sc_request"
    params = {
        "sysparm_query": f"number={ticket_number}",
        "sysparm_fields": "sys_id,number,state,requested_for,opened_by",
        "sysparm_limit": 1,
        "sysparm_display_value": "true",
    }
    try:
        data = _get(url, params).get("result", [])
    except Exception as e:
        logger.error(f"[get_request] {ticket_number}: {e}")
        return None

    if not data:
        return None

    r = data[0]
    return {
        "number": r.get("number"),
        "sys_id": r.get("sys_id"),
        "state": _map_state(r.get("state")),
        "requested_for": _display(r.get("requested_for")),
        "opened_by": _display(r.get("opened_by")),
    }


# ------------------------------------------------------------------
# RITMs under a REQ
# ------------------------------------------------------------------
def get_ritms(request_sys_id: str) -> List[Dict]:
    """
    Fetch all sc_req_item records linked to a request sys_id.
    Returns an empty list on error.
    """
    url = f"{BASE_URL}/api/now/table/sc_req_item"
    params = {
        "sysparm_query": f"request={request_sys_id}",
        "sysparm_fields": "number,state,assigned_to",
        "sysparm_limit": 20,
        "sysparm_display_value": "true",
    }
    try:
        items = _get(url, params).get("result", [])
    except Exception as e:
        logger.error(f"[get_ritms] request={request_sys_id}: {e}")
        return []

    return [
        {
            "number": item.get("number"),
            "state": _map_state(item.get("state")),
            "assigned_to": _display(item.get("assigned_to")),
        }
        for item in items
    ]


# ------------------------------------------------------------------
# Unified ticket detail lookup (REQ or RITM)
# ------------------------------------------------------------------
def get_ticket_details(ticket_number: str) -> Optional[Dict]:
    """
    Look up a REQ or RITM by number.

    Returns:
        For REQ: {"type":"REQ", "request": {...}, "ritms": [...]}
        For RITM: {"type":"RITM", "ritm": {...}}
        None if not found.
    """
    if not ticket_number:
        return None

    ticket_number = ticket_number.upper().strip()

    if ticket_number.startswith("REQ"):
        req = get_request(ticket_number)
        if not req:
            return None
        ritms = get_ritms(req["sys_id"])
        return {"type": "REQ", "request": req, "ritms": ritms}

    if ticket_number.startswith("RITM"):
        url = f"{BASE_URL}/api/now/table/sc_req_item"
        params = {
            "sysparm_query": f"number={ticket_number}",
            "sysparm_fields": "number,state,assigned_to",
            "sysparm_limit": 1,
            "sysparm_display_value": "true",
        }
        try:
            data = _get(url, params).get("result", [])
        except Exception as e:
            logger.error(f"[get_ticket_details] {ticket_number}: {e}")
            return None

        if not data:
            return None

        item = data[0]
        return {
            "type": "RITM",
            "ritm": {
                "number": item.get("number"),
                "state": _map_state(item.get("state")),
                "assigned_to": _display(item.get("assigned_to")),
            },
        }

    logger.warning(f"[get_ticket_details] Unknown ticket prefix: {ticket_number}")
    return None


# ------------------------------------------------------------------
# Duplicate check — open RITMs for a user + catalog item
# ------------------------------------------------------------------
def search_existing_ritms(
    user_sys_id: str,
    access_type: str,
    states: Optional[List[str]] = None,
) -> List[Dict]:
    """
    Return open RITMs for the given user sys_id and catalog item.

    Args:
        user_sys_id:  The sys_id of the user (requested_for).
        access_type:  e.g. "user_access" or "create_project" — used to build
                      the env var name (CATALOG_ITEM_USER_ACCESS_SYS_ID).
        states:       List of state codes to consider "open". Defaults to ["1","2"].

    Returns:
        List of matching RITM dicts, or [] if none / env var missing.
    """
    if not user_sys_id:
        return []

    if states is None:
        states = ["1", "2"]

    env_key = f"CATALOG_ITEM_{access_type.upper()}_SYS_ID"
    catalog_item_id = os.getenv(env_key, "").strip()
    if not catalog_item_id:
        logger.debug(f"[search_existing_ritms] {env_key} not set — skipping dup check")
        return []

    state_condition = f"stateIN{','.join(states)}"
    query = (
        f"requested_for={user_sys_id}"
        f"^cat_item={catalog_item_id}"
        f"^{state_condition}"
    )
    url = f"{BASE_URL}/api/now/table/sc_req_item"
    params = {
        "sysparm_query": query,
        "sysparm_fields": "number,state,assigned_to,sys_id",
        "sysparm_limit": 10,
        "sysparm_display_value": "true",
    }

    try:
        data = _get(url, params).get("result", [])
        return [
            {
                "number": item.get("number"),
                "state": _map_state(item.get("state")),
                "assigned_to": _display(item.get("assigned_to")),
                "sys_id": item.get("sys_id"),
            }
            for item in data
        ]
    except Exception as e:
        logger.error(f"[search_existing_ritms] error: {e}")
        return []


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------
def _display(value: any) -> Optional[str]:
    """
    ServiceNow reference fields come back as a dict with display_value
    when sysparm_display_value=true, or as a plain string otherwise.
    Handle both.
    """
    if isinstance(value, dict):
        return value.get("display_value") or value.get("value")
    return value or None