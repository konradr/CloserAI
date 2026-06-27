# CloserAI — CRM Service Integration Guide

For the **voice / Google Meet** side. This is everything you need to use the CRM
component. You never touch Attio, MCP, OAuth, or Tavily — just call 3 HTTP endpoints.

---

## 1. Start the service

```bash
cd CloserAI
pip install -r requirements.txt
cp .env.example .env          # set GOOGLE_API_KEY and TAVILY_API_KEY
uvicorn crm.service:app --port 8100
```

First run opens a browser once to log into Attio (OAuth). Token is cached after.
Wait for: `[crm] ready. 38 Attio tools available.`

Base URL: **`http://localhost:8100`**

---

## 2. Which endpoint to use, and when

| Call moment | Endpoint | Method |
|-------------|----------|--------|
| **Bot joins the call** | `/crm/precall?email=<prospect_email>` | GET |
| **During the call** (questions, objections, research) | `/crm/ask` | POST |
| **Call ends** (log + update CRM) | `/crm/post-call` | POST |
| Health check | `/health` | GET |

---

## 3. Endpoint details

### A. Pre-call — when the bot joins
Get a CRM brief + fresh web intel to greet the prospect with context.

```
GET /crm/precall?email=john@northwind.io
```
Response:
```json
{
  "email": "john@northwind.io",
  "brief": "John Smith, VP of Sales at Northwind Logistics. Open deal: CloserAI Enterprise, In Progress, $48,000. Most recent note: ...",
  "company": "Northwind Logistics",
  "web_intel": "WEB INTEL (Tavily):\n- <recent news>\n- <funding>..."
}
```
Use `brief` + `web_intel` to prime the agent / speak the opening.

> Tip: add `&research=false` to skip web intel and get the brief faster.

---

### B. In-call — any question or live action
Send a plain-English request. The agent uses Attio + web research as needed.

```
POST /crm/ask
Content-Type: application/json

{ "query": "Prospect says a competitor is cheaper and mentioned recent funding. Give me one talking point." }
```
Response:
```json
{
  "answer": "Congrats on the Series D — that's exactly why teams your size pick us...",
  "tool_calls": [ { "name": "web_research", "args": {"company": "Acme"} } ]
}
```
Speak `answer` in the call. `tool_calls` is just a trace (nice for a dashboard).

---

### C. Post-call — autonomous CRM update
Fires the wrap-up: logs a note, advances the deal stage, creates a follow-up task.

```
POST /crm/post-call
Content-Type: application/json

{
  "email": "john@northwind.io",
  "summary": "Prospect impressed by live demo, ready to move forward.",
  "task": "Send proposal and pricing",
  "outcome": "well"
}
```
Response:
```json
{
  "answer": "Logged the note, advanced the deal to Proposal, and created a follow-up task.",
  "tool_calls": [ {"name": "create-note"}, {"name": "update-record"}, {"name": "create-task"} ]
}
```
Only `email` + `summary` are required; `task` and `outcome` have defaults.

---

## 4. Minimal client examples

### Python
```python
import httpx
BASE = "http://localhost:8100"

# bot joins
pre = httpx.get(f"{BASE}/crm/precall", params={"email": email}, timeout=120).json()
speak(pre["brief"]); speak(pre["web_intel"])

# during the call
ans = httpx.post(f"{BASE}/crm/ask", json={"query": transcript_question}, timeout=120).json()
speak(ans["answer"])

# call ends
httpx.post(f"{BASE}/crm/post-call", json={
    "email": email,
    "summary": call_summary,
    "task": "Send proposal and pricing",
}, timeout=120)
```

### curl
```bash
curl "http://localhost:8100/crm/precall?email=john@northwind.io"
curl -X POST http://localhost:8100/crm/ask -H "Content-Type: application/json" \
  -d '{"query":"handle a pricing objection"}'
curl -X POST http://localhost:8100/crm/post-call -H "Content-Type: application/json" \
  -d '{"email":"john@northwind.io","summary":"Great call, ready to proceed."}'
```

---

## 5. Notes
- **Timeouts**: use ≥ 120s. `post-call` does several tool calls (~20–40s).
- **One call at a time**: the service serialises requests (single CRM session) — fine for a live demo.
- **Errors**: every endpoint returns JSON; on failure you get `{"error": "..."}` instead of a crash.
- **Test emails** in the demo workspace: `john@northwind.io`, `sarah@brightwave.com`, `marcus@heliosfin.com`.
