"""Shared pytest fixtures for the clinic-voice-agent test suite."""

import pytest

from app.routers import tools as tools_router
from app.services import n8n_client


@pytest.fixture(autouse=True)
def _clear_booking_store():
    """Reset the in-memory idempotency store between tests.

    The store lives at module scope (it models a cross-request cache), so
    tests must start from a clean slate.
    """
    tools_router.BOOKING_STORE.clear()
    yield
    tools_router.BOOKING_STORE.clear()


@pytest.fixture(autouse=True)
def _reset_n8n_client():
    """Discard the shared n8n httpx client before (and after) each test.

    respx intercepts requests on httpx clients that are created while its mock
    router is active. The production client module caches a singleton, so
    tests drop it to guarantee a fresh client that picks up the respx
    transport.
    """
    n8n_client._client = None
    yield
    n8n_client._client = None