"""Shared FastAPI dependencies (authentication for inbound webhooks)."""

import logging
import secrets

from fastapi import Header, HTTPException, status

from app.config import settings

logger = logging.getLogger(__name__)

# Requests must provide the shared secret as `Authorization: Bearer <secret>`.
# We compare it in constant time to avoid leaking timing information.
#
# When VAPI_WEBHOOK_SECRET is empty the check is disabled so local dev works
# with zero setup — make sure it's set anywhere real traffic is possible.
if not settings.vapi_webhook_secret:
    logger.warning(
        "VAPI_WEBHOOK_SECRET is not set — /tools/* and /webhook/* "
        "authentication is DISABLED"
    )


def verify_vapi_secret(authorization: str | None = Header(default=None)) -> None:
    """Dependency: reject requests with a missing or invalid Bearer token.

    Applied to every route on the /tools and /webhook routers. Returns 401 for
    a missing or mismatched header. Skipped entirely when VAPI_WEBHOOK_SECRET
    is empty (local development).
    """
    if not settings.vapi_webhook_secret:
        return
    bearer_prefix = "Bearer "
    received_secret = (
        authorization[len(bearer_prefix) :]
        if authorization is not None and authorization.startswith(bearer_prefix)
        else None
    )
    if received_secret is None or not secrets.compare_digest(
        received_secret, settings.vapi_webhook_secret
    ):
        logger.warning("Rejected request: missing or invalid Authorization header")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing Authorization header",
        )