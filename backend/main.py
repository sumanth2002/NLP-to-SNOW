"""
main.py — FastAPI entry point for the ServiceNow Ticket Agent.
"""

import logging
import traceback
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from agent import ticket_agent

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title="ServiceNow Ticket Agent",
    description="Conversational agent for raising and checking ServiceNow tickets.",
    version="2.0.0",
)

# ------------------------------------------------------------------
# CORS — tighten origins in production
# ------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8501", "http://127.0.0.1:8501"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------
# Global exception handler — catches anything the agent misses
# ------------------------------------------------------------------
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception on {request.url}: {exc}\n{traceback.format_exc()}")
    return JSONResponse(
        status_code=500,
        content={
            "status": "failure",
            "message": "An unexpected error occurred. Please try again.",
            "context": {},
        },
    )


# ------------------------------------------------------------------
# Request / Response models
# ------------------------------------------------------------------
class TicketRequest(BaseModel):
    prompt: str
    context: Optional[Dict[str, Any]] = None

    @field_validator("prompt")
    @classmethod
    def prompt_must_not_be_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("prompt cannot be blank")
        return v.strip()


class TicketResponse(BaseModel):
    status: str
    message: Optional[str] = None
    context: Dict[str, Any] = {}
    # Optional fields returned on specific statuses
    request_number: Optional[str] = None
    ritm_number: Optional[str] = None
    users: Optional[List[Dict[str, Any]]] = None
    ui_action: Optional[str] = None
    options: Optional[List[str]] = None


# ------------------------------------------------------------------
# Main endpoint
# ------------------------------------------------------------------
@app.post("/create-ticket", response_model=TicketResponse)
async def create_ticket(req: TicketRequest):
    """
    Conversational ticket creation / status-check endpoint.
    The client must echo back the `context` dict from each response
    so the agent can continue the conversation.
    """
    prompt = req.prompt
    context = req.context or {}

    logger.info(f"[/create-ticket] state={context.get('state','INIT')} prompt={prompt!r}")

    try:
        result = ticket_agent(prompt, context)
    except Exception as e:
        logger.error(f"Agent crash: {e}\n{traceback.format_exc()}")
        return TicketResponse(
            status="failure",
            message=f"Internal agent error: {str(e)[:200]}",
            context=context,
        )

    if not isinstance(result, dict):
        return TicketResponse(
            status="failure",
            message="Agent returned unexpected data.",
            context=context,
        )

    # Normalise status field
    if "status" not in result:
        result["status"] = "error"
    # Map internal "error" to "failure" for the API contract
    if result.get("status") == "error":
        result["status"] = "failure"

    # Always ensure context is present so the client can echo it back
    if "context" not in result or result["context"] is None:
        result["context"] = context

    return TicketResponse(**{k: v for k, v in result.items() if k in TicketResponse.model_fields})


# ------------------------------------------------------------------
# Health check
# ------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok", "version": app.version}