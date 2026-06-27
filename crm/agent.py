"""CRMAgent — the reusable CRM brain for CloserAI.

Wraps Attio's hosted MCP server + Gemini behind two simple async methods so the
rest of the system (voice, Google Meet, orchestration) can use the CRM without
knowing anything about MCP, OAuth, or Attio's API.

    agent = CRMAgent()
    await agent.start()                      # opens MCP session (OAuth once)
    brief = await agent.get_context(email)   # pre-call CRM brief
    reply = await agent.ask("Log a note ...")# any natural-language CRM action
    await agent.stop()
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import re
from contextlib import AsyncExitStack

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from .config import load_env
from .oauth import MCP_ENDPOINT, TOKEN_FILE, make_oauth, mcp_tools_to_genai

load_env()

SYSTEM_PROMPT = (
    "You are CloserAI, an AI sales assistant wired into the Attio CRM via MCP. "
    "Use the Attio tools to look up contacts, deals, notes, tasks and to make "
    "updates. When asked to create or update something, do it, then confirm what "
    "you changed in one short sentence. Be concise and factual."
)

CONTEXT_PROMPT = (
    "Give me a tight pre-call CRM brief for the contact with email {email}. "
    "Search the CRM, then return: who they are (name, title, company), their "
    "open deals (name, stage, value), and the single most important recent note. "
    "If there is no record, say so. Keep it under 120 words, plain text. "
    "On the VERY LAST line add exactly: COMPANY=<their company name> (or COMPANY=unknown)."
)

POST_CALL_PROMPT = (
    "The call with {email} just ended and went {outcome}. You MUST complete ALL "
    "THREE actions below in Attio, in order. Do not stop or give a final answer "
    "until all three are done:\n"
    "1. create-note on the person record summarising: {summary}\n"
    "2. Advance their deal to the next stage toward closing. First look up the "
    "deal and its valid stage options, then update-record the stage.\n"
    "3. create-task: a follow-up titled '{task}', due in 2 days, linked to the "
    "deal (object 'deals' + the deal record_id).\n"
    "After all three succeed, confirm in ONE short sentence exactly what you did."
)


class CRMAgent:
    def __init__(self, google_api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = google_api_key or os.getenv("GOOGLE_API_KEY")
        self.model = model or os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
        if not self.api_key:
            raise RuntimeError("GOOGLE_API_KEY is not set (env or constructor arg).")
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._gemini = None
        self._config = None
        self._tool_names: list[str] = []

    # -- lifecycle --------------------------------------------------------- #

    async def start(self) -> None:
        """Open the MCP session and prepare Gemini. Call once before use.

        If the cached Attio token is expired/invalid, clears it and re-runs the
        OAuth browser login once so the demo never dies on a stale token.
        """
        from google import genai
        from google.genai import types

        self._types = types
        self._gemini = genai.Client(api_key=self.api_key)

        try:
            await self._open_session(types)
        except Exception as exc:  # noqa: BLE001
            if self._looks_like_auth_error(exc) and TOKEN_FILE.exists():
                print("[crm] cached Attio token invalid — clearing and re-authenticating...")
                TOKEN_FILE.unlink(missing_ok=True)
                await self._safe_close_stack()
                await self._open_session(types)
            else:
                raise

    async def _open_session(self, types) -> None:
        self._stack = AsyncExitStack()
        read, write, _ = await self._stack.enter_async_context(
            streamablehttp_client(MCP_ENDPOINT, auth=make_oauth())
        )
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        tools = await self._session.list_tools()
        self._tool_names = [t.name for t in tools.tools]
        genai_tools = mcp_tools_to_genai(tools.tools, types)
        # Add a local Tavily web-research tool the agent can call mid-call.
        genai_tools[0].function_declarations.append(
            types.FunctionDeclaration(
                name="web_research",
                description=(
                    "Search the public web (Tavily) for fresh intel about a company "
                    "or person — recent news, funding, competitors. Use this during a "
                    "call when a company, competitor, or current event comes up that "
                    "isn't in the CRM."
                ),
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "company": {"type": "STRING", "description": "Company to research"},
                        "name": {"type": "STRING", "description": "Optional person name"},
                    },
                    "required": ["company"],
                },
            )
        )
        self._tool_names.append("web_research")
        self._config = types.GenerateContentConfig(
            temperature=0,
            system_instruction=SYSTEM_PROMPT,
            tools=genai_tools,
        )

    async def _safe_close_stack(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._stack = None
            self._session = None

    @staticmethod
    def _looks_like_auth_error(exc: Exception) -> bool:
        text = repr(exc).lower()
        return any(k in text for k in ("401", "unauthorized", "oauth", "token", "invalid_grant"))

    async def stop(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
            self._session = None

    async def __aenter__(self) -> "CRMAgent":
        await self.start()
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.stop()

    @property
    def tools(self) -> list[str]:
        return self._tool_names

    async def _generate_with_retry(self, convo, max_retries: int = 4):
        """Call Gemini, retrying on 429 rate-limit with the server's suggested delay."""
        for attempt in range(max_retries + 1):
            try:
                return await self._gemini.aio.models.generate_content(
                    model=self.model, contents=convo, config=self._config
                )
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                if "429" not in msg and "RESOURCE_EXHAUSTED" not in msg:
                    raise
                if attempt == max_retries:
                    raise
                match = re.search(r"retry in ([\d.]+)s|retryDelay'?: ?'?([\d.]+)", msg)
                delay = float(match.group(1) or match.group(2)) if match else 6.0
                print(f"[crm] rate-limited, retrying in {delay:.0f}s...")
                await asyncio.sleep(delay + 0.5)

    # -- public API -------------------------------------------------------- #

    async def get_context(self, email: str) -> str:
        """Return a concise CRM brief for a contact (for pre-call use)."""
        result = await self.ask(CONTEXT_PROMPT.format(email=email))
        brief, _company = self._split_company(result["answer"])
        return brief

    async def precall(self, email: str, research: bool = True) -> dict:
        """Full pre-call package: CRM brief + (optional) Tavily web intel.

        Returns {"brief": str, "company": str, "web_intel": str}.
        """
        result = await self.ask(CONTEXT_PROMPT.format(email=email))
        brief, company = self._split_company(result["answer"])
        web_intel = ""
        if research and company and company.lower() != "unknown":
            from .research import research_company
            web_intel = await research_company(company)
        return {"brief": brief, "company": company, "web_intel": web_intel}

    @staticmethod
    def _split_company(answer: str) -> tuple[str, str]:
        """Pull the trailing 'COMPANY=...' marker out of the brief."""
        company = ""
        lines = (answer or "").splitlines()
        kept = []
        for line in lines:
            if line.strip().upper().startswith("COMPANY="):
                company = line.split("=", 1)[1].strip()
            else:
                kept.append(line)
        return "\n".join(kept).strip(), company

    async def post_call(
        self,
        email: str,
        summary: str,
        task: str = "Send proposal and pricing",
        outcome: str = "well",
    ) -> dict:
        """Autonomous post-call wrap-up: note + deal-stage advance + follow-up task.

        The note and stage advance are driven by the model; the follow-up task is
        created deterministically so it is never skipped.
        """
        result = await self.ask(
            POST_CALL_PROMPT.format(
                email=email, summary=summary, task=task, outcome=outcome
            )
        )

        # Deterministic follow-up task — don't rely on the model remembering it.
        # Skip if the model already created one this turn (avoid duplicates).
        already_created = any(c.get("name") == "create-task" for c in result["tool_calls"])
        person_id = self._person_id_from_calls(result["tool_calls"]) or \
            await self._find_record_id("people", email)
        if not already_created and person_id:
            deadline = (
                datetime.date.today() + datetime.timedelta(days=2)
            ).isoformat() + "T09:00:00.000Z"
            try:
                await self._session.call_tool(
                    "create-task",
                    {
                        "content": task,
                        "deadline_at": deadline,
                        "linked_record_object": "people",
                        "linked_record_id": person_id,
                    },
                )
                result["tool_calls"].append(
                    {"name": "create-task", "args": {"content": task, "linked_record_id": person_id}}
                )
                if "task" not in (result["answer"] or "").lower():
                    base = (result["answer"] or "").strip().rstrip(".")
                    result["answer"] = (
                        f"{base}. Created a follow-up task: '{task}'.".lstrip(". ").strip()
                    )
            except Exception as exc:  # noqa: BLE001
                result["answer"] = (result["answer"] or "") + f" (task creation failed: {exc})"
        return result

    @staticmethod
    def _person_id_from_calls(calls: list[dict]) -> str | None:
        """Pull the person record_id from a create-note call made during this turn."""
        for c in calls:
            if c.get("name") == "create-note":
                rid = c.get("args", {}).get("parent_record_id")
                if rid:
                    return rid
        return None

    async def _find_record_id(self, object_: str, query: str) -> str | None:
        """Resolve a record_id via MCP search-records (parses the first UUID)."""
        try:
            res = await self._session.call_tool(
                "search-records", {"object": object_, "query": query}
            )
            text = "\n".join(
                c.text for c in res.content if getattr(c, "type", None) == "text"
            )
        except Exception:  # noqa: BLE001
            return None
        m = re.search(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", text
        )
        return m.group(0) if m else None

    async def ask(self, query: str, history: list | None = None) -> dict:
        """
        Run one natural-language CRM request. Gemini may call Attio tools.

        Returns {"answer": str, "tool_calls": [{"name", "args"}], "history": [...]}.
        Pass the returned history back in for multi-turn conversations.
        """
        if self._session is None:
            raise RuntimeError("CRMAgent.start() must be called first.")
        types = self._types
        convo = list(history or [])
        convo.append(types.Content(role="user", parts=[types.Part(text=query)]))
        tool_calls: list[dict] = []

        for _ in range(14):  # cap tool-call rounds (multi-action flows need headroom)
            resp = await self._generate_with_retry(convo)
            candidate = resp.candidates[0] if resp.candidates else None
            if candidate and candidate.content:
                convo.append(candidate.content)

            calls = resp.function_calls or []
            if not calls:
                answer = resp.text or ""
                # If the model finished silently after doing work, force a confirmation.
                if not answer.strip() and tool_calls:
                    convo.append(
                        types.Content(
                            role="user",
                            parts=[types.Part(text="Confirm in one short sentence exactly what you changed in Attio.")],
                        )
                    )
                    followup = await self._generate_with_retry(convo)
                    answer = (followup.text or "").strip() or "Done."
                return {"answer": answer, "tool_calls": tool_calls, "history": convo}

            tool_parts = []
            for call in calls:
                args = dict(call.args) if call.args else {}
                tool_calls.append({"name": call.name, "args": args})
                try:
                    if call.name == "web_research":
                        from .research import research_company
                        output = await research_company(
                            args.get("company", ""), args.get("name")
                        ) or "(no web results)"
                    else:
                        result = await self._session.call_tool(call.name, args)
                        output = "\n".join(
                            c.text for c in result.content
                            if getattr(c, "type", None) == "text"
                        ) or "(no content)"
                except Exception as exc:  # noqa: BLE001
                    output = f"ERROR: {exc}"
                tool_parts.append(
                    types.Part.from_function_response(
                        name=call.name, response={"result": output}
                    )
                )
            convo.append(types.Content(role="user", parts=tool_parts))

        return {"answer": "(stopped after too many tool calls)", "tool_calls": tool_calls, "history": convo}
