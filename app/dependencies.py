"""Shared FastAPI dependencies (authentication for inbound webhooks)."""

import logging
import secrets

from fastapi import Header, HTTPException, status

from app.config import settings

logger = logging.getLogger(__name__)

# Vapi's server authentication sends the shared secret as the `x-vapi-secret`
# header on every request (see Vapi docs -> "Server authentication" ->
# "Legacy X-Vapi-Secret Support": the `secret` field maps to the X-Vapi-Secret
# header). We compare it in constant time to avoid leaking timing information.
#
# When VAPI_WEBHOOK_SECRET is empty the check is disabled so local dev works
# with zero setup — make sure it's set anywhere real traffic is possible.
if not settings.vapi_webhook_secret:
    logger.warning(
        "VAPI_WEBHOOK_SECRET is not set — /tools/* and /webhook/* "
        "authentication is DISABLED"
    )


def verify_vapi_secret(x_vapi_secret: str | None = Header(default=None)) -> None:
    """Dependency: reject requests with a missing/invalid x-vapi-secret header.

    Applied to every route on the /tools and /webhook routers. Returns 401 for
    a missing or mismatched header. Skipped entirely when VAPI_WEBHOOK_SECRET
    is empty (local development).
    """
    if not settings.vapi_webhook_secret:
        return
    if x_vapi_secret is None or not secrets.compare_digest(
        x_vapi_secret, settings.vapi_webhook_secret
    ):
        logger.warning("Rejected request: missing or invalid x-vapi-secret header")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing x-vapi-secret header",
        )