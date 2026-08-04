# NLP-to-SNOW — Setup & Run Guide

**Stack:** Python · FastAPI · Streamlit · Google Gemini 2.5 Flash · ServiceNow · Splunk  
**Version:** 2.0

---

## What This Does

A conversational AI agent that lets you:

1. **Create ServiceNow tickets** via plain English chat — supports Bitbucket and Jira catalog items (user access, project creation, repo permissions).
2. **Query Splunk logs** — describe what you want in natural language and get back results, an AI summary, and an outage analysis.

You type; the agent figures out what you need, collects any missing fields through follow-up questions, and submits the ticket or executes the log query for you.

---

## Prerequisites

| Requirement | Minimum version | Notes |
|---|---|---|
| Python | 3.10+ | 3.13 used in `.venv` |
| ServiceNow instance | Any PDI / dev instance | Basic auth credentials required |
| Google Gemini API key | — | Free tier works; quota limits apply |
| Splunk (optional) | 8.x+ | Only needed for log queries |

---

## Project Structure

```
NLP-to-SNOW/
├── backend/
│   ├── main.py                  # FastAPI app — /create-ticket and /splunk-query
│   ├── agent.py                 # Conversational ticket agent (state machine)
│   ├── llm.py                   # Gemini calls: intent detection, field questions, payload build
│   ├── splunk_agent.py          # NL → SPL pipeline + outage analysis
│   ├── splunk_client.py         # Splunk REST API wrapper
│   ├── ritm_client.py           # ServiceNow user search + RITM creation
│   ├── servicenow_client.py     # Ticket lookup and duplicate checks
│   └── ticket_catalogue.json   # Defines supported apps, ticket types, and their fields
├── frontend/
│   └── app.py                   # Streamlit chat UI
├── requirements.txt
└── .env                         # Your credentials (never committed)
```

---

## Step 1 — Clone and Create a Virtual Environment

```bash
git clone <repo-url>
cd NLP-to-SNOW

python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate
```

---

## Step 2 — Install Dependencies

```bash
pip install -r requirements.txt
```

---

## Step 3 — Configure Environment Variables

Create a `.env` file in the **`backend/`** folder (the backend reads from there):

```
NLP-to-SNOW/backend/.env
```

Paste in the following and fill in your values:

```env
# ── ServiceNow ──────────────────────────────────────────────────────
SNOW_INSTANCE=your-instance.service-now.com
SNOW_USER=your_username
SNOW_PASS=your_password

# ── Google Gemini ────────────────────────────────────────────────────
GEMINI_API_KEY=your_gemini_api_key

# ── ServiceNow Catalog Item SYS IDs ─────────────────────────────────
# Find these in ServiceNow: Service Catalog > Items > (open the item) > copy sys_id from the URL

CATALOG_ITEM_BITBUCKET_SYS_ID=<bitbucket_user_access_catalog_item_sys_id>
CATALOG_ITEM_BITBUCKET_PROJECT_SYS_ID=<bitbucket_project_creation_catalog_item_sys_id>
CATALOG_ITEM_BITBUCKET_REPO_SYS_ID=<bitbucket_repo_permission_catalog_item_sys_id>
CATALOG_ITEM_JIRA_SYS_ID=<jira_user_access_catalog_item_sys_id>
CATALOG_ITEM_JIRA_PROJECT_SYS_ID=<jira_project_creation_catalog_item_sys_id>

# ── Splunk (only needed for log queries) ─────────────────────────────
SPLUNK_URL=https://your-splunk-host:8089
SPLUNK_USER=admin
SPLUNK_PASS=your_splunk_password
```

