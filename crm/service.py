"""HTTP service exposing the CRM component to other parts of CloserAI.

Your teammate's voice / Google Meet code calls these endpoints — no MCP, OAuth,
or Attio knowledge required on their side.

Run:
    uvicorn crm.service:app --port 8100
    # first run opens a browser once for Attio OAuth

Endpoints:
    GET  /health
    GET  /crm/context?email=john@northwind.io      -> pre-call CRM brief
    POST /crm/ask        {"query": "..."}           -> natural-language CRM action
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

from .agent import CRMAgent

_agent: CRMAgent | None = None
_lock = asyncio.Lock()  # serialise access to the single MCP session


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _agent
    _agent = CRMAgent()
    print("[crm] starting agent + Attio MCP session...")
    await _agent.start()
    print(f"[crm] ready. {len(_agent.tools)} Attio tools available.")
    yield
    await _agent.stop()
    print("[crm] stopped.")


app = FastAPI(title="CloserAI CRM Service", lifespan=lifespan)


class AskRequest(BaseModel):
    query: str


class PostCallRequest(BaseModel):
    email: str
    summary: str
    task: str = "Send proposal and pricing"
    outcome: str = "well"


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "tools": _agent.tools if _agent else []}


@app.get("/crm/context")
async def crm_context(email: str) -> dict:
    try:
        async with _lock:
            brief = await _agent.get_context(email)
        return {"email": email, "brief": brief}
    except Exception as exc:  # noqa: BLE001
        return {"email": email, "brief": None, "error": str(exc)[:300]}


@app.get("/crm/precall")
async def crm_precall(email: str, research: bool = True) -> dict:
    """Pre-call package: CRM brief + Tavily web intel on the company."""
    try:
        async with _lock:
            result = await _agent.precall(email, research=research)
        return {"email": email, **result}
    except Exception as exc:  # noqa: BLE001
        return {"email": email, "brief": None, "web_intel": "", "error": str(exc)[:300]}


@app.post("/crm/ask")
async def crm_ask(req: AskRequest) -> dict:
    try:
        async with _lock:
            result = await _agent.ask(req.query)
        return {"answer": result["answer"], "tool_calls": result["tool_calls"]}
    except Exception as exc:  # noqa: BLE001
        return {"answer": None, "tool_calls": [], "error": str(exc)[:300]}


@app.post("/crm/post-call")
async def crm_post_call(req: PostCallRequest) -> dict:
    """Autonomous post-call wrap-up: note + deal-stage advance + follow-up task."""
    try:
        async with _lock:
            result = await _agent.post_call(
                req.email, req.summary, req.task, req.outcome
            )
        return {"answer": result["answer"], "tool_calls": result["tool_calls"]}
    except Exception as exc:  # noqa: BLE001
        return {"answer": None, "tool_calls": [], "error": str(exc)[:300]}
