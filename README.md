# Clinic Voice Agent — Backend Tool Router

FastAPI backend that serves as the **tool-calling webhook** for a voice AI
appointment-booking demo (medical/dental clinic). Vapi.ai's LLM (Groq
Llama-3.1) decides mid-conversation to call a tool (check availability, book,
cancel, ...); Vapi then POSTs to this API, which validates the payload,
forwards the action to an **n8n** webhook (Google Calendar + notifications),
and responds in the exact shape Vapi expects — fast (target: under 2–3s).

> Demo/portfolio project — intentionally lean, **not** a production HIPAA system.

## Architecture

```
Caller ──phone──▶ Vapi.ai (voice agent, Groq Llama-3.1)
                        │  LLM emits a tool call
                        ▼
             This FastAPI app (tool router)
                        │  validates params, performs the action
                        ▼
                 n8n webhook ──▶ Google Calendar (availability / booking / cancel)
                             └──▶ Notifications (SMS / email confirmations)
```

## Project layout

```
clinic-voice-agent/
├── app/
│   ├── main.py               # FastAPI entrypoint: /health, router wiring, logging
│   ├── config.py             # pydantic-settings, env-driven configuration
│   ├── decision_log.py       # structured JSON-lines decision logging (Phase 2)
│   ├── models.py             # Vapi tool-call request/response models + /tools/booking schema
│   ├── routers/
│   │   ├── tools.py          # /tools/* endpoints (tool implementations go here)
│   │   ├── booking.py        # POST /tools/booking — unified intent-based booking decision
│   │   └── dashboard.py      # GET /dashboard — read-only decision-log viewer
│   └── services/
│       └── n8n_client.py     # async httpx wrapper for n8n webhooks
├── data/decision_log.jsonl   # runtime decision log (gitignored, created on first entry)
├── requirements.txt
├── .env.example
├── Dockerfile
└── README.md
```

## Run locally

Prereq: Python 3.11+ (3.12 recommended).

```bash
cd clinic-voice-agent

# 1. Create & activate a virtual environment
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS/Linux

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment (optional locally — see table below)
copy .env.example .env        # Windows
# cp .env.example .env        # macOS/Linux

# 4. Start the dev server
uvicorn app.main:app --reload
```

Health check:

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok"}
```

Interactive API docs: <http://127.0.0.1:8000/docs>

## Run tests

```bash
.venv\Scripts\python.exe -m pytest -q
```

The suites in `tests/` validate the Vapi wire-format models against sample
payloads (including the legacy `toolCallList` field and OpenAI-style string
`arguments`), exercise the tool endpoints with n8n mocked out, and cover the
hardening behaviors: 401s on bad/missing signatures, clean 422s/500s, and the
single outbound retry. `tests/test_n8n_http_mock.py` additionally simulates the
n8n webhook at the HTTP transport level with `respx` and drives each endpoint
with a real Vapi-shaped payload.

## Manual testing with curl

With `uvicorn app.main:app --reload` running locally (see above), these
commands hit each endpoint directly. The payloads are exactly what Vapi sends.

> If `VAPI_WEBHOOK_SECRET` is set, every request needs the matching
> `Authorization: Bearer <secret>` header. With no secret configured (local dev) the header is
> ignored — the examples include it so they work either way once you swap in
> your own value.
>
> **Windows PowerShell 5.1** can mangle inline JSON when calling native
> `curl`. If you see a `422` with a `json_invalid` error, save the body to
> `body.json` and use `curl --data-binary @body.json ...` instead.

### Health check

```bash
curl -s http://127.0.0.1:8000/health
# {"status":"ok"}
```

### Check availability

```bash
curl -s -X POST http://127.0.0.1:8000/tools/check-availability \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer change-me" \
  -d '{
    "message": {
      "type": "tool-calls",
      "toolCalls": [
        {
          "id": "toolu_curl_01",
          "type": "function",
          "function": {
            "name": "checkAvailability",
            "parameters": {
              "date": "2026-09-10",
              "service_type": "cleaning"
            }
          }
        }
      ]
    }
  }'
```

With n8n wired up you'll get the open slots back:

```json
{
  "results": [
    {
      "toolCallId": "toolu_curl_01",
      "result": "Open slots: 10:00 AM, 2:30 PM",
      "error": null
    }
  ]
}
```

Without n8n configured the endpoint still answers `200` with the graceful
fallback `"I'm having trouble checking availability right now, let me take
your number and have someone call you back"`.