> **Where to get each value:**
>
> - `SNOW_INSTANCE` — the hostname of your ServiceNow PDI, e.g. `dev12345.service-now.com` (no `https://`).
> - `GEMINI_API_KEY` — get a free key at [aistudio.google.com](https://aistudio.google.com/apikey).
> - `CATALOG_ITEM_*_SYS_ID` — in ServiceNow, navigate to **Service Catalog → Catalog Items**, open the relevant item, and copy the `sys_id` from the URL or the record's **i** button.
> - `SPLUNK_URL` — the Splunk management port (default `8089`); leave the default values if not using Splunk.

---

## Step 4 — Run the Backend

Open a terminal in the `backend/` directory and start the FastAPI server:

```bash
cd NLP-to-SNOW/backend
uvicorn main:app --reload --port 8003
```

The API will be available at `http://localhost:8003`.  
You can browse the auto-generated docs at `http://localhost:8003/docs`.

To confirm it is running:

```bash
curl http://localhost:8003/health
# {"status":"ok","version":"2.0.0"}
```

---

## Step 5 — Run the Frontend

Open a **second** terminal (keep the backend running) and start Streamlit:

```bash
cd NLP-to-SNOW/frontend
streamlit run app.py
```

The UI opens automatically at `http://localhost:8501`.

---

## Quick Smoke Test

Once both processes are running, open `http://localhost:8501` and try:

| What to type | Expected behaviour |
|---|---|
| `I need access to Bitbucket` | Agent asks for action (add/remove) and then for the user's name |
| `create a Jira project` | Agent collects project name, key, lead, and type |
| `status of REQ0012345` | Agent returns the ticket status from ServiceNow |
| `show tickets` | Lists the most recent 50 requests |
| `get me yesterday's logs from the dummy index` | Splunk query runs and returns a table + AI summary |

---

## Supported Ticket Types

Defined in [`backend/ticket_catalogue.json`](backend/ticket_catalogue.json).

| App | Ticket type | Fields collected |
|---|---|---|
| Bitbucket | User access | Action (add/remove), User |
| Bitbucket | Project creation | Project name, Project key, Project lead, Visibility |
| Bitbucket | Repo permission change | Repository name, User, Permission level (read/write/admin) |
| Jira | User access | Action (add/remove), User |
| Jira | Project creation | Project name, Project key, Project lead, Project type |

To add a new ticket type, add an entry to `ticket_catalogue.json` and set the corresponding `CATALOG_ITEM_*_SYS_ID` env var.

---

## Architecture Overview

```
Browser (Streamlit — port 8501)
        │
        │  POST /create-ticket  or  POST /splunk-query
        ▼
FastAPI backend (main.py — port 8003)
        │
        ├── ticket_agent()   (agent.py)
        │       │
        │       ├── detect_intent()   ──►  Gemini 2.5 Flash  (llm.py)
        │       ├── ask_next_field()  ──►  Gemini 2.5 Flash  (llm.py)
        │       ├── build_payload()   ──►  Gemini 2.5 Flash  (llm.py)
        │       ├── search_users()    ──►  ServiceNow sys_user  (ritm_client.py)
        │       └── create_ritm()     ──►  ServiceNow order_now API  (ritm_client.py)
        │
        └── splunk_agent()   (splunk_agent.py)
                │
                ├── NL → SPL plan     ──►  Gemini 2.5 Flash
                ├── run_search()      ──►  Splunk REST API  (splunk_client.py)
                ├── _summarise_logs() ──►  Gemini 2.5 Flash
                └── _analyse_for_outage() ──►  Gemini 2.5 Flash
```

---

## API Reference

### `POST /create-ticket`

Conversational ticket endpoint. The client must echo back the `context` object from each response so the agent can maintain conversation state across turns.

**Request:**
```json
{
  "prompt": "add john smith to bitbucket",
  "context": {}
}
```

**Response — needs more info (`incomplete`):**
```json
{
  "status": "incomplete",
  "message": "What is the Action? Choose one of: add user, remove user.",
  "context": { "state": "COLLECTING_FIELDS", "..." : "..." },
  "ui_action": "select_options",
  "options": ["add user", "remove user"]
}
```

**Response — multiple users found (`select_user`):**
```json
{
  "status": "select_user",
  "message": "I found 2 matches for 'john'. Pick the right one:",
  "users": [
    { "name": "John Smith", "email": "john.smith@example.com", "sys_id": "abc123" }
  ],
  "context": { "..." : "..." }
}
```

**Response — ticket created (`success`):**
```json
{
  "status": "success",
  "request_number": "REQ0012345",
  "ritm_number": "RITM0012346",
  "message": "Done! Your ticket has been submitted.",
  "context": { "state": "INIT" }
}
```

**Response — error:**
```json
{
  "status": "failure",
  "message": "ServiceNow returned 401 Unauthorized",
  "context": {}
}
```

---

### `POST /splunk-query`

Natural-language log search endpoint.

**Request:**
```json
{ "prompt": "show me yesterday's errors from the nginx index" }
```

**Response:**
```json
{
  "status": "success",
  "message": "🔍 Query: `search index=nginx log_level=ERROR | head 100`\n\n✅ Found 14 event(s).",
  "spl": "search index=nginx log_level=ERROR | head 100",
  "count": 14,
  "fields": ["_time", "_raw", "host", "source"],
  "rows": [ { "_time": "...", "_raw": "..." } ],
  "ai_summary": "14 error-level events found ...",
  "outage_analysis": {
    "outage_detected": false,
    "severity": "none",
    "title": "No outage detected",
    "signals": [],
    "recommendation": "No action required."
  }
}
```

---

### `GET /health`

```json
{ "status": "ok", "version": "2.0.0" }
```

---

## How the Ticket Agent Works

The agent in [`agent.py`](backend/agent.py) is a stateless function — all state is held in the `context` dict that the frontend echoes back on every request.

```
State machine:
  INIT
    └─► detect intent (Gemini)
          ├─► CLARIFYING      — app or ticket type unclear; ask a follow-up
          └─► COLLECTING_FIELDS — schema known; start asking for fields
                ├─► AWAITING_USER_SELECT — user search returned > 1 result
                └─► AWAITING_CONFIRMATION — all fields collected; ask to confirm
                      └─► _submit() — call ServiceNow order_now API → success
```

**Field types supported:**

| Type | Behaviour |
|---|---|
| `text` | Free text; validated by regex if a validator is configured |
| `select` | Renders as clickable buttons in the UI; rejects invalid values |
| `user_search` | Searches `sys_user` in ServiceNow; shows selection cards for multiple matches |

---

## Gemini API Quota

The project uses `gemini-2.5-flash`. The free tier has per-minute and per-day rate limits. If you hit a quota, the UI shows:

> ⏳ Gemini API daily quota reached. Please cool down and retry in Xs.

The agent will resume normally once the quota resets.

---

## Troubleshooting

### Backend fails to start with `Missing required ServiceNow credentials`

The `.env` file must be in `backend/`, not the project root:

```
NLP-to-SNOW/backend/.env   ✅ correct
NLP-to-SNOW/.env           ❌ not read by the backend
```

### `GEMINI_API_KEY is not set`

Check that your `.env` file has `GEMINI_API_KEY=...` and that you activated the virtual environment before running `uvicorn`.

### ServiceNow returns 401

Verify `SNOW_USER` and `SNOW_PASS` in `.env`. On a PDI, use the admin credentials or a user with the `catalog` and `itil` roles.

### Catalog item not found / ticket created with wrong user

Each `CATALOG_ITEM_*_SYS_ID` must match the exact sys_id of the catalog item in your ServiceNow instance. Copy it from the URL when you open the item in the catalog admin view.

### Splunk connection refused

Confirm `SPLUNK_URL` points to the Splunk management port (default `8089`), not the UI port (`8000`). Self-signed cert warnings are suppressed automatically.

### Frontend can't reach the backend

The frontend is hardcoded to `http://localhost:8003`. Ensure the FastAPI server is running on that port, or update `BACKEND_URL` at the top of [`frontend/app.py`](frontend/app.py).

---

## Known Limitations

- Ticket history is session-only — resets on browser refresh (no database persistence).
- The Gemini free tier has daily quota limits; a paid API key avoids interruptions.
- No authentication on the FastAPI backend — intended for internal/local use only.
