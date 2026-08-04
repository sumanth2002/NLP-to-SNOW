"""
splunk_agent.py — Natural language → SPL query pipeline.

Flow:
  1. LLM receives the user's plain-English request.
  2. LLM returns JSON with:
       - spl:       the SPL search string (WITHOUT index — index is inferred)
       - index:     the Splunk index name (user says "app" → index name)
       - earliest:  relative time string  e.g. "-1d@d"
       - latest:    relative time string  e.g. "@d"
  3. splunk_client.run_search() executes the query.
  4. We return a formatted summary + raw rows back to the caller.
"""

import json
import logging
import re
from typing import Optional, Dict, Any

import os
from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types

from splunk_client import run_search
from llm import QuotaExceededError, _is_quota_error, _extract_retry_seconds

load_dotenv()
logger = logging.getLogger(__name__)

_api_key = os.getenv("GEMINI_API_KEY")
if not _api_key:
    raise EnvironmentError("GEMINI_API_KEY is not set in environment / .env")

_client = genai.Client(api_key=_api_key)
_MODEL  = "gemini-2.5-flash"
_CONFIG = genai_types.GenerateContentConfig(
    response_mime_type="application/json",
    temperature=0.0,
)


def _generate(prompt: str) -> str:
    resp = _client.models.generate_content(
        model=_MODEL,
        contents=prompt,
        config=_CONFIG,
    )
    return resp.text

