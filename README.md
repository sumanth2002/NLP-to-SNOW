# AI Ticket Generator — Technical Documentation

**Project:** Bitbucket Access Request Automation  
**Stack:** Python · FastAPI · Streamlit · TinyLlama (Ollama) · ServiceNow  
**Version:** 1.0  

---

## Overview

This system lets users create Bitbucket access requests in ServiceNow using plain English. Instead of filling out a form manually, the user describes what they need in a chat interface, and the AI agent extracts the intent, finds the right user in ServiceNow, and raises a RITM automatically.

---

## Architecture

```
User (Streamlit UI)
        │
        ▼
  FastAPI Backend  (main.py)
        │
        ▼
  ticket_agent()   (agent.py)
     │        │
     ▼        ▼
  LLM        SNOW User Search
 (llm.py)   (ritm_client.py)
                  │
                  ▼
          ServiceNow Catalog API
          (order_now → RITM)
```

---

## File Structure

```
├── main.py          # FastAPI app — exposes POST /create-ticket
├── agent.py         # Core logic — orchestrates LLM + SNOW calls
├── llm.py           # TinyLlama wrapper — intent detection & extraction
├── ritm_client.py   # ServiceNow API client — user search + RITM creation
├── app.py           # Streamlit frontend — chat UI
└── .env             # SNOW credentials (not committed)
```

---

## How It Works — Step by Step

### 1. User sends a message
The Streamlit frontend sends the user's prompt and any accumulated context to `POST /create-ticket`.

```json
{
  "prompt": "remove abel.tuter@example.com from bitbucket",
  "context": {}
}
```

### 2. Agent pre-processes the prompt
Before calling the LLM, `agent.py` runs lightweight checks:
- **Email regex** — if an email is in the prompt, it's extracted directly (no LLM needed)
- **Self-request detection** — phrases like "I need access" or "give me access" are caught and the agent asks for the user's name instead of searching for "me"

### 3. LLM analyses intent
`llm.py` sends the prompt to TinyLlama via Ollama and gets back a structured JSON:

```json
{
  "intent": "create_ticket",
  "request_type": "remove user",
  "search_term": "abel.tuter@example.com",
  "reply": null
}
```

**Three possible intents:**

| Intent | Meaning | Agent Action |
|---|---|---|
| `create_ticket` | User wants to add/remove someone | Search SNOW, create RITM |
| `ask_clarification` | Topic is Bitbucket but info is missing | Ask a follow-up question |
| `out_of_scope` | Nothing to do with Bitbucket | Politely redirect |

### 4. ServiceNow user search
`ritm_client.search_users()` queries `sys_user` table using `nameLIKE` or `emailLIKE`:

- **0 results** → ask user to try a different name
- **1 result** → proceed directly to ticket creation
- **2+ results** → show selectable cards in the UI

### 5. RITM creation
Once a `sys_id` is confirmed, `create_ritm()` calls the ServiceNow catalog `order_now` API:

```
POST /api/sn_sc/servicecatalog/items/{CATALOG_ITEM_SYS_ID}/order_now
```

Payload variables:
- `request_type` → `"user id"` (add) or `"remove"` (remove)
- `recipient` → `sys_id` of the selected user (reference field — plain text names don't work)

---

## Why sys_id and Not a Username

The **Recipient** field in the ServiceNow catalog item is a **reference field** pointing to the `sys_user` table. It only accepts a valid `sys_id`. Sending a plain name or email string will silently fail or be ignored. This is why we always search first and pass the `sys_id`.

---

## LLM Limitations & Mitigations

TinyLlama is a small 1.1B parameter model. It's fast and runs locally but makes mistakes. The following safeguards are in place:

| Problem | Mitigation |
|---|---|
| Returns `None` instead of `null` | `safe_parse_json()` replaces Python literals before parsing |
| Puts "bitbucket" or "access" as `search_term` | `INVALID_SEARCH_TERMS` blocklist sanitizes the output |
| Hallucinates regex code instead of JSON | Regex extracts first `{...}` block, discards everything else |
| Fails to extract email from sentences | Agent extracts email directly with regex before calling LLM |
| Misidentifies self-requests | `is_self_request()` catches "I need / give me / for me" patterns |
| LLM completely fails or crashes | Graceful fallback returns `ask_clarification` with a safe message |

---

## ServiceNow Configuration

| Setting | Value |
|---|---|
| Instance | `SNOW_INSTANCE` (from `.env`) |
| Catalog Item SYS ID | `20f46a670f908310bcf6c6e530d1b2d2` |
| Auth | Basic auth — `SNOW_USER` / `SNOW_PASS` |
| User table | `sys_user` |
| Catalog API | `/api/sn_sc/servicecatalog/items/{sys_id}/order_now` |

> **Note:** Only one catalog item is currently configured — Bitbucket access. The system is intentionally scoped to Bitbucket only. Any other service requests are rejected by the LLM and shown as out-of-scope.

---

## Environment Variables

Create a `.env` file in the project root:

```env
SNOW_INSTANCE=your-instance.service-now.com
SNOW_USER=your_username
SNOW_PASS=your_password
```

---

## Running the Project

```bash
# 1. Start Ollama with TinyLlama
ollama run tinyllama

# 2. Start the FastAPI backend
uvicorn main:app --reload --port 8000

# 3. Start the Streamlit frontend
streamlit run app.py
```

Default ports:
- Streamlit UI: `http://localhost:8501`
- FastAPI backend: `http://localhost:8000`
- Ollama: `http://localhost:11434`

---

## API Reference

### `POST /create-ticket`

**Request:**
```json
{
  "prompt": "add john smith to bitbucket",
  "context": {
    "request_type": "add user"
  }
}
```

**Response — Success:**
```json
{
  "status": "success",
  "request_number": "REQ0012345",
  "ritm_number": "RITM0012346"
}
```

**Response — Needs more info:**
```json
{
  "status": "incomplete",
  "message": "Who should be added? Please enter their name or email.",
  "context": { "request_type": "add user" },
  "show_type_buttons": false
}
```

**Response — Multiple users found:**
```json
{
  "status": "select_user",
  "message": "Found 3 users matching 'john'. Please select one:",
  "users": [
    { "name": "John Smith", "email": "john.smith@example.com", "sys_id": "abc123" }
  ],
  "context": { "request_type": "add user" }
}
```

**Response — Error:**
```json
{
  "status": "failure",
  "message": "ServiceNow returned 401 Unauthorized"
}
```

---

## Frontend Features

- **Natural language input** — no forms, just chat
- **Add / Remove buttons** — shown only when the LLM cannot determine the action from the prompt
- **User selection cards** — shown when SNOW returns multiple matches
- **Processing spinner** — visible while backend is working, input disabled to prevent duplicate sends
- **Ticket history** — sidebar shows all tickets created in the current session with RITM, request number, type, recipient, and timestamp

---

## Known Limitations

- Ticket history is **session-only** — it resets on browser refresh (no database persistence yet)
- Only **Bitbucket** catalog item is configured in ServiceNow
- TinyLlama can still occasionally misparse complex prompts — the safeguards catch most cases but a larger model (e.g. Llama 3) would be more reliable
- No authentication on the FastAPI backend — intended for internal network use only
