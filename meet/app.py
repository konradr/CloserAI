"""Meet orchestrator — wires Recall.ai transcript -> CRM service -> SLNG voice.

Run:
    # 1. CRM service (other terminal)
    uvicorn crm.service:app --port 8100
    # 2. expose this orchestrator publicly so Recall.ai can reach the webhook
    ngrok http 8200            # copy https URL into PUBLIC_URL in .env
    # 3. this orchestrator
    uvicorn meet.app:app --port 8200

Then start a call:
    curl -X POST localhost:8200/start-call -H "Content-Type: application/json" \
      -d '{"meeting_url":"https://meet.google.com/xxx-xxxx-xxx","contact_email":"john@northwind.io"}'

When the call ends:
    curl -X POST localhost:8200/end-call/<bot_id> \
      -H "Content-Type: application/json" -d '{"summary":"Great call."}'
"""

from __future__ import annotations

import asyncio
import os

import httpx
from fastapi import BackgroundTasks, FastAPI, Request
from google import genai
from google.genai import types
from pydantic import BaseModel

from crm.config import load_env

from . import recall, slng

load_env()

CRM_URL = os.getenv("CRM_URL", "http://localhost:8100")
PUBLIC_URL = os.getenv("PUBLIC_URL", "")
BOT_NAME = "CloserAI"
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# Fast, direct LLM client for low-latency in-call replies (no MCP tool loop).
_gemini = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
_FAST_SYS = (
    "You are CloserAI, a sharp, warm AI sales rep on a LIVE voice call. "
    "Reply with ONE short spoken sentence under 25 words, using the CRM context "
    "and conversation. Never repeat yourself. If nothing useful to add, reply "
    "with exactly: [SILENT].\n"
    "Most turns need NO tools — answer directly from the context. Only call "
    "web_research for fresh external facts (news, funding, a competitor), or "
    "crm_lookup for a specific CRM detail that is NOT already in the context. "
    "Call end_call ONLY when the conversation is clearly over — the prospect says "
    "goodbye, agrees on next steps and is leaving, or asks to end — passing a one-"
    "line summary of the outcome. After calling it, say a brief warm goodbye."
)
_FAST_TOOLS = [
    types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="web_research",
                description=(
                    "Search the public web for fresh info about a company or "
                    "person (news, funding, competitors)."
                ),
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "company": {"type": "STRING", "description": "Company to research"},
                        "name": {"type": "STRING", "description": "Optional person name"},
                    },
                    "required": ["company"],
                },
            ),
            types.FunctionDeclaration(
                name="crm_lookup",
                description=(
                    "Look up a specific CRM fact not already in the context "
                    "(deal value, past notes, tasks, other contacts). Ask a clear question."
                ),
                parameters={
                    "type": "OBJECT",
                    "properties": {"question": {"type": "STRING"}},
                    "required": ["question"],
                },
            ),
            types.FunctionDeclaration(
                name="end_call",
                description=(
                    "End the call and trigger autonomous CRM wrap-up (log note, "
                    "advance the deal, create a follow-up task, save transcript). "
                    "Call ONLY when the conversation is clearly finished."
                ),
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "summary": {"type": "STRING", "description": "One-line call outcome"},
                        "task": {"type": "STRING", "description": "Follow-up task to create"},
                    },
                    "required": ["summary"],
                },
            ),
        ]
    )
]
_FAST_CONFIG = types.GenerateContentConfig(
    temperature=0.4,
    system_instruction=_FAST_SYS,
    max_output_tokens=120,
    thinking_config=types.ThinkingConfig(thinking_budget=0),  # disable thinking for speed
    tools=_FAST_TOOLS,
)


async def _crm_lookup(question: str, session: dict) -> str:
    """Targeted CRM lookup via the full MCP-backed CRM service (used only when needed)."""
    email = session.get("email", "")
    q = f"For the contact {email}: {question}. Answer concisely with facts from the CRM."
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(f"{CRM_URL}/crm/ask", json={"query": q})
            return (r.json().get("answer") or "")[:400]
    except Exception as exc:  # noqa: BLE001
        return f"(crm lookup failed: {exc})"