# ------------------------------------------------------------------
# Prompt sent to the LLM
# ------------------------------------------------------------------
_SPLUNK_SYSTEM = """You are a Splunk search expert. Convert the user's natural-language
log request into a valid Splunk search.

IMPORTANT TERMINOLOGY:
- When the user says "app" or "application" they mean the Splunk **index** name (e.g. index=app).
- When the user mentions a specific name like "dummy", "nginx", "apache" etc.
  that is the **index name** to search.
- For security/investigation queries (account lock, failed login, error, exception, outage etc.)
  build an appropriate SPL filter. Examples:
    - "account lock" → search for "account locked" OR "lockout" OR "locked" in _raw
    - "failed login" → search for "authentication failed" OR "login failed" in _raw
    - "exception" → search for "exception" OR "error" in _raw
    - "outage" or "check for outage" → search for "error" OR "exception" OR "timeout" OR
      "unavailable" OR "connection refused" OR "500" OR "503" OR "down" in _raw

TIME RANGE RULES — read carefully:
- "yesterday"      → earliest="-1d@d", latest="@d"
- "today"          → earliest="@d",    latest="now"
- "last hour"      → earliest="-1h",   latest="now"
- "last 7 days"    → earliest="-7d@d", latest="now"
- "last 24 hours"  → earliest="-24h",  latest="now"
- "latest", "recent", "any time", "anytime", "all time", no time mentioned
                   → earliest="alltime", latest="now"

ABSOLUTE TIME RANGES — when the user gives specific dates/times:
- Convert them to Splunk absolute time format: "MM/DD/YYYY:HH:MM:SS"
- Examples:
    - "from 2pm to 4pm"                  → earliest="HH/DD/YYYY:14:00:00", latest="HH/DD/YYYY:16:00:00"
      (use today's date for HH/DD/YYYY)
    - "from 2pm to 4pm today"            → same as above but explicit today
    - "from 2pm yesterday to 4pm yesterday" → use yesterday's date for both
    - "from 2024-01-15 14:00 to 2024-01-15 16:00"
                                         → earliest="01/15/2024:14:00:00", latest="01/15/2024:16:00:00"
    - "from 2024-01-15 to 2024-01-16"   → earliest="01/15/2024:00:00:00", latest="01/16/2024:23:59:59"
    - "from January 15 2pm to 6pm"      → earliest="01/15/YYYY:14:00:00", latest="01/15/YYYY:18:00:00"
      (use current year if not specified)
    - "between 10am and 11am"            → earliest="MM/DD/YYYY:10:00:00", latest="MM/DD/YYYY:11:00:00"
      (use today's date)
- ALWAYS produce a valid "MM/DD/YYYY:HH:MM:SS" string for absolute times.
  Do NOT use relative strings like "-1d@d" when an absolute time was given.

Return ONLY valid JSON with these exact keys:
{
  "index":    "the index name extracted from the user message (string, no 'index=' prefix)",
  "spl":      "the full SPL query including index=... e.g. search index=dummy | head 100",
  "earliest": "Splunk time string — relative (e.g. -1d@d) OR absolute (MM/DD/YYYY:HH:MM:SS)",
  "latest":   "Splunk time string — relative (e.g. @d)    OR absolute (MM/DD/YYYY:HH:MM:SS)",
  "description": "one-sentence plain-English description of what this query does"
}

Rules:
- Always include `index=<name>` in the spl field.
- Keep the SPL simple and correct.
- Do NOT wrap spl in quotes inside the JSON value.
- If no specific fields are requested, use `| head 100` to limit results.
- If the user asks for errors, add `| where like(lower(_raw), "%error%")` or
  use sourcetype/log_level filtering if obvious.
- If the user asks to "check for outage", "any outage", "was there an outage" — add a broad
  error-signal filter so you only retrieve relevant events:
  `(_raw="*error*" OR _raw="*exception*" OR _raw="*timeout*" OR _raw="*unavailable*" OR _raw="*connection refused*" OR _raw="*500*" OR _raw="*503*" OR _raw="*down*")`
- If the user combines a time range AND an outage/error check, apply BOTH the time range
  and the error filter together.

Examples:
User: "get me yesterday's logs from the app dummy"
Output: {"index":"dummy","spl":"search index=dummy | head 100","earliest":"-1d@d","latest":"@d","description":"All events from index=dummy for yesterday"}

User: "show me error logs from nginx today"
Output: {"index":"nginx","spl":"search index=nginx log_level=ERROR | head 100","earliest":"@d","latest":"now","description":"Error-level events from index=nginx today"}

User: "last 7 days of logs from payments index"
Output: {"index":"payments","spl":"search index=payments | head 100","earliest":"-7d@d","latest":"now","description":"All events from index=payments over the last 7 days"}

User: "get me the latest lines of log for dummy application"
Output: {"index":"dummy","spl":"search index=dummy | head 100","earliest":"alltime","latest":"now","description":"Latest 100 events from index=dummy across all time"}

User: "show me recent logs from app dummy"
Output: {"index":"dummy","spl":"search index=dummy | head 100","earliest":"alltime","latest":"now","description":"Latest 100 events from index=dummy across all time"}

User: "can you check if there was an account lock for any user from dummy application"
Output: {"index":"dummy","spl":"search index=dummy (\"account locked\" OR \"lockout\" OR \"locked\" OR \"lock\") | head 100","earliest":"alltime","latest":"now","description":"Account lockout events from index=dummy"}

User: "find any failed logins from dummy app"
Output: {"index":"dummy","spl":"search index=dummy (\"authentication failed\" OR \"login failed\" OR \"invalid password\") | head 100","earliest":"alltime","latest":"now","description":"Failed login events from index=dummy"}

User: "get logs from dummy from 2pm to 4pm"
Output: {"index":"dummy","spl":"search index=dummy | head 100","earliest":"MM/DD/YYYY:14:00:00","latest":"MM/DD/YYYY:16:00:00","description":"Events from index=dummy between 2pm and 4pm today"}

User: "get logs from dummy from 2024-06-10 10:00 to 2024-06-10 12:00"
Output: {"index":"dummy","spl":"search index=dummy | head 100","earliest":"06/10/2024:10:00:00","latest":"06/10/2024:12:00:00","description":"Events from index=dummy on 2024-06-10 between 10am and 12pm"}

User: "get yesterday's logs from dummy and check if there was an outage"
Output: {"index":"dummy","spl":"search index=dummy (_raw=\"*error*\" OR _raw=\"*exception*\" OR _raw=\"*timeout*\" OR _raw=\"*unavailable*\" OR _raw=\"*500*\" OR _raw=\"*503*\" OR _raw=\"*down*\") | head 100","earliest":"-1d@d","latest":"@d","description":"Outage-signal events from index=dummy for yesterday"}

User: "check for outage in dummy app from 2pm to 6pm"
Output: {"index":"dummy","spl":"search index=dummy (_raw=\"*error*\" OR _raw=\"*exception*\" OR _raw=\"*timeout*\" OR _raw=\"*unavailable*\" OR _raw=\"*500*\" OR _raw=\"*503*\" OR _raw=\"*down*\") | head 100","earliest":"MM/DD/YYYY:14:00:00","latest":"MM/DD/YYYY:18:00:00","description":"Outage-signal events from index=dummy between 2pm and 6pm today"}
"""


