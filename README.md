# CloserAI

An autonomous AI sales agent that joins live calls, listens, speaks, and updates the CRM — built at the Tech:Europe London AI Hackathon.

This repo is split into independent components that connect over simple HTTP:

| Component | Owner | What it does |
|-----------|-------|--------------|
| **`crm/`** | Harish | CRM intelligence — Attio via MCP + Gemini. Pre-call briefs and autonomous CRM actions. |
| **voice / meet** | Partner | Joins the Google Meet, streams transcript, speaks responses. Calls the CRM component. |

---

## The `crm` component

A self-contained CRM brain. It talks to **Attio's hosted MCP server** (OAuth) and uses **Gemini** to reason over 37 Attio tools — searching contacts/deals, logging notes, creating tasks, updating stages — all in natural language. No Attio API knowledge needed by callers.

### Two ways to use it

#### 1. As an HTTP service (recommended for the voice/Meet side)

```bash
cd CloserAI
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # add your GOOGLE_API_KEY
uvicorn crm.service:app --port 8100
# first run opens a browser once to log into Attio (token cached after)
```

Then your code just makes HTTP calls:

```
GET  /health
GET  /crm/context?email=john@northwind.io
POST /crm/ask     {"query": "Log a note on John Smith: ready for proposal"}
```

**Pre-call brief** (call this when the bot joins):
```bash
curl "http://localhost:8100/crm/context?email=john@northwind.io"
```
```json
{
  "email": "john@northwind.io",
  "brief": "John Smith, VP of Sales at Northwind. Open deal: CloserAI Enterprise, In Progress, $48,000. Most recent note: Discovery call done..."
}
```

**Any CRM action** (call this during/after the call):
```bash
curl -X POST http://localhost:8100/crm/ask \
  -H "Content-Type: application/json" \
  -d '{"query": "Log a note on John Smith: great demo, advance the deal to proposal"}'
```
```json
{
  "answer": "Logged the note and moved the Northwind deal to Proposal.",
  "tool_calls": [{"name": "search-records", "args": {}}, {"name": "create-note", "args": {}}]
}
```

#### 2. As a Python library (in-process)

```python
from crm import CRMAgent

async with CRMAgent() as agent:
    brief  = await agent.get_context("john@northwind.io")   # pre-call brief (str)
    result = await agent.ask("Create a task to follow up with John in 2 days")
    print(result["answer"], result["tool_calls"])
```

### Integration contract (for the voice / Google Meet side)

Your code never touches Attio or MCP. The flow:

1. **Bot joins the call** → `GET /crm/context?email=<prospect>` → speak the brief / prime the agent.
2. **During the call** → when you need CRM info, `POST /crm/ask` with a plain-English question.
3. **Call ends** → `POST /crm/ask` with the call summary and the action (log note, advance deal, create follow-up task). The agent does it autonomously.

That's the whole surface: `/crm/context` and `/crm/ask`.

---

## Configuration

`.env`:
```env
GOOGLE_API_KEY=your_gemini_dev_key
GEMINI_MODEL=gemini-2.5-flash
```

Attio needs **no API key** here — the component authenticates to Attio MCP via OAuth (browser login on first run; token cached in `.attio_mcp_token.json`).

---

## Tech

- **Attio MCP** — hosted CRM tools over the Model Context Protocol (OAuth)
- **Gemini** (`google-genai`) — reasoning + function calling over the Attio tools
- **FastAPI** — the HTTP service wrapper
