"""Thin async httpx wrapper for calling n8n webhooks.

n8n owns the Google Calendar + notification workflows; this client just
forwards a JSON payload and hands back whatever the workflow responds with.
"""

import logging
import time
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None


class N8NError(RuntimeError):
    """Raised when an n8n webhook call fails (network error or non-2xx status)."""


def get_client() -> httpx.AsyncClient:
    """Lazily create a shared AsyncClient so connections get pooled."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=settings.n8n_timeout_seconds)
    return _client


async def close_client() -> None:
    """Close the shared client; called from the FastAPI lifespan on shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def call_n8n_webhook(payload: dict[str, Any], *, path: str = "") -> Any:
    """POST `payload` as JSON to the configured n8n webhook.

    Args:
        payload: JSON-serializable body forwarded to the workflow.
        path: Optional sub-path appended to N8N_WEBHOOK_URL — useful when one
            n8n host serves several workflows ("availability", "booking", ...).

    Returns:
        The parsed JSON response, or `{}` when the workflow replies with an
        empty body (common for fire-and-forget notification workflows).

    Raises:
        N8NError: On timeout/connection failure or a non-2xx response.
    """
    base_url = settings.n8n_webhook_url
    if not base_url:
        raise N8NError("N8N_WEBHOOK_URL is not configured")

    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}" if path else base_url
    client = get_client()

    # Timeout comes from the shared client (settings.n8n_timeout_seconds).
    # Transient failures — timeouts/connection errors and n8n 502/503/504 —
    # are retried up to `n8n_max_retries` (default 1); 4xx responses never are.
    max_attempts = settings.n8n_max_retries + 1

    logger.info("Calling n8n webhook: %s", url)
    started = time.perf_counter()

    response: httpx.Response | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            break
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if attempt < max_attempts and code in {502, 503, 504}:
                logger.warning(
                    "n8n webhook %s returned HTTP %s; retrying (attempt %d/%d)",
                    url,
                    code,
                    attempt,
                    max_attempts,
                )
                continue
            logger.error(
                "n8n webhook %s returned HTTP %s: %s", url, code, exc.response.text[:200]
            )
            raise N8NError(f"n8n webhook returned HTTP {code}") from exc
        except httpx.RequestError as exc:
            if attempt < max_attempts:
                logger.warning(
                    "n8n webhook %s failed (%s); retrying (attempt %d/%d)",
                    url,
                    exc.__class__.__name__,
                    attempt,
                    max_attempts,
                )
                continue
            logger.error("n8n webhook %s failed: %s", url, exc)
            raise N8NError(
                f"n8n webhook request failed: {exc.__class__.__name__}"
            ) from exc

    assert response is not None  # loop always breaks on success or raises

    elapsed_ms = (time.perf_counter() - started) * 1000
    logger.info("n8n webhook %s responded in %.0f ms", url, elapsed_ms)

    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        logger.warning("n8n webhook %s returned a non-JSON body; returning raw text", url)
        return {"raw": response.text}


async def get_availability(date: str, service_type: str | None = None) -> Any:
    """Query open appointment slots for `date` via the n8n calendar workflow.

    POSTs to {N8N_WEBHOOK_URL}/check-availability; n8n queries Google
    Calendar and answers synchronously through its "Respond to Webhook" node:

        {"available_slots": ["10:00 AM", "2:30 PM"]}

    Raises N8NError on timeouts/connection errors or a non-2xx response —
    callers turn that into a graceful message for Vapi.
    """
    payload: dict[str, Any] = {"date": date}
    if service_type:
        payload["service_type"] = service_type
    return await call_n8n_webhook(payload, path="check-availability")


async def create_booking(
    patient_name: str,
    phone_number: str,
    date: str,
    time: str,
    service_type: str,
) -> str:
    """Create an appointment via the n8n calendar workflow.

    POSTs to {N8N_WEBHOOK_URL}/book-appointment; n8n creates the Google
    Calendar event and answers synchronously through its "Respond to Webhook"
    node:

        {"booking_reference": "BK-2026-0001"}

    Returns the booking_reference string.

    Raises N8NError on transport failures, non-2xx responses, or a reply that
    doesn't include a booking_reference.
    """
    payload = {
        "patient_name": patient_name,
        "phone_number": phone_number,
        "date": date,
        "time": time,
        "service_type": service_type,
    }
    data = await call_n8n_webhook(payload, path="book-appointment")

    if isinstance(data, str):
        # Tolerate a bare-string reply carrying just the reference.
        reference = data.strip()
        if reference:
            return reference
    elif isinstance(data, dict):
        reference = data.get("booking_reference")
        if isinstance(reference, str) and reference.strip():
            return reference.strip()

    raise N8NError("n8n book-appointment returned no booking_reference")


async def cancel_booking(booking_reference: str) -> Any:
    """Cancel an appointment via the n8n calendar workflow.

    POSTs to {N8N_WEBHOOK_URL}/cancel-appointment; n8n deletes the Google
    Calendar event and may trigger a notification. Any JSON response is
    returned as-is (a fire-and-forget reply is fine here too).

    Raises N8NError on transport failures or non-2xx responses.
    """
    return await call_n8n_webhook(
        {"booking_reference": booking_reference}, path="cancel-appointment"
    )