def _safe_json(text: str) -> Optional[dict]:
    if not text:
        return None
    try:
        return json.loads(text.strip())
    except Exception:
        pass
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            pass
    return None


def is_splunk_query(prompt: str) -> bool:
    """
    Quick heuristic to detect if the user is asking for Splunk logs
    rather than a ServiceNow ticket.
    """
    pl = prompt.lower()
    log_keywords = [
        "log", "logs", "splunk", "error log", "search log",
        "show me", "get me", "fetch", "retrieve", "query log",
        "yesterday's log", "today's log", "last hour", "last 24"
    ]
    return any(kw in pl for kw in log_keywords)


def splunk_agent(prompt: str) -> Dict[str, Any]:
    """
    Converts a plain-English log request into a Splunk query,
    executes it, and returns a structured result.

    Returns:
      {
        "status":      "success" | "failure",
        "description": str,
        "spl":         str,
        "count":       int,
        "fields":      [...],
        "rows":        [...],
        "message":     str   (user-facing summary)
      }
    """
    # ------------------------------------------------------------------
    # Step 1: LLM → SPL plan
    # ------------------------------------------------------------------
    try:
        plan = _safe_json(_generate(f"{_SPLUNK_SYSTEM}\n\nUser: {prompt}"))
    except QuotaExceededError as e:
        return {
            "status":  "quota",
            "message": f"⏳ Gemini API daily quota reached. Please cool down and retry in **{e.retry_seconds}s**.",
        }
    except Exception as e:
        logger.error(f"[splunk_agent] LLM call failed: {e}")
        return {
            "status": "failure",
            "message": f"LLM could not parse your request: {e}",
        }

    if not plan or "spl" not in plan:
        return {
            "status": "failure",
            "message": "I couldn't understand the log request. Please be more specific, e.g. 'show me yesterday's logs from the dummy index'.",
        }

    spl       = plan["spl"]
    earliest  = plan.get("earliest", "-24h")
    latest    = plan.get("latest",   "now")
    desc      = plan.get("description", spl)

    logger.info(f"[splunk_agent] plan={plan}")

    # ------------------------------------------------------------------
    # Step 2: Execute query
    # ------------------------------------------------------------------
    result = run_search(spl, earliest=earliest, latest=latest)

    if not result["success"]:
        return {
            "status":  "failure",
            "message": f"Splunk error: {result['error']}",
            "spl":     spl,
        }

    count  = result["count"]
    fields = result["fields"]
    rows   = result["rows"]

    # ------------------------------------------------------------------
    # Step 3: Build the header message
    # ------------------------------------------------------------------
    if count == 0:
        summary = f"🔍 **Query:** `{spl}`\n\n⚠️ No results found for: _{desc}_"
        return {
            "status":      "success",
            "message":     summary,
            "description": desc,
            "spl":         spl,
            "count":       count,
            "fields":      fields,
            "rows":        rows,
            "ai_summary":  "",
        }

    summary = (
        f"🔍 **Query:** `{spl}`\n\n"
        f"📋 _{desc}_\n\n"
        f"✅ Found **{count}** event(s)."
    )

    # ------------------------------------------------------------------
    # Step 4: LLM summarises the log content + outage analysis
    # ------------------------------------------------------------------
    ai_summary      = _summarise_logs(rows, desc)
    outage_analysis = _analyse_for_outage(rows)

    return {
        "status":           "success",
        "message":          summary,
        "description":      desc,
        "spl":              spl,
        "count":            count,
        "fields":           fields,
        "rows":             rows,
        "ai_summary":       ai_summary,
        "outage_analysis":  outage_analysis,
    }


# ---------- Log summary prompt ----------
_SUMMARY_SYSTEM = """You are a log analyst. Given a list of application log lines,
produce a concise plain-English summary for a developer or ops engineer.

Focus on:
- Overall health / activity pattern
- Any errors, warnings, or exceptions — list them specifically
- Recurring events or patterns
- Any anomalies worth noting

Keep it under 150 words. Use bullet points. Do NOT repeat the raw log lines verbatim."""


