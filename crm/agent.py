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
    "If there is no record, say so. Keep it under 120 words, plain text."
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
        self._config = types.GenerateContentConfig(
            temperature=0,
            system_instruction=SYSTEM_PROMPT,
            tools=mcp_tools_to_genai(tools.tools, types),
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
        return result["answer"]

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

        for _ in range(8):  # cap tool-call rounds
            resp = await self._generate_with_retry(convo)
            candidate = resp.candidates[0] if resp.candidates else None
            if candidate and candidate.content:
                convo.append(candidate.content)

            calls = resp.function_calls or []
            if not calls:
                return {"answer": resp.text or "", "tool_calls": tool_calls, "history": convo}

            tool_parts = []
            for call in calls:
                args = dict(call.args) if call.args else {}
                tool_calls.append({"name": call.name, "args": args})
                try:
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