async def fast_reply(session: dict, user_text: str) -> str:
    """Low-latency spoken reply. Direct Gemini call; calls a small toolset only when needed."""
    from crm.research import research_company

    convo = "\n".join(session.get("history", [])[-12:]) or "(call just started)"
    prompt = (
        f"CRM context:\n{session.get('brief', '')}\n\n"
        f"Conversation so far:\n{convo}\n\n"
        f"The prospect just said: \"{user_text}\"\nYour reply:"
    )
    contents = [types.Content(role="user", parts=[types.Part(text=prompt)])]

    resp = None
    for _ in range(3):  # at most a couple of tool rounds
        resp = await _gemini.aio.models.generate_content(
            model=GEMINI_MODEL, contents=contents, config=_FAST_CONFIG
        )
        cand = resp.candidates[0] if resp.candidates else None
        if cand and cand.content:
            contents.append(cand.content)
        calls = resp.function_calls or []
        if not calls:
            return (resp.text or "").strip()
        parts = []
        for c in calls:
            args = dict(c.args or {})
            if c.name == "web_research":
                out = await research_company(args.get("company", ""), args.get("name")) or "(no results)"
            elif c.name == "crm_lookup":
                out = await _crm_lookup(args.get("question", ""), session)
            elif c.name == "end_call":
                # Flag the call for autonomous wrap-up; speak a goodbye first.
                session["_pending_end"] = {
                    "summary": args.get("summary") or "Call ended.",
                    "task": args.get("task") or "Send proposal and pricing",
                }
                out = "Acknowledged. Say a brief, warm one-line goodbye now."
            else:
                out = "(unknown tool)"
            parts.append(types.Part.from_function_response(name=c.name, response={"result": out}))
        contents.append(types.Content(role="user", parts=parts))
    return (resp.text or "").strip() if resp else ""


app = FastAPI(title="CloserAI Meet Orchestrator")

# bot_id -> {"email": str, "brief": str}
sessions: dict[str, dict] = {}


class StartCall(BaseModel):
    meeting_url: str
    contact_email: str


class EndCall(BaseModel):
    summary: str = "Call completed."
    task: str = "Send proposal and pricing"


class Say(BaseModel):
    text: str | None = None     # speak this exact text, OR
    prompt: str | None = None   # ask the CRM agent and speak its answer


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "public_url": PUBLIC_URL, "active_bots": list(sessions)}


@app.post("/start-call")
async def start_call(req: StartCall) -> dict:
    # 1. Pre-call context (CRM + web intel) from the CRM service.
    brief = ""
    async with httpx.AsyncClient(timeout=120) as client:
        try:
            r = await client.get(f"{CRM_URL}/crm/precall", params={"email": req.contact_email})
            data = r.json()
            brief = data.get("brief", "")
            if data.get("web_intel"):
                brief += "\n\n" + data["web_intel"]
        except Exception as exc:  # noqa: BLE001
            print(f"[meet] precall failed: {exc}")

    # 2. Send the bot to the meeting. Real-time transcript only if PUBLIC_URL is set.
    webhook = f"{PUBLIC_URL.rstrip('/')}/webhook/transcript" if PUBLIC_URL else None
    bot_id = await recall.create_bot(req.meeting_url, webhook, BOT_NAME)
    sessions[bot_id] = {"email": req.contact_email, "brief": brief, "history": [], "bot_id": bot_id}
    mode = "live-transcript" if webhook else "trigger-to-speak (localhost)"
    print(f"[meet] bot {bot_id} joining {req.meeting_url} [{mode}]")
    return {"bot_id": bot_id, "brief": brief, "mode": mode}


@app.post("/say/{bot_id}")
async def say(bot_id: str, req: Say) -> dict:
    """Make the bot speak. Provide `text` for exact words, or `prompt` to let the
    CRM agent decide what to say. Remembers the whole call via per-bot history."""
    session = sessions.get(bot_id)
    if not session:
        return {"error": "unknown bot_id"}
    history: list[str] = session.setdefault("history", [])

    answer = req.text or ""
    if not answer and req.prompt:
        answer = await fast_reply(session, req.prompt)
        history.append(f"Prospect/context: {req.prompt}")

    if not answer or "[SILENT]" in answer:
        return {"error": "nothing to say"}
    history.append(f"CloserAI: {answer}")
    audio = await slng.synthesize_mp3(answer)
    ok = await recall.output_audio(bot_id, audio)
    print(f"[meet] CloserAI says: {answer}")
    return {"spoke": ok, "text": answer, "turns": len(history)}


