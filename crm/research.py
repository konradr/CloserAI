"""Pre-call web research via Tavily.

Enriches a prospect with fresh, public web intel before the call — recent news,
funding, and company context the agent can reference live.
"""

from __future__ import annotations

import asyncio
import os

from .config import load_env

load_env()


def _client():
    from tavily import TavilyClient

    key = os.getenv("TAVILY_API_KEY")
    if not key:
        return None
    return TavilyClient(api_key=key)


def _search_sync(company: str, name: str | None) -> str:
    client = _client()
    if client is None:
        return ""

    queries = [f"{company} company news funding 2025 2026"]
    if name:
        queries.append(f"{name} {company}")

    lines: list[str] = []
    for q in queries:
        try:
            res = client.search(q, max_results=3, search_depth="basic")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"(research error: {exc})")
            continue
        for r in res.get("results", [])[:3]:
            title = (r.get("title") or "").strip()
            content = (r.get("content") or "").strip().replace("\n", " ")
            if title:
                lines.append(f"- {title}: {content[:180]}")

    if not lines:
        return ""
    return "WEB INTEL (Tavily):\n" + "\n".join(lines[:6])


async def research_company(company: str, name: str | None = None) -> str:
    """Return a short web-intel summary for a company (and optional contact)."""
    if not company:
        return ""
    return await asyncio.to_thread(_search_sync, company, name)
