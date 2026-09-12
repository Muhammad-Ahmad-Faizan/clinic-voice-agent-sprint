"""Webhook endpoints for Vapi.ai server-to-server events (e.g. call-ended).

These routes are mounted under the /webhook prefix (see app/main.py). Unlike
the /tools endpoints, Vapi does not wait on a `results` payload here — a fast
2xx ack is all it needs, so handlers do no downstream calls.
"""

import logging

from fastapi import APIRouter, Depends

from app.models import CallEndedWebhookRequest
from app.dependencies import verify_vapi_secret

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["webhooks"], dependencies=[Depends(verify_vapi_secret)])


@router.post("/call-ended")
async def call_ended(request: CallEndedWebhookRequest) -> dict[str, str]:
    """Receive Vapi's end-of-call report and log a call summary.

    Demo-friendly: logs only, no persistence. Handles both the legacy nested
    `callReport` payload shape and the current flattened message shape
    (summary under `artifact` / `analysis`, call id under `call`).
    """
    message = request.message
    summary: str | None = None
    ended_reason = call_id = None

    if message is not None:
        if message.call_report is not None:
            # Legacy generation: report fields nested under callReport.
            ended_reason = message.call_report.ended_reason
            summary = message.call_report.summary
        else:
            # Current generation: fields live flat on the message.
            ended_reason = message.ended_reason
            if message.artifact:
                summary = message.artifact.get("summary")
            if summary is None and message.analysis:
                summary = message.analysis.get("summary")
        if message.call:
            call_id = message.call.get("id")

    logger.info(
        "call-ended: type=%s call_id=%s ended_reason=%s summary=%r",
        message.type if message else None,
        call_id,
        ended_reason,
        summary,
    )
    return {"status": "ok"}