### Book an appointment

```bash
curl -s -X POST http://127.0.0.1:8000/tools/book-appointment \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer change-me" \
  -d '{
    "message": {
      "type": "tool-calls",
      "toolCalls": [
        {
          "id": "toolu_curl_02",
          "type": "function",
          "function": {
            "name": "bookAppointment",
            "parameters": {
              "patient_name": "Jane Doe",
              "phone_number": "+1-555-0100",
              "date": "2026-09-12",
              "time": "10:30",
              "service_type": "cleaning"
            }
          }
        }
      ]
    }
  }'
```

Response — a confirmation payload including the booking reference:

```json
{
  "results": [
    {
      "toolCallId": "toolu_curl_02",
      "result": {
        "booking_reference": "BK-2026-0001",
        "confirmation": "Great news, Jane Doe! Your cleaning appointment on 2026-09-12 at 10:30 is confirmed. Your booking reference is BK-2026-0001."
      },
      "error": null
    }
  ]
}
```

**Idempotency:** re-run the exact same command (same `toolCallId`) and you'll
get the *same* `booking_reference` back — the cached result is returned and
n8n isn't called again, so retries can't double-book.

### Cancel an appointment

```bash
curl -s -X POST http://127.0.0.1:8000/tools/cancel-appointment \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer change-me" \
  -d '{
    "message": {
      "type": "tool-calls",
      "toolCalls": [
        {
          "id": "toolu_curl_03",
          "type": "function",
          "function": {
            "name": "cancelAppointment",
            "parameters": {
              "booking_reference": "BK-2026-0001"
            }
          }
        }
      ]
    }
  }'
```

Response:

```json
{
  "results": [
    {
      "toolCallId": "toolu_curl_03",
      "result": "Your appointment BK-2026-0001 has been cancelled. Is there anything else I can help you with?",
      "error": null
    }
  ]
}
```

### Call-ended webhook (server event)

```bash
curl -s -X POST http://127.0.0.1:8000/webhook/call-ended \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer change-me" \
  -d '{
    "message": {
      "type": "end-of-call-report",
      "endedReason": "customer-ended-call",
      "artifact": {
        "summary": "The patient booked a cleaning appointment and confirmed the follow-up."
      },
      "call": { "id": "call-uuid-123" }
    }
  }'
```

The handler logs the summary server-side and acks immediately:

```json
{ "status": "ok" }
```

### Pointing real Vapi at your local server

Vapi needs a public URL, so expose the local app with a tunnel before wiring
up the assistant:

```bash
ngrok http 8000
# then use https://<tunnel>.ngrok-free.app as the server URL prefix
```

In the Vapi dashboard, set each tool's server URL to
`https://<tunnel-domain>/tools/<endpoint>` (e.g.
`.../tools/check-availability`) and keep the tool's *Server Secret* in sync
with `VAPI_WEBHOOK_SECRET`.

## Environment variables

| Variable | Required | Description |
| --- | --- | --- |
| `N8N_WEBHOOK_URL` | needed for tools | Base URL of the n8n webhook that runs the Google Calendar / notification workflows. |
| `N8N_RESCHEDULE_WEBHOOK_URL` | needed for reschedule | Dedicated n8n reschedule webhook. Expects `{"existing_appointment_id", "new_date", "new_time"}`; answers `{"status":"success","appointment_id"}` or `{"status":"conflict"}`. |
| `VAPI_WEBHOOK_SECRET` | recommended | Shared secret; Vapi sends it as `Authorization: Bearer <secret>`. Tool routes reject requests that don't match. Leave empty to disable the check locally. |
| `GROQ_API_KEY` | reference only | Groq runs the LLM *inside* Vapi — this backend never calls Groq. Kept here so the whole demo's env lives in one place. |
| `PORT` | optional | Port for uvicorn (Render/Railway inject it automatically). Default `8000`. |
| `N8N_TIMEOUT_SECONDS` | optional | Outbound timeout for n8n calls. Default `5` — keep it tight; Vapi expects tool results in ~2–3s. |
| `N8N_MAX_RETRIES` | optional | Retries for transient n8n failures (timeouts, 502/503/504). Default `1`. |
| `DECISION_LOG_PATH` | optional | JSON-lines decision log (file store). Default `data/decision_log.jsonl` — used when `DATABASE_URL` isn't set. |
| `DATABASE_URL` | optional | Postgres connection string (Supabase / Neon / Render). When set, decision-log entries live in an auto-created `decision_log` table (survives redeploys) instead of the file; the dashboard reads the same store. When empty, the file is used — local dev has zero setup. |