@app.get("/history/{bot_id}")
async def get_history(bot_id: str) -> dict:
    """Inspect the running conversation memory for a call."""
    session = sessions.get(bot_id, {})
    return {"bot_id": bot_id, "history": session.get("history", [])}


@app.post("/leave/{bot_id}")
async def leave(bot_id: str) -> dict:
    """Force the bot to leave the meeting immediately (no CRM writes)."""
    await recall.leave_call(bot_id)
    sessions.pop(bot_id, None)
    return {"left": bot_id}


@app.post("/webhook/transcript")
async def webhook_transcript(request: Request, background: BackgroundTasks) -> dict:
    body = await request.json()
    if body.get("event") != "transcript.data":
        return {"ok": True}

    data = body.get("data", {}).get("data", {})
    participant = data.get("participant") or {}
    speaker = participant.get("name") or "Prospect"
    words = data.get("words") or []
    sentence = " ".join(w.get("text", "") for w in words).strip()
    bot_id = body.get("data", {}).get("bot", {}).get("id")

    # Ignore our own bot's speech and empty utterances.
    if not sentence or speaker == BOT_NAME or bot_id not in sessions:
        return {"ok": True}

    background.add_task(_handle_utterance, bot_id, speaker, sentence)
    return {"ok": True}


async def _handle_utterance(bot_id: str, speaker: str, sentence: str) -> None:
    session = sessions.get(bot_id)
    if not session:
        return
    print(f"[meet] {speaker}: {sentence}")
    answer = await fast_reply(session, sentence)

    if not answer or "[SILENT]" in answer:
        return
    history: list[str] = session.setdefault("history", [])
    history.append(f"{speaker}: {sentence}")
    history.append(f"CloserAI: {answer}")
    print(f"[meet] CloserAI says: {answer}")
    audio = await slng.synthesize_mp3(answer)
    await recall.output_audio(bot_id, audio)

    # If the agent decided the call is over, let the goodbye play, then wrap up.
    pending = session.pop("_pending_end", None)
    if pending is not None:
        await asyncio.sleep(4)  # let the goodbye audio finish
        await _wrap_up_call(bot_id, pending["summary"], pending["task"])


@app.post("/end-call/{bot_id}")
async def end_call(bot_id: str, req: EndCall) -> dict:
    return await _wrap_up_call(bot_id, req.summary, req.task)


async def _wrap_up_call(bot_id: str, summary: str, task: str = "Send proposal and pricing") -> dict:
    """Autonomous end-of-call: summary note + deal advance + task + full transcript, then leave."""
    session = sessions.get(bot_id, {})
    email = session.get("email")
    history: list[str] = session.get("history", [])
    result = {}
    transcript_saved = False
    if email:
        async with httpx.AsyncClient(timeout=120) as client:
            # 1. Summary note + deal advance + follow-up task.
            r = await client.post(
                f"{CRM_URL}/crm/post-call",
                json={"email": email, "summary": summary, "task": task},
            )
            result = r.json()

            # 2. Persist the FULL conversation transcript as its own note.
            if history:
                transcript = "\n".join(history)
                q = (
                    f"Create a note on the person with email {email}. "
                    f"Title it exactly 'Full call transcript'. "
                    f"The note content must be exactly the following transcript, verbatim:\n\n"
                    f"{transcript}"
                )
                tr = await client.post(f"{CRM_URL}/crm/ask", json={"query": q})
                transcript_saved = "error" not in tr.json()

    await recall.leave_call(bot_id)
    sessions.pop(bot_id, None)
    print(
        f"[meet] END CALL {bot_id} | bot left | "
        f"crm: {result.get('answer', result)} | "
        f"transcript_saved={transcript_saved} | turns={len(history)}"
    )
    return {"bot_id": bot_id, "crm": result, "transcript_saved": transcript_saved,
            "turns": len(history)}
