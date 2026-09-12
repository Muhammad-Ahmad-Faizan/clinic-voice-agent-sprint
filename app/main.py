"""Clinic Voice Agent API — FastAPI entrypoint.

Fast backend ("tool router") that Vapi.ai calls when its LLM (Groq
Llama-3.1) decides to invoke a tool during a clinic appointment-booking
call. Each tool endpoint validates Vapi's payload, forwards the action to
an n8n webhook (Google Calendar + notifications), and replies in the exact
shape Vapi expects — fast, so the phone call never stalls.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import settings
from app.middleware import setup_exception_handlers
from app.models import HealthResponse
from app.routers import tools, webhooks
from app.services import n8n_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("clinic-voice-agent")


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Log startup/shutdown and clean up the shared n8n HTTP client."""
    logger.info(
        "clinic-voice-agent starting (port=%s, n8n_webhook_configured=%s, vapi_secret_set=%s)",
        settings.port,
        bool(settings.n8n_webhook_url),
        bool(settings.vapi_webhook_secret),
    )
    yield
    await n8n_client.close_client()
    logger.info("clinic-voice-agent shutdown complete")


app = FastAPI(
    title="Clinic Voice Agent API",
    version="0.1.0",
    description=(
        "Tool router for a voice AI appointment-booking demo. Vapi.ai calls "
        "these endpoints mid-conversation; work is forwarded to n8n "
        "(Google Calendar + notifications)."
    ),
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Rate limiting: intentionally skipped for this demo.
# PRODUCTION TODO: add per-call/IP rate limiting (e.g. slowapi middleware or a
# Redis-backed token bucket) to /tools/* and /webhook/* before pointing real
# patient traffic here.
# ---------------------------------------------------------------------------

# Catch-all: unhandled errors become clean JSON 500s (full traceback logged).
setup_exception_handlers(app)

# Vapi tool endpoints, e.g. POST /tools/book-appointment
app.include_router(tools.router, prefix="/tools")

# Vapi server-to-server webhooks, e.g. POST /webhook/call-ended
app.include_router(webhooks.router, prefix="/webhook")


@app.get("/health", response_model=HealthResponse, tags=["health"])
async def health() -> HealthResponse:
    """Liveness probe (Render/Railway health checks, local smoke tests)."""
    return HealthResponse()
