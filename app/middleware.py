"""Global exception handling so unhandled errors never leak stack traces."""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

INTERNAL_ERROR_MESSAGE = "Internal server error"


def setup_exception_handlers(app: FastAPI) -> None:
    """Register a catch-all handler for unhandled exceptions.

    Any error that escapes the route handlers — and isn't a FastAPI/Starlette
    HTTPException or a validation error (those keep their standard 404/422/...
    responses) — is logged in full server-side and returned to the caller as a
    clean JSON 500, never a raw stack trace.
    """

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        logger.error(
            "Unhandled error on %s %s: %s",
            request.method,
            request.url.path,
            exc,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return JSONResponse(status_code=500, content={"detail": INTERNAL_ERROR_MESSAGE})