These nine variables (`app/config.py`) are the complete configuration surface —
there are no hidden settings. For a bare `/health` smoke test the defaults are
fine; to exercise the tools you only need `N8N_WEBHOOK_URL`, plus
`VAPI_WEBHOOK_SECRET` anywhere real traffic is possible.

## Security & hardening

- **Webhook auth** — every `/tools/*` and `/webhook/*` route requires the
  `Authorization: Bearer <secret>` header (set the same
  value as the tool's *Server Secret* in Vapi). Missing/mismatched secrets get
  a `401`. Comparison is constant-time (`secrets.compare_digest`). Leave
  `VAPI_WEBHOOK_SECRET` empty to disable the check for local development.
- **Global error handling** — unhandled exceptions return a clean
  `{"detail": "Internal server error"}` with HTTP 500 and the full traceback
  logged server-side. Pydantic validation failures keep their standard,
  clean `422` responses.
- **Outbound calls** — n8n calls use a 5s timeout (`N8N_TIMEOUT_SECONDS`) and
  one automatic retry (`N8N_MAX_RETRIES`) on transient failures only
  (timeouts/connection errors and n8n 502/503/504).
- **Rate limiting** — intentionally not implemented for the demo (see the
  `PRODUCTION TODO` in `app/main.py`). Add per-call/IP rate limiting before
  real patient traffic.

## Vapi.ai wiring

1. In your Vapi assistant, add a **function tool** (e.g. `checkAvailability`)
   and point its **server URL** at `https://<your-host>/tools/<endpoint>` once
   the endpoint is implemented in `app/routers/tools.py`.
2. Set the tool's **Server Secret** to the same value as `VAPI_WEBHOOK_SECRET`;
  Vapi sends it as an `Authorization: Bearer <secret>` header on every request.
3. When the LLM calls the tool, Vapi POSTs a payload like:

```json
{
  "message": {
    "type": "tool-calls",
    "toolCalls": [
      {
        "id": "call_abc123",
        "type": "function",
        "function": {
          "name": "checkAvailability",
          "parameters": { "date": "2026-09-10", "service": "cleaning" }
        }
      }
    ]
  }
}
```

4. Endpoints answer with one entry per tool call (already modeled in
   `app/models.py`, legacy `toolCallList` payloads accepted too):

```json
{
  "results": [
    { "toolCallId": "call_abc123", "result": "Open slots: 9:00, 10:30" }
  ]
}
```

## Deploying to Render

Two options, both built on the included `Dockerfile` (Render auto-detects it —
no build command needed). Either way Render injects `PORT` and the container
starts with `uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1`.

### Option A — Blueprint (recommended)

The repo includes `render.yaml`, so you can deploy as Infrastructure-as-Code:

1. Push this project (including `render.yaml` and `Dockerfile`) to GitHub.
2. Render: **New + → Blueprint** → connect the repo.
3. Render reads `render.yaml`, builds the Docker image for the `web` service
   and creates the env vars automatically. `VAPI_WEBHOOK_SECRET` is generated
   randomly — copy it from the dashboard and paste it into Vapi's tool
   **Server Secret**.
4. Edit `N8N_WEBHOOK_URL` in the dashboard to your real n8n webhook URL, and
   (optionally) set `GROQ_API_KEY`.
5. Open `https://<service-name>.onrender.com/health` → `{"status":"ok"}`.

### Option B — Manual web service

1. Push the repo to GitHub.
2. Render: **New + → Web Service** → connect the repo.
3. Configure:
   - **Runtime**: Docker (auto-detected) — no build command needed.
   - **Health Check Path**: `/health`
   - **Environment**: add the variables from the table above.
4. Deploy. Verify `https://<service-name>.onrender.com/health` returns
   `{"status":"ok"}`.

> Keep **one** uvicorn worker (the Dockerfile CMD passes `--workers 1`): the
> booking idempotency store is in-memory and would be sharded across multiple
> processes. A single worker also matches the free tier's 512 MB.

## Deploying to Railway

1. Push the repo to GitHub.
2. Railway: **New Project → Deploy from GitHub repo** → select the repo.
3. Railway detects the `Dockerfile`. The container CMD reads `$PORT`, which
   Railway injects automatically.
4. Add the environment variables from the table above (service → **Variables**).
5. Deploy, open the generated `*.up.railway.app` domain and check `/health`.

## Local Docker smoke test

```bash
docker build -t clinic-voice-agent .
docker run -p 8000:8000 clinic-voice-agent            # uses PORT default (8000)
curl http://127.0.0.1:8000/health                     # {"status":"ok"}

# the same image, respecting an injected PORT:
docker run -p 9000:9000 -e PORT=9000 clinic-voice-agent
curl http://127.0.0.1:9000/health
```

## Cold starts on free tiers (important before a live demo)

Render's free tier and Railway's trial plan **sleep idle instances** — Render
sleeps after ~15 minutes without traffic, and the first request to a sleeping
instance must cold-start the container (image pull + boot), which can take
**30–60+ seconds**. Vapi only gives tool webhooks ~2–3 seconds, so the first
tool call of a demo against a cold instance will fail. Warm the instance up a
few minutes ahead of any live demo or recording.

**Keep-alive ping (cron-job.org):**

1. Create a free account at <https://cron-job.org>.
2. **Create cronjob**:
   - **URL**: `https://<service-name>.onrender.com/health` (GET)
   - **Schedule**: Every 10 minutes (cron `*/10 * * * *`)
3. Save. `/health` is deliberately cheap — no auth, no n8n call — so a public
   ping every 10 minutes keeps the instance awake and the demo smooth.

## Tools

- `POST /tools/check-availability` ✅ — n8n → Google Calendar free/busy lookup (implemented)
- `POST /tools/book-appointment` ✅ — n8n → create Calendar event + confirmation, idempotent via `toolCallId` (implemented)
- `POST /tools/cancel-appointment` ✅ — n8n → delete event + notify (implemented)
- `POST /tools/booking` ✅ — **unified, intent-based booking decision** (implemented):
  accepts a normalized flat body and validates it before any business logic
  runs — `call_id` (required), `intent` (`book` | `reschedule` | `cancel` |
  `unclear`), and optional `patient_name` / `requested_date` / `requested_time`
  / `reason` / `existing_appointment_id` / `test_case_tag`. Malformed payloads
  get a clean `422`. `book`/`cancel` forward to n8n; `reschedule` moves the
  appointment via `N8N_RESCHEDULE_WEBHOOK_URL` (missing fields → clarifying
  question; `{"status":"conflict"}` → 2–3 alternative slots offered; any other
  failure → the graceful callback fallback); `unclear` asks for clarification.
  Every reply carries a spoken-friendly `message` — failures never leak
  exceptions.
- `POST /tools/reschedule-appointment` — n8n → move event + notify (planned; the
  unified `POST /tools/booking` reschedule intent already covers this flow).

## Decision log & dashboard (read-only)

Every decision point — booked, cancelled, rescheduled, escalated, or failed —
records one structured entry: `call_id`, `timestamp`, `test_case_tag`,
`intent_detected`, `action_taken`, `outcome` (`success` | `failure` |
`escalated`), `notes`.

- **With `DATABASE_URL` set** (Supabase/Neon/Render Postgres): entries are
  stored in an auto-created `decision_log` table — **survives redeploys and
  restarts**. The table is created on first use, so no migration step is
  needed for the demo.
- **Without it**: entries append to `data/decision_log.jsonl`
  (`DECISION_LOG_PATH`) — the original file store, kept so local dev works with
  zero setup. The file is wiped whenever a free-tier instance restarts.

- `GET /dashboard` — plain HTML page (no auth, no build step) rendering the
  last 50 entries as a table, newest first, from whichever store is active.
  Useful for demos and review: `curl -s http://127.0.0.1:8000/dashboard`.

## Webhooks

- `POST /webhook/call-ended` — receives Vapi's `end-of-call-report` server
  message and logs a summary (call id, ended reason, LLM summary). Demo keeps
  it to logging only — no persistence. Handles both Vapi's legacy nested
  `callReport` payloads and the current flat message shape.
