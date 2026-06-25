"""
ritm_client.py — ServiceNow user search and RITM creation.

Uses a shared requests.Session with retry logic and a simple TTL cache
for user lookups.
"""

import os
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv

env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Config — validated at import time so misconfiguration is caught early
# ------------------------------------------------------------------
SNOW_INSTANCE = os.getenv("SNOW_INSTANCE", "").strip().rstrip("/")
SNOW_USER = os.getenv("SNOW_USER", "").strip()
SNOW_PASS = os.getenv("SNOW_PASS", "").strip()

_missing = [k for k, v in [
    ("SNOW_INSTANCE", SNOW_INSTANCE),
    ("SNOW_USER", SNOW_USER),
    ("SNOW_PASS", SNOW_PASS),
] if not v]

if _missing:
    raise EnvironmentError(
        f"Missing required ServiceNow credentials: {', '.join(_missing)}. "
        "Set them in your .env file."
    )

_BASE_URL = f"https://{SNOW_INSTANCE}"

# ------------------------------------------------------------------
# Shared session with retry + connection pooling
# ------------------------------------------------------------------
_DEFAULT_TIMEOUT = (5.0, 15.0)  # (connect, read) seconds


def _build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=10,
        pool_maxsize=20,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.auth = (SNOW_USER, SNOW_PASS)
    session.headers.update({
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    return session


_SESSION: Optional[requests.Session] = None


def _session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = _build_session()
    return _SESSION


def _get(url: str, params: dict) -> dict:
    """Thin GET wrapper with consistent timeout and error surfacing."""
    resp = _session().get(url, params=params, timeout=_DEFAULT_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _post(url: str, payload: dict) -> dict:
    """Thin POST wrapper with consistent timeout and error surfacing."""
    resp = _session().post(url, json=payload, timeout=_DEFAULT_TIMEOUT)
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        # Attach response body to the exception for better diagnostics
        try:
            body = resp.json()
        except Exception:
            body = resp.text[:300]
        raise requests.HTTPError(
            f"HTTP {resp.status_code}: {body}", response=resp
        ) from e
    return resp.json()


# ------------------------------------------------------------------
# User search — with TTL cache
# ------------------------------------------------------------------
_user_cache: Dict[str, tuple] = {}   # query → (datetime, List[Dict])
_CACHE_TTL_SECONDS = 60


def search_users(query: str, use_cache: bool = True) -> List[Dict]:
    """
    Search sys_user by name or email (case-insensitive LIKE).

    Returns a list of {"sys_id", "name", "email"} dicts.
    Raises an Exception on network/API errors (let the caller handle messaging).
    """
    query = (query or "").strip()
    if len(query) < 2:
        return []

    cache_key = query.lower()
    if use_cache and cache_key in _user_cache:
        ts, cached = _user_cache[cache_key]
        if (datetime.now() - ts).total_seconds() < _CACHE_TTL_SECONDS:
            logger.debug(f"[search_users] cache hit for '{query}'")
            return cached

    url = f"{_BASE_URL}/api/now/table/sys_user"
    params = {
        "sysparm_query": f"nameLIKE{query}^ORemailLIKE{query}^active=true",
        "sysparm_fields": "sys_id,name,email",
        "sysparm_limit": 10,
    }

    try:
        data = _get(url, params)
        users: List[Dict] = [
            {
                "sys_id": u.get("sys_id"),
                "name": u.get("name"),
                "email": u.get("email"),
            }
            for u in data.get("result", [])
        ]
        _user_cache[cache_key] = (datetime.now(), users)
        logger.info(f"[search_users] '{query}' → {len(users)} result(s)")
        return users
    except Exception as e:
        logger.error(f"[search_users] failed for '{query}': {e}")
        raise Exception(f"ServiceNow user search error: {str(e)[:150]}")


def invalidate_user_cache(query: Optional[str] = None) -> None:
    """Clear the user search cache (all entries, or a specific query)."""
    if query:
        _user_cache.pop(query.lower(), None)
    else:
        _user_cache.clear()


# ------------------------------------------------------------------
# Create RITM (generic)
# ------------------------------------------------------------------
def create_ritm(data: Dict[str, Any]) -> Dict[str, Optional[str]]:
    catalog_item_id = (data.get("catalog_item_sys_id") or "").strip()
    variables = data.get("variables") or {}
    requested_for = data.get("requested_for")  # ✅ NEW

    if not catalog_item_id:
        raise ValueError("catalog_item_sys_id is required")
    if not variables:
        raise ValueError("variables dict must not be empty")

    url = f"{_BASE_URL}/api/sn_sc/servicecatalog/items/{catalog_item_id}/order_now"

    payload = {
        "sysparm_quantity": "1",
        "variables": variables,
    }

    # ✅ FIX: set requested_for correctly
    if requested_for:
        payload["requested_for"] = requested_for

    logger.info(
        f"[create_ritm] catalog_item={catalog_item_id} "
        f"variables_keys={list(variables.keys())}"
    )
    logger.info(f"[create_ritm] Full payload: {payload}")

    try:
        result_data = _post(url, payload)
        result = result_data.get("result", {})

        req_number = result.get("request_number")
        ritm_number = result.get("number")

        logger.info(f"[create_ritm] success — REQ={req_number} RITM={ritm_number}")

        return {
            "request_number": req_number,
            "ritm_number": ritm_number,
        }

    except requests.HTTPError as e:
        logger.error(f"[create_ritm] HTTP error: {e}")
        raise Exception(f"ServiceNow API error: {str(e)[:200]}")

    except Exception as e:
        logger.exception("[create_ritm] unexpected error")
        raise Exception(f"RITM creation failed: {str(e)[:200]}")


def update_request_field(request_number: str, field_name: str, field_value: str) -> bool:
    """
    Update a field on an sc_request record.
    
    Args:
        request_number: The request number (e.g., "REQ0010040")
        field_name: The field name (e.g., "requested_for")
        field_value: The sys_id or value to set
        
    Returns:
        True if successful, False otherwise.
    """
    if not request_number or not field_name or not field_value:
        logger.warning(f"[update_request_field] Missing required parameters")
        return False
        
    url = f"{_BASE_URL}/api/now/table/sc_request"
    
    # First get the sys_id of the request
    params = {
        "sysparm_query": f"number={request_number}",
        "sysparm_fields": "sys_id",
        "sysparm_limit": 1,
    }
    
    try:
        data = _get(url, params).get("result", [])
        if not data:
            logger.error(f"[update_request_field] Request {request_number} not found")
            return False
            
        request_sys_id = data[0].get("sys_id")
        if not request_sys_id:
            logger.error(f"[update_request_field] Could not extract sys_id for {request_number}")
            return False
            
        # Update the request field
        update_url = f"{_BASE_URL}/api/now/table/sc_request/{request_sys_id}"
        update_payload = {field_name: field_value}
        
        logger.info(f"[update_request_field] Updating {request_number}.{field_name}={field_value}")
        resp = _session().patch(update_url, json=update_payload, timeout=_DEFAULT_TIMEOUT)
        resp.raise_for_status()
        
        logger.info(f"[update_request_field] Successfully updated {request_number}")
        return True
        
    except Exception as e:
        logger.error(f"[update_request_field] Failed to update {request_number}: {e}")
        return False
