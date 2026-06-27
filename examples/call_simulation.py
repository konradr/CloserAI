"""
End-to-end call simulation — plays the role of the partner's voice / Google Meet
side, calling the CRM service exactly as it would during a real sales call.

Run the CRM service first:
    uvicorn crm.service:app --port 8100

Then run this:
    python examples/call_simulation.py
    python examples/call_simulation.py --email sarah@brightwave.com

It walks through the three product moments:
  1. PRE-CALL   — GET  /crm/context   (bot joins, gets briefed)
  2. IN-CALL    — POST /crm/ask        (live question backed by CRM)
  3. POST-CALL  — POST /crm/ask        (log note, advance deal, create task)
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx

BASE = "http://localhost:8100"


def _print_header(step: str, title: str) -> None:
    print(f"\n{'=' * 60}\n  {step}  {title}\n{'=' * 60}")


def _ask(client: httpx.Client, query: str) -> dict:
    resp = client.post(f"{BASE}/crm/ask", json={"query": query}, timeout=120)
    resp.raise_for_status()
    return resp.json()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", default="john@northwind.io")
    args = parser.parse_args()
    email = args.email

    with httpx.Client() as client:
        # Make sure the service is up.
        try:
            health = client.get(f"{BASE}/health", timeout=10).json()
        except Exception:
            print("❌ CRM service not reachable. Start it with:")
            print("   uvicorn crm.service:app --port 8100")
            sys.exit(1)
        print(f"CRM service up — {len(health.get('tools', []))} Attio tools available.")

        # 1. PRE-CALL ------------------------------------------------------- #
        _print_header("1. PRE-CALL", f"Bot joins — briefing on {email}")
        brief = client.get(f"{BASE}/crm/precall", params={"email": email}, timeout=120).json()
        # /crm/precall returns CRM brief + Tavily web intel.
        print(brief["brief"])
        if brief.get("web_intel"):
            print("\n" + brief["web_intel"])

        # 2. IN-CALL -------------------------------------------------------- #
        _print_header("2. IN-CALL", "Prospect mentions recent news + a competitor")
        result = _ask(
            client,
            f"On a live call with {email}. The prospect just said they recently "
            "had some big company news and are also evaluating a cheaper competitor. "
            "Look up fresh web intel on their company if useful, then give me ONE "
            "concise, personalised talking point I can say out loud right now.",
        )
        print(f"\n🗣️  Agent says: {result['answer']}")
        _show_calls(result)

        # 3. POST-CALL ------------------------------------------------------ #
        _print_header("3. POST-CALL", "Autonomous CRM update")
        resp = client.post(
            f"{BASE}/crm/post-call",
            json={
                "email": email,
                "summary": "Prospect was impressed by the live demo and is ready to move forward.",
                "task": "Send proposal and pricing",
                "outcome": "well",
            },
            timeout=180,
        )
        resp.raise_for_status()
        result = resp.json()
        print(f"\n✅ Agent did: {result['answer']}")
        _show_calls(result)

        print("\n" + "=" * 60)
        print("  Demo complete — check Attio: new note, advanced deal, new task.")
        print("=" * 60)


def _show_calls(result: dict) -> None:
    calls = result.get("tool_calls", [])
    if calls:
        print("   Attio tools used:")
        for c in calls:
            print(f"     - {c['name']}({json.dumps(c['args'])[:90]})")


if __name__ == "__main__":
    main()
