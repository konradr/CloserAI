"""CloserAI CRM component — Attio-powered CRM intelligence over MCP.

Public API:
    from crm import CRMAgent

    agent = CRMAgent()
    await agent.start()
    answer = await agent.ask("Log a note on John Smith: ready for proposal")
    ctx = await agent.get_context("john@northwind.io")
    await agent.stop()
"""

from .agent import CRMAgent

__all__ = ["CRMAgent"]