def _summarise_logs(rows: list, description: str) -> str:
    """Send up to 50 raw log lines to the LLM and return a plain-English summary."""
    lines    = [r.get("_raw", str(r)) for r in rows[:50]]
    log_text = "\n".join(lines)

    prompt = (
        f"{_SUMMARY_SYSTEM}\n\n"
        f"Context: {description}\n\n"
        f"Log lines:\n{log_text}"
    )

    from google.genai import types as _t
    text_config = _t.GenerateContentConfig(temperature=0.2)

    last_err = None
    for attempt in range(2):          # try twice before giving up
        try:
            resp = _client.models.generate_content(
                model=_MODEL, contents=prompt, config=text_config
            )
            text = (resp.text or "").strip()
            if text:
                return text
        except Exception as e:
            last_err = e
            if _is_quota_error(e):
                secs = _extract_retry_seconds(e)
                logger.warning(f"[splunk_agent] summary quota hit, retry in {secs}s")
                return f"_(AI summary unavailable — quota reached. Retry in {secs}s)_"
            logger.warning(f"[splunk_agent] summary attempt {attempt+1} failed: {e}")

    logger.error(f"[splunk_agent] summary failed after retries: {last_err}")
    return "_(AI summary unavailable — could not reach the LLM)_"


# ---------- Outage analysis prompt ----------
_OUTAGE_SYSTEM = """You are an SRE (Site Reliability Engineer) analysing application logs
for signs of an outage or service degradation.

Examine the log lines and return ONLY valid JSON with this exact schema:
{
  "outage_detected": true | false,
  "severity": "critical" | "high" | "medium" | "low" | "none",
  "title": "short one-line description, e.g. 'Database connection failures detected'",
  "signals": ["bullet 1", "bullet 2", ...],
  "recommendation": "one-sentence recommended action"
}

Outage signals to look for:
- HTTP 5xx errors (500, 502, 503, 504)
- Connection refused / timeout / timed out
- Service unavailable / down / unreachable
- OOM / out of memory / killed
- Crash / panic / fatal / segfault
- Database errors (deadlock, connection pool exhausted, query timeout)
- High error rate or repeated identical errors
- Circuit breaker open
- Dependency failure (external API down, queue full)

Severity guide:
  critical — service is completely down or data loss risk
  high     — major feature broken, significant user impact
  medium   — degraded performance, partial failures
  low      — minor / isolated errors, normal noise
  none     — no outage signals found

If outage_detected is false, set severity to "none", title to "No outage detected",
signals to [], and recommendation to "No action required."

Do NOT include any text outside the JSON object."""


def _analyse_for_outage(rows: list) -> dict:
    """
    Runs the outage-detection LLM pass on up to 100 log lines.
    Returns a dict with keys: outage_detected, severity, title, signals, recommendation.
    On any failure returns a safe 'unknown' dict so the caller can always render something.
    """
    _SAFE_DEFAULT = {
        "outage_detected": False,
        "severity": "none",
        "title": "Outage analysis unavailable",
        "signals": [],
        "recommendation": "Could not analyse logs for outage signals.",
    }

    if not rows:
        return {
            "outage_detected": False,
            "severity": "none",
            "title": "No events to analyse",
            "signals": [],
            "recommendation": "No log lines were returned by the query.",
        }

    lines    = [r.get("_raw", str(r)) for r in rows[:100]]
    log_text = "\n".join(lines)

    prompt = f"{_OUTAGE_SYSTEM}\n\nLog lines:\n{log_text}"

    try:
        raw = _generate(prompt)
        parsed = _safe_json(raw)
        if parsed and "outage_detected" in parsed:
            return parsed
        logger.warning(f"[splunk_agent] outage parse failed, raw={raw[:200]!r}")
        return _SAFE_DEFAULT
    except QuotaExceededError as e:
        logger.warning(f"[splunk_agent] outage analysis quota hit: {e}")
        return {**_SAFE_DEFAULT, "recommendation": f"Quota reached — retry in {e.retry_seconds}s."}
    except Exception as e:
        logger.error(f"[splunk_agent] outage analysis failed: {e}")
        return _SAFE_DEFAULT
