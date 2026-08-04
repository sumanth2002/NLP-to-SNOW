"""
splunk_client.py — Thin wrapper around the Splunk REST API.

Uses the Splunk search/jobs endpoint (blocking dispatch) so we get
results back in a single round-trip without polling.

Splunk REST docs:
  POST /services/search/jobs          — create a search job
  GET  /services/search/jobs/{sid}/results — fetch results
"""

import logging
import time
import requests
import os
from typing import Any, Dict, List, Optional
from requests.auth import HTTPBasicAuth
from urllib3.exceptions import InsecureRequestWarning
from urllib3 import disable_warnings

# Splunk's default self-signed cert — suppress the warning for local dev
disable_warnings(InsecureRequestWarning)

logger = logging.getLogger(__name__)

SPLUNK_URL  = os.getenv("SPLUNK_URL",  "https://localhost:8089")
SPLUNK_USER = os.getenv("SPLUNK_USER", "admin")
SPLUNK_PASS = os.getenv("SPLUNK_PASS", "admin1234")

_AUTH    = HTTPBasicAuth(SPLUNK_USER, SPLUNK_PASS)
_HEADERS = {"Content-Type": "application/x-www-form-urlencoded"}

# Maximum rows returned to the UI (keeps responses readable)
_MAX_RESULTS = 100


def run_search(spl: str, earliest: str = "-1d@d", latest: str = "@d") -> Dict[str, Any]:
    """
    Submit a blocking SPL search and return the results dict.

    Returns:
      {
        "success": True,
        "fields":  ["_time", "host", ...],
        "rows":    [ {...}, {...}, ... ],
        "count":   int,
        "spl":     str   # the exact SPL that was run
      }
    or on error:
      { "success": False, "error": "..." }
    """
    # Ensure query starts with 'search'
    spl_trimmed = spl.strip()
    if not spl_trimmed.lower().startswith("search"):
        spl_trimmed = f"search {spl_trimmed}"

    # Build job parameters.
    # Pass earliest/latest as separate API params so Splunk handles both
    # relative ("-1d@d") and absolute ("MM/DD/YYYY:HH:MM:SS") formats natively.
    job_data: Dict[str, Any] = {
        "search":      spl_trimmed,
        "exec_mode":   "blocking",
        "output_mode": "json",
    }
    if earliest and earliest != "alltime":
        job_data["earliest_time"] = earliest
        job_data["latest_time"]   = latest

    logger.info(f"[splunk] running SPL: {spl_trimmed!r}  earliest={earliest!r} latest={latest!r}")

    # ------------------------------------------------------------------
    # 1. Create job (exec_mode=blocking waits until the job is done)
    # ------------------------------------------------------------------
    try:
        job_resp = requests.post(
            f"{SPLUNK_URL}/services/search/jobs",
            auth=_AUTH,
            headers=_HEADERS,
            data=job_data,
            verify=False,
            timeout=60,
        )
        job_resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"[splunk] job creation failed: {e}")
        return {"success": False, "error": f"Could not connect to Splunk: {e}"}

    sid = job_resp.json().get("sid")
    if not sid:
        return {"success": False, "error": "Splunk did not return a job SID."}

    # ------------------------------------------------------------------
    # 2. Fetch results
    # ------------------------------------------------------------------
    try:
        results_resp = requests.get(
            f"{SPLUNK_URL}/services/search/jobs/{sid}/results",
            auth=_AUTH,
            params={
                "output_mode": "json",
                "count":       _MAX_RESULTS,
            },
            verify=False,
            timeout=60,
        )
        results_resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"[splunk] results fetch failed: {e}")
        return {"success": False, "error": f"Failed to retrieve results: {e}"}

    data = results_resp.json()
    results: List[Dict] = data.get("results", [])
    fields: List[str]   = [f["name"] for f in data.get("fields", [])]

    return {
        "success": True,
        "fields":  fields,
        "rows":    results,
        "count":   len(results),
        "spl":     spl_trimmed,
    }
